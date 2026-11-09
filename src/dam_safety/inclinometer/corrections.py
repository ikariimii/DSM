"""4_CORRECTIONS -> corrections (REPORT §6.1 row 4, §5, §6.3; procedure §5).

Corrections are separate, versioned, append-only records; raw readings are never edited (REPORT D10). The engine
applies the records that are `applied=True` and not superseded, in a fixed order, and returns new values. Detection
is separate (diagnostics.py) and never auto-applies: a SUSPECT checksum shift on its own is NOT a bias error
[MIKKELSEN-2003 PDF p4-5; DGSI-BIAS PDF p1], so no record is ever created from the checksum alone.

Arithmetic, all on D = R0 - R180 in reading units, per depth interval (top-keyed):
  bias        D' = D + c              ZA = −ΔD (ΔD = mean(ΔA0 − ΔA180) over the stable zone), added to A0;
                                      proposed only, never auto-applied (reviewer approval required). Equals
                                      −bs only for an A0-only shift. (GTILT ZA form) [procedure §5.1]; never
                                      c/2 on both faces (REPORT E1: that cancels)
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

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, timezone

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
    reviewed_utc: str | None = None   # reviewed_by/reviewed_utc: DENORMALIZED copy of the approving review (v1.5)
    supersedes: str | None = None     # v1.5 append-only versioning: record_id of the version this row replaces
    review_id: str | None = None      # v1.5 (QA-039): audit.review_record row approving this version (applied=True)

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


# ---- v1.5 / QA-039: approval = an insert-only audit.review_record row ------------------------------------------
REVIEW_SUBJECT_TYPE = "incl_correction"
REVIEW_APPROVE = "approve"                     # audit.review_record.decision ('approve' | 'reject'), lowercase
REVIEW_SIGNED_FIELDS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                        "reviewer_id", "reviewer_pe_license", "signed_ts")
_VALUE_FIELDS = ("instrument_id", "survey_id", "axis", "correction_type", "parameter_name", "auto_value",
                 "override_value", "sign_convention", "depth_range")


def canonical_value(v):
    """SCHEMA_CONTRACT §5.5 canonical form for the signature: timestamps -> UTC ISO-8601 with microseconds and
    '+00:00' (naive = UTC; strings are parsed, 'Z' allowed); NaN/None -> null; everything else unchanged."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, str) and len(v) >= 19 and v[4] == "-" and v[10] in "T " and v[13] == ":":
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    if isinstance(v, datetime):
        v = v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)
        return v.isoformat(timespec="microseconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, tuple):
        return f"{v[0]}..{v[1]}"
    return v


def review_signature(review: dict, subject_row: dict) -> str:
    """sha256 hex over canonical JSON {"signoff": REVIEW_SIGNED_FIELDS of the review row, "subject": every
    ref.incl_correction column (AUDIT_FIELDS, DDL order) of the reviewed record as stored}, sort_keys, separators
    (',', ':'), UTF-8, ensure_ascii=False. Same structure as the app's backend/signoff.py; the canonical_value rule
    must be applied on both sides (interop vector: tests/fixtures/review_signature_vector.json). A content hash
    (tamper evidence), not a key-based signature."""
    payload = {"signoff": {k: canonical_value(review.get(k)) for k in REVIEW_SIGNED_FIELDS},
               "subject": {k: canonical_value(subject_row.get(k)) for k in AUDIT_FIELDS}}
    txt = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()


def _same(a, b) -> bool:
    return bool(a) and bool(b) and str(a).strip().lower() == str(b).strip().lower()


def approval_problem(row: dict, rows_by_id: dict, reviews_by_id: dict) -> str | None:
    """None when the applied stored record `row` is approved per SCHEMA_CONTRACT §5.5 v1.5, else the reason.
    Approved = an audit.review_record row R with R.review_id = row.review_id, subject_type 'incl_correction',
    decision 'approve', R.subject_id = row.record_id or row.supersedes (the proposal it was approved from),
    R.reviewer_id = row.reviewed_by (denormalized copy) and not the author of row or of the reviewed subject,
    R.signature_hash = review_signature(R, subject as stored), and - when the subject is the proposal - the applied
    row carries exactly the reviewed values (_VALUE_FIELDS). reviewed_by/reviewed_utc alone never approve."""
    rv = reviews_by_id.get(row.get("review_id")) if row.get("review_id") else None
    if rv is None:
        return "no audit.review_record approval"
    if rv.get("subject_type") != REVIEW_SUBJECT_TYPE or str(rv.get("decision") or "").lower() != REVIEW_APPROVE:
        return "review is not an incl_correction approval"
    sid = rv.get("subject_id")
    if sid not in (row.get("record_id"), row.get("supersedes")):
        return "review subject is neither this record nor the version it supersedes"
    subject = row if sid == row.get("record_id") else rows_by_id.get(sid)
    if subject is None:
        return "reviewed record not found"
    rid = rv.get("reviewer_id")
    if not rid or not _same(rid, row.get("reviewed_by")):
        return "reviewed_by does not match the review row"
    if _same(rid, row.get("user")) or _same(rid, subject.get("user")):
        return "self-review"
    if rv.get("signature_hash") != review_signature(rv, subject):
        return "signature_hash does not match the reviewed record"
    if subject is not row and any(canonical_value(row.get(k)) != canonical_value(subject.get(k)) for k in _VALUE_FIELDS):
        return "applied values differ from the reviewed proposal"
    return None


def effective_records(rows, review_rows):
    """Stored rows -> (CorrectionRecords with v1.5 rules applied, {survey_id: [problem]}).
    - superseded: a later row names it in `supersedes` (append-only versioning) or legacy superseded_by is set;
    - an applied row without a valid approval (approval_problem) is kept as applied=False (never applied) and its
      survey is reported. Rows that fail CorrectionRecord validation raise to the caller as before."""
    rows = list(rows)
    by_id = {r.get("record_id"): r for r in rows}
    # v1.6: only incl_correction review rows are approvals; alarm_event (ack/close) records never reach approval_problem
    reviews = {r.get("review_id"): r for r in (review_rows or ()) if r.get("subject_type") == REVIEW_SUBJECT_TYPE}
    newer = {r["supersedes"]: r["record_id"] for r in rows if r.get("supersedes")}
    out, problems = [], {}
    for r in rows:
        rec = r
        if r.get("applied"):
            why = approval_problem(r, by_id, reviews)
            if why:
                problems.setdefault(r.get("survey_id"), []).append(f"{r.get('record_id')}: {why}")
                rec = dict(r, applied=False)
        out.append(dict(rec, superseded_by=r.get("superseded_by") or newer.get(r.get("record_id"))))
    return out, problems


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
