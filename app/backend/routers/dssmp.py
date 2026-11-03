"""DSSMP / PFM module: potential failure modes, their linked instruments and
thresholds (traceability PFM -> instrument -> threshold -> response), and review notes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..common import gate_for, site_or_404
from ..deps import Ctx, analytics, ctx, require, store

router = APIRouter(prefix="/api/dams/{dam_id}")

REVIEW_STATUSES = ("no_change", "update_required", "updated")


class ReviewBody(BaseModel):
    status: str
    notes: str = Field(default="", max_length=8000)


@router.get("/dssmp")
def dssmp(dam_id: str, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    require(c, dam_id)
    site = site_or_404(an, dam_id)
    gate = gate_for(an, dam_id)
    insts = {i["instrument_id"]: i for i in an.instruments(dam_id)}
    latest = {r["instrument_id"]: gate.apply(r) for r in an.latest_readings(dam_id)}
    ths = an.thresholds(dam_id)
    reviews = st.pfm_reviews(dam_id)
    pfms = []
    for p in an.pfms(dam_id):
        linked = []
        for iid in p.get("instruments") or []:
            i = insts.get(iid)
            linked.append({"instrument_id": iid, "missing": i is None,
                           "instrument_type": i and i["instrument_type"], "eng_unit": i and i["eng_unit"],
                           "latest": latest.get(iid),
                           "thresholds": [t for t in ths if t["instrument_id"] == iid]})
        gaps = [x["instrument_id"] for x in linked if x["missing"] or not x["thresholds"]]
        pfms.append({**p, "linked": linked, "traceability_gaps": gaps,
                     "reviews": [r for r in reviews if r["pfm_id"] == p["pfm_id"]]})
    # instruments that are not tied to any PFM (DSSMP should justify each instrument)
    tied = {iid for p in pfms for iid in p.get("instruments") or []}
    return {"site": site, "pfms": pfms, "untied_instruments": sorted(set(insts) - tied),
            "can_review": c.can(dam_id, "dso")}


@router.post("/pfms/{pfm_id}/review")
def review(dam_id: str, pfm_id: str, body: ReviewBody, c: Ctx = Depends(ctx), an=Depends(analytics),
           st=Depends(store)):
    require(c, dam_id, "dso")
    if pfm_id not in {p["pfm_id"] for p in an.pfms(dam_id)}:
        raise HTTPException(404, f"Unknown PFM {pfm_id} for {dam_id}")
    if body.status not in REVIEW_STATUSES:
        raise HTTPException(422, f"status must be one of {REVIEW_STATUSES}")
    st.add_pfm_review(pfm_id, dam_id, c.email, body.status, body.notes)
    st.audit(c.email, "pfm_review", dam_id, pfm_id, {"status": body.status})
    return {"reviews": [r for r in st.pfm_reviews(dam_id) if r["pfm_id"] == pfm_id]}
