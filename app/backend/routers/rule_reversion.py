"""v1.7 bulk re-version of the rules set under a superseded DSSMP version (Genie; SCHEMA_CONTRACT §7 item 21).

GET  /api/dams/{dam_id}/rule-reversions?to=<dssmp_version_id>   viewer / CDSE: affected rules + proposals
POST /api/dams/{dam_id}/rule-reversions                          dam_admin / platform admin / the dam's CDSE: ONE
                                                                 proposal covering EVERY affected rule
POST /api/dams/{dam_id}/rule-reversions/{proposal_id}/approve    the dam's CDSE only
POST /api/dams/{dam_id}/rule-reversions/{proposal_id}/reject     the dam's CDSE only
GET  /api/rule-reversions/pending                                 the caller's CDSE queue (all dams)

Affected rules = the dam's current ref.threshold rows whose dssmp_version_id is not the target version (i.e. set under
the version being superseded). Re-versioning copies each rule unchanged except dssmp_version_id -> target (N+1, same
cdse_ack_required, same limits). Approval re-checks every rule (stale: another version, flag or DSSMP since the
proposal, or another applied change waiting to be published -> 409, nothing queued) and the target version (must be
loaded, approved and in effect: dssmp_findings, fail closed), then appends ONE rule_reversion_approve record and queues
the N+1 publication of EVERY rule in ONE transaction (ref_change_publication, kind ref.threshold.new_version, payload
cause 'dssmp_reversion'). The flag-apply 409 dssmp_superseded stays (no grace path). Alarms and open episodes are not
touched: the ack rule ORs the episode snapshot with the current flag, and the flag is carried unchanged."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import audit_chain as ac
from ..config import approval_policy
from ..deps import Ctx, analytics, ctx, store
from ..onboarding import PUBLISH_REQUIRED, dssmp_findings
from . import alarm_rules as ar
from . import alarms as alarms_router
from . import signoffs as sg

router = APIRouter(prefix="/api/dams/{dam_id}/rule-reversions")
queue_router = APIRouter()
SUBJECT = "rule_reversion"


class ProposeBody(BaseModel):
    to_dssmp_version_id: str | None = None
    rationale: str | None = None


class DecisionBody(BaseModel):
    rationale: str | None = None


def affected_rules(an, dam_id: str, to: str) -> list[dict]:
    return sorted(({"threshold_id": t["threshold_id"], "threshold_version": ar._version(t),
                    "dssmp_version_id": t.get("dssmp_version_id"), "cdse_ack_required": ar._flag(t)}
                   for t in an.thresholds(dam_id) if str(t.get("dssmp_version_id") or "").strip() != to),
                  key=lambda r: r["threshold_id"])


def _may_propose(c: Ctx, dam_id: str, an) -> bool:
    return c.is_admin or (c.roles or {}).get(dam_id) == "dam_admin" or sg.is_cdse(c, dam_id, an)


def _target_ok(an, dam_id: str, to: str) -> None:
    f = dssmp_findings(an.dssmp_versions, dam_id, [to]).get(to)
    if f:
        raise HTTPException(409, {"code": f["code"], "message": f"target {f['message']}: rules can only be "
                                  "re-versioned under a DSSMP version that is loaded, approved and in effect"})


def _record(c, request, an, dam_id, pid, action, decision, rationale, original, new) -> dict:
    who = sg.reviewer_identity(c, request)
    base = {"review_id": f"RS-{uuid.uuid4().hex}", "dam_id": dam_id, "subject_type": SUBJECT, "subject_id": pid,
            "review_level": sg.review_level(c, dam_id, an), "decision": decision, "comment": rationale,
            "reviewer_id": who["reviewer_id"], "reviewer_pe_license": None, "signed_ts": sg.now_utc(),
            "action_type": action, "reviewer_email": who["reviewer_email"], "reviewer_sub": who["reviewer_sub"],
            "original_value": ac.canon_json(original), "corrected_value": ac.canon_json(new)}
    base["signature_hash"] = alarms_router._content_hash(base, original)
    return base


def _out(r: dict) -> dict:
    return {**r, "rules": json.loads(r["rules"]), "pub_ids": json.loads(r["pub_ids"]) if r.get("pub_ids") else None}


@router.get("")
def preview(dam_id: str, request: Request, to: str | None = None, c: Ctx = Depends(ctx), an=Depends(analytics),
            st=Depends(store)):
    if not (c.can(dam_id, "viewer") or sg.is_cdse(c, dam_id, an)):
        raise HTTPException(403, f"No access to dam {dam_id}")
    rules = affected_rules(an, dam_id, to.strip()) if to else None
    return {"dam_id": dam_id, "to_dssmp_version_id": to, "affected_rules": rules,
            "affected_count": None if rules is None else len(rules),
            "proposals": [_out(r) for r in st.rows("rule_reversion_proposal", "proposed_at DESC", dam_id=dam_id)],
            "can_propose": _may_propose(c, dam_id, an), "can_decide": sg.is_cdse(c, dam_id, an),
            "approval_policy": approval_policy(request.app.state.settings)}


@queue_router.get("/api/rule-reversions/pending")
def queue(c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    rows = st.rows("rule_reversion_proposal", "proposed_at", status="pending")
    mine = {d for d in {r["dam_id"] for r in rows} if sg.is_cdse(c, d, an)}
    return {"proposals": [_out(r) for r in rows if r["dam_id"] in mine]}


def create(c: Ctx, request: Request, an, st, dam_id: str, to: str, rationale: str) -> dict:
    _target_ok(an, dam_id, to)
    rules = affected_rules(an, dam_id, to)
    if not rules:
        raise HTTPException(409, {"code": "nothing_to_reversion",
                                  "message": f"every current rule of {dam_id} is already set under {to}"})
    full = {t["threshold_id"]: t for t in an.thresholds(dam_id)}
    lacking = [r["threshold_id"] for r in rules
               if any(full[r["threshold_id"]].get(k) in (None, "") for k in PUBLISH_REQUIRED)]
    if lacking:
        raise HTTPException(409, f"rules {', '.join(lacking)} lack NOT NULL ref.threshold columns; the DA must "
                                 "backfill them before they can be re-versioned (no defaults are invented)")
    if st.rows("rule_reversion_proposal", "proposed_at", dam_id=dam_id, status="pending"):
        raise HTTPException(409, {"code": "reversion_pending", "message": f"{dam_id} already has a pending "
                                                                          "re-version proposal"})
    who = sg.reviewer_identity(c, request)
    pid = f"RRV-{uuid.uuid4().hex}"
    row = {"proposal_id": pid, "dam_id": dam_id, "to_dssmp_version_id": to, "rules": json.dumps(rules),
           "proposed_by": c.email, "proposed_by_id": who["reviewer_id"], "proposed_at": sg.now_utc(),
           "rationale": rationale, "status": "pending", "decided_by": None, "decided_by_id": None,
           "decided_at": None, "decision_rationale": None, "pub_ids": None}
    base = _record(c, request, an, dam_id, pid, "rule_reversion_propose", "propose", rationale,
                   {"rules": rules}, {"proposal_id": pid, "to_dssmp_version_id": to, "rule_count": len(rules),
                                      "proposed_by": c.email, "state": "pending"})
    rec = sg.append(st, request, base, lambda q: st.insert_row(q, "rule_reversion_proposal", row), c=c, an=an)
    st.audit(c.email, "rule_reversion_propose", dam_id, to, {"proposal_id": pid, "rules": len(rules)})
    return {**_out(row), "chain_seq": rec["chain_seq"]}


@router.post("")
def propose(dam_id: str, body: ProposeBody, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics),
            st=Depends(store)):
    if not _may_propose(c, dam_id, an):
        raise HTTPException(403, f"Only a dam_admin of {dam_id}, a platform admin or the dam's CDSE may propose a "
                                 "re-version")
    rationale = sg.rationale_of(body.rationale)
    to = (body.to_dssmp_version_id or "").strip()
    if not to:
        raise HTTPException(422, "to_dssmp_version_id is required")
    return create(c, request, an, st, dam_id, to, rationale)


def _pending(st, dam_id: str, pid: str) -> dict:
    rows = st.rows("rule_reversion_proposal", "proposed_at", proposal_id=pid)
    if not rows or rows[0]["dam_id"] != dam_id:
        raise HTTPException(404, f"Unknown re-version proposal {pid}")
    if rows[0]["status"] != "pending":
        raise HTTPException(409, f"re-version proposal {pid} is {rows[0]['status']}")
    return rows[0]


def _guard_cdse(c: Ctx, dam_id: str, an, what: str) -> None:
    if not sg.is_cdse(c, dam_id, an):
        raise HTTPException(403, f"Only the CDSE of dam {dam_id} (ref.v_dam_reviewer_authority) may {what}")


@router.post("/{proposal_id}/approve")
def approve(dam_id: str, proposal_id: str, body: DecisionBody, request: Request, c: Ctx = Depends(ctx),
            an=Depends(analytics), st=Depends(store)):
    _guard_cdse(c, dam_id, an, "approve a re-version")
    rationale = sg.rationale_of(body.rationale)
    p = _pending(st, dam_id, proposal_id)
    who = sg.reviewer_identity(c, request)
    if request.app.state.settings.cdse_flag_require_second_approver and (
            (p["proposed_by"] or "").lower() == (c.email or "").lower()):
        raise HTTPException(403, {"code": "self_approval_forbidden", "provisional": True,
                                  "setting": "CDSE_FLAG_REQUIRE_SECOND_APPROVER",
                                  "message": f"{c.email} proposed {proposal_id} and cannot approve it"})
    to = p["to_dssmp_version_id"]
    _target_ok(an, dam_id, to)
    rules = json.loads(p["rules"])
    current = {t["threshold_id"]: t for t in an.thresholds(dam_id)}
    stale = []
    for r in rules:
        cur = current.get(r["threshold_id"])
        if cur is None or str(cur.get("dssmp_version_id") or "").strip() != str(r["dssmp_version_id"] or "").strip():
            stale.append(f"{r['threshold_id']}: no longer current or its DSSMP version changed")
            continue
        why = ar._stale(st, cur, int(r["threshold_version"]), bool(r["cdse_ack_required"]), None)
        if why:
            stale.append(f"{r['threshold_id']}: {why}")
    missing = sorted({r["threshold_id"] for r in affected_rules(an, dam_id, to)} - {r["threshold_id"] for r in rules})
    if stale or missing:
        raise HTTPException(409, {"code": "reversion_stale", "stale": stale, "not_covered": missing,
                                  "message": "the affected rules changed since the proposal; reject it and propose "
                                             "again (nothing was queued)"})
    now = sg.now_utc()
    demo = request.app.state.settings.demo_mode
    payloads = []
    for r in rules:
        rule = current[r["threshold_id"]]
        insert = {k: v for k, v in rule.items() if k not in ar.DERIVED}
        insert.update(threshold_version=ar._version(rule) + 1, dssmp_version_id=to, effective_from=now,
                      effective_to=None, is_current=True, set_by=c.email)
        payloads.append((str(uuid.uuid4()), {"kind": ar.PUB_KIND, "threshold_id": rule["threshold_id"],
                                             "dam_id": dam_id, "supersedes_version": ar._version(rule),
                                             "cause": "dssmp_reversion", "reversion_proposal_id": proposal_id,
                                             "insert": insert}))
    base = _record(c, request, an, dam_id, proposal_id, "rule_reversion_approve", "approve", rationale,
                   {"rules": rules}, {"proposal_id": proposal_id, "to_dssmp_version_id": to,
                                      "proposed_by": p["proposed_by"], "state": "applied",
                                      "publications": [{"threshold_id": pl["threshold_id"], "publication_id": pub,
                                                        "threshold_version": pl["insert"]["threshold_version"]}
                                                       for pub, pl in payloads]})

    def also_row(q, rec):  # ONE transaction: decision + every N+1 publication
        st.decide_pending(q, "rule_reversion_proposal", "proposal_id", proposal_id, {
            "status": "applied", "decided_by": c.email, "decided_by_id": who["reviewer_id"],
            "decided_at": rec["signed_ts"], "decision_rationale": rationale,
            "pub_ids": json.dumps([pub for pub, _ in payloads])})
        for pub, pl in payloads:
            st.insert_ref_publication(q, pub, ar.PUB_KIND, dam_id, proposal_id, pl | {
                "approval_record_hash": rec["record_hash"]}, c.email, "applied" if demo else "pending")

    try:
        rec = sg.append(st, request, base, c=c, an=an, also_row=also_row)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    if demo:
        for _, pl in payloads:
            an.apply_threshold_version(pl)
    st.audit(c.email, "rule_reversion_approve", dam_id, to, {"proposal_id": proposal_id, "rules": len(rules)})
    return {**_out(st.rows("rule_reversion_proposal", "proposed_at", proposal_id=proposal_id)[0]),
            "chain_seq": rec["chain_seq"], "publication_status": "applied" if demo else "pending"}


@router.post("/{proposal_id}/reject")
def reject(dam_id: str, proposal_id: str, body: DecisionBody, request: Request, c: Ctx = Depends(ctx),
           an=Depends(analytics), st=Depends(store)):
    _guard_cdse(c, dam_id, an, "reject a re-version")
    rationale = sg.rationale_of(body.rationale)
    p = _pending(st, dam_id, proposal_id)
    who = sg.reviewer_identity(c, request)
    base = _record(c, request, an, dam_id, proposal_id, "rule_reversion_reject", "reject", rationale,
                   {"rules": json.loads(p["rules"])}, {"proposal_id": proposal_id, "state": "rejected",
                                                       "to_dssmp_version_id": p["to_dssmp_version_id"],
                                                       "proposed_by": p["proposed_by"]})

    def also_row(q, rec):
        st.decide_pending(q, "rule_reversion_proposal", "proposal_id", proposal_id, {
            "status": "rejected", "decided_by": c.email, "decided_by_id": who["reviewer_id"],
            "decided_at": rec["signed_ts"], "decision_rationale": rationale})

    try:
        rec = sg.append(st, request, base, c=c, an=an, also_row=also_row)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {**_out(st.rows("rule_reversion_proposal", "proposed_at", proposal_id=proposal_id)[0]),
            "chain_seq": rec["chain_seq"]}
