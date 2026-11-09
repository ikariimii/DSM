"""v1.7 (app SCHEMA_CONTRACT §7 item 20): plan for the dam_ref_publish task apply_cdse_assignments. Source: the app
outbox dam_app.dam_cdse_outbox (one row per applied dam_cdse_assign / dam_cdse_change_approve, inserted in the same
transaction as the chain record). Target: ref.dam_cdse_assignment (SCD2; dam_safety.cdse_assignment) and
ref.dam.cdse_id; ref.v_dam_reviewer_authority exposes the new CDSE. Every processed row is audited: one
audit.event_log row (event_type CDSE_ASSIGNMENT_LOAD) per load outcome, plus the assignment history row when loaded.

Outcome per pending row: status 'loaded', 'rejected' (reason_code = first code, message = every '<code>: <detail>'),
or None (deferred: stays pending). Refusal codes (CODES):
  missing_required_column / invalid_value      the outbox row itself (kind assign|change, change_reason_code enum,
                                               approval_record_hash sha256, approved_at ISO-8601)
  approval_record_missing                      no audit.review_record with that record_hash after APPROVAL_WAIT
  approval_record_wrong_action                 assign needs dam_cdse_assign, change needs dam_cdse_change_approve
  approval_record_mismatch                     dam, new/previous CDSE, reason code, outbox_id, approver or
                                               approved_at differ between the record and the row
  cdse_exists                                  assign, but the dam already has a CDSE (ref.dam.cdse_id or an open
                                               assignment)
  cdse_stale                                   change, but the dam's designated CDSE is not previous_cdse_id (also:
                                               skipped by the MERGE's own stale re-check)
  approval_not_authorized                      the engine (dam_safety.cdse_assignment) refuses the record; the
                                               message lists its dam_cdse_* codes
Chain integrity of the record (hash, head) is verify_review_chain's job, not repeated here.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Iterable, List, Mapping

from dam_safety.cdse_assignment import REASON_CODES, _cv, _norm, current_cdse, evaluate
from dam_safety.rule_flag_authority import _utc, _who

OUTBOX_COLS = ("outbox_id", "dam_id", "kind", "previous_cdse_id", "new_cdse_id", "change_reason_code", "proposed_by",
               "approved_by", "approved_at", "approval_record_hash", "status", "reason_code", "message",
               "processed_at")
REQUIRED = ("outbox_id", "dam_id", "kind", "new_cdse_id", "change_reason_code", "proposed_by", "approved_by",
            "approved_at", "approval_record_hash")
CODES = ("missing_required_column", "invalid_value", "approval_record_missing", "approval_record_wrong_action",
         "approval_record_mismatch", "cdse_exists", "cdse_stale", "approval_not_authorized")
ACTION_FOR_KIND = {"assign": "dam_cdse_assign", "change": "dam_cdse_change_approve"}
APPROVAL_WAIT = dt.timedelta(hours=1)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_TYPE = "CDSE_ASSIGNMENT_LOAD"
EVENT_COLS = ("event_id", "dam_id", "event_type", "subject_type", "subject_id", "actor", "event_ts", "payload",
              "recorded_ts")


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _ts(v):
    try:
        return None if _blank(v) else _utc(v)
    except (TypeError, ValueError):
        return None


def _set(v) -> set:
    return {_norm(x) for x in str(v or "").split(",") if _norm(x)}


def row_problems(r: Mapping) -> List[str]:
    out = [f"missing_required_column: {c}" for c in REQUIRED if _blank(r.get(c))]
    if not _blank(r.get("kind")) and r.get("kind") not in ACTION_FOR_KIND:
        out.append(f"invalid_value: kind {r.get('kind')!r} is not assign | change")
    if not _blank(r.get("change_reason_code")) and r.get("change_reason_code") not in REASON_CODES:
        out.append(f"invalid_value: change_reason_code {r.get('change_reason_code')!r}")
    if not _blank(r.get("approval_record_hash")) and not SHA_RE.match(str(r["approval_record_hash"])):
        out.append("invalid_value: approval_record_hash must be 64 lower-case hex characters")
    if not _blank(r.get("approved_at")) and _ts(r["approved_at"]) is None:
        out.append(f"invalid_value: approved_at {r.get('approved_at')!r} is not an ISO-8601 timestamp")
    if r.get("kind") == "change" and _blank(r.get("previous_cdse_id")):
        out.append("missing_required_column: previous_cdse_id (kind change)")
    return out


def mismatch(r: Mapping, rec: Mapping) -> List[str]:
    cv, bad = _cv(rec), []
    if rec.get("dam_id") != r.get("dam_id") or str(rec.get("subject_id")) != str(r.get("dam_id")):
        bad.append("dam_id")
    if _norm(cv.get("new_cdse_id")) != _norm(r.get("new_cdse_id")):
        bad.append("new_cdse_id")
    if _set(cv.get("previous_cdse_id")) != _set(r.get("previous_cdse_id")):
        bad.append("previous_cdse_id")
    if cv.get("change_reason_code") != r.get("change_reason_code"):
        bad.append("change_reason_code")
    if cv.get("outbox_id") not in (None, r.get("outbox_id")):
        bad.append("outbox_id")
    if _norm(r.get("approved_by")) not in _who(rec):
        bad.append("approved_by")
    if _ts(rec.get("signed_ts")) != _ts(r.get("approved_at")):
        bad.append("approved_at")
    return bad


def plan(pending: Iterable[Mapping], records: Iterable[Mapping], assignments: Iterable[Mapping], legacy: Mapping,
         admins: Iterable[Mapping], dam_state: Mapping, dam_cdse: Mapping, now, memberships: Iterable[Mapping] = (),
         require_second_approver=None) -> List[dict]:
    """dam_cdse: dam_id -> ref.dam.cdse_id (rows that exist). Returns per row {outbox_id, dam_id, decision, status,
    reason_code, message, codes, rows (assignment rows to MERGE), record_hash}."""
    now = _utc(now)
    records, assignments = list(records), list(assignments)
    probs, new_rows = evaluate(records, assignments, legacy, admins, dam_state, memberships, require_second_approver)
    by_hash = {r.get("record_hash"): r for r in records}
    applied = {a.get("approval_record_hash") for a in assignments if a.get("approval_record_hash")}
    out, busy = [], set()

    def decide(r, decision, status, msgs, rows=()):
        msg = "; ".join(msgs) if isinstance(msgs, list) else msgs
        codes = [m.split(":", 1)[0] for m in msg.split("; ")] if status == "rejected" else []
        out.append({"outbox_id": r.get("outbox_id"), "dam_id": r.get("dam_id"), "kind": r.get("kind"),
                    "decision": decision, "status": status, "reason_code": codes[0] if codes else None,
                    "message": msg, "codes": codes, "rows": list(rows), "record_hash": r.get("approval_record_hash"),
                    "previous_cdse_id": r.get("previous_cdse_id"), "new_cdse_id": r.get("new_cdse_id")})

    for r in sorted(pending, key=lambda x: (str(x.get("approved_at")), str(x.get("outbox_id")))):
        dam, h = r.get("dam_id"), r.get("approval_record_hash")
        p = row_problems(r)
        if p:
            decide(r, "rejected_invalid", "rejected", p)
            continue
        if h in applied:
            decide(r, "already_applied", "loaded", f"already loaded (assignment {h})")
            continue
        if dam in busy:
            decide(r, "deferred", None, "another CDSE load for this dam is in this run")
            continue
        rec = by_hash.get(h)
        if rec is None:
            if now - (_ts(r.get("approved_at")) or now) < APPROVAL_WAIT:
                decide(r, "deferred", None, "approval record not yet in audit.review_record (extract lag)")
            else:
                decide(r, "rejected_invalid", "rejected", f"approval_record_missing: no audit.review_record {h}")
            continue
        if rec.get("action_type") != ACTION_FOR_KIND[r["kind"]] or rec.get("subject_type") != "dam":
            decide(r, "rejected_invalid", "rejected", f"approval_record_wrong_action: {rec.get('subject_type')!r}/"
                   f"{rec.get('action_type')!r} for kind {r['kind']!r}")
            continue
        mm = mismatch(r, rec)
        if mm:
            decide(r, "rejected_invalid", "rejected", f"approval_record_mismatch: {', '.join(mm)} differ")
            continue
        state_now = current_cdse(dam, now + dt.timedelta(microseconds=1), assignments, legacy)
        if r["kind"] == "assign" and (not _blank(dam_cdse.get(dam)) or state_now):
            decide(r, "rejected_cdse_exists", "rejected", f"cdse_exists: {dam} already has CDSE "
                   f"{dam_cdse.get(dam) or state_now}")
            continue
        if r["kind"] == "change" and _norm(state_now) not in _set(r.get("previous_cdse_id")):
            decide(r, "rejected_stale", "rejected", f"cdse_stale: {dam}'s CDSE is {state_now!r}, not "
                   f"{r.get('previous_cdse_id')!r}")
            continue
        mine = [p for p in probs if p.get("review_id") == rec.get("review_id")]
        if mine:
            decide(r, "rejected_invalid", "rejected", "approval_not_authorized: " + ", ".join(
                f"{p['code']} ({p['detail']})" for p in mine))
            continue
        rows = [x for x in new_rows if x.get("for_record") == h]
        busy.add(dam)
        decide(r, "applied", "loaded", f"{dam} CDSE -> {r['new_cdse_id']} ({r['kind']})", rows)
    return out


def mark_merge_skipped(decisions: List[dict], live_ids: set) -> None:
    """After the MERGE: an applied decision whose assignment row is not in the table was skipped by the stale
    re-check inside the MERGE -> rejected cdse_stale."""
    for d in decisions:
        if d["decision"] == "applied" and d["record_hash"] not in live_ids:
            d.update(decision="rejected_stale", status="rejected", reason_code="cdse_stale", codes=["cdse_stale"],
                     message="cdse_stale: skipped by the MERGE stale re-check (concurrent change)")


def outbox_updates(decisions, now_iso):
    return [(d["status"], d["reason_code"], d["message"], now_iso, d["outbox_id"])
            for d in decisions if d["status"] is not None]


def audit_rows(decisions, job_run_id: str, now) -> List[dict]:
    """One append-only audit.event_log row per load outcome (deferred rows are not outcomes). event_id is
    deterministic, so a re-run after a crash does not duplicate it (the job inserts WHERE NOT EXISTS)."""
    now = _utc(now)
    out = []
    for d in decisions:
        if d["status"] is None:
            continue
        payload = {"outbox_id": d["outbox_id"], "kind": d["kind"], "status": d["status"],
                   "reason_code": d["reason_code"], "message": d["message"], "approval_record_hash": d["record_hash"],
                   "assignment_id": d["record_hash"] if d["status"] == "loaded" else None,
                   "previous_cdse_id": d["previous_cdse_id"], "new_cdse_id": d["new_cdse_id"],
                   "job_run_id": job_run_id}
        out.append({"event_id": f"cdse_load:{d['outbox_id']}:{d['status']}", "dam_id": d["dam_id"],
                    "event_type": EVENT_TYPE, "subject_type": "dam", "subject_id": d["dam_id"],
                    "actor": "dam_ref_publish", "event_ts": now, "payload": json.dumps(payload, sort_keys=True),
                    "recorded_ts": now})
    return out


def audit_insert_sql(target: str, source: str) -> str:
    cols = ", ".join(EVENT_COLS)
    return (f"INSERT INTO {target} ({cols})\nSELECT {', '.join('s.' + c for c in EVENT_COLS)} FROM {source} AS s\n"
            f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS e WHERE e.event_id = s.event_id)")


__all__ = ["OUTBOX_COLS", "CODES", "EVENT_TYPE", "EVENT_COLS", "row_problems", "mismatch", "plan",
           "mark_merge_skipped", "outbox_updates", "audit_rows", "audit_insert_sql"]
