"""v1.8 dam lifecycle (contract agreed 2026-11-09 14:20, citation list 14:22). Pure engine: states, edges, refusal
codes, citation / suspension-reason rules, approval authority, the per-dam event hash chain and the alarm policy.
Tables (sql/ddl/14_dam_lifecycle.sql, migration sql/migrations/v1_8_001_dam_lifecycle.sql):
  audit.lifecycle_event (append-only, chained per dam), ref.dam_lifecycle_state (SCD-2) + mirror ref.dam.lifecycle_state
  / lifecycle_effective_from, ref.v_dam_lifecycle_current, ref.lifecycle_citation and ref.lifecycle_suspension_reason
  (UC-owned enums stored as DATA; this module hard-codes neither the citation list nor its edges).
Source of transitions: Lakebase dam_app.lifecycle_transition_outbox (lakebase/migrations/v1_8_001), loaded by the
dam_ref_publish task apply_lifecycle_transitions (dam_safety.lifecycle_load).

Refusal codes (HTTP 409 on the app side; check order = CODES order, the first is reason_code):
  lifecycle_unverifiable             the dam's lifecycle state (or the citation / reason enums) cannot be read, or its
                                     chain does not verify -> no lifecycle write; alarms stay on (rule 7)
  dam_archived                       the dam is transferred or decommissioned_archived: read-only (rules 2, 5)
  lifecycle_stale                    from_state is not the dam's current state
  lifecycle_transition_invalid       (from_state, to_state) is not an edge; voided_in_error is terminal
  lifecycle_evidence_required        no evidence reference (rule 8)
  lifecycle_suspension_reason_required  a transition into suspended_monitoring_modified without a known reason
  lifecycle_citation_invalid         citation_code missing / unknown / not for this edge (or this suspension reason)
  lifecycle_withdrawal_date_required a *_withdrawn_* citation (withdrawal_notice row) without withdrawal_filed_date
  lifecycle_incident_report_required emergency_modification without incident_report_uri
  lifecycle_sworn_verification_required construction / emergency modification without sworn_verification_12_13 = true
                                     and sworn_verification_uri (18 CFR 12.13)
  lifecycle_cdse_basis_required      drawdown / monitoring_change without cdse_basis + cdse_basis_by
  lifecycle_rationale_required       reason 'other' without a rationale
  lifecycle_re_directive_required    reason re_directed without re_directive_uri
  lifecycle_dssmp_required           into suspended: no NEW approved DSSMP version in effect at effective_ts;
                                     suspended -> live: no approved DSSMP version in effect at effective_ts
  dam_go_live_not_ready              draft -> live (and the transfer's new record): ref.v_dam_go_live_readiness not ready
  lifecycle_transfer_target_invalid  pending_transfer -> transferred: counterpart_dam_id missing, the same dam, or not a
                                     draft record
  lifecycle_approval_invalid         the approver (and for void the proposer) lacks the authority at decision time, or
                                     the void is not two distinct people covering CDSE + platform_admin
Warnings (never block): lifecycle_notice_60_day (construction_modification, effective_ts less than 60 days after the
approval; 18 CFR 12.11(b)(2)); lifecycle_withdrawal_15_day (effective_ts less than 15 days after withdrawal_filed_date;
18 CFR 385.216(b)). drawdown / monitoring_change lead time is per license or RE direction: no CFR lead-time check.
Authority per edge is PROVISIONAL (docs/VERIFICATION.md LIFECYCLE-AUTHORITY): see APPROVER_ROLES.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Iterable, List, Mapping, Optional

STATES = ("draft_commissioning", "live", "suspended_monitoring_modified", "pending_transfer", "transferred",
          "pending_surrender", "decommissioned_archived", "voided_in_error")
DRAFT, LIVE, SUSP, P_TRANSFER, TRANSFERRED, P_SURRENDER, DECOM, VOID = STATES
EDGES = frozenset({
    (DRAFT, LIVE), (DRAFT, VOID),
    (LIVE, SUSP), (SUSP, LIVE),
    (LIVE, P_TRANSFER), (P_TRANSFER, LIVE), (P_TRANSFER, TRANSFERRED),
    (LIVE, P_SURRENDER), (SUSP, P_SURRENDER),
    (P_SURRENDER, LIVE), (P_SURRENDER, DECOM),
    (LIVE, VOID),
})
READ_ONLY = frozenset({TRANSFERRED, DECOM})            # 409 dam_archived on every write
ALARMS_OFF = frozenset({VOID, DECOM, TRANSFERRED})     # rule 1: alarms stop
SUSPENSION_REASONS = ("construction_modification", "emergency_modification", "drawdown", "monitoring_change", "other",
                      "re_directed")
CODES = ("lifecycle_unverifiable", "dam_archived", "lifecycle_stale", "lifecycle_transition_invalid",
         "lifecycle_evidence_required", "lifecycle_suspension_reason_required", "lifecycle_citation_invalid",
         "lifecycle_withdrawal_date_required", "lifecycle_incident_report_required",
         "lifecycle_sworn_verification_required", "lifecycle_cdse_basis_required", "lifecycle_rationale_required",
         "lifecycle_re_directive_required", "lifecycle_dssmp_required", "dam_go_live_not_ready",
         "lifecycle_transfer_target_invalid", "lifecycle_approval_invalid")
WARNINGS = ("lifecycle_notice_60_day", "lifecycle_withdrawal_15_day")
NOTICE_60 = dt.timedelta(days=60)
WITHDRAWAL_15 = dt.timedelta(days=15)
GENESIS = "0" * 64
# PROVISIONAL: who may approve each edge (roles resolved at the approve record's signed_ts). 'cdse' = CDSE of the dam
# in ref.v_dam_reviewer_authority; 'platform_admin' = ref.project_membership ADMIN on '*'. Void: two people (VOID_ROLES).
APPROVER_ROLES = {
    (DRAFT, LIVE): ("cdse", "platform_admin"),
    (LIVE, SUSP): ("cdse",), (SUSP, LIVE): ("cdse",),
    (LIVE, P_TRANSFER): ("cdse", "platform_admin"), (P_TRANSFER, LIVE): ("cdse", "platform_admin"),
    (P_TRANSFER, TRANSFERRED): ("cdse", "platform_admin"),
    (LIVE, P_SURRENDER): ("cdse", "platform_admin"), (SUSP, P_SURRENDER): ("cdse", "platform_admin"),
    (P_SURRENDER, LIVE): ("cdse", "platform_admin"), (P_SURRENDER, DECOM): ("cdse", "platform_admin"),
}
VOID_ROLES = frozenset({"cdse", "platform_admin"})

UNREADABLE = "__unreadable__"
# hash input, in this order (event_hash itself and created_ts are not hashed)
HASH_FIELDS = ("prev_event_hash", "event_id", "dam_id", "from_state", "to_state", "trigger_code", "authorizer_role",
               "authorizer_user_id", "evidence_uri", "ferc_docket_or_order", "effective_ts", "monitoring_rule",
               "notification_checklist", "retention_action", "created_by", "rationale", "citation_code",
               "proposed_by", "approved_by", "second_approved_by", "approval_record_hash", "dssmp_version_id",
               "reason_code", "suspension_reason", "incident_report_uri", "sworn_verification_12_13",
               "sworn_verification_uri", "cdse_basis", "cdse_basis_by", "re_directive_uri", "withdrawal_filed_date",
               "counterpart_dam_id")
EVENT_COLS = ("event_id",) + tuple(f for f in HASH_FIELDS if f != "event_id") + ("created_ts", "event_hash")


# ---------------------------------------------------------------- small helpers
def _utc(v) -> Optional[dt.datetime]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        v = dt.datetime(v.year, v.month, v.day)
    d = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)


def _ts(v):
    try:
        return _utc(v)
    except (TypeError, ValueError):
        return None


def _date(v) -> Optional[dt.date]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _norm(p) -> str:
    return str(p or "").strip().lower()


def uri_list(v) -> List[str]:
    """evidence_uri as a list: a list/tuple, a JSON array text, or one URI text."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = v
    else:
        s = str(v).strip()
        if s.startswith("["):
            try:
                items = json.loads(s)
            except ValueError:
                return []
            if not isinstance(items, list):
                return []
        else:
            items = [s]
    return [str(x).strip() for x in items if x is not None and str(x).strip()]


def ts_text(v) -> Optional[str]:
    """Canonical UTC second-precision text 'YYYY-MM-DDTHH:MM:SSZ' (the hashed form of effective_ts)."""
    t = _ts(v)
    return None if t is None else t.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- hash chain
def canon_value(field, v) -> Optional[str]:
    if v is None:
        return None
    if field == "evidence_uri":
        return " ".join(uri_list(v)) if uri_list(v) else None
    if field == "effective_ts":
        return ts_text(v)
    if field == "withdrawal_filed_date":
        d = _date(v)
        return None if d is None else d.isoformat()
    if field == "sworn_verification_12_13":
        return "true" if v is True or str(v).lower() == "true" else "false"
    return str(v)


def hash_input(event: Mapping) -> str:
    out = []
    for f in HASH_FIELDS:
        s = canon_value(f, event.get(f))
        out.append(f"{-1 if s is None else len(s)}:{'' if s is None else s}|")
    return "".join(out)


def event_hash(event: Mapping) -> str:
    return hashlib.sha256(hash_input(event).encode("utf-8")).hexdigest()


def hash_sql(expr: Mapping[str, str]) -> str:
    """The same hash in Spark SQL: expr maps each HASH_FIELDS name to a STRING-valued SQL expression already in the
    canonical form (NULL allowed). Used by the backfill migration (tests assert the file carries exactly this). One
    concat per field inside the outer concat: 32 arguments, not 128 (PostgreSQL caps a call at 100 arguments; the
    tests run the same SQL there)."""
    parts = []
    for f in HASH_FIELDS:
        x = expr.get(f, "CAST(NULL AS STRING)")
        parts.append(f"concat(coalesce(CAST(length({x}) AS STRING), '-1'), ':', coalesce({x}, ''), '|')")
    return "sha2(concat(\n    " + ",\n    ".join(parts) + "), 256)"


def verify_chain(events: Iterable[Mapping]) -> dict:
    """events of ONE OR MORE dams -> {dam_id: problem text or None}. Order per dam = ledger order (effective_ts,
    created_ts, event_id); each prev_event_hash must be the previous event_hash (first = GENESIS) and each
    event_hash must recompute."""
    by = {}
    for e in events:
        by.setdefault(e.get("dam_id"), []).append(e)
    out = {}
    for dam, evs in by.items():
        prev, problem = GENESIS, None
        for e in order_events(evs):
            if e.get("prev_event_hash") != prev:
                problem = f"event {e.get('event_id')}: prev_event_hash does not link to the previous event"
                break
            if event_hash(e) != e.get("event_hash"):
                problem = f"event {e.get('event_id')}: event_hash does not recompute"
                break
            prev = e.get("event_hash")
        out[dam] = problem
    return out


def order_events(evs):
    """Chain order: by prev_event_hash links from GENESIS (robust to equal timestamps); falls back to time order."""
    evs = list(evs)
    nxt = {}
    for e in evs:
        nxt.setdefault(e.get("prev_event_hash"), []).append(e)
    out, cur = [], GENESIS
    while cur in nxt and len(nxt[cur]) == 1 and len(out) < len(evs):
        e = nxt[cur][0]
        out.append(e)
        cur = e.get("event_hash")
    if len(out) == len(evs):
        return out
    return sorted(evs, key=lambda e: (str(ts_text(e.get("effective_ts"))), str(e.get("created_ts")),
                                      str(e.get("event_id"))))


# ---------------------------------------------------------------- current state
def current_states(events: Optional[Iterable[Mapping]]) -> Optional[dict]:
    """audit.lifecycle_event rows -> {dam_id: {state, effective_from, event_id, event_hash} | UNREADABLE}.
    None in -> None out (the table could not be read: every dam is unverifiable). A dam whose chain does not verify
    or whose events do not form one path of edges is UNREADABLE (fail closed)."""
    if events is None:
        return None
    events = list(events)
    chk = verify_chain(events)
    out = {}
    for dam, problem in chk.items():
        evs = order_events([e for e in events if e.get("dam_id") == dam])
        if problem:
            out[dam] = UNREADABLE
            continue
        state, ok = None, True
        for e in evs:
            if e.get("trigger_code") == "transfer_counterpart" and state is None:
                pass
            elif state is not None and e.get("from_state") != state:
                ok = False
            state = e.get("to_state")
        last = evs[-1]
        out[dam] = UNREADABLE if not ok or state not in STATES else {
            "state": state, "effective_from": _ts(last.get("effective_ts")), "event_id": last.get("event_id"),
            "event_hash": last.get("event_hash")}
    return out


def state_of(dam_id, current: Optional[Mapping], has_ref_dam: Iterable = ()):
    """(state, info). No events: implicit draft_commissioning only when the dam has no ref.dam row; a ref.dam row
    without any lifecycle event (not backfilled) is unverifiable. current None -> unverifiable."""
    if current is None:
        return UNREADABLE, None
    c = current.get(dam_id)
    if c is None:
        return (UNREADABLE, None) if dam_id in set(has_ref_dam) else (DRAFT, None)
    if c == UNREADABLE:
        return UNREADABLE, None
    return c["state"], c


# ---------------------------------------------------------------- alarm policy (rule 1 / 7)
def alarm_mode(state) -> str:
    """'production' | 'test_only' (non-paging) | 'off'. Unknown / unreadable -> production (fail closed)."""
    if state == DRAFT:
        return "test_only"
    if state in ALARMS_OFF:
        return "off"
    return "production"


def alarm_mode_at(dam_id, ts, history: Optional[Iterable[Mapping]]) -> str:
    """history = ref.dam_lifecycle_state rows (dam_id, lifecycle_state, effective_from, effective_to), or None when it
    could not be read (-> production). The state in force at ts decides (half-open [from, to)); a dam with no rows ->
    production (not backfilled, or no ref record: alarms on). Before the first row (pre-go-live) -> test_only."""
    if history is None:
        return "production"
    t = _ts(ts)
    rows = [h for h in history if h.get("dam_id") == dam_id]
    if not rows or t is None:
        return "production"
    for h in rows:
        f, to = _ts(h.get("effective_from")), _ts(h.get("effective_to"))
        if f is not None and f <= t and (to is None or t < to):
            return alarm_mode(h.get("lifecycle_state"))
    first = min((_ts(h.get("effective_from")) for h in rows if _ts(h.get("effective_from"))), default=None)
    if first is not None and t < first:
        return "test_only"
    return "production"


def alarm_filter(readings_by_sensor: Mapping[str, list], dam_of: Mapping[str, str],
                 history: Optional[Iterable[Mapping]]):
    """Rule 1 for the alarm evaluator: keep only the readings whose dam was in a production-alarm state at the
    reading's ts. 'off' (voided / decommissioned / transferred) and 'test_only' (draft, pre-go-live) readings are not
    evaluated (no transition, no exceedance, nothing pages). history None (ref.dam_lifecycle_state unreadable) ->
    every reading kept (alarms on) and one lifecycle_unverifiable issue. Returns (kept, issues)."""
    issues = []
    if history is None:
        issues.append({"issue": "lifecycle_unverifiable: ref.dam_lifecycle_state unreadable; every alarm evaluated "
                                "as production"})
        return {k: list(v) for k, v in readings_by_sensor.items()}, issues
    by_dam = {}
    for h in history:
        by_dam.setdefault(h.get("dam_id"), []).append(h)
    kept = {}
    for sid, rows in readings_by_sensor.items():
        dam = dam_of.get(sid)
        n = {"off": 0, "test_only": 0}
        keep = []
        for r in rows:
            m = alarm_mode_at(dam, r.get("ts_utc"), by_dam.get(dam, []))
            if m == "production":
                keep.append(r)
            else:
                n[m] += 1
        kept[sid] = keep
        for m, c in n.items():
            if c:
                issues.append({"sensor_id": sid, "issue": f"lifecycle: {c} readings not alarmed (dam {dam} alarms {m})"})
    return kept, issues


# ---------------------------------------------------------------- citations / reasons (data from UC)
def load_citations(rows: Optional[Iterable[Mapping]]) -> Optional[dict]:
    """ref.lifecycle_citation rows -> {code: {from_states:set, to_state, suspension_reasons:set|None, citation, status,
    withdrawal_notice}}; None (unreadable) -> None."""
    if rows is None:
        return None
    out = {}
    for r in rows:
        if r.get("active") is False:
            continue
        sr = r.get("suspension_reasons")
        out[r["citation_code"]] = {
            "from_states": set(r.get("from_states") or ()), "to_state": r.get("to_state"),
            "suspension_reasons": set(sr) if sr else None, "citation": r.get("citation"),
            "status": r.get("verification_status"), "withdrawal_notice": bool(r.get("withdrawal_notice"))}
    return out


def citation_ok(code, frm, to, reason, citations: Mapping) -> Optional[str]:
    c = citations.get(code) if code else None
    if c is None:
        return f"citation_code {code!r} is not in ref.lifecycle_citation"
    if frm not in c["from_states"] or to != c["to_state"]:
        return f"citation_code {code!r} is not valid for {frm} -> {to}"
    if to == SUSP and (c["suspension_reasons"] is None or reason not in c["suspension_reasons"]):
        return f"citation_code {code!r} is not valid for suspension_reason {reason!r}"
    return None


# ---------------------------------------------------------------- DSSMP
def dssmp_in_effect(dam_id, version_id, at, dssmp: Iterable[Mapping]) -> Optional[Mapping]:
    for v in dssmp:
        if v.get("dam_id") == dam_id and v.get("dssmp_version_id") == version_id and v.get("status") == "approved":
            f, to = _ts(v.get("effective_from")), _ts(v.get("effective_to"))
            if f is not None and f <= at and (to is None or at < to):
                return v
    return None


# ---------------------------------------------------------------- authority
def roles_at(person, dam_id, at, authority: Iterable[Mapping], admins: Iterable[Mapping]) -> set:
    """{'cdse', 'platform_admin'} the person held at `at` (authority = ref.v_dam_reviewer_authority rows; admins =
    ref.project_membership ADMIN rows on '*')."""
    p, out = _norm(person), set()
    if not p or at is None:
        return out
    for a in authority:
        if a.get("dam_id") == dam_id and _norm(a.get("principal")) == p:
            g, r = _ts(a.get("granted_ts")), _ts(a.get("revoked_ts"))
            if (g is None or g <= at) and (r is None or r > at):
                out.add("cdse")
    for a in admins:
        if _norm(a.get("principal")) == p and str(a.get("dam_id", "*")) == "*" \
                and str(a.get("role", "ADMIN")).upper() == "ADMIN":
            g, r = _ts(a.get("granted_ts")), _ts(a.get("revoked_ts"))
            if (g is None or g <= at) and (r is None or r > at):
                out.add("platform_admin")
    return out


def approval_problems(req: Mapping, proposed_at, approved_at, authority, admins) -> List[str]:
    frm, to, dam = req.get("from_state"), req.get("to_state"), req.get("dam_id")
    prop, appr = _norm(req.get("proposed_by")), _norm(req.get("approved_by"))
    if to == VOID:
        out = []
        if not prop or not appr or prop == appr:
            out.append("void needs two distinct people (no self-approval)")
        rp = roles_at(prop, dam, _ts(proposed_at), authority, admins) & VOID_ROLES
        ra = roles_at(appr, dam, _ts(approved_at), authority, admins) & VOID_ROLES
        if not out and not any({a, b} == VOID_ROLES for a in rp for b in ra):
            out.append(f"void needs one CDSE and one platform_admin (proposer {sorted(rp)}, approver {sorted(ra)} "
                       "at decision time)")
        second = _norm(req.get("second_approved_by"))
        if second and second in (prop, appr):
            out.append("second_approved_by repeats the proposer or approver")
        return out
    need = APPROVER_ROLES.get((frm, to), ())
    have = roles_at(appr, dam, _ts(approved_at), authority, admins)
    if not have & set(need):
        return [f"approver {appr or '<none>'} holds none of {list(need)} for {dam} at {ts_text(approved_at)}"]
    return []


# ---------------------------------------------------------------- the check
def check_transition(req: Mapping, *, current, has_ref_dam=(), citations=None, reasons=None, dssmp=(),
                     readiness: Optional[Mapping] = None, authority=(), admins=(), proposed_at=None,
                     approved_at=None) -> dict:
    """req: one transition (outbox row fields). Returns {codes: [...], messages: ['<code>: <detail>'], warnings:
    ['<code>: <detail>'], from_state_now}. Pure; the caller supplies every UC read (None = unreadable)."""
    msgs, warns = [], []

    def bad(code, detail):
        msgs.append(f"{code}: {detail}")

    dam, frm, to = req.get("dam_id"), req.get("from_state"), req.get("to_state")
    now_state, info = state_of(dam, current, has_ref_dam)
    if now_state == UNREADABLE or citations is None or reasons is None:
        what = "the dam's lifecycle state" if now_state == UNREADABLE else "ref.lifecycle_citation / reasons"
        bad("lifecycle_unverifiable", f"{what} cannot be read or verified for {dam}")
        return _result(msgs, warns, now_state)
    if now_state in READ_ONLY:
        bad("dam_archived", f"{dam} is {now_state} (read-only)")
        return _result(msgs, warns, now_state)
    if frm != now_state:
        bad("lifecycle_stale", f"{dam} is {now_state}, not {frm}")
        return _result(msgs, warns, now_state)
    if (frm, to) not in EDGES:
        bad("lifecycle_transition_invalid", f"{frm} -> {to} is not an allowed transition")
        return _result(msgs, warns, now_state)
    eff = _ts(req.get("effective_ts"))
    if eff is None:
        bad("lifecycle_transition_invalid", f"effective_ts {req.get('effective_ts')!r} is not a timestamp")
        return _result(msgs, warns, now_state)
    if info and info.get("effective_from") and eff < info["effective_from"]:
        bad("lifecycle_stale", f"effective_ts {ts_text(eff)} is before the current state's {ts_text(info['effective_from'])}")
        return _result(msgs, warns, now_state)
    if not uri_list(req.get("evidence_uri")):
        bad("lifecycle_evidence_required", "evidence_uri is empty")
    reason = req.get("suspension_reason")
    if to == SUSP and reason not in reasons:
        bad("lifecycle_suspension_reason_required", f"suspension_reason {reason!r} is not in "
            "ref.lifecycle_suspension_reason")
    elif to != SUSP and not _blank(reason):
        bad("lifecycle_citation_invalid", f"suspension_reason {reason!r} on a transition to {to}")
    cp = citation_ok(req.get("citation_code"), frm, to, reason, citations)
    if cp and not any(m.startswith("lifecycle_suspension_reason_required") for m in msgs):
        bad("lifecycle_citation_invalid", cp)
    cit = citations.get(req.get("citation_code")) or {}
    if not cp and cit.get("withdrawal_notice"):
        wd = _date(req.get("withdrawal_filed_date"))
        if wd is None:
            bad("lifecycle_withdrawal_date_required", "the withdrawal notice's filed date (withdrawal_filed_date)")
        elif eff < _utc(wd) + WITHDRAWAL_15:
            warns.append(f"lifecycle_withdrawal_15_day: effective {ts_text(eff)} is less than 15 days after the "
                         f"withdrawal notice filed {wd.isoformat()} (18 CFR 385.216(b))")
    if to == SUSP and reason in reasons:
        if reason == "emergency_modification" and _blank(req.get("incident_report_uri")):
            bad("lifecycle_incident_report_required", "emergency_modification needs incident_report_uri")
        if reason in ("construction_modification", "emergency_modification") and not (
                req.get("sworn_verification_12_13") is True and not _blank(req.get("sworn_verification_uri"))):
            bad("lifecycle_sworn_verification_required", f"{reason} needs sworn_verification_12_13 = true and "
                "sworn_verification_uri (18 CFR 12.13)")
        if reason in ("drawdown", "monitoring_change") and (_blank(req.get("cdse_basis"))
                                                             or _blank(req.get("cdse_basis_by"))):
            bad("lifecycle_cdse_basis_required", f"{reason} needs cdse_basis and cdse_basis_by")
        if reason == "other" and _blank(req.get("rationale")):
            bad("lifecycle_rationale_required", "suspension_reason other needs a rationale")
        if reason == "re_directed" and _blank(req.get("re_directive_uri")):
            bad("lifecycle_re_directive_required", "re_directed needs re_directive_uri (the RE directive)")
        if reason == "construction_modification" and _ts(approved_at) and eff < _ts(approved_at) + NOTICE_60:
            warns.append(f"lifecycle_notice_60_day: effective {ts_text(eff)} is less than 60 days after the approval "
                         f"{ts_text(approved_at)} (18 CFR 12.11(b)(2))")
    if to == SUSP or (frm, to) == (SUSP, LIVE):
        v = dssmp_in_effect(dam, req.get("dssmp_version_id"), eff, dssmp) if req.get("dssmp_version_id") else None
        if v is None:
            bad("lifecycle_dssmp_required", f"no approved DSSMP version {req.get('dssmp_version_id')!r} in effect "
                f"for {dam} at {ts_text(eff)}")
        elif to == SUSP and info and info.get("effective_from") and _ts(v.get("effective_from")) < info["effective_from"]:
            bad("lifecycle_dssmp_required", f"DSSMP version {v.get('dssmp_version_id')} predates the current state; "
                "a suspension needs a NEW version")
    if (frm, to) == (DRAFT, LIVE) and not (readiness or {}).get(dam):
        bad("dam_go_live_not_ready", f"{dam} is not ready in ref.v_dam_go_live_readiness")
    if (frm, to) == (P_TRANSFER, TRANSFERRED):
        tgt = req.get("counterpart_dam_id")
        if _blank(tgt) or tgt == dam:
            bad("lifecycle_transfer_target_invalid", "counterpart_dam_id (the new licensee's record) is required")
        else:
            ts_, _ = state_of(tgt, current, has_ref_dam)
            if ts_ != DRAFT:
                bad("lifecycle_transfer_target_invalid", f"the new record {tgt} is {ts_}, not {DRAFT}")
            elif not (readiness or {}).get(tgt):
                bad("dam_go_live_not_ready", f"the new record {tgt} is not live-ready (ref.v_dam_go_live_readiness)")
    for p in approval_problems(req, proposed_at, approved_at, authority, admins):
        bad("lifecycle_approval_invalid", p)
    return _result(msgs, warns, now_state)


def _result(msgs, warns, now_state):
    order = {c: i for i, c in enumerate(CODES)}
    msgs = sorted(msgs, key=lambda m: order.get(m.split(":", 1)[0], len(CODES)))
    return {"codes": [m.split(":", 1)[0] for m in msgs], "messages": msgs, "warnings": warns,
            "from_state_now": now_state}


# ---------------------------------------------------------------- events
def build_events(req: Mapping, current: Mapping, created_by: str, created_ts, event_id: str) -> List[dict]:
    """The audit.lifecycle_event row(s) for an accepted transition: one, or two for pending_transfer -> transferred
    (the old record -> transferred and the new record draft -> live at the same effective_ts, one transaction)."""
    eff = _utc(req["effective_ts"]).replace(microsecond=0)
    base = {f: req.get(f) for f in HASH_FIELDS if f not in ("prev_event_hash", "event_id", "created_by")}
    base.update(effective_ts=eff, created_by=created_by, evidence_uri=uri_list(req.get("evidence_uri")),
                withdrawal_filed_date=_date(req.get("withdrawal_filed_date")),
                sworn_verification_12_13=req.get("sworn_verification_12_13"))
    out = []

    def add(ev):
        prev = (current.get(ev["dam_id"]) or {}).get("event_hash") if isinstance(current.get(ev["dam_id"]), dict) \
            else None
        ev["prev_event_hash"] = prev or GENESIS
        ev["created_ts"] = created_ts
        ev["event_hash"] = event_hash(ev)
        out.append(ev)

    add(dict(base, event_id=event_id))
    if (req.get("from_state"), req.get("to_state")) == (P_TRANSFER, TRANSFERRED):
        add(dict(base, event_id=f"{event_id}:counterpart", dam_id=req["counterpart_dam_id"], from_state=DRAFT,
                 to_state=LIVE, trigger_code="transfer_counterpart", counterpart_dam_id=req["dam_id"]))
    return out


def fold_go_live(markers: Mapping, go_live_rows: Optional[Iterable[Mapping]], ambiguous="ambiguous") -> dict:
    """v1.8: go_live_at is derived from the lifecycle (ref.v_dam_go_live: dam_id, go_live_at). Folded into the
    v1.7 go-live markers (dam_safety.cdse_assignment.go_live_markers) for is_live: the EARLIEST known go-live wins
    (fail closed: a dam is never treated as pre-live after any source says it went live); an ambiguous marker stays
    ambiguous. go_live_rows None (view unreadable / not created yet) -> markers unchanged."""
    out = dict(markers)
    for r in go_live_rows or ():
        d, t = r.get("dam_id"), _ts(r.get("go_live_at"))
        if not d or t is None or out.get(d) == ambiguous:
            continue
        cur = out.get(d)
        cur_t = cur if isinstance(cur, dt.datetime) else _ts(cur)
        if cur is None or (cur_t is not None and t < (cur_t if cur_t.tzinfo else cur_t.replace(tzinfo=dt.timezone.utc))):
            out[d] = t
    return out


def write_refusal(dam_id, current: Optional[Mapping], has_ref_dam=()) -> Optional[str]:
    """For the OTHER loaders (DSSMP, CDSE, ref publications): 'dam_archived: ...' when the dam is read-only, else None.
    An unreadable lifecycle does not block them (rule 7 refuses lifecycle writes only); the job fails if the table
    itself cannot be read."""
    st, _ = state_of(dam_id, current or {}, ())
    if st in READ_ONLY:
        return f"dam_archived: {dam_id} is {st} (read-only)"
    return None


__all__ = ["STATES", "EDGES", "CODES", "WARNINGS", "SUSPENSION_REASONS", "HASH_FIELDS", "EVENT_COLS", "GENESIS",
           "UNREADABLE", "APPROVER_ROLES", "alarm_filter", "alarm_mode", "alarm_mode_at", "build_events", "check_transition",
           "citation_ok", "current_states", "fold_go_live", "event_hash", "hash_input", "hash_sql", "load_citations", "roles_at",
           "state_of", "uri_list", "verify_chain", "write_refusal", "ts_text"]
