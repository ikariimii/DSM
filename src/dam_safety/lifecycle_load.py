"""v1.8: plan for the dam_ref_publish task apply_lifecycle_transitions. Source: Lakebase
dam_app.lifecycle_transition_outbox (one row per approved transition, inserted by the app in the same transaction as
its lifecycle_transition_approve chain record, under the dam lock 4702047 -> check -> chain lock 4702046). Target:
audit.lifecycle_event (append-only, chained per dam), the SCD-2 projection ref.dam_lifecycle_state and the mirror
ref.dam.lifecycle_state / lifecycle_effective_from. Every outcome is audited (audit.event_log LIFECYCLE_LOAD).

Outcome per pending row: 'loaded', 'rejected' (reason_code = first code, message = every '<code>: <detail>'; warnings
appended as 'warning <code>: ...'), or None (deferred: approval record not extracted yet, or another transition for
the same dam / counterpart in this run). Row codes: missing_required_column, invalid_value, approval_record_missing,
approval_record_wrong_action, approval_record_mismatch; then the engine codes (dam_safety.lifecycle.CODES).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Iterable, List, Mapping, Optional

from dam_safety import lifecycle as L

OUTBOX_COLS = ("outbox_id", "dam_id", "from_state", "to_state", "trigger_code", "authorizer_role", "authorizer_user_id",
               "evidence_uri", "ferc_docket_or_order", "effective_ts", "monitoring_rule", "notification_checklist",
               "retention_action", "rationale", "citation_code", "proposed_by", "proposed_at", "approved_by",
               "approved_at", "second_approved_by", "proposal_record_hash", "approval_record_hash",
               "dssmp_version_id", "transition_reason_code", "suspension_reason", "incident_report_uri",
               "sworn_verification_12_13", "sworn_verification_uri", "cdse_basis", "cdse_basis_by",
               "re_directive_uri", "withdrawal_filed_date", "counterpart_dam_id", "created_at", "status",
               "reason_code", "message", "processed_at", "safety_related_ops_event")
REQUIRED = ("outbox_id", "dam_id", "from_state", "to_state", "trigger_code", "authorizer_role", "authorizer_user_id",
            "effective_ts", "citation_code", "proposed_by", "proposed_at", "approved_by", "approved_at",
            "proposal_record_hash", "approval_record_hash")
ROW_CODES = ("missing_required_column", "invalid_value", "approval_record_missing", "approval_record_wrong_action",
             "approval_record_mismatch")
SUBJECT = "dam_lifecycle"
ACTIONS = ("lifecycle_transition_propose", "lifecycle_transition_approve", "lifecycle_transition_reject")
APPROVAL_WAIT = dt.timedelta(hours=1)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_TYPE = "LIFECYCLE_LOAD"
LOG_COLS = ("event_id", "dam_id", "event_type", "subject_type", "subject_id", "actor", "event_ts", "payload",
            "recorded_ts")
STATE_COLS = ("state_id", "dam_id", "lifecycle_state", "effective_from", "effective_to", "event_id")


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _cv(rec):
    try:
        v = json.loads(rec.get("corrected_value") or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _who(rec):
    return {str(x).strip().lower() for x in (rec.get("reviewer_id"), rec.get("reviewer_email")) if x and str(x).strip()}


def row_problems(r: Mapping) -> List[str]:
    out = [f"missing_required_column: {c}" for c in REQUIRED if _blank(r.get(c))]
    for c in ("proposal_record_hash", "approval_record_hash"):
        if not _blank(r.get(c)) and not SHA_RE.match(str(r[c])):
            out.append(f"invalid_value: {c} must be 64 lower-case hex characters")
    for c in ("effective_ts", "approved_at", "proposed_at"):
        if not _blank(r.get(c)) and L._ts(r[c]) is None:
            out.append(f"invalid_value: {c} {r.get(c)!r} is not an ISO-8601 timestamp")
    for c in ("from_state", "to_state"):
        if not _blank(r.get(c)) and r[c] not in L.STATES:
            out.append(f"invalid_value: {c} {r.get(c)!r} is not a lifecycle state")
    if not _blank(r.get("withdrawal_filed_date")) and L._date(r["withdrawal_filed_date"]) is None:
        out.append(f"invalid_value: withdrawal_filed_date {r.get('withdrawal_filed_date')!r} is not a date")
    for c in ("sworn_verification_12_13", "safety_related_ops_event"):
        if r.get(c) is not None and not isinstance(r.get(c), bool):
            out.append(f"invalid_value: {c} must be a boolean")
    if not _blank(r.get("notification_checklist")):
        try:
            json.loads(r["notification_checklist"])
        except (TypeError, ValueError):
            out.append("invalid_value: notification_checklist is not JSON")
    return out


def mismatch(r: Mapping, approve: Mapping, propose: Mapping) -> List[str]:
    bad, cv = [], _cv(approve)
    for rec in (approve, propose):
        if rec.get("dam_id") != r.get("dam_id"):
            bad.append("dam_id")
    for f in ("from_state", "to_state", "citation_code"):
        if cv.get(f) != r.get(f):
            bad.append(f)
    if cv.get("outbox_id") not in (None, r.get("outbox_id")):
        bad.append("outbox_id")
    if str(r.get("approved_by") or "").strip().lower() not in _who(approve):
        bad.append("approved_by")
    if str(r.get("proposed_by") or "").strip().lower() not in _who(propose):
        bad.append("proposed_by")
    if L._ts(approve.get("signed_ts")) != L._ts(r.get("approved_at")):
        bad.append("approved_at")
    if L._ts(propose.get("signed_ts")) != L._ts(r.get("proposed_at")):
        bad.append("proposed_at")
    return sorted(set(bad))


def to_request(r: Mapping) -> dict:
    """outbox row -> engine/event fields (transition_reason_code is the event's reason_code; the outbox's own
    reason_code is load bookkeeping)."""
    q = {f: r.get(f) for f in L.HASH_FIELDS if f in r}
    q["reason_code"] = r.get("transition_reason_code")
    q["safety_related_ops_event"] = r.get("safety_related_ops_event")         # NULL -> treated as false
    q["evidence_uri"] = L.uri_list(r.get("evidence_uri"))
    return q


def plan(pending: Iterable[Mapping], records: Iterable[Mapping], events, *, has_ref_dam=(), citations=None,
         reasons=None, dssmp=(), readiness=None, authority=(), admins=(), now, created_by="dam_ref_publish") -> dict:
    """events: audit.lifecycle_event rows, or None when unreadable. Returns {decisions: [...], events: [new event
    rows]}; each decision {outbox_id, dam_id, decision, status, reason_code, message, codes, warnings, event_ids}."""
    now = L._utc(now)
    by_hash = {x.get("record_hash"): x for x in records}
    current = L.current_states(events)
    applied = {e.get("approval_record_hash") for e in (events or []) if e.get("approval_record_hash")}
    decisions, new_events, busy = [], [], set()

    def decide(r, decision, status, msgs, warns=(), evs=()):
        msg = "; ".join(msgs) if isinstance(msgs, list) else msgs
        codes = [m.split(":", 1)[0] for m in msg.split("; ")] if status == "rejected" else []
        full = "; ".join([msg] + [f"warning {w}" for w in warns])
        decisions.append({"outbox_id": r.get("outbox_id"), "dam_id": r.get("dam_id"), "decision": decision,
                          "status": status, "reason_code": codes[0] if codes else None, "message": full,
                          "codes": codes, "warnings": list(warns), "event_ids": [e["event_id"] for e in evs],
                          "event_hashes": {e["event_id"]: e["event_hash"] for e in evs},
                          "record_hash": r.get("approval_record_hash"), "from_state": r.get("from_state"),
                          "to_state": r.get("to_state")})

    for r in sorted(pending, key=lambda x: (str(x.get("approved_at")), str(x.get("outbox_id")))):
        dam, h = r.get("dam_id"), r.get("approval_record_hash")
        p = row_problems(r)
        if p:
            decide(r, "rejected_invalid", "rejected", p)
            continue
        if h in applied:
            decide(r, "already_applied", "loaded", f"already loaded (approval {h})")
            continue
        dams = {dam} | ({r["counterpart_dam_id"]} if not _blank(r.get("counterpart_dam_id")) else set())
        if dams & busy:
            decide(r, "deferred", None, "another lifecycle transition for this dam is in this run")
            continue
        approve, propose = by_hash.get(h), by_hash.get(r.get("proposal_record_hash"))
        if approve is None or propose is None:
            if now - (L._ts(r.get("approved_at")) or now) < APPROVAL_WAIT:
                decide(r, "deferred", None, "approval / proposal record not yet in audit.review_record (extract lag)")
            else:
                decide(r, "rejected_invalid", "rejected", "approval_record_missing: no audit.review_record "
                       f"{h if approve is None else r.get('proposal_record_hash')}")
            continue
        if (approve.get("subject_type"), approve.get("action_type")) != (SUBJECT, "lifecycle_transition_approve") or \
                (propose.get("subject_type"), propose.get("action_type")) != (SUBJECT, "lifecycle_transition_propose"):
            decide(r, "rejected_invalid", "rejected", "approval_record_wrong_action: "
                   f"{approve.get('action_type')!r} / {propose.get('action_type')!r}")
            continue
        mm = mismatch(r, approve, propose)
        if mm:
            decide(r, "rejected_invalid", "rejected", f"approval_record_mismatch: {', '.join(mm)} differ")
            continue
        req = to_request(r)
        res = L.check_transition(req, current=current, has_ref_dam=has_ref_dam, citations=citations, reasons=reasons,
                                 dssmp=dssmp, readiness=readiness, authority=authority, admins=admins,
                                 proposed_at=propose.get("signed_ts"), approved_at=approve.get("signed_ts"))
        if res["codes"]:
            decide(r, "rejected_" + res["codes"][0], "rejected", res["messages"], res["warnings"])
            continue
        evs = L.build_events(req, current or {}, created_by, now, f"lc:{h}", citations)
        for e in evs:
            current[e["dam_id"]] = {"state": e["to_state"], "effective_from": e["effective_ts"],
                                    "event_id": e["event_id"], "event_hash": e["event_hash"]}
        new_events.extend(evs)
        busy |= dams
        decide(r, "applied", "loaded", f"{dam}: {r['from_state']} -> {r['to_state']} ({r['citation_code']})",
               res["warnings"], evs)
    return {"decisions": decisions, "events": new_events}


def project_states(events: Iterable[Mapping]) -> List[dict]:
    """SCD-2 ref.dam_lifecycle_state rows from the full event history (one row per event; effective_to = the next
    event's effective_ts). Deterministic, so the job can MERGE it every run and heal a crash between steps."""
    by = {}
    for e in events:
        by.setdefault(e["dam_id"], []).append(e)
    out = []
    for dam, evs in by.items():
        evs = L.order_events(evs)
        for i, e in enumerate(evs):
            nxt = evs[i + 1] if i + 1 < len(evs) else None
            out.append({"state_id": e["event_id"], "dam_id": dam, "lifecycle_state": e["to_state"],
                        "effective_from": L._ts(e["effective_ts"]),
                        "effective_to": L._ts(nxt["effective_ts"]) if nxt else None, "event_id": e["event_id"]})
    return out


def outbox_updates(decisions, now_iso):
    return [(d["status"], d["reason_code"], d["message"], now_iso, d["outbox_id"])
            for d in decisions if d["status"] is not None]


def audit_rows(decisions, job_run_id: str, now) -> List[dict]:
    now = L._utc(now)
    out = []
    for d in decisions:
        if d["status"] is None:
            continue
        payload = {k: d[k] for k in ("outbox_id", "status", "reason_code", "message", "warnings", "event_ids",
                                     "from_state", "to_state")}
        payload.update(approval_record_hash=d["record_hash"], job_run_id=job_run_id)
        out.append({"event_id": f"lifecycle_load:{d['outbox_id']}:{d['status']}", "dam_id": d["dam_id"],
                    "event_type": EVENT_TYPE, "subject_type": SUBJECT, "subject_id": d["dam_id"],
                    "actor": "dam_ref_publish", "event_ts": now, "payload": json.dumps(payload, sort_keys=True),
                    "recorded_ts": now})
    return out


def insert_sql(target: str, source: str, cols, key: str) -> str:
    c = ", ".join(cols)
    return (f"INSERT INTO {target} ({c})\nSELECT {', '.join('s.' + x for x in cols)} FROM {source} AS s\n"
            f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS e WHERE e.{key} = s.{key})")


def event_insert_sql(target: str, source: str) -> str:
    """Append the planned events in ONE statement (one Delta commit), idempotent on event_id, never forking a chain,
    and all-or-nothing per transition: the events of one approval (approval_record_hash; a transfer writes the old
    record -> transferred AND the new record draft -> live) are skipped TOGETHER when any of them would fork its dam's
    chain (a stored event other than itself already links to the same prev_event_hash: a concurrent writer). Skipped
    rows stay pending in the outbox and are re-planned next run (mark_insert_skipped)."""
    cols = ", ".join(L.EVENT_COLS)
    return (f"INSERT INTO {target} ({cols})\nSELECT {', '.join('s.' + c for c in L.EVENT_COLS)} FROM {source} AS s\n"
            f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS e WHERE e.event_id = s.event_id)\n"
            f"  AND NOT EXISTS (SELECT 1 FROM {target} AS e WHERE e.dam_id = s.dam_id "
            "AND e.prev_event_hash = s.prev_event_hash)\n"
            f"  AND NOT EXISTS (SELECT 1 FROM {source} AS g JOIN {target} AS e ON e.dam_id = g.dam_id "
            "AND e.prev_event_hash = g.prev_event_hash AND e.event_id <> g.event_id\n"
            "    WHERE g.approval_record_hash = s.approval_record_hash)")


def mark_insert_skipped(decisions, stored: Mapping[str, str]) -> None:
    """stored: event_id -> event_hash read back after the insert. An applied decision whose events are not all
    stored with the planned hash is deferred (status None: the outbox row stays pending, no audit row)."""
    for d in decisions:
        if d["decision"] == "applied" and any(stored.get(i) != h for i, h in d["event_hashes"].items()):
            d.update(decision="deferred_concurrent_write", status=None, reason_code=None,
                     message="the lifecycle chain moved during the load (another writer); re-planned next run")


ARCHIVED_EVENT_TYPE = "DAM_ARCHIVED_REFUSAL"


def archived_split(pending: Iterable[Mapping], current: Optional[Mapping], id_key: str):
    """Rule 2 / 5 for the OTHER dam_ref_publish loaders (CDSE assignment, DSSMP registration, ref publications): a
    pending row of a read-only dam (transferred / decommissioned_archived) is refused 'dam_archived' and never planned.
    current = dam_safety.lifecycle.current_states(audit.lifecycle_event rows); None (unreadable) blocks nothing here
    (rule 7 refuses lifecycle transitions only). Returns (keep, refused) with refused = [{id, dam_id, message}]."""
    keep, refused = [], []
    for r in pending:
        msg = L.write_refusal(r.get("dam_id"), current)
        if msg:
            refused.append({"id": r.get(id_key), "dam_id": r.get("dam_id"), "message": msg})
        else:
            keep.append(r)
    return keep, refused


def archived_audit_rows(refused, job: str, outbox: str, job_run_id, now) -> List[dict]:
    """One audit.event_log row per dam_archived refusal (deterministic event_id, inserted WHERE NOT EXISTS)."""
    now = L._utc(now)
    return [{"event_id": f"dam_archived:{outbox}:{x['id']}", "dam_id": x["dam_id"], "event_type": ARCHIVED_EVENT_TYPE,
             "subject_type": outbox, "subject_id": x["id"], "actor": job, "event_ts": now,
             "payload": json.dumps({"outbox": outbox, "id": x["id"], "reason_code": "dam_archived",
                                    "message": x["message"], "job_run_id": job_run_id}, sort_keys=True),
             "recorded_ts": now} for x in refused]


def state_merge_sql(target: str, source: str) -> str:
    return (f"MERGE INTO {target} AS t USING {source} AS s ON t.state_id = s.state_id\n"
            "WHEN MATCHED AND NOT (t.effective_to <=> s.effective_to) THEN UPDATE SET effective_to = s.effective_to\n"
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(STATE_COLS)}) VALUES ({', '.join('s.' + c for c in STATE_COLS)})")


def dam_mirror_sql(dam_table: str, state_table: str) -> str:
    """ref.dam.lifecycle_state / lifecycle_effective_from = the open SCD-2 row (only dams that have a ref.dam row)."""
    return (f"MERGE INTO {dam_table} AS d USING (SELECT dam_id, lifecycle_state, effective_from FROM {state_table} "
            "WHERE effective_to IS NULL) AS s ON d.dam_id = s.dam_id\n"
            "WHEN MATCHED AND (NOT (d.lifecycle_state <=> s.lifecycle_state) OR NOT (d.lifecycle_effective_from <=> "
            "s.effective_from)) THEN UPDATE SET lifecycle_state = s.lifecycle_state, lifecycle_effective_from = "
            "s.effective_from")


__all__ = ["OUTBOX_COLS", "REQUIRED", "ROW_CODES", "ACTIONS", "SUBJECT", "EVENT_TYPE", "LOG_COLS", "STATE_COLS",
           "plan", "project_states", "outbox_updates", "audit_rows", "insert_sql", "state_merge_sql",
           "dam_mirror_sql", "archived_split", "archived_audit_rows", "ARCHIVED_EVENT_TYPE", "event_insert_sql", "mark_insert_skipped", "row_problems", "mismatch", "to_request"]
