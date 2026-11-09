"""Alarms console: list (merged with Lakebase ack state) and acknowledge workflow.

Workflow: open -> acknowledged (rationale required) -> closed (dso+, reason required). Acknowledging an alarm at
level >= CDSE_ACK_MIN_LEVEL (Evaluate/ACTION = HIGH, Act = CRITICAL) is the CDSE acknowledgement: only the dam's named
CDSE may do it (signoffs.is_cdse; FERC Ch.14 App K 14-K-4) - no app role (dso, dam_admin, platform admin) substitutes.
Level-1 (Verify/ALERT) alarms stay engineer+, unless the alarm rule is flagged cdse_ack_required (ref.threshold, per dam
and rule, DA-owned column): then the CDSE is needed at any level. The flag counts if the episode snapshot
(ops.alarm_episode.cdse_ack_required_at_open) OR the episode view's rule flag OR the current rule row is true, so
un-flagging a rule never weakens an open episode; an absent column is false (logged once). Ack and close
are reviewer actions: each appends one record to the dam's hash chain (audit_chain.py) atomically with the state change.
Comments: engineer+. Every action is written to the audit log by the app service
principal with the acting user's email (shared writes via SP)."""
from __future__ import annotations

import hashlib
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit_chain as ac
from .. import signoff as so
from . import signoffs as sg

from ..common import gate_for, merge_ack
from ..deps import Ctx, analytics, ctx, require, store

router = APIRouter(prefix="/api/alarms")
log = logging.getLogger(__name__)


class CloseBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class AckBody(BaseModel):
    # QA-039 v2: the acknowledgement (CDSE ack) is a chained reviewer action and needs a non-empty rationale.
    # `comment` is accepted as the older field name for the same text.
    rationale: str | None = Field(default=None, max_length=4000)
    comment: str | None = Field(default=None, max_length=4000)


# HIGH = level 2 (Evaluate / ACTION), CRITICAL = level 3 (Act); level 1 (Verify / ALERT) is not a CDSE action.
CDSE_ACK_MIN_LEVEL = 2


# cdse_ack_required sources, all OR-ed: the episode's snapshot taken when it opened, the rule flag the DA exposes on the
# ops.alarm_episode view, and the current ref.threshold row of the rule (is_current, same threshold_id).
CDSE_FLAG_EPISODE_COLS = ("cdse_ack_required_at_open", "cdse_ack_required")
CDSE_FLAG_RULE_COL = "cdse_ack_required"
_absent_logged: set[str] = set()


def _truthy(v) -> bool:
    return v is True or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 1) or \
        (isinstance(v, str) and v.strip().lower() in ("true", "t", "1"))


def _log_absent_once(what: str) -> None:
    if what not in _absent_logged:
        _absent_logged.add(what)
        log.warning("%s is absent (older schema): treating cdse_ack_required as false there "
                    "(level-based CDSE rule still applies)", what)


def rule_flag(an, ev: dict, rules: dict | None = None) -> bool:
    """True when the alarm's rule demands the CDSE acknowledgement at any level. rules: optional cache
    {(dam_id, instrument_id): an.thresholds(...)} shared across a list request."""
    flagged = False
    for col in CDSE_FLAG_EPISODE_COLS:
        if col in ev:
            flagged = flagged or _truthy(ev[col])
    if not any(col in ev for col in CDSE_FLAG_EPISODE_COLS):
        _log_absent_once("ops.alarm_episode.cdse_ack_required_at_open / cdse_ack_required")
    tid = ev.get("threshold_id")
    if tid:
        key = (ev["dam_id"], ev.get("instrument_id"))
        if rules is None or key not in rules:
            rows = an.thresholds(*key)
            if rules is not None:
                rules[key] = rows
        else:
            rows = rules[key]
        mine = [t for t in rows if t.get("threshold_id") == tid]
        if mine and not any(CDSE_FLAG_RULE_COL in t for t in mine):
            _log_absent_once("ref.threshold.cdse_ack_required")
        flagged = flagged or any(_truthy(t.get(CDSE_FLAG_RULE_COL)) for t in mine)
    return flagged


def needs_cdse_ack(ev: dict, flagged: bool | None = None) -> bool:
    """CDSE acknowledgement needed: level >= CDSE_ACK_MIN_LEVEL, or a flagged rule (any level).
    flagged=None reads ev['cdse_ack_required'] as already resolved by rule_flag()."""
    f = ev.get("cdse_ack_required") if flagged is None else flagged
    return (ev.get("level") or 0) >= CDSE_ACK_MIN_LEVEL or _truthy(f)


def _can_ack(c: Ctx, ev: dict, an, cdse: bool | None = None) -> bool:
    if ev.get("state", "open") != "open":
        return False
    if ev.get("requires_cdse", needs_cdse_ack(ev)):
        return sg.is_cdse(c, ev["dam_id"], an) if cdse is None else cdse
    return c.can(ev["dam_id"], "engineer")


ALARM_ORIGINAL_FIELDS = ("event_id", "dam_id", "instrument_id", "level", "value", "unit", "event_ts")


def _chain_base(ev: dict, prior: dict | None, action: str, decision: str, new_state: str, rationale: str,
                c: Ctx, request: Request, an) -> dict:
    """Chain record for an alarm action. original_value = the alarm as raised (measured value, unit, level) and its
    prior workflow state; corrected_value = the new workflow state. The measured value is never changed."""
    who = sg.reviewer_identity(c, request)
    original = {**{k: ev.get(k) for k in ALARM_ORIGINAL_FIELDS}, "state": (prior or {}).get("state") or "open"}
    base = {"review_id": f"RS-{uuid.uuid4().hex}", "dam_id": ev["dam_id"], "subject_type": "alarm_event",
            "subject_id": ev["event_id"], "review_level": sg.review_level(c, ev["dam_id"], an), "decision": decision,
            "comment": rationale, "reviewer_id": who["reviewer_id"], "reviewer_pe_license": None,
            "signed_ts": sg.now_utc(), "action_type": action, "reviewer_email": who["reviewer_email"],
            "reviewer_sub": who["reviewer_sub"], "original_value": ac.canon_json(original),
            "corrected_value": ac.canon_json({"state": new_state, "measured_value_changed": False})}
    base["signature_hash"] = _content_hash(base, original)
    return base


def _content_hash(base: dict, subject: dict) -> str:
    # same construction as the engine-compatible review signature: signed fields + the subject as reviewed
    payload = {"signoff": {k: so.canonical_value(base.get(k)) for k in so.SIGNED_FIELDS},
               "subject": {k: so.canonical_value(v) for k, v in subject.items()}}
    return hashlib.sha256(so._canon(payload).encode("utf-8")).hexdigest()


def _event(an, c: Ctx, event_id: str, min_role: str) -> dict:
    ev = an.alarm_event(event_id)
    if ev is None:
        raise HTTPException(404, f"Unknown alarm {event_id}")
    require(c, ev["dam_id"], min_role)
    return ev


@router.get("")
def list_alarms(dam_id: str | None = None, state: str | None = None, min_level: int = 0,
                c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    dams = c.dams([s["dam_id"] for s in an.sites()])
    if dam_id:
        require(c, dam_id)
        dams = [dam_id]
    events = an.alarm_events(dams)
    rules: dict = {}
    flags = {e["event_id"]: rule_flag(an, e, rules) for e in events}
    gates = {d: gate_for(an, d) for d in {e["dam_id"] for e in events}}
    events = [gates[e["dam_id"]].apply_event(e) for e in events]
    events = merge_ack(events, st.ack_states([e["event_id"] for e in events]))
    if state:
        events = [e for e in events if e["state"] == state]
    events = [e for e in events if (e.get("level") or 0) >= min_level]
    cdse = {d: sg.is_cdse(c, d, an) for d in {e["dam_id"] for e in events}}
    for e in events:
        e["cdse_ack_required"] = flags.get(e["event_id"], False)
        e["requires_cdse"] = needs_cdse_ack(e, e["cdse_ack_required"])
        e["is_cdse"] = cdse[e["dam_id"]]
        e["can_ack"] = _can_ack(c, e, an, cdse[e["dam_id"]])
        e["can_close"] = c.can(e["dam_id"], "dso") and e["state"] == "acknowledged"
    events.sort(key=lambda e: ({"open": 0, "acknowledged": 1, "closed": 2}[e["state"]], -(e.get("level") or 0),
                               e["event_ts"]), reverse=False)
    return {"alarms": events}


@router.get("/{event_id}")
def get_alarm(event_id: str, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    ev = _event(an, c, event_id, "viewer")
    flagged = rule_flag(an, ev)
    ev = merge_ack([gate_for(an, ev["dam_id"]).apply_event(ev)], st.ack_states([event_id]))[0]
    ev["comments"] = st.comments(event_id)
    ev["cdse_ack_required"] = flagged
    ev["requires_cdse"] = needs_cdse_ack(ev, flagged)
    ev["is_cdse"] = sg.is_cdse(c, ev["dam_id"], an)
    ev["can_ack"] = _can_ack(c, ev, an, ev["is_cdse"])
    ev["can_close"] = c.can(ev["dam_id"], "dso") and ev["state"] == "acknowledged"
    return ev


@router.post("/{event_id}/ack")
def ack(event_id: str, request: Request, body: AckBody | None = None, c: Ctx = Depends(ctx), an=Depends(analytics),
        st=Depends(store)):
    raw = an.alarm_event(event_id)
    flagged = rule_flag(an, raw) if raw else False
    if raw and needs_cdse_ack(raw, flagged):
        # CDSE acknowledgement: the dam ACL (viewer+) still applies, then only the dam's named CDSE may act.
        ev = _event(an, c, event_id, "viewer")
        if not sg.is_cdse(c, ev["dam_id"], an):
            why = ("its rule is flagged cdse_ack_required" if flagged and (ev.get("level") or 0) < CDSE_ACK_MIN_LEVEL
                   else f"level {ev.get('level')} alarm")
            raise HTTPException(403, f"CDSE acknowledgement ({why}): only the CDSE of dam {ev['dam_id']} "
                                     "(ref.dam.cdse_id) may acknowledge it; app roles (dso, dam_admin, admin) do not "
                                     "confer CDSE")
    else:
        ev = _event(an, c, event_id, "engineer")
    rationale = sg.rationale_of((body.rationale or body.comment) if body else None)
    prior = st.ack_states([event_id]).get(event_id)
    if prior and prior["state"] != "open":
        raise HTTPException(409, f"alarm {event_id} is already {prior['state']}")
    base = _chain_base(ev, prior, "alarm_ack", "acknowledge", "acknowledged", rationale, c, request, an)
    out = {}
    try:  # the state change and its chained record commit together (or not at all)
        row = sg.append(st, request, base, also=lambda q: out.update(st.acknowledge(event_id, ev["dam_id"], c.email, q)), c=c, an=an)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    st.add_comment(event_id, ev["dam_id"], c.email, rationale)
    st.audit(c.email, "alarm_ack", ev["dam_id"], event_id, {"level": ev.get("level"), "chain_seq": row["chain_seq"],
                                                             "review_id": row["review_id"]})
    return {**out, "review_id": row["review_id"], "chain_seq": row["chain_seq"], "record_hash": row["record_hash"]}


@router.post("/{event_id}/close")
def close(event_id: str, body: CloseBody, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics),
          st=Depends(store)):
    ev = _event(an, c, event_id, "dso")
    rationale = sg.rationale_of(body.reason)
    prior = st.ack_states([event_id]).get(event_id)
    if not prior or prior["state"] != "acknowledged":
        raise HTTPException(409, f"alarm {event_id} must be acknowledged before it is closed")
    base = _chain_base(ev, prior, "alarm_close", "close", "closed", rationale, c, request, an)
    out = {}
    try:
        row = sg.append(st, request, base,
                        also=lambda q: out.update(st.close_alarm(event_id, ev["dam_id"], c.email, rationale, q)), c=c, an=an)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    st.audit(c.email, "alarm_close", ev["dam_id"], event_id, {"reason": rationale, "chain_seq": row["chain_seq"],
                                                               "review_id": row["review_id"]})
    return {**out, "review_id": row["review_id"], "chain_seq": row["chain_seq"], "record_hash": row["record_hash"]}


@router.post("/{event_id}/comment")
def comment(event_id: str, body: CommentBody, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    ev = _event(an, c, event_id, "engineer")
    st.add_comment(event_id, ev["dam_id"], c.email, body.body)
    st.audit(c.email, "alarm_comment", ev["dam_id"], event_id)
    return {"comments": st.comments(event_id)}
