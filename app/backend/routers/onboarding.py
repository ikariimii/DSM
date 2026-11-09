"""New-project onboarding wizard API.

Steps: details, structures, instruments (CSV import), calibrations, formulas,
thresholds, data_sources, users, validate, golive. Drafts live in Lakebase
(onboarding_draft); go-live writes an outbox row (onboarding_publication) that a
Data Architect job publishes into ref.* tables. In demo mode the publication is
applied immediately to the in-memory dataset."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import formulas as fx
from .. import onboarding as ob
from ..deps import Ctx, analytics, ctx, store

router = APIRouter(prefix="/api/onboarding")


class CsvBody(BaseModel):
    csv: str = Field(max_length=2_000_000)


def _can_onboard(c: Ctx) -> bool:
    return c.is_admin or "dam_admin" in c.roles.values()


def _draft(st, c: Ctx, draft_id: str) -> dict:
    d = st.get_draft(draft_id)
    if d is None:
        raise HTTPException(404, "Unknown draft")
    if not c.is_admin and d["created_by"] != c.email:
        raise HTTPException(403, "Only the draft owner or an admin may access this draft")
    return d


def _summary(d: dict) -> dict:
    return {k: d[k] for k in ("draft_id", "created_by", "created_at", "updated_at", "status")} | {
        "dam_id": (d["data"].get("details") or {}).get("dam_id"),
        "name": (d["data"].get("details") or {}).get("name")}


@router.get("/steps")
def steps():
    return {"steps": ob.STEPS, "csv_columns": ob.CSV_COLUMNS, "csv_required": ob.CSV_REQUIRED,
            "type_units": {k: sorted(v) for k, v in ob.TYPE_UNITS.items()}, "hazard_classes": ob.HAZARD,
            "source_types": ob.SOURCE_TYPES,
            "formulas": [{"formula_id": f.formula_id, "version": f.version, "unit": f.unit,
                          "required_params": f.required_params, "description": f.description}
                         for f in fx.REGISTRY.values()]}


@router.get("/drafts")
def list_drafts(c: Ctx = Depends(ctx), st=Depends(store)):
    if not _can_onboard(c):
        raise HTTPException(403, "Onboarding requires admin or dam_admin")
    return {"drafts": [_summary(d) for d in st.list_drafts(None if c.is_admin else c.email)]}


@router.post("/drafts")
def create_draft(c: Ctx = Depends(ctx), st=Depends(store)):
    if not _can_onboard(c):
        raise HTTPException(403, "Onboarding requires admin or dam_admin")
    d = st.create_draft(c.email, {})
    st.audit(c.email, "onboarding_create", None, d["draft_id"])
    return d


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str, c: Ctx = Depends(ctx), st=Depends(store)):
    return _draft(st, c, draft_id)


@router.put("/drafts/{draft_id}/steps/{step}")
async def put_step(draft_id: str, step: str, request: Request, c: Ctx = Depends(ctx), st=Depends(store)):
    d = _draft(st, c, draft_id)
    if d["status"] != "draft":
        raise HTTPException(409, f"Draft is {d['status']}")
    if step not in ob.EDITABLE:
        raise HTTPException(404, f"Unknown or non-editable step {step}")
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(422, "Body must be JSON") from e
    expect = dict if step in ("details", "formulas") else list
    if not isinstance(body, expect):
        raise HTTPException(422, f"Step {step} expects a JSON {expect.__name__}")
    data = d["data"]
    changes = ob.cdse_flag_changes(data.get("thresholds"), body) if step == "thresholds" else []
    data[step] = body
    out = st.update_draft(draft_id, data)
    dam = (data.get("details") or {}).get("dam_id")
    for ch in changes:   # changing the CDSE-acknowledgement flag of an alarm rule is itself an audited admin action
        st.audit(c.email, "alarm_rule_cdse_ack_required", dam, draft_id, ch)
    return out


@router.post("/parse-csv")
def parse_csv(body: CsvBody, c: Ctx = Depends(ctx)):
    if not _can_onboard(c):
        raise HTTPException(403, "Onboarding requires admin or dam_admin")
    return ob.parse_instrument_csv(body.csv)


@router.post("/drafts/{draft_id}/formulas/check")
def check_formulas(draft_id: str, c: Ctx = Depends(ctx), st=Depends(store), an=Depends(analytics)):
    d = _draft(st, c, draft_id)
    eng = {(f["formula_id"], str(f["formula_version"])): f for f in an.formulas()}
    checks = {}
    for i in d["data"].get("instruments") or []:
        key = (i.get("formula_id"), str(i.get("formula_version")))
        k = f"{key[0]}:{key[1]}"
        if k in checks:
            continue
        app = fx.run_vectors(*key)
        checks[k] = {"formula_id": key[0], "version": key[1], "app_check": app,
                     "engine_verified": bool(eng.get(key, {}).get("verified")),
                     "verified": bool(eng.get(key, {}).get("verified"))}
    data = d["data"]
    data["formulas"] = {"checks": checks}
    st.update_draft(draft_id, data)
    return {"checks": checks}


@router.post("/drafts/{draft_id}/validate")
def validate(draft_id: str, c: Ctx = Depends(ctx), st=Depends(store), an=Depends(analytics)):
    d = _draft(st, c, draft_id)
    issues = ob.validate(d["data"], {s["dam_id"] for s in an.sites()}, an.formulas())
    issues += ob.dssmp_issues(d["data"], an.dssmp_versions)      # ref.dssmp_version, same dam (fail closed)
    errors = [i for i in issues if i["severity"] == "error"]
    return {"ok": not errors, "errors": len(errors), "warnings": len(issues) - len(errors), "issues": issues}


@router.post("/drafts/{draft_id}/golive")
def golive(draft_id: str, request: Request, c: Ctx = Depends(ctx), st=Depends(store), an=Depends(analytics)):
    if not c.is_admin:
        raise HTTPException(403, "Go-live requires a global admin")
    d = _draft(st, c, draft_id)
    if d["status"] != "draft":
        raise HTTPException(409, f"Draft is {d['status']}")
    issues = ob.validate(d["data"], {s["dam_id"] for s in an.sites()}, an.formulas())
    issues += ob.dssmp_issues(d["data"], an.dssmp_versions)      # ref.dssmp_version, same dam (fail closed)
    if any(i["severity"] == "error" for i in issues):
        raise HTTPException(422, {"message": "Validation errors", "issues": issues})
    payload = ob.golive_payload(d["data"])
    dam_id = payload["dam_id"]
    demo = request.app.state.settings.demo_mode
    pub = st.publish(draft_id, dam_id, payload, c.email, status="applied" if demo else "pending")
    if demo:
        an.apply_publication(payload)
    for u in payload["users"]:
        email = u["email"].strip().lower()
        if st.get_user(email) is None:
            st.upsert_user(email, u.get("display_name") or email, False, c.email)
        st.set_role(email, dam_id, u["role"], c.email)
    st.update_draft(draft_id, d["data"], status="published")
    st.audit(c.email, "onboarding_golive", dam_id, draft_id,
             {"publication_id": pub["pub_id"], "status": pub["status"],
              "cdse_ack_required_rules": [t["threshold_id"] for t in payload["thresholds"] if t["cdse_ack_required"]],
              "warnings": [i["message"] for i in issues if i["severity"] == "warning"][:50]})
    return {"publication": pub, "warnings": [i for i in issues if i["severity"] == "warning"]}
