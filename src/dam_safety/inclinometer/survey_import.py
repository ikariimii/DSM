"""2_RAWDATA -> survey_import (REPORT §6.1 row 2; procedure §2, §4.4, §8).

Raw readings are immutable: each record keeps the native raw value and unit next to the normalised value.
Validation replaces the workbook's IFERROR(...,0) (REPORT E9): bad input is MISSING/BAD, never 0 displacement.
SAA: stored B = sign_mult_B * Y with sign_mult_B = -1, applied exactly once at ingest; re-ingest of a record already
marked sign_normalized is a no-op [procedure §4.4 lead ruling; MEASURAND-KB-ORIENT 'Y is a mirror of B']."""
from __future__ import annotations

import hashlib
import math
import json
from dataclasses import dataclass, replace
from collections.abc import Mapping

from .installation import interval_top_from_label

SIGN_MULT = {"probe": {"A": 1, "B": 1}, "saa": {"A": 1, "B": -1}}   # procedure §3 sign_mult_A/B row


@dataclass(frozen=True)
class RawReading:
    instrument_id: str
    survey_id: str
    survey_ts_utc: str
    depth_top_m: float          # interval top, depth from top of casing (installation.rekey_* if needed)
    axis: str                   # 'A' | 'B'
    r0: float | None            # normalised A0/B0 (probe convention, after sign_mult)
    r180: float | None
    raw_r0: str | float | None  # native value as received (kept, never edited)
    raw_r180: str | float | None
    raw_unit: str
    sign_mult: int = 1
    sign_normalized: bool = False
    qa_flag: str = "OK"         # OK | SUSPECT | BAD | MISSING (procedure §7)
    qa_reason: str = ""
    raw_depth_label: str | float | None = None   # depth label exactly as in the source file (kept, never edited)
    depth_reference: str = "top_of_casing"       # source label convention (installation.DEPTH_REFERENCES)
    depth_metadata_json: str | None = None      # immutable per-output provenance, never overwrite raw depth


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if math.isfinite(v) else None
    s = str(v).strip()
    if s == "":
        return None
    try:
        f = float(s)
    except ValueError:
        return "TEXT"
    return f if math.isfinite(f) else None


def make_reading(instrument_id, survey_id, survey_ts_utc, depth_top_m, axis, raw_r0, raw_r180, raw_unit,
                 system="probe", max_abs=None) -> RawReading:
    """Validate one depth/axis pair (range, missing, text) and normalise sign once."""
    if axis not in ("A", "B"):
        raise ValueError("axis must be A or B")
    m = SIGN_MULT[system][axis]
    v0, v1 = _num(raw_r0), _num(raw_r180)
    flags = []
    if "TEXT" in (v0, v1):
        flags.append("BAD:TEXT")
    if v0 is None or v1 is None:
        flags.append("MISSING:PASS")
    if max_abs is not None:
        for v in (v0, v1):
            if isinstance(v, float) and abs(v) > max_abs:
                flags.append("BAD:RANGE")
                break
    ok = lambda v: v if isinstance(v, float) else None   # noqa: E731
    flag = "OK"
    if any(f.startswith("BAD") for f in flags):
        flag = "BAD"
    elif flags:
        flag = "MISSING"
    r0, r1 = ok(v0), ok(v1)
    return RawReading(instrument_id, survey_id, survey_ts_utc, round(float(depth_top_m), 9), axis,
                      None if r0 is None else m * r0, None if r1 is None else m * r1, raw_r0, raw_r180, raw_unit,
                      sign_mult=m, sign_normalized=True, qa_flag=flag, qa_reason=",".join(flags))


def import_reading(profile, raw_depth_label, label_unit_m, instrument_id, survey_id, survey_ts_utc, axis, raw_r0,
                   raw_r180, raw_unit, casing_depth_m=None, L_m=None, system="probe", max_abs=None, *, depth_metadata=None) -> RawReading:
    """make_reading with the depth label re-keyed per import profile (installation.IMPORT_PROFILES): stores the interval
    top from top of casing, the raw label and depth_reference ('from_bottom_label' for the workbook profile only)."""
    if profile in ("gtilt", "digipro") and isinstance(depth_metadata, Mapping):
        if depth_metadata.get("export_type") in ("processed", "plot"):
            raise ValueError("Processed/plot-only exports are display/cross-check only; never re-reduced")
    top, ref = interval_top_from_label(profile, raw_depth_label, label_unit_m, casing_depth_m, L_m,
                                       depth_metadata=depth_metadata)
    r = make_reading(instrument_id, survey_id, survey_ts_utc, top, axis, raw_r0, raw_r180, raw_unit, system, max_abs)
    return replace(r, raw_depth_label=raw_depth_label, depth_reference=ref,
                   depth_metadata_json=None if depth_metadata is None else json.dumps(dict(depth_metadata), sort_keys=True))


def renormalise(r: RawReading, system: str) -> RawReading:
    """Idempotent: a record already sign-normalised is returned unchanged (procedure §4.4)."""
    if r.sign_normalized:
        return r
    m = SIGN_MULT[system][r.axis]
    return replace(r, r0=None if r.r0 is None else m * r.r0, r180=None if r.r180 is None else m * r.r180,
                   sign_mult=m, sign_normalized=True)


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
