"""Identity, portfolio overview, per-dam drill-down and formula registry."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Request

from .. import formulas as fx
from ..common import LEVEL_NAMES, gate_for, merge_ack, site_or_404
from ..config import approval_policy
from ..deps import Ctx, analytics, ctx, require, store
from ..seed import SEED_USERS

router = APIRouter(prefix="/api")


@router.get("/health")
def health(request: Request):
    s = request.app.state.settings
    return {"ok": True, "demo_mode": s.demo_mode}


@router.get("/me")
def me(request: Request, c: Ctx = Depends(ctx), an=Depends(analytics)):
    s = request.app.state.settings
    dams = c.dams([x["dam_id"] for x in an.sites()])
    out = {"email": c.email, "known": c.known, "is_admin": c.is_admin, "roles": {d: c.role(d) for d in dams},
           "dams": dams, "demo_mode": s.demo_mode, "can_onboard": c.is_admin or "dam_admin" in c.roles.values(),
           # PROVISIONAL approval rule for cdse_ack_required edits (config CDSE_FLAG_REQUIRE_SECOND_APPROVER)
           "approval_policy": approval_policy(s)}
    if s.demo_mode:
        out["demo_users"] = [{"email": e, "display_name": n} for e, n, _ in SEED_USERS]
    return out


def _dam_summary(an, st, dam_id: str, site: dict) -> dict:
    gate = gate_for(an, dam_id)
    insts = an.instruments(dam_id)
    latest = {r["instrument_id"]: gate.apply(r) for r in an.latest_readings(dam_id)}
    events = an.alarm_events([dam_id])
    events = merge_ack(events, st.ack_states([e["event_id"] for e in events]))
    active = [e for e in events if e["state"] != "closed"]
    status = Counter(r["display_status"] for r in latest.values())
    qa = Counter(r.get("qa_flag") for r in latest.values())
    max_level = max([e["level"] for e in active], default=0)
    return {
        **site,
        "n_instruments": len(insts),
        "n_reporting": len(latest),
        "latest_status_counts": dict(status),
        "latest_qa_counts": dict(qa),
        "n_unverified": sum(v for k, v in status.items() if k != "VERIFIED"),
        "alarms_open": sum(1 for e in active if e["state"] == "open"),
        "alarms_acknowledged": sum(1 for e in active if e["state"] == "acknowledged"),
        "max_active_level": max_level,
        "max_active_level_name": LEVEL_NAMES[max_level],
    }


@router.get("/portfolio")
def portfolio(c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    sites = an.sites()
    allowed = set(c.dams([s["dam_id"] for s in sites]))
    return {"dams": [_dam_summary(an, st, s["dam_id"], s) for s in sites if s["dam_id"] in allowed]}


@router.get("/dams/{dam_id}")
def dam(dam_id: str, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    require(c, dam_id)
    site = site_or_404(an, dam_id)
    gate = gate_for(an, dam_id)
    latest = {r["instrument_id"]: gate.apply(r) for r in an.latest_readings(dam_id)}
    insts = an.instruments(dam_id)
    for i in insts:
        i["latest"] = latest.get(i["instrument_id"])
    events = an.alarm_events([dam_id])
    events = merge_ack([gate.apply_event(e) for e in events], st.ack_states([e["event_id"] for e in events]))
    return {"site": site, "summary": _dam_summary(an, st, dam_id, site), "structures": an.structures(dam_id),
            "instruments": insts, "alarms": sorted(events, key=lambda e: e["event_ts"], reverse=True),
            "my_role": c.role(dam_id), "pfms": an.pfms(dam_id)}


@router.get("/formulas")
def list_formulas(c: Ctx = Depends(ctx), an=Depends(analytics)):
    """Formula versions as recorded by the engine (ref.formula) plus the app's own
    re-run of the registered test vectors, so a disagreement is visible."""
    out = []
    for f in an.formulas():
        rv = fx.run_vectors(f["formula_id"], str(f["formula_version"]))
        out.append({**f, "app_check": {"known": rv["known"], "verified": rv["verified"], "results": rv["results"]}})
    return {"formulas": out}
