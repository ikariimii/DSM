"""v1.7 (Genie 2026-11-09): register a dam's CDSE-approved DSSMP version from the app outbox into ref.dssmp_version.

Source: Lakebase dam_app.dssmp_registration_outbox, the agreed app contract (app SCHEMA_CONTRACT §7 item 19;
lakebase/001 + lakebase/migrations/v1_7_004). The app INSERTs at 'pending' in the CDSE approve transaction; the
publisher role may only UPDATE status / reason_code / message / processed_at. Columns: outbox_id, dam_id,
dssmp_version_id, version_no, effective_from, change_reason, source_ref, document_uri, document_sha256, proposed_by,
approved_by, approved_at, approval_record_hash, status (pending | loaded | rejected; 'rejected_invalid' was
dropped from the CHECK on 2026-11-09), reason_code, message, processed_at. The previous approved version is
looked up in ref.dssmp_version (no supersedes column).
Target: ref.dssmp_version (sql/ddl/04 + migration v1_7_003_dssmp_registration). ONE conditional MERGE per run inserts
the new version as 'approved' (loaded_from_outbox_id = outbox_id) and, in the same statement, closes the dam's previous
approved version (status 'superseded', effective_to = the new effective_from), re-checking inside the statement that
the previous approved version is still the one the row supersedes (stale re-check).

Outcome: status 'loaded', or 'rejected' with reason_code = the first refusal code and message = every
'<code>: <detail>' joined by '; '. Refusal codes:
  missing_required_column / invalid_value      the outbox row itself
  approval_record_missing                      no audit.review_record with record_hash = approval_record_hash after
                                               APPROVAL_WAIT (before that the row is `deferred`: the extract may lag)
  approval_record_wrong_action                 that record is not a dssmp_version_approve / approve
  approval_record_mismatch                     dam, subject, reviewer, signed_ts, version_no, effective_from or
                                               document_sha256 differ between the record and the outbox row
  approval_not_cdse                            the approver held no CDSE authority for the dam at signed_ts
                                               (ref.v_dam_reviewer_authority); plus any dssmp_authority code on it
  dssmp_version_exists                         dssmp_version_id already registered (not by this outbox row)
  version_not_newer / effective_from_not_after_current
  stale                                        the dam's approved versions changed while the row was planned (MERGE
                                               re-check) or the dam has more than one open approved version
Document copy (dam_safety.dssmp_documents, run by the job between plan and MERGE): dssmp_document_missing,
dssmp_document_other_dam, dssmp_document_hash_mismatch, dssmp_document_copy_failed; ref.dssmp_version.volume_uri.
Chain integrity of the approve record (hash, signature, head) is verify_review_chain's job, not repeated here.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, List, Mapping

from dam_safety.dssmp_authority import dssmp_authority_problems
from dam_safety.rule_flag_authority import _utc, _who, cdse_dams_at

DSSMP_COLS = ("dssmp_version_id", "dam_id", "version_no", "effective_from", "effective_to", "status", "change_reason",
              "source_ref", "document_uri", "document_sha256", "approved_by", "approved_at", "approval_record_hash",
              "loaded_from_outbox_id", "volume_uri")
OUTBOX_COLS = ("outbox_id", "dam_id", "dssmp_version_id", "version_no", "effective_from", "change_reason",
               "source_ref", "document_uri", "document_sha256", "proposed_by", "approved_by", "approved_at",
               "approval_record_hash", "status", "reason_code", "message", "processed_at")
STATUSES = ("pending", "loaded", "rejected")   # v1.7 steering 2026-11-09: no rejected_invalid
REQUIRED = ("outbox_id", "dam_id", "dssmp_version_id", "version_no", "effective_from", "document_sha256",
            "approved_by", "approved_at", "approval_record_hash")
APPROVAL_WAIT = dt.timedelta(hours=1)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
CODES = ("missing_required_column", "invalid_value", "approval_record_missing", "approval_record_wrong_action",
         "approval_record_mismatch", "approval_not_cdse", "dssmp_version_exists", "version_not_newer",
         "effective_from_not_after_current", "stale")


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _ts(v):
    try:
        return None if _blank(v) else _utc(v)
    except (TypeError, ValueError):
        return None


def _cv(rec) -> dict:
    import json
    try:
        v = json.loads(rec.get("corrected_value") or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def row_problems(r: Mapping) -> List[str]:
    out = [f"missing_required_column: {c}" for c in REQUIRED if _blank(r.get(c))]
    if not _blank(r.get("version_no")):
        try:
            if isinstance(r["version_no"], bool) or int(r["version_no"]) != float(r["version_no"]) or int(r["version_no"]) < 1:
                raise ValueError
        except (TypeError, ValueError):
            out.append(f"invalid_value: version_no {r.get('version_no')!r} must be an integer >= 1")
    for c in ("effective_from", "approved_at"):
        if not _blank(r.get(c)) and _ts(r.get(c)) is None:
            out.append(f"invalid_value: {c} {r.get(c)!r} is not an ISO-8601 timestamp")
    for c in ("document_sha256", "approval_record_hash"):
        if not _blank(r.get(c)) and not SHA_RE.match(str(r[c])):
            out.append(f"invalid_value: {c} must be 64 lower-case hex characters")
    return out


def approval_problems(r: Mapping, rec, authority, app_roles=None) -> List[str]:
    if rec is None:
        return ["approval_record_missing: no audit.review_record with record_hash "
                f"{r.get('approval_record_hash')!r}"]
    if rec.get("action_type") != "dssmp_version_approve" or rec.get("decision") != "approve" \
            or rec.get("subject_type") != "dssmp_version":
        return [f"approval_record_wrong_action: {rec.get('subject_type')!r}/{rec.get('action_type')!r}/"
                f"{rec.get('decision')!r} is not a dssmp_version approve record"]
    out, cv = [], _cv(rec)
    mism = []
    if rec.get("dam_id") != r.get("dam_id"):
        mism.append("dam_id")
    if str(rec.get("subject_id")) != str(r.get("dssmp_version_id")) or \
            str(cv.get("dssmp_version_id")) != str(r.get("dssmp_version_id")):
        mism.append("dssmp_version_id")
    if str(r.get("approved_by") or "").strip().lower() not in _who(rec):
        mism.append("approved_by")
    if _ts(rec.get("signed_ts")) != _ts(r.get("approved_at")):
        mism.append("approved_at")
    try:
        if int(cv.get("version_no")) != int(r.get("version_no")):
            mism.append("version_no")
    except (TypeError, ValueError):
        mism.append("version_no")
    if _ts(cv.get("effective_from")) != _ts(r.get("effective_from")):
        mism.append("effective_from")
    if str(cv.get("document_sha256") or "") != str(r.get("document_sha256") or ""):
        mism.append("document_sha256")
    if mism:
        out.append(f"approval_record_mismatch: {', '.join(mism)} differ between the approve record and the outbox row")
    if rec.get("dam_id") not in cdse_dams_at(rec, authority):
        out.append(f"approval_not_cdse: approver held no CDSE authority for {rec.get('dam_id')} at signed_ts")
    for p in dssmp_authority_problems([rec], authority, app_roles):
        if p["severity"] == "error" and not p["code"].endswith("_proposal_missing"):
            out.append(f"{p['code']}: {p['detail']}")
    return out


def plan(pending: Iterable[Mapping], versions: Iterable[Mapping], approvals: Mapping[str, Mapping],
         authority: Iterable[Mapping], now, app_roles=None) -> List[dict]:
    """pending: outbox rows (status 'pending'). versions: ref.dssmp_version rows of the dams concerned (all statuses).
    approvals: record_hash -> audit.review_record row. Returns decisions {outbox_id, dam_id, decision, status,
    message, codes, reason_code, insert, close} with decision applied | already_applied | rejected_invalid |
    rejected_stale | deferred; status loaded | rejected | None (deferred: stays pending)."""
    now = _utc(now)
    authority = list(authority)
    by_id = {v["dssmp_version_id"]: v for v in versions}
    out, busy = [], set()

    def decide(r, decision, status, msgs, insert=None, close=None):
        msg = "; ".join(msgs) if isinstance(msgs, list) else msgs
        codes = [m.split(":", 1)[0] for m in msg.split("; ")] if decision.startswith("rejected") else []
        out.append({"outbox_id": r.get("outbox_id"), "dam_id": r.get("dam_id"), "decision": decision,
                    "status": status, "reason_code": codes[0] if codes else None, "message": msg, "codes": codes,
                    "insert": insert, "close": close})

    for r in sorted(pending, key=lambda x: (str(x.get("approved_at")), str(x.get("outbox_id")))):
        dam, vid = r.get("dam_id"), r.get("dssmp_version_id")
        probs = row_problems(r)
        if probs:
            decide(r, "rejected_invalid", "rejected", probs)
            continue
        have = by_id.get(vid)
        if have is not None:
            if have.get("loaded_from_outbox_id") == r.get("outbox_id"):
                decide(r, "already_applied", "loaded", f"already registered as {vid}")
            else:
                decide(r, "rejected_invalid", "rejected",
                       f"dssmp_version_exists: {vid!r} is already registered (dam {have.get('dam_id')!r}, "
                       f"loaded_from_outbox_id {have.get('loaded_from_outbox_id')!r})")
            continue
        if dam in busy:
            decide(r, "deferred", None, "another registration for this dam is in this run")
            continue
        rec = approvals.get(r.get("approval_record_hash"))
        if rec is None and now - (_ts(r.get("approved_at")) or now) < APPROVAL_WAIT:
            decide(r, "deferred", None, "approve record not yet in audit.review_record (extract lag)")
            continue
        probs = approval_problems(r, rec, authority, app_roles)
        if probs:
            decide(r, "rejected_invalid", "rejected", probs)
            continue
        dam_versions = [v for v in versions if v.get("dam_id") == dam]
        cur = [v for v in dam_versions if v.get("status") == "approved" and v.get("effective_to") is None]
        cur_id = cur[0]["dssmp_version_id"] if len(cur) == 1 else None
        if len(cur) > 1:
            decide(r, "rejected_stale", "rejected",
                   f"stale: the dam has {len(cur)} open approved versions {[v['dssmp_version_id'] for v in cur]}; "
                   "nothing written")
            continue
        eff, vno = _ts(r["effective_from"]), int(r["version_no"])
        msgs = []
        if dam_versions and vno <= max(int(v["version_no"]) for v in dam_versions):
            msgs.append(f"version_not_newer: version_no {vno} <= {max(int(v['version_no']) for v in dam_versions)}")
        if cur and eff <= _ts(cur[0]["effective_from"]):
            msgs.append(f"effective_from_not_after_current: {eff.isoformat()} <= {cur_id}'s effective_from")
        if msgs:
            decide(r, "rejected_invalid", "rejected", msgs)
            continue
        ins = {"dssmp_version_id": vid, "dam_id": dam, "version_no": vno, "effective_from": eff, "effective_to": None,
               "status": "approved", "change_reason": r.get("change_reason"), "source_ref": r.get("source_ref"),
               "document_uri": r.get("document_uri"), "document_sha256": r["document_sha256"],
               "approved_by": r["approved_by"], "approved_at": _ts(r["approved_at"]),
               "approval_record_hash": r["approval_record_hash"], "loaded_from_outbox_id": r["outbox_id"],
               "volume_uri": None}                         # set by dssmp_documents.apply_documents before the MERGE
        busy.add(dam)
        decide(r, "applied", "loaded", f"registered {vid} (approved)" + (f", superseding {cur_id}" if cur_id else ""),
               insert=ins, close=({"dssmp_version_id": cur_id, "effective_to": eff} if cur_id else None))
    return out


def merge_source_rows(decisions: Iterable[Mapping]) -> List[dict]:
    """Per applied decision: an 'insert' row (target key = the new id) and, when it supersedes, a 'close' row keyed on
    the previous id. supersedes_id is carried on both so the MERGE re-checks staleness in the same statement."""
    out = []
    for d in decisions:
        if d["decision"] != "applied":
            continue
        ins, cl = d["insert"], d["close"]
        sup = cl["dssmp_version_id"] if cl else None
        out.append({**ins, "merge_action": "insert", "merge_key": ins["dssmp_version_id"], "supersedes_id": sup})
        if cl:
            out.append({**ins, "merge_action": "close", "merge_key": sup, "supersedes_id": sup})
    return out


def merge_sql(target: str, source: str) -> str:
    """ONE conditional MERGE: a source row is used only while the dam's current approved version (effective_to NULL)
    is still supersedes_id (NULL: the dam has none yet); the close sets only status / effective_to of that version."""
    cols = ", ".join(DSSMP_COLS)
    vals = ", ".join(f"s.{c}" for c in DSSMP_COLS)
    return (
        f"MERGE INTO {target} AS t\n"
        f"USING (SELECT s.* FROM {source} AS s WHERE\n"
        f"         (s.supersedes_id IS NULL AND NOT EXISTS (SELECT 1 FROM {target} AS c WHERE c.dam_id = s.dam_id\n"
        f"            AND c.status = 'approved' AND c.effective_to IS NULL))\n"
        f"      OR (s.supersedes_id IS NOT NULL AND EXISTS (SELECT 1 FROM {target} AS c WHERE c.dam_id = s.dam_id\n"
        f"            AND c.dssmp_version_id = s.supersedes_id AND c.status = 'approved' AND c.effective_to IS NULL)"
        f"\n            AND 1 = (SELECT count(*) FROM {target} AS c WHERE c.dam_id = s.dam_id\n"
        f"            AND c.status = 'approved' AND c.effective_to IS NULL))) AS s\n"
        f"ON t.dssmp_version_id = s.merge_key\n"
        f"WHEN MATCHED AND s.merge_action = 'close' AND t.status = 'approved' AND t.effective_to IS NULL\n"
        f"  THEN UPDATE SET status = 'superseded', effective_to = s.effective_from\n"
        f"WHEN NOT MATCHED AND s.merge_action = 'insert'\n"
        f"  THEN INSERT ({cols}) VALUES ({vals})")


__all__ = ["DSSMP_COLS", "OUTBOX_COLS", "STATUSES", "REQUIRED", "CODES", "APPROVAL_WAIT", "row_problems", "approval_problems",
           "plan", "merge_source_rows", "merge_sql"]
