"""Live-dam edits of an alarm rule's cdse_ack_required flag (Genie decision, v1.6).

GET  /api/dams/{dam_id}/alarm-rules                                   viewer+: current rules, flags, proposals
POST /api/dams/{dam_id}/alarm-rules/{threshold_id}/cdse-flag/proposals  dam_admin / platform admin / the dam's CDSE
POST /api/dams/{dam_id}/alarm-rules/{threshold_id}/cdse-flag            the dam's CDSE only: set or clear directly
POST /api/dams/{dam_id}/alarm-rules/proposals/{proposal_id}/approve     the dam's CDSE only (may approve their own)
POST /api/dams/{dam_id}/alarm-rules/proposals/{proposal_id}/reject      the dam's CDSE only

Only the dam's CDSE (signoffs.is_cdse: ref.v_dam_reviewer_authority in live mode, fail closed) may APPLY a set or
clear. dam_admin and platform admin may only PROPOSE; a proposal stays pending until that dam's CDSE approves or
rejects it, so an admin can never approve their own (or anyone's) proposal. engineer, dso, viewer: 403.
Every proposal, apply and rejection needs a non-empty rationale and appends one record to the dam's hash chain
(alarm_rule_flag_propose / _apply / _reject, subject_type 'alarm_rule', original and new values) atomically with the
workflow change. An apply never edits a ref.threshold row: it queues (ref_change_publication) a NEW version
N+1 = the current row with the new flag, effective_from = apply time, set_by = the CDSE; the DA publish job appends it
(demo: applied at once). Approval is refused (409) when the rule changed since the proposal (stale): another version,
another flag, or another applied change still waiting to be published. Open episodes keep their protection: the ack
rule ORs the episode snapshot cdse_ack_required_at_open with the current flag (routers/alarms.rule_flag)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit_chain as ac
from ..config import approval_policy
from ..deps import Ctx, analytics, ctx, require, store
from ..onboarding import PUBLISH_REQUIRED  # ref.threshold NOT NULL columns carried from version N into N+1
from . import alarms as alarms_router
from . import signoffs as sg

router = APIRouter(prefix="/api/dams/{dam_id}/alarm-rules")
# ref.threshold columns derived by contract.threshold() for display only; never published
DERIVED = ("rule", "evaluated", "rule_label", "upper_limit", "lower_limit", "abs_limit", "rate_limit_per_day",
           "pfm_ids", "response_action")
PUB_KIND = "ref.threshold.new_version"


class FlagBody(BaseModel):
    cdse_ack_required: bool
    rationale: str | None = Field(default=None, max_length=4000)


class DecisionBody(BaseModel):
    rationale: str | None = Field(default=None, max_length=4000)


def _rule(an, dam_id: str, threshold_id: str) -> dict:
    rule = next((t for t in an.thresholds(dam_id) if t["threshold_id"] == threshold_id), None)
    if rule is None:
        raise HTTPException(404, f"Unknown alarm rule {threshold_id} on dam {dam_id}")
    return rule


def _flag(rule: dict) -> bool:
    return alarms_router._truthy(rule.get("cdse_ack_required"))


def _version(rule: dict) -> int:
    return int(rule.get("threshold_version") or 1)


def _may_propose(c: Ctx, dam_id: str, cdse: bool) -> bool:
    return cdse or c.is_admin or c.role(dam_id) == "dam_admin"


def _record(c: Ctx, request: Request, an, dam_id: str, threshold_id: str, action: str, decision: str, rationale: str,
            original: dict, new: dict) -> dict:
    who = sg.reviewer_identity(c, request)
    base = {"review_id": f"RS-{uuid.uuid4().hex}", "dam_id": dam_id, "subject_type": "alarm_rule",
            "subject_id": threshold_id, "review_level": sg.review_level(c, dam_id, an), "decision": decision,
            "comment": rationale, "reviewer_id": who["reviewer_id"], "reviewer_pe_license": None,
            "signed_ts": sg.now_utc(), "action_type": action, "reviewer_email": who["reviewer_email"],
            "reviewer_sub": who["reviewer_sub"], "original_value": ac.canon_json(original),
            "corrected_value": ac.canon_json(new)}
    base["signature_hash"] = alarms_router._content_hash(base, original)
    return base


def _original(rule: dict) -> dict:
    return {"threshold_id": rule["threshold_id"], "threshold_version": _version(rule),
            "cdse_ack_required": _flag(rule)}


def _stale(st, rule: dict, base_version: int, base_flag: bool, proposal_id: str | None) -> str | None:
    if _version(rule) != base_version or _flag(rule) != base_flag:
        return (f"rule changed since the proposal (now v{_version(rule)} cdse_ack_required={_flag(rule)}, "
                f"proposed against v{base_version} cdse_ack_required={base_flag})")
    waiting = [p for p in st.flag_proposals(rule["dam_id"], "applied", rule["threshold_id"])
               if p["proposal_id"] != proposal_id and int(p.get("new_threshold_version") or 0) > base_version]
    if waiting:
        return f"another applied change (v{waiting[-1]['new_threshold_version']}) is waiting to be published"
    return None


def _apply(c: Ctx, request: Request, an, st, rule: dict, new_flag: bool, rationale: str, proposal: dict | None) -> dict:
    """Append the apply record + queue version N+1 + close the proposal in ONE chain transaction."""
    dam_id, tid = rule["dam_id"], rule["threshold_id"]
    missing = [k for k in PUBLISH_REQUIRED if rule.get(k) is None or str(rule.get(k)).strip() == ""]
    if missing:  # copied from version N, never defaulted: refuse before anything is recorded or queued
        raise HTTPException(409, f"{tid} v{_version(rule)} lacks the NOT NULL ref.threshold column(s) "
                                 f"{', '.join(missing)}; version {_version(rule) + 1} cannot be published without them "
                                 "(no defaults are invented) - the DA must backfill the current version first")
    now = sg.now_utc()
    new_version = _version(rule) + 1
    insert = {k: v for k, v in rule.items() if k not in DERIVED}
    insert.update(threshold_version=new_version, cdse_ack_required=new_flag, effective_from=now, effective_to=None,
                  is_current=True, set_by=c.email)
    payload = {"kind": PUB_KIND, "threshold_id": tid, "dam_id": dam_id, "supersedes_version": _version(rule),
               "insert": insert}
    demo = request.app.state.settings.demo_mode
    pub_id = str(uuid.uuid4())
    pid = proposal["proposal_id"] if proposal else f"RFP-{uuid.uuid4().hex}"
    who = sg.reviewer_identity(c, request)
    base = _record(c, request, an, dam_id, tid, "alarm_rule_flag_apply", "apply", rationale, _original(rule),
                   {"cdse_ack_required": new_flag, "threshold_version": new_version, "proposal_id": pid,
                    "publication_id": pub_id, "proposed_by": proposal["proposed_by"] if proposal else c.email})

    def also(q):
        if proposal is None:   # the CDSE's own direct change: recorded as an applied proposal of their own
            st.insert_flag_proposal(q, {
                "proposal_id": pid, "dam_id": dam_id, "threshold_id": tid, "base_threshold_version": _version(rule),
                "base_flag": _flag(rule), "new_flag": new_flag, "proposed_by": c.email,
                "proposed_by_id": who["reviewer_id"], "proposed_at": now, "rationale": rationale,
                "status": "pending"})
        st.decide_flag_proposal(q, pid, "applied", c.email, who["reviewer_id"], now, rationale, pub_id, new_version)
        st.insert_ref_publication(q, pub_id, PUB_KIND, dam_id, pid, payload, c.email,
                                  "applied" if demo else "pending")

    try:
        rec = sg.append(st, request, base, also, c=c, an=an)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    if demo:
        an.apply_threshold_version(payload)
    st.audit(c.email, "alarm_rule_flag_apply", dam_id, tid,
             {"proposal_id": pid, "from": _flag(rule), "to": new_flag, "new_threshold_version": new_version,
              "publication_id": pub_id})
    return {"proposal_id": pid, "status": "applied", "new_threshold_version": new_version, "publication_id": pub_id,
            "publication_status": "applied" if demo else "pending", "chain_seq": rec["chain_seq"],
            "cdse_ack_required": new_flag}


# ---------------------------------------------------------------------------------------------------------------
# DECISION POINT - PROVISIONAL approval rule (Genie, v1.6; the user is deciding whether to require a second
# approver). Setting CDSE_FLAG_REQUIRE_SECOND_APPROVER (config.Settings.cdse_flag_require_second_approver):
#   false (default, current behaviour): the dam's CDSE may set/clear directly and may approve their own proposal.
#   true: no direct set/clear (403 second_approver_required); every change is a proposal; the approver must be the
#         dam's CDSE and NOT the proposer (403 self_approval_forbidden). dam_admin / admin still only propose.
#         With one CDSE per dam, a CDSE's own proposal can then only be rejected (= withdrawn) by that CDSE; a
#         CDSE-initiated change needs another principal: a second CDSE grant for the dam, or a dam_admin / admin
#         proposing it for the CDSE to approve.
# ---------------------------------------------------------------------------------------------------------------
def _second_approver(request: Request) -> bool:
    return bool(request.app.state.settings.cdse_flag_require_second_approver)


def _same_person(c: Ctx, who: dict, p: dict) -> bool:
    """Approver == proposer: same email (case-insensitive) or same stable reviewer id."""
    if (p.get("proposed_by") or "").strip().lower() == (c.email or "").strip().lower():
        return True
    pid, rid = p.get("proposed_by_id"), who.get("reviewer_id")
    return bool(pid) and bool(rid) and str(pid) == str(rid)


def _policy_403(code: str, message: str) -> HTTPException:
    return HTTPException(403, {"code": code, "message": message, "provisional": True,
                               "setting": "CDSE_FLAG_REQUIRE_SECOND_APPROVER"})


def _guard_cdse(c: Ctx, dam_id: str, an, what: str) -> None:
    require(c, dam_id, "viewer")
    if not sg.is_cdse(c, dam_id, an):
        raise HTTPException(403, f"Only the CDSE of dam {dam_id} (ref.v_dam_reviewer_authority) may {what}; "
                                 "dam_admin and admin may only propose")


@router.get("")
def list_rules(dam_id: str, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    require(c, dam_id, "viewer")
    cdse = sg.is_cdse(c, dam_id, an)
    rules = sorted(an.thresholds(dam_id), key=lambda t: (t["instrument_id"], t.get("level") or 0, t["threshold_id"]))
    props = st.flag_proposals(dam_id)
    pending = [p for p in props if p["status"] == "pending"]
    second = _second_approver(request)
    who = sg.reviewer_identity(c, request)
    for p in pending:
        p["can_decide"] = cdse                       # may reject (a CDSE's own proposal: withdraw by rejecting)
        p["can_reject"] = cdse
        p["can_approve"] = cdse and not (second and _same_person(c, who, p))
        p["own"] = _same_person(c, who, p)
    return {"dam_id": dam_id, "is_cdse": cdse, "can_propose": _may_propose(c, dam_id, cdse),
            "can_apply": cdse and not second, "approval_policy": approval_policy(request.app.state.settings),
            "rules": [{"threshold_id": t["threshold_id"], "threshold_version": _version(t),
                       "instrument_id": t["instrument_id"], "level": t.get("level"), "rule_label": t.get("rule_label"),
                       "value": t.get("value"), "unit": t.get("unit"), "cdse_ack_required": _flag(t),
                       "pending": [p["proposal_id"] for p in pending if p["threshold_id"] == t["threshold_id"]]}
                      for t in rules],
            "pending": pending, "history": [p for p in props if p["status"] != "pending"][-50:]}


@router.post("/{threshold_id}/cdse-flag/proposals")
def propose(dam_id: str, threshold_id: str, body: FlagBody, request: Request, c: Ctx = Depends(ctx),
            an=Depends(analytics), st=Depends(store)):
    require(c, dam_id, "viewer")
    if not _may_propose(c, dam_id, sg.is_cdse(c, dam_id, an)):
        raise HTTPException(403, f"Proposing a cdse_ack_required change on {dam_id} needs dam_admin, admin or the "
                                 "dam's CDSE")
    rationale = sg.rationale_of(body.rationale)
    rule = _rule(an, dam_id, threshold_id)
    if _flag(rule) == body.cdse_ack_required:
        raise HTTPException(409, f"{threshold_id} already has cdse_ack_required={body.cdse_ack_required}")
    who = sg.reviewer_identity(c, request)
    pid = f"RFP-{uuid.uuid4().hex}"
    now = sg.now_utc()
    base = _record(c, request, an, dam_id, threshold_id, "alarm_rule_flag_propose", "propose", rationale,
                   _original(rule), {"cdse_ack_required": body.cdse_ack_required, "proposal_id": pid,
                                     "state": "pending"})
    row = {"proposal_id": pid, "dam_id": dam_id, "threshold_id": threshold_id,
           "base_threshold_version": _version(rule), "base_flag": _flag(rule), "new_flag": body.cdse_ack_required,
           "proposed_by": c.email, "proposed_by_id": who["reviewer_id"], "proposed_at": now, "rationale": rationale,
           "status": "pending"}
    rec = sg.append(st, request, base, lambda q: st.insert_flag_proposal(q, row), c=c, an=an)
    st.audit(c.email, "alarm_rule_flag_propose", dam_id, threshold_id,
             {"proposal_id": pid, "from": _flag(rule), "to": body.cdse_ack_required})
    return {**st.flag_proposal(pid), "chain_seq": rec["chain_seq"]}


@router.post("/{threshold_id}/cdse-flag")
def apply_direct(dam_id: str, threshold_id: str, body: FlagBody, request: Request, c: Ctx = Depends(ctx),
                 an=Depends(analytics), st=Depends(store)):
    _guard_cdse(c, dam_id, an, "set or clear cdse_ack_required")
    if _second_approver(request):  # PROVISIONAL rule, see DECISION POINT above
        raise _policy_403("second_approver_required",
                          f"A second approver is required (CDSE_FLAG_REQUIRE_SECOND_APPROVER): the CDSE of {dam_id} "
                          "cannot set or clear cdse_ack_required directly; submit a proposal instead")
    rationale = sg.rationale_of(body.rationale)
    rule = _rule(an, dam_id, threshold_id)
    if _flag(rule) == body.cdse_ack_required:
        raise HTTPException(409, f"{threshold_id} already has cdse_ack_required={body.cdse_ack_required}")
    why = _stale(st, rule, _version(rule), _flag(rule), None)
    if why:
        raise HTTPException(409, why)
    return _apply(c, request, an, st, rule, body.cdse_ack_required, rationale, None)


def _pending(st, dam_id: str, proposal_id: str) -> dict:
    p = st.flag_proposal(proposal_id)
    if p is None or p["dam_id"] != dam_id:
        raise HTTPException(404, f"Unknown proposal {proposal_id} on dam {dam_id}")
    if p["status"] != "pending":
        raise HTTPException(409, f"proposal {proposal_id} is already {p['status']}")
    return p


@router.post("/proposals/{proposal_id}/approve")
def approve(dam_id: str, proposal_id: str, body: DecisionBody, request: Request, c: Ctx = Depends(ctx),
            an=Depends(analytics), st=Depends(store)):
    _guard_cdse(c, dam_id, an, "approve a cdse_ack_required change")
    rationale = sg.rationale_of(body.rationale)
    p = _pending(st, dam_id, proposal_id)
    if _second_approver(request) and _same_person(c, sg.reviewer_identity(c, request), p):  # PROVISIONAL rule
        raise _policy_403("self_approval_forbidden",
                          f"A second approver is required (CDSE_FLAG_REQUIRE_SECOND_APPROVER): {c.email} proposed "
                          f"{proposal_id} and cannot approve it; it can only be rejected (withdrawn)")
    rule = _rule(an, dam_id, p["threshold_id"])
    why = _stale(st, rule, int(p["base_threshold_version"]), bool(p["base_flag"]), proposal_id)
    if why:
        raise HTTPException(409, f"stale proposal {proposal_id}: {why}; reject it and propose again")
    return _apply(c, request, an, st, rule, bool(p["new_flag"]), rationale, p)


@router.post("/proposals/{proposal_id}/reject")
def reject(dam_id: str, proposal_id: str, body: DecisionBody, request: Request, c: Ctx = Depends(ctx),
           an=Depends(analytics), st=Depends(store)):
    _guard_cdse(c, dam_id, an, "reject a cdse_ack_required change")
    rationale = sg.rationale_of(body.rationale)
    p = _pending(st, dam_id, proposal_id)
    rule = _rule(an, dam_id, p["threshold_id"])
    who = sg.reviewer_identity(c, request)
    now = sg.now_utc()
    base = _record(c, request, an, dam_id, p["threshold_id"], "alarm_rule_flag_reject", "reject", rationale,
                   _original(rule), {"cdse_ack_required": bool(p["new_flag"]), "proposal_id": proposal_id,
                                     "state": "rejected", "proposed_by": p["proposed_by"]})
    try:
        rec = sg.append(st, request, base, lambda q: st.decide_flag_proposal(
            q, proposal_id, "rejected", c.email, who["reviewer_id"], now, rationale), c=c, an=an)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    st.audit(c.email, "alarm_rule_flag_reject", dam_id, p["threshold_id"], {"proposal_id": proposal_id})
    return {**st.flag_proposal(proposal_id), "chain_seq": rec["chain_seq"]}
