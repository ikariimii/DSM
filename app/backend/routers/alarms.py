"""Alarms console: list (merged with Lakebase ack state) and acknowledge workflow.

Workflow: open -> acknowledged (engineer+, rationale required) -> closed (dso+, reason required). Ack and close
are reviewer actions: each appends one record to the dam's hash chain (audit_chain.py) atomically with the state change.
Comments: engineer+. Every action is written to the audit log by the app service
principal with the acting user's email (shared writes via SP)."""
from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit_chain as ac
from .. import signoff as so
from . import signoffs as sg

from ..common import gate_for, merge_ack
from ..deps import Ctx, analytics, ctx, require, store

router = APIRouter(prefix="/api/alarms")


class CloseBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class AckBody(BaseModel):
    # QA-039 v2: the acknowledgement (CDSE ack) is a chained reviewer action and needs a non-empty rationale.
    # `comment` is accepted as the older field name for the same text.
    rationale: str | None = Field(default=None, max_length=4000)
    comment: str | None = Field(default=None, max_length=4000)


ALARM_ORIGINAL_FIELDS = ("event_id", "dam_id", "instrument_id", "level", "value", "unit", "event_ts")


def _chain_base(ev: dict, prior: dict | None, action: str, decision: str, new_state: str, rationale: str,
                c: Ctx, request: Request) -> dict:
    """Chain record for an alarm action. original_value = the alarm as raised (measured value, unit, level) and its
    prior workflow state; corrected_value = the new workflow state. The measured value is never changed."""
    who = sg.reviewer_identity(c, request)
    original = {**{k: ev.get(k) for k in ALARM_ORIGINAL_FIELDS}, "state": (prior or {}).get("state") or "open"}
    base = {"review_id": f"RS-{uuid.uuid4().hex}", "dam_id": ev["dam_id"], "subject_type": "alarm_event",
            "subject_id": ev["event_id"], "review_level": sg.review_level(c, ev["dam_id"]), "decision": decision,
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
    gates = {d: gate_for(an, d) for d in {e["dam_id"] for e in events}}
    events = [gates[e["dam_id"]].apply_event(e) for e in events]
    events = merge_ack(events, st.ack_states([e["event_id"] for e in events]))
    if state:
        events = [e for e in events if e["state"] == state]
    events = [e for e in events if (e.get("level") or 0) >= min_level]
    for e in events:
        e["can_ack"] = c.can(e["dam_id"], "engineer") and e["state"] == "open"
        e["can_close"] = c.can(e["dam_id"], "dso") and e["state"] == "acknowledged"
    events.sort(key=lambda e: ({"open": 0, "acknowledged": 1, "closed": 2}[e["state"]], -(e.get("level") or 0),
                               e["event_ts"]), reverse=False)
    return {"alarms": events}


@router.get("/{event_id}")
def get_alarm(event_id: str, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    ev = _event(an, c, event_id, "viewer")
    ev = merge_ack([gate_for(an, ev["dam_id"]).apply_event(ev)], st.ack_states([event_id]))[0]
    ev["comments"] = st.comments(event_id)
    ev["can_ack"] = c.can(ev["dam_id"], "engineer") and ev["state"] == "open"
    ev["can_close"] = c.can(ev["dam_id"], "dso") and ev["state"] == "acknowledged"
    return ev


@router.post("/{event_id}/ack")
def ack(event_id: str, request: Request, body: AckBody | None = None, c: Ctx = Depends(ctx), an=Depends(analytics),
        st=Depends(store)):
    ev = _event(an, c, event_id, "engineer")
    rationale = sg.rationale_of((body.rationale or body.comment) if body else None)
    prior = st.ack_states([event_id]).get(event_id)
    if prior and prior["state"] != "open":
        raise HTTPException(409, f"alarm {event_id} is already {prior['state']}")
    base = _chain_base(ev, prior, "alarm_ack", "acknowledge", "acknowledged", rationale, c, request)
    out = {}
    try:  # the state change and its chained record commit together (or not at all)
        row = sg.append(st, request, base, also=lambda q: out.update(st.acknowledge(event_id, ev["dam_id"], c.email, q)))
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
    base = _chain_base(ev, prior, "alarm_close", "close", "closed", rationale, c, request)
    out = {}
    try:
        row = sg.append(st, request, base,
                        also=lambda q: out.update(st.close_alarm(event_id, ev["dam_id"], c.email, rationale, q)))
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
