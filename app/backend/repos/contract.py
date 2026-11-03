"""Adapter: SCHEMA_CONTRACT v1.1 (data layer, /SCHEMA_CONTRACT.md) -> the app's internal row model.

The routers, gate, reports and the demo seed all use one internal shape. The warehouse uses the
contract's column names. These pure functions are the only place where the two meet, so a contract
change is a change here plus a test. Nothing here computes an engineering value; values are passed
through with their lineage, and missing or inconsistent lineage makes the display gate withhold them."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- §5.4 instrument types
# Canonical ref.instrument_type vocabulary (sql/seed/ref_instrument_type_seed.sql). engine_formulas == ()
# means the data layer has no engine formula for the type, so the app must not compute or display a
# computed value for it (strain_gauge and others today).
INSTRUMENT_TYPES: dict[str, dict] = {
    "piezometer": {"units": ("m", "kPa"), "engine_formulas": ("VW_PIEZO", "STANDPIPE_DTW"),
                   "legacy": ("VW_PIEZOMETER", "STANDPIPE", "PIEZOMETER", "OPEN_STANDPIPE")},
    "reservoir_level": {"units": ("m",), "engine_formulas": ("LEVEL_DATUM",), "legacy": ("WATER_LEVEL", "LEVEL")},
    "seepage_weir": {"units": ("L/s", "ft3/s"), "engine_formulas": ("WEIR_VNOTCH",),
                     "legacy": ("SEEPAGE_WEIR", "WEIR", "SEEPAGE")},
    "inclinometer": {"units": ("mm",), "engine_formulas": ("INCL_PROBE_2PASS",), "legacy": ("INCLINOMETER",)},
    "crackmeter": {"units": ("mm",), "engine_formulas": ("VW_DISP_LIN",),
                   "legacy": ("CRACKMETER", "JOINTMETER", "VW_DISPLACEMENT")},
    "extensometer": {"units": ("mm",), "engine_formulas": ("VW_DISP_LIN",), "legacy": ("EXTENSOMETER",)},
    "settlement": {"units": ("mm",), "engine_formulas": ("SETTLEMENT_DELTA",),
                   "legacy": ("SETTLEMENT", "SURVEY_MONUMENT", "GNSS")},
    "strain_gauge": {"units": ("microstrain",), "engine_formulas": (), "legacy": ("SG", "STRAIN_GAUGE", "STRAINMETER")},
    "pressure_cell": {"units": ("kPa",), "engine_formulas": (), "legacy": ("PRESSURE_CELL", "STRESS_CELL")},
    "load_cell": {"units": ("kN",), "engine_formulas": (), "legacy": ("LOAD_CELL",)},
    "tiltmeter": {"units": ("arcsec", "mm/m"), "engine_formulas": (), "legacy": ("TILTMETER",)},
    "plumbline": {"units": ("mm",), "engine_formulas": (), "legacy": ("PLUMBLINE", "PENDULUM")},
    "accelerometer": {"units": ("g", "cm/s2"), "engine_formulas": (), "legacy": ("ACCELEROMETER", "STRONG_MOTION")},
    "thermometer": {"units": ("degC",), "engine_formulas": (), "legacy": ("THERMISTOR", "TEMPERATURE")},
}
# legacy upper-case code -> (canonical type, default subtype)
_LEGACY_SUBTYPE = {"VW_PIEZOMETER": "vibrating_wire", "STANDPIPE": "standpipe", "OPEN_STANDPIPE": "standpipe",
                   "SURVEY_MONUMENT": "survey_monument", "VW_DISPLACEMENT": "vibrating_wire",
                   "CRACKMETER": "vibrating_wire", "INCLINOMETER": "probe", "WEIR": "v_notch"}
TYPE_MAP = {code: (t, _LEGACY_SUBTYPE.get(code)) for t, spec in INSTRUMENT_TYPES.items() for code in spec["legacy"]}


def canonical_type(code: Any) -> tuple[str | None, str | None, bool]:
    """-> (canonical type or lower-cased unknown code, default subtype, known)."""
    typ = str(code or "").strip()
    if not typ:
        return None, None, False
    if typ.lower() in INSTRUMENT_TYPES:
        return typ.lower(), None, True
    if typ.upper() in TYPE_MAP:
        t, sub = TYPE_MAP[typ.upper()]
        return t, sub, True
    return typ.lower(), None, False


def type_is_computable(instrument_type: str | None) -> bool:
    spec = INSTRUMENT_TYPES.get(instrument_type or "")
    return bool(spec and spec["engine_formulas"])


def _num(v: Any) -> Any:
    """MAP<STRING,STRING> params arrive as strings. Convert numbers and booleans; keep text."""
    if not isinstance(v, str):
        return v
    low = v.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return float(v)
    except ValueError:
        return v


# ---------------------------------------------------------------- ref.calibration
CAL_COLUMNS_NOT_PARAMS = ("sign_mult", "offset")   # §10 Q3: columns on ref.calibration, never in params
CAL_METHODS = ("linear", "poly")                     # §10 Q3
# data-layer param spelling -> app formula spelling (the engine registry accepts both, §5.5)
PARAM_ALIASES = {"top_el_m": "top_elev_m", "tip_el_m": "tip_elev_m", "sensor_el_m": "sensor_elev_m",
                 "datum_el_m": "datum_elev_m"}


def normalize_params(params: dict | None) -> dict:
    p = {k: _num(v) for k, v in (params or {}).items()}
    for el, app in PARAM_ALIASES.items():
        if el in p and app not in p:
            p[app] = p[el]
    return p


def calibration(row: dict) -> dict:
    """sign_mult / offset are read ONLY from their columns (§10 Q3). A params key with either name is a
    data error: it is dropped (never used) and reported in `params_ignored` + `calibration_issues`, and the
    calibration does not count as approved for display (verify.Gate)."""
    r = dict(row)
    params = normalize_params(r.get("params"))
    issues: list[str] = []
    ignored = [k for k in CAL_COLUMNS_NOT_PARAMS if k in params]
    for k in ignored:
        params.pop(k)
        issues.append(f"params.{k} ignored: {k} is a ref.calibration column (SCHEMA_CONTRACT §10 Q3)")
    sm = r.get("sign_mult")
    sm = 1 if sm is None else _num(sm)
    if not (isinstance(sm, (int, float)) and not isinstance(sm, bool) and float(sm) in (1.0, -1.0)):
        issues.append(f"sign_mult must be +1 or -1, got {r.get('sign_mult')!r}")
        r["sign_mult"] = r.get("sign_mult")
    else:
        r["sign_mult"] = int(sm)
    off = r.get("offset")
    off = 0.0 if off is None else _num(off)
    if isinstance(off, (int, float)) and not isinstance(off, bool):
        r["offset"] = float(off)
    else:
        issues.append(f"offset must be numeric, got {r.get('offset')!r}")
        r["offset"] = r.get("offset")
    method = params.get("method", r.get("method"))
    r["method"] = method if isinstance(method, str) else None
    if r["method"] is not None and r["method"] not in CAL_METHODS:
        issues.append(f"method must be one of {'|'.join(CAL_METHODS)}, got {r['method']!r}")
    r["params"] = params
    r["params_ignored"] = ignored
    r["calibration_issues"] = issues
    r.setdefault("certificate_uri", None)
    r.pop("certificate", None)
    return r


def formula_inputs(cal: dict, **extra) -> dict:
    """Inputs for an app formula call: params plus the sign_mult/offset COLUMNS (engine apply_formula order)."""
    return {**(cal.get("params") or {}), "sign_mult": cal.get("sign_mult", 1), "offset": cal.get("offset", 0.0),
            **extra}


# ---------------------------------------------------------------- ref.instrument
def instrument(row: dict) -> dict:
    """Row from ref.instrument LEFT JOIN current calibration (formula_id/version) and ref.formula (output_unit)."""
    r = dict(row)
    src = r.get("instrument_type")
    typ, sub, known = canonical_type(src)
    r["source_instrument_type"] = src
    r["instrument_type"] = typ
    r["instrument_type_known"] = known
    r["computable"] = type_is_computable(typ)
    r["subtype"] = r.get("subtype") or sub
    r.setdefault("tip_el_m", None)
    r.setdefault("top_el_m", None)
    r.setdefault("eng_unit", r.get("output_unit"))
    r.setdefault("output_name", None)
    r.setdefault("replaced_by", None)
    r.setdefault("parent_id", None)
    return r


# ---------------------------------------------------------------- ref.threshold (§4, §10 Q7)
# (limit_type, direction) -> app rule. The alarm machine evaluates magnitude/rate x upper/lower/abs
# (dam_safety.alarms._exceeds, abs = |value| vs limit). cyclic is NOT evaluated (§10 Q7).
_RULES = {("magnitude", "upper"): "value_above", ("magnitude", "lower"): "value_below",
          ("magnitude", "abs"): "abs_above", ("rate", "upper"): "rate_above", ("rate", "lower"): "rate_below",
          ("rate", "abs"): "rate_abs_above"}
_LEGACY_RULES = {v: k for k, v in _RULES.items()}
RULE_LABELS = {"value_above": "value > limit", "value_below": "value < limit", "abs_above": "|value| > limit",
               "rate_above": "rate > limit per day", "rate_below": "rate < limit per day",
               "rate_abs_above": "|rate| > limit per day"}
NOT_EVALUATED = "not evaluated"


def threshold(row: dict) -> dict:
    """Canonical columns (limit_type, direction, value, pfm_id, required_actions) are the source; the legacy app
    shape (rule/upper_limit/lower_limit/rate_limit_per_day/pfm_ids/response_action) is accepted on input only
    when the canonical columns are absent (older demo/onboarding rows) and is always re-derived."""
    r = dict(row)
    lt = (r.get("limit_type") or "").strip().lower()
    dr = (r.get("direction") or "").strip().lower()
    v = r.get("value")
    if not lt and r.get("rule") in _LEGACY_RULES:
        lt, dr = _LEGACY_RULES[r["rule"]]
        v = {"value_above": r.get("upper_limit"), "value_below": r.get("lower_limit")}.get(
            r["rule"], r.get("rate_limit_per_day"))
    if r.get("pfm_id") is None and r.get("pfm_ids"):
        r["pfm_id"] = r["pfm_ids"][0]
    if r.get("required_actions") is None and r.get("response_action") is not None:
        r["required_actions"] = r["response_action"]
    r.update(limit_type=lt or None, direction=dr or None, value=v)
    rule = _RULES.get((lt, dr))
    if lt == "cyclic":
        r["rule"], r["evaluated"] = "cyclic", False
        r["rule_label"] = f"cyclic {dr or ''} limit: {NOT_EVALUATED}".replace("  ", " ")
    elif rule is None:
        r["rule"], r["evaluated"] = f"unsupported:{lt}/{dr}", False
        r["rule_label"] = f"{lt}/{dr}: {NOT_EVALUATED}"
    else:
        r["rule"], r["evaluated"], r["rule_label"] = rule, True, RULE_LABELS[rule]
    r["upper_limit"] = v if r["rule"] == "value_above" else None
    r["lower_limit"] = v if r["rule"] == "value_below" else None
    r["abs_limit"] = v if r["rule"] == "abs_above" else None
    r["rate_limit_per_day"] = v if r["rule"] in ("rate_above", "rate_below", "rate_abs_above") else None
    r["pfm_ids"] = [r["pfm_id"]] if r.get("pfm_id") else []
    r["response_action"] = r.get("required_actions")
    return r


# ---------------------------------------------------------------- ref.pfm (§5.4)
PFM_FIELDS = ("pfm_id", "dam_id", "pfm_code", "description", "loading_condition", "ch17_disposition",
              "legacy_ch14_category", "mechanism", "status", "is_general_health", "effective_from", "effective_to")


def pfm(row: dict) -> dict:
    """Row from ref.pfm with `instruments` = collect_set of current ref.pfm_instrument.instrument_id."""
    r = dict(row)
    for k in PFM_FIELDS:
        r.setdefault(k, None)
    r["instruments"] = sorted(r.get("instruments") or [])
    # app-only DSSMP narrative fields (not in ref.pfm; shown only when the demo seed provides them)
    r.setdefault("expected_behavior", None)
    r.setdefault("monitoring_frequency", None)
    r.pop("title", None)
    r.pop("category", None)
    return r


# ---------------------------------------------------------------- inclinometers (§5.5)
INCL_COLS = {"inc_a": "a_inc_mm", "inc_b": "b_inc_mm", "cum_a": "a_cum_mm", "cum_b": "b_cum_mm",
             "resultant": "resultant_mm", "checksum_a": "a_chk", "checksum_b": "b_chk"}
INCL_API = {v: k for k, v in INCL_COLS.items()}
# ref.incl_correction audit fields, in contract order (§5.5; == dam_safety.inclinometer.corrections.AUDIT_FIELDS)
INCL_CORRECTION_FIELDS = (
    "record_id", "dam_id", "instrument_id", "survey_id", "axis", "correction_type", "parameter_name", "auto_value",
    "detection_method", "detection_statistic", "override_value", "applied", "sign_convention", "depth_range",
    "reason", "source_citation", "user", "timestamp_utc", "before_value", "after_value", "software_version",
    "input_file_sha256", "superseded_by")
CORRECTION_UNVERIFIED = "CORRECTION_UNVERIFIED"
UNCORRECTED_NOTE = ("Uncorrected: no bias, depth, sensitivity or rotation correction is applied (SCHEMA_CONTRACT "
                    "§5.5). The v1.3 reduce_inclinometers writer is not deployed, so the app treats values as "
                    "uncorrected; any row that references a correction record is withheld.")
BIAS_PROPOSAL_NOTE = ("QA-020/QA-021 between-set bias QC (engine, SCHEMA_CONTRACT §5.5): the suspect flag and proposed "
                      "c are UNVERIFIED and NEVER applied. Applying a bias is a separate reviewed ref.incl_correction "
                      "row; the profile values above stay uncorrected.")

# §3 / §5.5 v1.3 qa_reason codes (comma-joined). (label, severity); severity is what the code implies for qa_flag.
# A code ending in _A/_B is per axis. Unknown codes are shown verbatim, never dropped.
QA_REASON_CODES = {
    "BAD:TEXT": ("reading is not numeric", "BAD"),
    "BAD:RANGE": ("reading outside the valid range", "BAD"),
    "MISSING:PASS": ("A0 or A180 pass missing", "MISSING"),
    "CHECKSUM": ("checksum outside tolerance", "SUSPECT"),
    "MEAN_CS_GT_100_A": ("survey mean checksum > 100 (A)", "SUSPECT"),
    "MEAN_CS_GT_100_B": ("survey mean checksum > 100 (B)", "SUSPECT"),
    "INDIVIDUAL_CS_A": ("individual checksum outside band (A)", "SUSPECT"),
    "INDIVIDUAL_CS_B": ("individual checksum outside band (B)", "SUSPECT"),
    "BIAS_CS_SHIFT_A": ("between-set checksum shift vs reference (A): investigate", "SUSPECT"),
    "BIAS_CS_SHIFT_B": ("between-set checksum shift vs reference (B): investigate", "SUSPECT"),
    "BIAS_PROPOSED_A": ("bias correction proposed (A), UNVERIFIED, not applied", "SUSPECT"),
    "BIAS_PROPOSED_B": ("bias correction proposed (B), UNVERIFIED, not applied", "SUSPECT"),
    "BIAS_CROSSCHECK_A": ("bias proposal ranked SUSPECT (A): cross-check disagrees, review; UNVERIFIED, not applied", "SUSPECT"),
    "BIAS_CROSSCHECK_B": ("bias proposal ranked SUSPECT (B): cross-check disagrees, review; UNVERIFIED, not applied", "SUSPECT"),
    "BIAS_BAD_RESURVEY_A": ("|c| above limit (A): resurvey", "BAD"),
    "BIAS_BAD_RESURVEY_B": ("|c| above limit (B): resurvey", "BAD"),
    "STABLE_ZONE_ASSUMED": ("stable zone not set; bottom intervals assumed", "INFO"),
    "CORRECTION_RECORD_INVALID": ("stored correction record failed validation and was skipped", "SUSPECT"),
    "SPIRAL_NOT_APPLIED": ("spiral correction not applied (deferred)", "INFO"),
}
CHECKSUM_CODES = {"CHECKSUM", "MEAN_CS_GT_100_A", "MEAN_CS_GT_100_B", "INDIVIDUAL_CS_A", "INDIVIDUAL_CS_B"}


def qa_reason_codes(reason) -> list[str]:
    """Comma-joined qa_reason (§3) -> codes, order kept, blanks dropped."""
    if not reason:
        return []
    return [x.strip() for x in str(reason).split(",") if x.strip()]


def qa_reason_detail(reason) -> list[dict]:
    out = []
    for code in qa_reason_codes(reason):
        key = code.upper()
        if key.startswith("CORRECTION_ERROR:"):
            label, sev = f"correction {code.split(':', 1)[1]} failed; no values", "BAD"
        else:
            label, sev = QA_REASON_CODES.get(key, (None, None))
        out.append({"code": code, "label": label, "severity": sev, "known": label is not None})
    return out


def incl_row(row: dict) -> dict:
    r = {INCL_COLS.get(k, k): v for k, v in row.items()}
    codes = {x.upper() for x in qa_reason_codes(r.get("qa_reason"))}
    r["qc_checksum_ok"] = not (codes & CHECKSUM_CODES)
    ids = r.get("correction_record_ids") or []
    r["correction_record_ids"] = list(ids)
    # §5.5: until the reduce job uses the engine, values are uncorrected. A row that claims corrections cannot
    # be presented as uncorrected, and the app cannot verify the corrections, so it is flagged for the gate.
    r["correction_basis"] = "uncorrected" if not ids else "corrected_unverified"
    # The app's own proposed_bias stays suppressed (owner decision 2026-11-02). Only the engine's QA-020 result is
    # shown, at profile level (bias_qc), from qa_reason codes and unapplied ref.incl_correction proposal rows.
    r.pop("proposed_bias", None)
    return r


def _parse_statistic(s) -> dict:
    out = {}
    for part in str(s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


PROPOSAL_STATUSES = ("PROPOSED", "SUSPECT_CROSSCHECK")   # statuses that carry an engine proposal record (QA021-v2)


VERDICT_ORDER = {"AGREE": 0, "SUSPECT": 1}


def _f(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


APPROVAL_STATUSES = ("proposed", "approved", "superseded")   # api_contract §4.1; the app never adds a value
APPLIED_WITHOUT_REVIEW = "APPLIED_WITHOUT_VALID_REVIEW"


class DataIntegrityError(ValueError):
    """A stored record violates the contract (QA-036): e.g. ref.incl_correction applied=true without an independent
    review, which ck_incl_correction_applied_reviewed (02 DDL, v1.5) makes unstorable. Never mapped to a status."""

    def __init__(self, code: str, record_id):
        super().__init__(f"{code}: ref.incl_correction {record_id}")
        self.code, self.record_id = code, record_id


def _reviewed(rec: dict) -> bool:
    rb = rec.get("reviewed_by")
    return bool(rb) and rb != rec.get("user") and bool(rec.get("reviewed_utc"))


def approval_status(rec: dict) -> str:
    """api_contract §4.1 / SCHEMA_CONTRACT §5.5: superseded (superseded_by set) > approved (applied AND reviewed_by
    set AND reviewed_by != user AND reviewed_utc set) > proposed (applied=false). Anything else is not a status: an
    applied record without that review raises DataIntegrityError (QA-036) instead of being given an invented status."""
    if rec.get("applied") is True and not _reviewed(rec):
        raise DataIntegrityError(APPLIED_WITHOUT_REVIEW, rec.get("record_id"))
    if rec.get("superseded_by"):
        return "superseded"
    return "approved" if rec.get("applied") is True else "proposed"


def bias_proposal(rec: dict) -> dict:
    """One ref.incl_correction bias QC record -> api_contract §4.1 fields. UNVERIFIED display only; the app applies
    nothing (an approved record is applied by the engine, and corrected rows are still withheld by the gate).
    A record that violates the approval rule stays listed (the anomaly is visible) but as a data-integrity error:
    approval_status null, every value and the evidence withheld."""
    st = _parse_statistic(rec.get("detection_statistic"))
    try:
        appr, integrity = approval_status(rec), None
    except DataIntegrityError as e:
        log.error("data integrity: %s", e)
        appr, integrity = None, e.code
    za = _f(st.get("ZA"))
    out = {
        "record_id": rec.get("record_id"), "axis": (rec.get("axis") or "").upper() or None,
        "za": za if za is not None else _f(rec.get("auto_value")),
        "za_unit": "reading units (ZA/ZB; D' = D + ZA)",
        "abs_dD": _f(st.get("abs_dD")), "abs_bs": _f(st.get("abs_bs")),
        "crosscheck_verdict": st.get("crosscheck_verdict"),
        "crosscheck_diff": _f(st.get("crosscheck_diff")), "crosscheck_tol": _f(st.get("crosscheck_tol")),
        "status": st.get("status"), "rule": st.get("rule"),
        "requires_review": st.get("requires_review") == "True",
        "approval_status": appr, "integrity_error": integrity,
        "user": rec.get("user"),
        "reviewed_by": rec.get("reviewed_by"), "reviewed_utc": rec.get("reviewed_utc"),
        "timestamp_utc": rec.get("timestamp_utc"),
        "evidence": st, "display_status": "UNVERIFIED", "applied_by_app": False,
    }
    if integrity:
        out.update({k: None for k in ("za", "abs_dD", "abs_bs", "crosscheck_diff", "crosscheck_tol")},
                   evidence={}, display_status="DATA_INTEGRITY_ERROR")
    return out


def bias_proposals(recs: list[dict]) -> list[dict]:
    """PROPOSED and SUSPECT_CROSSCHECK records (§4.1), AGREE verdicts first, then newest first, then record_id."""
    out = [bias_proposal(r) for r in recs if r.get("detection_method") == "between_set_bias_qc"]
    out = [p for p in out if p["status"] in PROPOSAL_STATUSES]
    out.sort(key=lambda p: str(p["record_id"]))
    out.sort(key=lambda p: str(p["timestamp_utc"] or ""), reverse=True)
    out.sort(key=lambda p: VERDICT_ORDER.get(p["crosscheck_verdict"], 2))
    return out


def bias_qc(rows: list[dict], proposals: list[dict]) -> dict:
    """Per-axis QA-020 summary for one survey, from the engine's qa_reason codes (silver.incl_reduced, §5.5 v1.3)
    and its unapplied proposal rows (ref.incl_correction, detection_method='between_set_bias_qc', applied=false).

    Everything here is UNVERIFIED and never applied: proposed_c is display-only and is never used to change a value.
    A proposal row that claims applied=true is not a proposal and is ignored here (applied corrections go through
    correction_record_ids and are withheld as CORRECTION_UNVERIFIED)."""
    codes = {c.upper() for r in rows for c in qa_reason_codes(r.get("qa_reason"))}
    out = {}
    for ax in ("A", "B"):
        status = ("BAD_RESURVEY" if f"BIAS_BAD_RESURVEY_{ax}" in codes else
                  "SUSPECT_CROSSCHECK" if f"BIAS_CROSSCHECK_{ax}" in codes else
                  "PROPOSED" if f"BIAS_PROPOSED_{ax}" in codes else None)
        props = [p for p in proposals if (p.get("axis") or "").upper() == ax and p.get("applied") is False
                 and p.get("detection_method") == "between_set_bias_qc" and not p.get("superseded_by")]
        props.sort(key=lambda p: (str(p.get("timestamp_utc") or ""), str(p.get("record_id"))))
        p = props[-1] if props else None
        c = p.get("auto_value") if p else None
        # QA-030: a SUSPECT_CROSSCHECK result is a proposal ranked SUSPECT (QA-021-v2), not a rejection; the reviewer
        # must see its ZA, record and evidence. Still UNVERIFIED, never applied, never changes a value.
        shown = p is not None and status in PROPOSAL_STATUSES
        out[ax] = {
            "suspect_flag": f"BIAS_CS_SHIFT_{ax}" in codes,
            "status": status,                       # None = the engine reported no QA-020 code for this axis
            "proposed_c": float(c) if c is not None and shown else None,
            "proposed_c_unit": "reading units (ZA/ZB; D' = D + ZA)",
            "proposal_record_id": p.get("record_id") if shown else None,
            "evidence": _parse_statistic(p.get("detection_statistic")) if shown else {},
            "ranking": "SUSPECT" if status == "SUSPECT_CROSSCHECK" else ("OK" if status == "PROPOSED" else None),
            "display_status": "UNVERIFIED",
            "applied": False,
            # api_contract §4.1: every PROPOSED / SUSPECT_CROSSCHECK record for this axis with its approval_status
            "proposals": [q for q in bias_proposals(proposals) if q["axis"] == ax],
        }
    return {"axes": out, "stable_zone_assumed": "STABLE_ZONE_ASSUMED" in codes, "note": BIAS_PROPOSAL_NOTE}


def incl_api_row(row: dict) -> dict:
    """Internal (gated) profile row -> contract column names for the API (§5.5: a_cum_mm -> cum_a, ...)."""
    return {INCL_API.get(k, k): v for k, v in row.items()}


def incl_correction(row: dict) -> dict:
    """ref.incl_correction row -> exactly the 23 audit fields in contract order (+ review / v1.5 fields if present)."""
    missing = [k for k in INCL_CORRECTION_FIELDS if k not in row]
    out = {k: row.get(k) for k in INCL_CORRECTION_FIELDS}
    for k in ("reviewed_by", "reviewed_utc", "supersedes", "review_id"):   # review / v1.5 versioning columns
        if k in row:
            out[k] = row[k]
    out["audit_complete"] = not missing
    out["missing_audit_fields"] = missing
    return out


# ---------------------------------------------------------------- alarms (§4, §5 v1.1 lineage)
EPISODE_LINEAGE = ("reading_id", "cal_version", "calib_id", "formula_version", "raise_reading_id",
                   "raise_cal_version", "mixed_calibration")


def alarm_event(row: dict) -> dict:
    """Row from ops.alarm_episode LEFT JOIN ref.threshold (id + version) LEFT JOIN ops.reading (reading_id, only
    for the engineer's erroneous mark).

    §4/§5 v1.4: the episode itself carries formula_id, formula_version and qa_flag (no app-side join). Its value
    is never NULL for a reading-driven episode; a NULL is still withheld by the gate (defensive)."""
    r = threshold({**row, "value": row.get("value_limit")})  # episode `value` is the measured value
    r["limit"] = row.get("value_limit")
    r["value"] = row.get("value")
    for k in EPISODE_LINEAGE:
        r[k] = row.get(k)
    r["mixed_calibration"] = bool(row.get("mixed_calibration"))
    r["formula_id"] = row.get("formula_id")
    r["lineage_issue"] = None
    qc = row.get("qa_flag")
    if row.get("reading_is_erroneous"):
        qc = "BAD"
    r["qa_flag"] = qc
    r["qa_reason"] = (join_reasons(row.get("qa_reason"), "ERRONEOUS_MARK") if row.get("reading_is_erroneous")
                      else row.get("qa_reason"))
    r.pop("reading_is_erroneous", None)
    r["message"] = (f"{r.get('instrument_id')} L{r.get('level')} {r.get('rule')} limit "
                    f"{r.get('limit')} {r.get('unit') or ''} ({r.get('machine_state')})").strip()
    return r


# ---------------------------------------------------------------- readings
def join_reasons(*parts: Any) -> str | None:
    """§3: qa_reason is a comma-joined code list."""
    out = [str(p) for p in parts if p]
    return ",".join(out) if out else None


def reading(row: dict) -> dict:
    """Row from the FERC-canvas contract view ops.reading -> app reading model.
    An engineer's erroneous mark (ops.reading_erroneous_mark) forces qa_flag BAD so the display
    gate withholds the value; the mark reason is kept in qa_reason (comma-joined, §3)."""
    r = dict(row)
    r["ts_utc"] = r.pop("reading_ts", r.get("ts_utc"))
    r["corrected_value"] = r.pop("value", r.get("corrected_value"))
    r["corrected_unit"] = r.pop("unit", r.get("corrected_unit"))
    r["qa_flag"] = r.pop("qc_flag", r.get("qa_flag"))
    r["qa_reason"] = r.pop("qc_reason", r.get("qa_reason"))
    if r.get("is_erroneous"):
        r["qa_flag"] = "BAD"
        r["qa_reason"] = join_reasons(r.get("qa_reason"), "ERRONEOUS_MARK", r.get("erroneous_reason"))
    return r


def formula(row: dict) -> dict:
    r = dict(row)
    r.setdefault("unit", r.get("output_unit"))
    r.setdefault("output_name", r.get("reading_type"))
    r.setdefault("required_params", [])
    r.setdefault("inputs", [])
    return r
