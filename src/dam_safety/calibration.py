"""SCD2 calibration / threshold versioning and history-recompute planning (pure Python).

ref.calibration and ref.threshold are SCD Type 2: every change inserts a new row with
``effective_from``; the previous current row gets ``effective_to`` = new effective_from
and ``is_current`` = false. Rows are never updated in place otherwise and never deleted
(FERC canvas REQ-23 append-only; REQ-19/20 calibration history).

Interval convention: half-open [effective_from, effective_to); effective_to None = open.

Recompute: when the set of calibration versions for a sensor changes (new version,
back-dated version, or a corrected params row superseding an earlier one), the readings
whose *resolved* calibration changes must be re-reduced. ``plan_recompute`` returns exactly
those windows by comparing the resolution function before and after the change.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class Version:
    key: str                      # sensor_id (calibration) or threshold scope key
    version_id: str               # calib_id / threshold_id
    effective_from: datetime
    effective_to: Optional[datetime] = None
    payload_hash: str = ""        # hash of params + formula_id/version


def payload_hash(params: dict, formula_id: str = "", formula_version: str = "",
                 sign_mult: int = 1, offset: float = 0.0) -> str:
    """Hash of everything that changes eng values; sign_mult/offset included so a sign or
    offset correction is detected as a new version and triggers recompute."""
    blob = json.dumps({"p": params, "f": formula_id, "v": formula_version,
                       "s": int(sign_mult), "o": float(offset or 0.0)}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def resolve(versions: Sequence[Version], ts: datetime) -> Optional[Version]:
    """Version valid at ts (latest effective_from <= ts with ts < effective_to)."""
    best = None
    for v in versions:
        if v.effective_from <= ts and (v.effective_to is None or ts < v.effective_to):
            if best is None or v.effective_from > best.effective_from:
                best = v
    return best


def scd2_insert(versions: Sequence[Version], new: Version) -> List[Version]:
    """Apply an SCD2 insert. The new version runs until the next later version (if a
    back-dated insert) or open-ended; the version it lands inside is closed at
    new.effective_from. Raises on a duplicate effective_from for the same key."""
    out: List[Version] = []
    later = [v.effective_from for v in versions if v.key == new.key and v.effective_from > new.effective_from]
    for v in versions:
        if v.key == new.key and v.effective_from == new.effective_from:
            raise ValueError("duplicate effective_from for key; supersede via a new version_id with later effective_from or use correct_version")
        if v.key == new.key and v.effective_from < new.effective_from and (v.effective_to is None or v.effective_to > new.effective_from):
            v = replace(v, effective_to=new.effective_from)
        out.append(v)
    out.append(replace(new, effective_to=min(later) if later else new.effective_to))
    return sorted(out, key=lambda v: (v.key, v.effective_from))


def correct_version(versions: Sequence[Version], version_id: str, new_version_id: str, new_hash: str) -> List[Version]:
    """Supersede a version's params over the SAME interval (e.g. a transcription error in
    a gauge factor). The old row is kept (audit) but marked superseded by returning it
    with effective_to == effective_from (zero-length, never resolves)."""
    out = []
    for v in versions:
        if v.version_id == version_id:
            out.append(replace(v, effective_to=v.effective_from))  # retired, kept for audit
            out.append(replace(v, version_id=new_version_id, payload_hash=new_hash))
        else:
            out.append(v)
    return sorted(out, key=lambda v: (v.key, v.effective_from, v.version_id))


@dataclass(frozen=True)
class RecomputeWindow:
    key: str
    start: datetime
    end: Optional[datetime]       # None = open (up to now)
    old_version_id: Optional[str]
    new_version_id: Optional[str]


def plan_recompute(old: Sequence[Version], new: Sequence[Version]) -> List[RecomputeWindow]:
    """Minimal set of [start, end) windows per key where the resolved version (id or
    payload) differs between ``old`` and ``new`` version sets. Adjacent windows with the
    same (old, new) pair are merged."""
    keys = sorted({v.key for v in old} | {v.key for v in new})
    windows: List[RecomputeWindow] = []
    for k in keys:
        o = [v for v in old if v.key == k]
        n = [v for v in new if v.key == k]
        bounds = sorted({b for v in (o + n) for b in (v.effective_from, v.effective_to) if b is not None})
        segs = list(zip(bounds, bounds[1:] + [None]))
        cur: Optional[RecomputeWindow] = None
        for s, e in segs:
            ro, rn = resolve(o, s), resolve(n, s)
            same = (ro is None and rn is None) or (
                ro is not None and rn is not None and ro.version_id == rn.version_id and ro.payload_hash == rn.payload_hash
            )
            if same:
                if cur:
                    windows.append(cur)
                    cur = None
                continue
            oid, nid = (ro.version_id if ro else None), (rn.version_id if rn else None)
            if cur and cur.end == s and cur.old_version_id == oid and cur.new_version_id == nid:
                cur = replace(cur, end=e)
            else:
                if cur:
                    windows.append(cur)
                cur = RecomputeWindow(k, s, e, oid, nid)
        if cur:
            windows.append(cur)
    return windows


def windows_to_sql_predicate(windows: Iterable[RecomputeWindow], key_col: str = "sensor_id", ts_col: str = "ts_utc") -> str:
    """SQL predicate selecting readings inside recompute windows (used by the recompute job)."""
    parts = []
    for w in windows:
        k = w.key.replace("'", "''")
        p = f"({key_col} = '{k}' AND {ts_col} >= TIMESTAMP'{w.start.isoformat()}'"
        if w.end is not None:
            p += f" AND {ts_col} < TIMESTAMP'{w.end.isoformat()}'"
        parts.append(p + ")")
    return " OR ".join(parts) if parts else "FALSE"
