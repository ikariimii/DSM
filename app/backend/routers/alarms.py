"""Alarms console: list (merged with Lakebase ack state) and acknowledge workflow.

Workflow: open -> acknowledged (engineer+) -> closed (dso+, reason required).
Comments: engineer+. Every action is written to the audit log by the app service
principal with the acting user's email (shared writes via SP)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..common import gate_for, merge_ack
from ..deps import Ctx, analytics, ctx, require, store

router = APIRouter(prefix="/api/alarms")


class CloseBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class AckBody(BaseModel):
    comment: str | None = Field(default=None, max_length=4000)


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
def ack(event_id: str, body: AckBody | None = None, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    ev = _event(an, c, event_id, "engineer")
    try:
        res = st.acknowledge(event_id, ev["dam_id"], c.email)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    if body and body.comment:
        st.add_comment(event_id, ev["dam_id"], c.email, body.comment)
    st.audit(c.email, "alarm_ack", ev["dam_id"], event_id, {"level": ev.get("level")})
    return res


@router.post("/{event_id}/close")
def close(event_id: str, body: CloseBody, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    ev = _event(an, c, event_id, "dso")
    try:
        res = st.close_alarm(event_id, ev["dam_id"], c.email, body.reason)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    st.audit(c.email, "alarm_close", ev["dam_id"], event_id, {"reason": body.reason})
    return res


@router.post("/{event_id}/comment")
def comment(event_id: str, body: CommentBody, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    ev = _event(an, c, event_id, "engineer")
    st.add_comment(event_id, ev["dam_id"], c.email, body.body)
    st.audit(c.email, "alarm_comment", ev["dam_id"], event_id)
    return {"comments": st.comments(event_id)}
