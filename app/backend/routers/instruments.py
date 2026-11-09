"""Instrument registry, calibrations, time series and inclinometer profiles."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import formulas as fx
from .. import signoff as so
from . import signoffs as sg
from ..repos import contract as ct
from ..common import gate_for, instrument_checked, site_or_404
from ..deps import Ctx, analytics, ctx, require, store
from ..verify import INCL_KEYS

router = APIRouter(prefix="/api")


def _formula_info(an, fid, ver) -> dict:
    f = next((x for x in an.formulas() if x["formula_id"] == fid and str(x["formula_version"]) == str(ver)), None)
    return {"formula_id": fid, "formula_version": ver, "verified": bool(f and f.get("verified")),
            "description": f.get("description") if f else None, "unit": f.get("unit") if f else None}


@router.get("/dams/{dam_id}/instruments")
def registry(dam_id: str, c: Ctx = Depends(ctx), an=Depends(analytics)):
    require(c, dam_id)
    site_or_404(an, dam_id)
    gate = gate_for(an, dam_id)
    latest = {r["instrument_id"]: gate.apply(r) for r in an.latest_readings(dam_id)}
    cals = an.calibrations_for_dam(dam_id)
    verified = {(f["formula_id"], str(f["formula_version"])) for f in an.formulas() if f.get("verified")}
    out = []
    for i in an.instruments(dam_id):
        mine = [x for x in cals if x["instrument_id"] == i["instrument_id"]]
        # "current" = the open-ended, non-retired calibration (not is_current alone; retired corrections never count)
        live = [x for x in mine if not x.get("is_retired")]
        cur = next((x for x in live if x.get("effective_to") is None), live[-1] if live else None)
        out.append({**i, "latest": latest.get(i["instrument_id"]),
                    "formula_verified": (i["formula_id"], str(i["formula_version"])) in verified,
                    "current_calibration": None if cur is None else {
                        "calib_id": cur["calib_id"], "cal_version": cur["cal_version"],
                        "approved": bool(cur.get("approved_by")) and not cur.get("calibration_issues"),
                        "calibration_issues": cur.get("calibration_issues") or [],
                        "sign_mult": cur.get("sign_mult"), "offset": cur.get("offset"),
                        "effective_from": cur.get("effective_from")}})
    return {"dam_id": dam_id, "instruments": out}


@router.get("/instruments/{instrument_id}")
def detail(instrument_id: str, c: Ctx = Depends(ctx), an=Depends(analytics)):
    inst = instrument_checked(an, c, instrument_id)
    cals = an.calibrations(instrument_id)
    return {"instrument": inst, "calibrations": cals,
            "formula": _formula_info(an, inst["formula_id"], inst["formula_version"]),
            "thresholds": an.thresholds(inst["dam_id"], instrument_id)}


@router.get("/instruments/{instrument_id}/timeseries")
def timeseries(instrument_id: str, start: str | None = None, end: str | None = None,
               c: Ctx = Depends(ctx), an=Depends(analytics)):
    inst = instrument_checked(an, c, instrument_id)
    if inst["instrument_type"] == "inclinometer":
        raise HTTPException(400, "Inclinometers are profile data; use /incl/surveys and /incl/profile")
    gate = gate_for(an, inst["dam_id"])
    rows = gate.apply_all(an.readings(instrument_id, start, end))
    status = Counter(r["display_status"] for r in rows)
    qa = Counter(r.get("qa_flag") for r in rows)
    cal_versions = sorted({(r["calib_id"], r["cal_version"]) for r in rows if r.get("calib_id")}, key=lambda x: x[1])
    units = {r.get("corrected_unit") or r.get("eng_unit") for r in rows} - {None}
    points = [{"ts_utc": r["ts_utc"], "value": r.get("corrected_value"), "eng_value": r.get("eng_value"),
               "eng_unit": r.get("eng_unit"), "unit": r.get("corrected_unit") or r.get("eng_unit"),
               "raw_value": r.get("raw_value"), "raw_unit": r.get("raw_unit"), "temp_c": r.get("temp_c"),
               "calib_id": r.get("calib_id"), "cal_version": r.get("cal_version"),
               "formula_id": r.get("formula_id"), "formula_version": r.get("formula_version"),
               "qa_flag": r.get("qa_flag"), "qa_reason": r.get("qa_reason"),
               "display_status": r["display_status"], "reading_type": r.get("reading_type")} for r in rows]
    return {"instrument": inst, "unit": sorted(units)[0] if len(units) == 1 else None, "units": sorted(units),
            "output_name": inst.get("output_name"), "points": points,
            "summary": {"n": len(rows), "display_status": dict(status), "qa_flag": dict(qa),
                        "n_shown": status.get("VERIFIED", 0), "n_withheld": len(rows) - status.get("VERIFIED", 0)},
            "calibration_versions": [{"calib_id": a, "cal_version": b} for a, b in cal_versions],
            "formula": _formula_info(an, inst["formula_id"], inst["formula_version"]),
            "thresholds": an.thresholds(inst["dam_id"], instrument_id)}


@router.get("/instruments/{instrument_id}/incl/surveys")
def incl_surveys(instrument_id: str, c: Ctx = Depends(ctx), an=Depends(analytics)):
    inst = instrument_checked(an, c, instrument_id)
    if inst["instrument_type"] != "inclinometer":
        raise HTTPException(400, "Not an inclinometer")
    return {"instrument": inst, "surveys": an.incl_surveys(instrument_id)}


@router.get("/instruments/{instrument_id}/incl/profile")
def incl_profile(instrument_id: str, survey_ids: str = Query(..., description="comma-separated survey ids"),
                 c: Ctx = Depends(ctx), an=Depends(analytics), state=Depends(store)):
    inst = instrument_checked(an, c, instrument_id)
    if inst["instrument_type"] != "inclinometer":
        raise HTTPException(400, "Not an inclinometer")
    # §5.5 v1.4: inclinometer rows (calib_id NULL) are approved via ref.incl_installation, not ref.calibration
    gate = gate_for(an, inst["dam_id"], an.incl_installations(instrument_id))
    profiles = []
    for sid in [x for x in survey_ids.split(",") if x]:
        rows = an.incl_profile(instrument_id, sid)
        if not rows:
            raise HTTPException(404, f"Unknown survey {sid}")
        rows = gate.apply_all(rows, INCL_KEYS)
        st = Counter(r["display_status"] for r in rows)
        rec_ids = sorted({x for r in rows for x in r.get("correction_record_ids") or []})
        basis = sorted({r.get("correction_basis") or "uncorrected" for r in rows})
        profiles.append({
            "survey_id": sid, "survey_ts": rows[0].get("survey_ts"), "unit": "mm",
            "calib_id": rows[0].get("calib_id"), "cal_version": rows[0].get("cal_version"),
            "formula_id": rows[0].get("formula_id"), "formula_version": rows[0].get("formula_version"),
            # §5.5 v1.4 lineage, per survey (distinct values across rows; >1 means mixed reduction batches)
            "formula_verified": all(gate.formula_verified(r.get("formula_id"), r.get("formula_version")) for r in rows),
            "qc_rule_version": _distinct(rows, "qc_rule_version"),
            "installation_version": _distinct(rows, "installation_version"),
            "probe_constants": _distinct(rows, "probe_constants"),
            "installation_approved": (all(gate.installation_approved(r) for r in rows)
                                      if all(r.get("calib_id") is None for r in rows) else None),
            "display_status": dict(st), "qa_flag": dict(Counter(r.get("qa_flag") for r in rows)),
            # No bias correction is applied. The app's own proposal stays suppressed; the engine's QA-020 suspect
            # flag / proposed c are shown UNVERIFIED and never applied (§5.5 v1.2, api_contract §3).
            "bias_applied": False,
            "bias_qc": ct.bias_qc(rows, recs := an.incl_bias_proposals(instrument_id, sid)),
            # QA-039: append-only sign-offs on this survey's proposals, each re-verified against the current record
            "bias_signoffs": _signoffs(state, recs),
            "qa_reasons": ct.qa_reason_detail(",".join(dict.fromkeys(
                c for r in rows for c in ct.qa_reason_codes(r.get("qa_reason"))))),
            # §5.5: values are uncorrected; a row that references corrections is withheld (CORRECTION_UNVERIFIED)
            "correction_basis": basis, "correction_note": ct.UNCORRECTED_NOTE,
            "corrections": an.incl_corrections(rec_ids) if rec_ids else [],
            # API uses the contract column names (cum_a, inc_a, resultant, checksum_a, ...)
            "rows": [ct.incl_api_row(r) for r in rows]})
    return {"instrument": inst, "profiles": profiles}


def _distinct(rows: list[dict], key: str):
    """One value when every row agrees, else the sorted distinct list (never silently picks one)."""
    vals = sorted({r.get(key) for r in rows if r.get(key) is not None}, key=str)
    return None if not vals else vals[0] if len(vals) == 1 else vals


@router.get("/formulas/{formula_id}/{version}/check")
def formula_check(formula_id: str, version: str, c: Ctx = Depends(ctx)):
    return fx.run_vectors(formula_id, version)


# ---------------------------------------------------------------- QA-039 v2 reviews -> per-dam hash chain
class BiasReviewBody(BaseModel):
    decision: str
    rationale: str = Field(min_length=1, max_length=8000)
    reviewer_pe_license: str | None = Field(default=None, max_length=64)


def _signoffs(st, recs: list[dict]) -> list[dict]:
    by_id = {r.get("record_id"): r for r in recs}
    out = st.review_signoffs(so.SUBJECT_TYPE, sorted(k for k in by_id if k))
    return [{**r, "signature_verified": so.verify(r, by_id.get(r["subject_id"]))} for r in out]


def _review(rec: dict | None, label: str, record_id: str, body: BiasReviewBody, inst: dict, request: Request,
            c: Ctx, st, an=None) -> dict:
    if body.decision not in so.DECISIONS:
        raise HTTPException(422, f"decision must be one of {', '.join(so.DECISIONS)}")
    rationale = sg.rationale_of(body.rationale)
    who = sg.reviewer_identity(c, request)
    if rec is None:
        raise HTTPException(404, f"Unknown {label} {record_id}")
    is_bias = rec.get("detection_method") == "between_set_bias_qc"
    if is_bias:
        view = ct.bias_proposal(rec)
        err, status, open_ = view["integrity_error"], view["approval_status"], view["status"] in ct.PROPOSAL_STATUSES
    else:
        try:
            err, status, open_ = None, ct.approval_status(rec), True
        except ct.DataIntegrityError as e:
            err, status, open_ = e.code, None, False
    if err:
        raise HTTPException(409, f"Record {record_id} has a data-integrity error ({err})")
    if status != "proposed" or not open_:
        raise HTTPException(409, f"Record {record_id} is {status}; only an open proposal can be reviewed")
    if so.same_person((c.user_id, c.email), rec.get("user")):
        raise HTTPException(403, "The reviewer must not be the author of the proposal")
    base = so.build(rec, inst["dam_id"], body.decision, rationale, who["reviewer_id"], body.reviewer_pe_license,
                    sg.now_utc(), action_type="bias_review" if is_bias else "data_correction_review",
                    review_level=sg.review_level(c, inst["dam_id"], an), reviewer_email=who["reviewer_email"],
                    reviewer_sub=who["reviewer_sub"])
    row = sg.append(st, request, base)
    st.audit(c.email, "bias_proposal_review" if is_bias else "data_correction_review", inst["dam_id"], record_id,
             {"review_id": row["review_id"], "decision": body.decision, "signature_hash": row["signature_hash"],
              "chain_seq": row["chain_seq"],
              "record_hash": row["record_hash"], "reviewer_id": who["reviewer_id"], "survey_id": rec.get("survey_id")})
    return {**row, "signature_verified": True, "applied_by_app": False}


def _incl(an, c: Ctx, instrument_id: str) -> dict:
    inst = instrument_checked(an, c, instrument_id, "engineer")
    if inst["instrument_type"] != "inclinometer":
        raise HTTPException(400, "Not an inclinometer")
    return inst


@router.post("/instruments/{instrument_id}/incl/surveys/{survey_id}/bias-proposals/{record_id}/review")
def review_bias_proposal(instrument_id: str, survey_id: str, record_id: str, body: BiasReviewBody, request: Request,
                         c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    """An engineer's approve/reject of one engine bias proposal: one record appended to the dam's hash chain.
    Never updates ref.incl_correction and never applies a correction: applying stays with the engine (§5.5)."""
    inst = _incl(an, c, instrument_id)
    rec = next((r for r in an.incl_bias_proposals(instrument_id, survey_id) if r.get("record_id") == record_id), None)
    return _review(rec, f"bias proposal for {instrument_id} survey {survey_id}:", record_id, body, inst, request, c, st, an)


@router.post("/instruments/{instrument_id}/incl/surveys/{survey_id}/corrections/{record_id}/review")
def review_correction(instrument_id: str, survey_id: str, record_id: str, body: BiasReviewBody, request: Request,
                      c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    """Approve/reject of any proposed ref.incl_correction data correction (depth / rotation / sensitivity / bias):
    one chained record holding the original and the proposed value. Nothing is applied or overwritten."""
    inst = _incl(an, c, instrument_id)
    rec = next((r for r in an.incl_corrections([record_id]) if r.get("record_id") == record_id
                and r.get("instrument_id") == instrument_id and r.get("survey_id") == survey_id), None)
    return _review(rec, f"correction for {instrument_id} survey {survey_id}:", record_id, body, inst, request, c, st, an)
