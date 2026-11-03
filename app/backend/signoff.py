"""QA-039: review of a ref.incl_correction record as an append-only, content-signed sign-off record (one link of
the per-dam hash chain, see audit_chain.py).

The app never updates ref.incl_correction and never applies a correction. A reviewer's decision is ONE new row in
Lakebase `dam_app.review_signoff` (the data layer's table, column-compatible with UC `audit.review_record`; the
`dam_lakebase_extract` job copies it to UC). Rows are insert-only: UPDATE/DELETE are refused by a trigger in both
backends, and the app has no code path that updates or deletes them.

signature_hash = sha256 over the canonical JSON of the sign-off fields AND the reviewed record exactly as the reviewer
saw it (all ref.incl_correction audit fields). Re-verifying against the current record therefore detects any later
change to either. It is a content hash (tamper evidence), not a key-based cryptographic signature."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone

from .repos.contract import INCL_CORRECTION_FIELDS

SUBJECT_TYPE = "incl_correction"
DECISIONS = ("approve", "reject")
REVIEW_LEVEL = "engineer"
SIGNED_FIELDS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                 "reviewer_id", "reviewer_pe_license", "signed_ts")
# the reviewed content: the stored record, not the app's derived view of it
# every ref.incl_correction column (v1.5 DDL: the 23 audit fields + reviewed_by, reviewed_utc, supersedes, review_id),
# identical key set to the engine's corrections.AUDIT_FIELDS; a column the row lacks signs as null on both sides
SUBJECT_FIELDS = (*INCL_CORRECTION_FIELDS, "reviewed_by", "reviewed_utc", "supersedes", "review_id")


def canonical_value(v):
    """SCHEMA_CONTRACT §5.5 v1.5 canonical form (same rule as the engine's corrections.canonical_value): timestamps ->
    UTC ISO-8601 with microseconds and '+00:00' (naive = UTC; ISO strings parsed, 'Z' allowed); NaN/None -> null;
    a (lo, hi) tuple -> 'lo..hi'; everything else unchanged. Needed so a value read back as a string or a datetime
    signs identically."""
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


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def subject_content(rec: dict) -> dict:
    return {k: canonical_value(rec.get(k)) for k in SUBJECT_FIELDS}


def signature(row: dict, rec: dict) -> str:
    payload = {"signoff": {k: canonical_value(row.get(k)) for k in SIGNED_FIELDS}, "subject": subject_content(rec)}
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


def build(rec: dict, dam_id: str, decision: str, comment: str, reviewer_id: str, pe_license: str | None,
          signed_ts: str, *, action_type: str = "bias_review", review_level: str = REVIEW_LEVEL,
          reviewer_email: str | None = None, reviewer_sub: str | None = None) -> dict:
    """Chain-ready row (audit_chain.finalizer adds chain_id/chain_seq/prev_hash/record_hash) for a review of a
    ref.incl_correction record. signature_hash stays the engine-compatible content hash. original_value = the value
    before the correction (never overwritten); corrected_value = what the proposal would make it."""
    from .audit_chain import canon_json
    row = {"review_id": f"RS-{uuid.uuid4().hex}", "dam_id": dam_id, "subject_type": SUBJECT_TYPE,
           "subject_id": rec["record_id"], "review_level": review_level, "decision": decision, "comment": comment,
           "reviewer_id": reviewer_id, "reviewer_pe_license": pe_license or None, "signed_ts": signed_ts}
    row["signature_hash"] = signature(row, rec)
    row.update(action_type=action_type, reviewer_email=reviewer_email, reviewer_sub=reviewer_sub,
               original_value=canon_json({"parameter_name": rec.get("parameter_name"),
                                          "before_value": rec.get("before_value"),
                                          "units": "as stored in ref.incl_correction"}),
               corrected_value=canon_json({"parameter_name": rec.get("parameter_name"),
                                           "auto_value": rec.get("auto_value"),
                                           "override_value": rec.get("override_value"),
                                           "after_value": rec.get("after_value"),
                                           "units": "as stored in ref.incl_correction",
                                           "applied_by_app": False}))
    return row


def verify(row: dict, rec: dict | None) -> bool:
    """True only when the stored hash matches the sign-off fields and the CURRENT record content."""
    return rec is not None and row.get("signature_hash") == signature(row, rec)


def same_person(reviewer_ids: tuple[str, ...], author) -> bool:
    a = (str(author or "")).strip().lower()
    return bool(a) and any(a == (r or "").strip().lower() for r in reviewer_ids if r)
