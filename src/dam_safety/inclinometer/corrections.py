"""4_CORRECTIONS -> corrections (REPORT §6.1 row 4, §5, §6.3; procedure §5).

Corrections are separate, versioned, append-only records; raw readings are never edited (REPORT D10). The engine
applies the records that are `applied=True` and not superseded, in a fixed order, and returns new values. Detection
is separate (diagnostics.py) and never auto-applies: a SUSPECT checksum shift on its own is NOT a bias error
[MIKKELSEN-2003 PDF p4-5; DGSI-BIAS PDF p1], so no record is ever created from the checksum alone.

Arithmetic, all on D = R0 - R180 in reading units, per depth interval (top-keyed):
  bias        D' = D + c              c = FULL value added to A0 (GTILT ZA form) [procedure §5.1]; never c/2
                                      on both faces (REPORT E1: that cancels)
  rotation    D_X' = D_X + k * v * D_Y  X = record axis, Y = the other axis, same survey; k fixed by the record's
                                      sign_convention (ROTATION_CONVENTIONS, required on EVERY rotation record):
                MIKKELSEN2003_EQ9     A' - A = -B'*sin(a_A)  ->  A = A' + sin(a_A)*B'  (k=+1, v=sin a_A)
                                      [MIKKELSEN-2003 PDF p9 eq 9; Fig 7 example sin a = 35/4500 = 0.0078]
                GTILT_PLUS_SIN        engine error e = +sin(rot)*B0 (a0+e, a180-e) -> A = A' - sin(rot)*B
                                      (k=-1, v=sin rot) [verify INFO T14f, T7d]
                A_corr=A-f*B          A = A' - f*B  (k=-1)  [MIKKELSEN-2007 PDF p17]
              Only axis A is implemented; axis B (eq 10, B' - B = A'*sin a_B) is UNVERIFIED and raises.
              OFF by default: a rotation record is applied only when applied=True AND it carries an independent
              review (reviewed_by != user, reviewed_utc) [TRB-EC129 PDF p44: not routinely performed, needs expertise].
  depth       corrected[i] = observed[i + n]  in bottom->top order (n whole intervals) [verify T13l]
  sensitivity D' = D * s              (UNVERIFIED: no team worked example; see docs/VERIFICATION.md)
  spiral      DEFERRED: 'spiral' stays a valid correction_type so the record can be stored, but applied=True is
              rejected (never marked corrected: the workbook says "Applied" while no formula uses it, REPORT E2).
              Any survey on an installation with a spiral survey on file gets info flag SPIRAL_NOT_APPLIED.
  baseline    not a D correction: selects the reduction baseline survey (REPORT D9), recorded for audit"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

CORRECTION_TYPES = ("bias", "rotation", "depth", "sensitivity", "spiral", "baseline")
APPLY_ORDER = ("depth", "bias", "sensitivity", "rotation")      # depth re-keys first; rotation uses corrected B
ROTATION_CONVENTIONS = {"MIKKELSEN2003_EQ9": +1.0, "GTILT_PLUS_SIN": -1.0, "A_corr=A-f*B": -1.0}   # code -> k
ROTATION_IMPLEMENTED_AXES = ("A",)          # B-axis rotation UNVERIFIED (docs/VERIFICATION.md INC-ROTB)
INFO_SPIRAL_NOT_APPLIED = "SPIRAL_NOT_APPLIED"
_OTHER = {"A": "B", "B": "A"}


@dataclass(frozen=True)
class CorrectionRecord:
    """Exactly the REPORT §6.3 audit fields (+ dam_id for the UC row filter)."""
    record_id: str
    dam_id: str
    instrument_id: str
    survey_id: str
    axis: str                         # 'A' | 'B'
    correction_type: str
    parameter_name: str               # c | f | n_intervals | s | baseline_survey_id
    auto_value: float | None          # detector's estimate (diagnostics.py), may be None
    detection_method: str | None
    detection_statistic: str | None   # e.g. 'r=1.000000;factor=133.4'
    override_value: float | str | None
    applied: bool
    sign_convention: str | None
    depth_range: tuple[float, float] | None   # (top_m, bottom_m) inclusive, None = whole survey
    reason: str
    source_citation: str
    user: str
    timestamp_utc: str
    before_value: float | None = None # top cumulative change, mm (filled by engine)
    after_value: float | None = None
    software_version: str = ""
    input_file_sha256: str = ""
    superseded_by: str | None = None
    reviewed_by: str | None = None    # independent reviewer; required to apply a rotation record (TRB-EC129 p44)
    reviewed_utc: str | None = None

    @property
    def reviewed(self) -> bool:
        return bool(self.reviewed_by and self.reviewed_utc and self.reviewed_by != self.user)

    def __post_init__(self):
        if self.correction_type not in CORRECTION_TYPES:
            raise ValueError(f"unknown correction_type {self.correction_type!r}")
        if self.axis not in ("A", "B"):
            raise ValueError("axis must be A or B")
        if self.applied and not (self.reason and self.source_citation and self.user):
            raise ValueError("an applied correction needs reason, source_citation and user (REPORT D10)")
        if self.correction_type == "rotation":
            if self.sign_convention not in ROTATION_CONVENTIONS:
                raise ValueError(f"rotation record needs sign_convention in {sorted(ROTATION_CONVENTIONS)} "
                                 "(procedure: store sign_convention on every rotation record; verify INFO T14f)")
            if self.applied and not self.reviewed:
                raise ValueError("rotation is off by default: applied=True needs an independent review "
                                 "(reviewed_by != user, reviewed_utc) [TRB-EC129 PDF p44]")
            if self.applied and self.axis not in ROTATION_IMPLEMENTED_AXES:
                raise ValueError("B-axis rotation correction is UNVERIFIED (Mikkelsen 2003 eq 10); store with applied=False")
        if self.correction_type == "bias" and self.applied and not self.reviewed:
            raise ValueError("a bias correction is never auto-applied: applied=True needs reviewer approval "
                             "(reviewed_by != user, reviewed_utc) [procedure §5 lead ruling 2026-11-02]")
        if self.correction_type == "spiral" and self.applied:
            raise ValueError("spiral correction is deferred: never mark a survey spiral-corrected (REPORT E2)")

    @property
    def value(self):
        return self.override_value if self.override_value is not None else self.auto_value

    def as_row(self) -> dict:
        d = asdict(self)
        d["depth_range"] = None if self.depth_range is None else f"{self.depth_range[0]}..{self.depth_range[1]}"
        return d


AUDIT_FIELDS = tuple(f.name for f in fields(CorrectionRecord))


def active(records, instrument_id, survey_id):
    """Applied, not superseded, for this survey."""
    return [r for r in records if r.instrument_id == instrument_id and r.survey_id == survey_id
            and r.applied and r.superseded_by is None
            and (r.correction_type not in ("rotation", "bias") or r.reviewed) and r.correction_type != "spiral"]


def info_flags(installation, records=()) -> tuple[str, ...]:
    """Info-level QA codes for EVERY survey of this installation (severity unchanged; appended to qa_reason).
    SPIRAL_NOT_APPLIED: a spiral survey is on file (installation.spiral_survey_id, or any spiral record for the
    instrument) but spiral correction is deferred, so nothing is spiral-corrected."""
    has_spiral = bool(getattr(installation, "spiral_survey_id", None)) or any(
        r.correction_type == "spiral" and r.instrument_id == installation.instrument_id for r in records)
    return (INFO_SPIRAL_NOT_APPLIED,) if has_spiral else ()


def merge_qa_reason(qa_reason: str, codes) -> str:
    """Comma-joined, as SCHEMA_CONTRACT §3 (`qa_reason` is comma-joined everywhere)."""
    parts = [p for p in (qa_reason or "").split(",") if p]
    return ",".join(parts + [c for c in codes if c not in parts])


def _in_range(depth, rng):
    return rng is None or (rng[0] - 1e-9 <= depth <= rng[1] + 1e-9)


def apply(D: dict, records, fill: str = "missing") -> dict:
    """D: {(depth_top_m, axis): float | None} for ONE survey. Returns a NEW dict; input untouched.
    fill: what a depth shift leaves in the vacated end interval: 'missing' (None, default) or 'zero' (the
    UNVERIFIED fill used by verify T13m; only for reproducing that test)."""
    if fill not in ("missing", "zero"):
        raise ValueError("fill must be 'missing' or 'zero'")
    out = dict(D)
    order = {t: i for i, t in enumerate(APPLY_ORDER)}
    for rec in sorted(records, key=lambda r: (order.get(r.correction_type, 99), r.timestamp_utc, r.record_id)):
        t, ax, v = rec.correction_type, rec.axis, rec.value
        if t == "baseline":
            continue
        if t == "spiral":
            raise NotImplementedError("spiral correction is deferred (record kept, never applied; REPORT E2)")
        if v is None:
            raise ValueError(f"{rec.record_id}: no auto_value/override_value")
        depths = sorted(d for (d, a) in out if a == ax)
        if t == "depth":
            n = int(v)
            if n != v:
                raise ValueError("depth shift must be whole intervals")
            b2t = depths[::-1]                             # bottom -> top
            src = [out[(d, ax)] for d in b2t]
            for i, d in enumerate(b2t):
                j = i + n
                out[(d, ax)] = src[j] if 0 <= j < len(src) else (0.0 if fill == "zero" else None)
            continue
        for d in depths:
            if not _in_range(d, rec.depth_range) or out[(d, ax)] is None:
                continue
            if t == "bias":
                out[(d, ax)] = out[(d, ax)] + float(v)
            elif t == "sensitivity":
                out[(d, ax)] = out[(d, ax)] * float(v)
            elif t == "rotation":
                if not rec.reviewed:
                    raise ValueError(f"{rec.record_id}: unreviewed rotation record is never applied (TRB-EC129 p44)")
                if ax not in ROTATION_IMPLEMENTED_AXES:
                    raise NotImplementedError("B-axis rotation is UNVERIFIED (Mikkelsen 2003 eq 10)")
                k = ROTATION_CONVENTIONS[rec.sign_convention]
                y = out.get((d, _OTHER[ax]))
                out[(d, ax)] = None if y is None else out[(d, ax)] + k * float(v) * y
    return out
