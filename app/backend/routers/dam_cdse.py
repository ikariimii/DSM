"""v1.7 first-CDSE bootstrap and CDSE changes (Genie; SCHEMA_CONTRACT §7 item 20).

GET  /api/dams/{dam_id}/cdse                                  platform admin, the dam's CDSE, viewer+ of the dam
POST /api/dams/{dam_id}/cdse/assign                           platform admin ONLY: the dam's FIRST CDSE, only while
                                                              the dam is not live and has no CDSE (signed action)
POST /api/dams/{dam_id}/cdse/changes                          platform admin ONLY: propose a cdse_id change
POST /api/dams/{dam_id}/cdse/changes/{change_id}/approve      the dam's CURRENT CDSE only
POST /api/dams/{dam_id}/cdse/changes/{change_id}/reject       the dam's CURRENT CDSE only
GET  /api/cdse-changes/pending                                the caller's queue as current CDSE

Every action needs a reason_code from CDSE_CHANGE_REASONS (assign / propose) and a non-empty rationale, and appends ONE
canon-2 chain record (subject_type 'dam', subject_id = dam_id): dam_cdse_assign, dam_cdse_change_propose / _approve /
_reject. An assignment and an approved change each insert ONE 'pending' row into dam_cdse_outbox in the same
transaction (approval_record_hash = that record's hash). The app NEVER writes ref.dam: the DA load job sets
ref.dam.cdse_id from the outbox; until then the view has no (new) CDSE and go-live stays blocked
(cdse_authority_missing). Lookups of the current CDSE (ref.v_dam_reviewer_authority) fail closed."""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import audit_chain as ac
from ..config import approval_policy
from ..deps import Ctx, analytics, ctx, store
from . import alarms as alarms_router
from . import signoffs as sg

router = APIRouter()
BASE = "/api/dams/{dam_id}/cdse"
# reason_code enum (documented in SCHEMA_CONTRACT §7 item 20; DA CHECK on the outbox / extract)
CDSE_CHANGE_REASONS = {
    "initial_appointment": "first CDSE named for the dam",
    "resignation": "the CDSE resigned",
    "retirement": "the CDSE retired",
    "reassignment": "the CDSE moved to other duties",
    "incapacity": "the CDSE is unable to act",
    "regulatory_direction": "directed by the regulator (e.g. FERC / state dam safety)",
    "correction": "the recorded cdse_id was wrong",
}
PRINCIPAL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OUTBOX = "dam_cdse_outbox"


class AssignBody(BaseModel):
    cdse_id: str | None = None
    reason_code: str | None = None
    rationale: str | None = None


class DecisionBody(BaseModel):
    rationale: str | None = None


def _reason(code: str | None) -> str:
    c = (code or "").strip()
    if c not in CDSE_CHANGE_REASONS:
        raise HTTPException(422, {"code": "reason_code_invalid",
                                  "message": f"reason_code is required, one of {', '.join(CDSE_CHANGE_REASONS)}"})
    return c


def _principal(v: str | None) -> str:
    p = (v or "").strip().lower()
    if not PRINCIPAL_RE.match(p):
        raise HTTPException(422, "cdse_id must be the CDSE's principal (e-mail)")
    return p


def _admin(c: Ctx, what: str) -> None:
    if not c.is_admin:
        raise HTTPException(403, f"Only a platform admin may {what}")


def _current(an, dam_id: str) -> list[str]:
    """The dam's CDSE principals from ref.v_dam_reviewer_authority; 409 cdse_unverifiable if unreadable."""
    fn = getattr(an, "cdse_principals", None)
    if fn is None:
        raise HTTPException(409, {"code": "cdse_unverifiable", "message": "no CDSE lookup available"})
    try:
        return sorted({p.strip().lower() for p in (fn(dam_id) or []) if p and p.strip()})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, {"code": "cdse_unverifiable",
                                  "message": f"ref.v_dam_reviewer_authority unreadable ({type(e).__name__})"}) from e


def _is_live(an, dam_id: str) -> bool:
    try:
        return dam_id in {s["dam_id"] for s in an.sites()}
    except Exception as e:  # noqa: BLE001 - cannot tell: refuse the bootstrap
        raise HTTPException(409, {"code": "dam_state_unverifiable",
                                  "message": f"ref.dam unreadable ({type(e).__name__})"}) from e


def _record(c: Ctx, request: Request, an, dam_id: str, action: str, decision: str, rationale: str, original: dict,
            new: dict) -> dict:
    who = sg.reviewer_identity(c, request)
    base = {"review_id": f"RS-{uuid.uuid4().hex}", "dam_id": dam_id, "subject_type": "dam", "subject_id": dam_id,
            "review_level": sg.review_level(c, dam_id, an), "decision": decision, "comment": rationale,
            "reviewer_id": who["reviewer_id"], "reviewer_pe_license": None, "signed_ts": sg.now_utc(),
            "action_type": action, "reviewer_email": who["reviewer_email"], "reviewer_sub": who["reviewer_sub"],
            "original_value": ac.canon_json(original), "corrected_value": ac.canon_json(new)}
    base["signature_hash"] = alarms_router._content_hash(base, original)
    return base


def _snap(r: dict) -> dict:
    return {k: r.get(k) for k in ("dam_id", "kind", "previous_cdse_id", "new_cdse_id", "change_reason_code",
                                  "proposed_by")} | {"change_id": r["change_id"], "proposal_id": r["change_id"]}


def _outbox_insert(st, q, r: dict, outbox_id: str, approver: str, rec: dict) -> None:
    st.insert_row(q, OUTBOX, {
        "outbox_id": outbox_id, "dam_id": r["dam_id"], "kind": r["kind"], "previous_cdse_id": r["previous_cdse_id"],
        "new_cdse_id": r["new_cdse_id"], "change_reason_code": r["change_reason_code"],
        "proposed_by": r["proposed_by"], "approved_by": approver, "approved_at": rec["signed_ts"],
        "approval_record_hash": rec["record_hash"], "status": "pending", "reason_code": None, "message": None,
        "processed_at": None})


def _demo_load(request: Request, an, st, outbox_id: str) -> None:
    if request.app.state.settings.demo_mode and hasattr(an, "load_cdse_outbox"):
        row = st.rows(OUTBOX, "outbox_id", outbox_id=outbox_id)[0]
        status, code, msg = an.load_cdse_outbox(row)
        if status != "pending":
            st.demo_publish(OUTBOX, outbox_id, status, code, msg)


def _view(st, rows: list[dict]) -> list[dict]:
    ob = {o["outbox_id"]: o for d in {r["dam_id"] for r in rows} for o in st.rows(OUTBOX, "approved_at", dam_id=d)}
    return [{**r, "outbox": ob.get(r.get("outbox_id"))} for r in rows]


def _new(c: Ctx, request: Request, dam_id: str, kind: str, previous: str | None, new: str, code: str,
         rationale: str) -> dict:
    who = sg.reviewer_identity(c, request)
    return {"change_id": f"DCC-{uuid.uuid4().hex}", "dam_id": dam_id, "kind": kind, "previous_cdse_id": previous,
            "new_cdse_id": new, "change_reason_code": code, "rationale": rationale, "proposed_by": c.email,
            "proposed_by_id": who["reviewer_id"], "proposed_at": sg.now_utc(), "status": "pending",
            "decided_by": None, "decided_by_id": None, "decided_at": None, "decision_rationale": None,
            "outbox_id": None, "approval_record_hash": None}


def _open_assignment(st, dam_id: str) -> dict | None:
    """An assignment already recorded and not refused by the load job (pending load or loaded)."""
    for r in _view(st, st.rows("dam_cdse_change", "proposed_at", dam_id=dam_id, kind="assign")):
        if r["status"] == "approved" and not (r["outbox"] and r["outbox"]["status"].startswith("rejected")):
            return r
    return None


# ------------------------------------------------------------------ read
@router.get(BASE)
def state(dam_id: str, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    is_cdse = sg.is_cdse(c, dam_id, an)
    if not (c.is_admin or is_cdse or c.can(dam_id, "viewer")):
        raise HTTPException(403, f"No access to dam {dam_id}")
    try:
        current, err = _current(an, dam_id), None
    except HTTPException as e:
        current, err = [], e.detail
    live = None
    try:
        live = _is_live(an, dam_id)
    except HTTPException:
        pass
    changes = _view(st, st.rows("dam_cdse_change", "proposed_at DESC", dam_id=dam_id))
    pending_assign = _open_assignment(st, dam_id)
    return {"dam_id": dam_id, "current_cdse": current, "lookup_error": err, "live": live, "changes": changes,
            "reason_codes": CDSE_CHANGE_REASONS,
            "can_assign": c.is_admin and live is False and not current and err is None and pending_assign is None,
            "can_propose_change": c.is_admin and bool(current), "can_decide": is_cdse,
            "approval_policy": approval_policy(request.app.state.settings)}


@router.get("/api/cdse-changes/pending")
def queue(c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    rows = st.rows("dam_cdse_change", "proposed_at", status="pending")
    mine = {d for d in {r["dam_id"] for r in rows} if sg.is_cdse(c, d, an)}
    return {"changes": _view(st, [r for r in rows if r["dam_id"] in mine])}


# ------------------------------------------------------------------ first CDSE (bootstrap)
@router.post(BASE + "/assign")
def assign(dam_id: str, body: AssignBody, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics),
           st=Depends(store)):
    _admin(c, "set a dam's first CDSE")
    code = _reason(body.reason_code)
    rationale = sg.rationale_of(body.rationale)
    new = _principal(body.cdse_id)
    if _is_live(an, dam_id):
        raise HTTPException(409, {"code": "dam_live", "message": f"{dam_id} is live: a CDSE change is a proposal the "
                                                                 "current CDSE approves (POST .../cdse/changes)"})
    cur = _current(an, dam_id)
    if cur:
        raise HTTPException(409, {"code": "cdse_exists", "message": f"{dam_id} already has a CDSE ({', '.join(cur)})"})
    if _open_assignment(st, dam_id):
        raise HTTPException(409, {"code": "cdse_assign_pending",
                                  "message": f"{dam_id} already has a first-CDSE assignment waiting for the DA load"})
    row = _new(c, request, dam_id, "assign", None, new, code, rationale)
    outbox_id = f"DCO-{uuid.uuid4().hex}"
    base = _record(c, request, an, dam_id, "dam_cdse_assign", "assign", rationale, {"cdse_id": None},
                   _snap(row) | {"state": "approved", "outbox_id": outbox_id})
    who = sg.reviewer_identity(c, request)

    def also_row(q, rec):
        st.insert_row(q, "dam_cdse_change", row | {
            "status": "approved", "decided_by": c.email, "decided_by_id": who["reviewer_id"],
            "decided_at": rec["signed_ts"], "decision_rationale": rationale, "outbox_id": outbox_id,
            "approval_record_hash": rec["record_hash"]})
        _outbox_insert(st, q, row, outbox_id, c.email, rec)

    rec = sg.append(st, request, base, c=c, an=an, also_row=also_row)
    st.audit(c.email, "dam_cdse_assign", dam_id, new, {"outbox_id": outbox_id, "reason_code": code})
    _demo_load(request, an, st, outbox_id)
    return {**_view(st, st.rows("dam_cdse_change", "proposed_at", change_id=row["change_id"]))[0],
            "chain_seq": rec["chain_seq"]}


# ------------------------------------------------------------------ changes (admin proposes, CURRENT CDSE decides)
@router.post(BASE + "/changes")
def propose_change(dam_id: str, body: AssignBody, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics),
                   st=Depends(store)):
    _admin(c, "propose a CDSE change")
    code = _reason(body.reason_code)
    rationale = sg.rationale_of(body.rationale)
    new = _principal(body.cdse_id)
    cur = _current(an, dam_id)
    if not cur:
        raise HTTPException(409, {"code": "cdse_authority_missing",
                                  "message": f"{dam_id} has no CDSE in ref.v_dam_reviewer_authority yet: a not-live "
                                             "dam's first CDSE is assigned (POST .../cdse/assign)"})
    if new in cur:
        raise HTTPException(409, {"code": "cdse_unchanged", "message": f"{new} already is the CDSE of {dam_id}"})
    if st.rows("dam_cdse_change", "proposed_at", dam_id=dam_id, status="pending"):
        raise HTTPException(409, {"code": "cdse_change_pending", "message": f"{dam_id} already has a pending change"})
    row = _new(c, request, dam_id, "change", ",".join(cur), new, code, rationale)
    base = _record(c, request, an, dam_id, "dam_cdse_change_propose", "propose", rationale,
                   {"cdse_id": ",".join(cur)}, _snap(row) | {"state": "pending"})
    rec = sg.append(st, request, base, lambda q: st.insert_row(q, "dam_cdse_change", row), c=c, an=an)
    st.audit(c.email, "dam_cdse_change_propose", dam_id, new, {"change_id": row["change_id"], "reason_code": code})
    return {**_view(st, [row])[0], "chain_seq": rec["chain_seq"]}


def _pending(st, dam_id: str, change_id: str) -> dict:
    rows = st.rows("dam_cdse_change", "proposed_at", change_id=change_id)
    if not rows or rows[0]["dam_id"] != dam_id or rows[0]["kind"] != "change":
        raise HTTPException(404, f"Unknown CDSE change {change_id}")
    if rows[0]["status"] != "pending":
        raise HTTPException(409, f"CDSE change {change_id} is {rows[0]['status']}")
    return rows[0]


def _decide(dam_id: str, change_id: str, body: DecisionBody, request: Request, c: Ctx, an, st, verb: str) -> dict:
    if not sg.is_cdse(c, dam_id, an):
        raise HTTPException(403, f"Only the CURRENT CDSE of {dam_id} (ref.v_dam_reviewer_authority) may {verb} a "
                                 "CDSE change")
    rationale = sg.rationale_of(body.rationale)
    r = _pending(st, dam_id, change_id)
    who = sg.reviewer_identity(c, request)
    if verb == "approve":
        if request.app.state.settings.cdse_flag_require_second_approver and (
                (r["proposed_by"] or "").lower() == (c.email or "").lower()):
            raise HTTPException(403, {"code": "self_approval_forbidden", "provisional": True,
                                      "setting": "CDSE_FLAG_REQUIRE_SECOND_APPROVER",
                                      "message": f"{c.email} proposed {change_id} and cannot approve it"})
        if ",".join(_current(an, dam_id)) != r["previous_cdse_id"]:
            raise HTTPException(409, {"code": "cdse_stale", "message": "the dam's CDSE changed since the proposal; "
                                                                       "reject it and propose again"})
    outbox_id = f"DCO-{uuid.uuid4().hex}" if verb == "approve" else None
    base = _record(c, request, an, dam_id, f"dam_cdse_change_{verb}", verb, rationale,
                   {"cdse_id": r["previous_cdse_id"]},
                   _snap(r) | {"state": "approved" if verb == "approve" else "rejected", "outbox_id": outbox_id})

    def also_row(q, rec):
        st.decide_pending(q, "dam_cdse_change", "change_id", change_id, {
            "status": "approved" if verb == "approve" else "rejected", "decided_by": c.email,
            "decided_by_id": who["reviewer_id"], "decided_at": rec["signed_ts"], "decision_rationale": rationale,
            "outbox_id": outbox_id, "approval_record_hash": rec["record_hash"] if outbox_id else None})
        if outbox_id:
            _outbox_insert(st, q, r, outbox_id, c.email, rec)

    try:
        rec = sg.append(st, request, base, c=c, an=an, also_row=also_row)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    st.audit(c.email, f"dam_cdse_change_{verb}", dam_id, r["new_cdse_id"], {"change_id": change_id})
    if outbox_id:
        _demo_load(request, an, st, outbox_id)
    return {**_view(st, st.rows("dam_cdse_change", "proposed_at", change_id=change_id))[0],
            "chain_seq": rec["chain_seq"]}


@router.post(BASE + "/changes/{change_id}/approve")
def approve_change(dam_id: str, change_id: str, body: DecisionBody, request: Request, c: Ctx = Depends(ctx),
                   an=Depends(analytics), st=Depends(store)):
    return _decide(dam_id, change_id, body, request, c, an, st, "approve")


@router.post(BASE + "/changes/{change_id}/reject")
def reject_change(dam_id: str, change_id: str, body: DecisionBody, request: Request, c: Ctx = Depends(ctx),
                  an=Depends(analytics), st=Depends(store)):
    return _decide(dam_id, change_id, body, request, c, an, st, "reject")
