"""SCHEMA_CONTRACT v1.1 adapter (warehouse column names -> app model)."""
from backend.repos import contract as ct
from backend.verify import Gate

F = [{"formula_id": "F", "formula_version": "1", "verified": True}]


def test_calibration_params_from_string_map():
    c = ct.calibration({"calib_id": "X-C1", "certificate_uri": "/Volumes/c.pdf", "sign_mult": "-1", "offset": "0.5",
                        "params": {"G": "0.1795", "R0": "9082", "method": "linear", "vented": "false",
                                   "top_el_m": "200.5", "tip_elev_m": "1", "tip_el_m": "2"}})
    p = c["params"]
    assert p["G"] == 0.1795 and p["R0"] == 9082.0 and p["vented"] is False and p["method"] == "linear"
    assert p["top_elev_m"] == 200.5 and p["tip_elev_m"] == 1  # explicit app spelling wins
    assert c["method"] == "linear" and c["certificate_uri"] == "/Volumes/c.pdf" and "certificate" not in c
    assert (c["sign_mult"], c["offset"], c["calibration_issues"]) == (-1, 0.5, [])
    c = ct.calibration({"calib_id": "Y", "params": None})
    assert c["params"] == {} and c["method"] is None and (c["sign_mult"], c["offset"]) == (1, 0.0)
    assert ct.calibration({"params": {}, "method": "poly"})["method"] == "poly"   # method column fallback


def test_calibration_sign_mult_offset_are_columns_only():
    """§10 Q3: sign_mult / offset in params are ignored, reported, and block approval for display."""
    r = ct.calibration({"calib_id": "C", "approved_by": "x", "params": {"G": "0.1795", "sign_mult": "1", "offset": "3"},
                        "sign_mult": -1, "offset": "0.5"})
    assert "sign_mult" not in r["params"] and "offset" not in r["params"] and r["params"]["G"] == 0.1795
    assert r["sign_mult"] == -1 and r["offset"] == 0.5 and r["params_ignored"] == ["sign_mult", "offset"]
    assert len(r["calibration_issues"]) == 2
    assert ct.formula_inputs(r, R1=1)["sign_mult"] == -1 and ct.formula_inputs(r)["offset"] == 0.5
    g = Gate(F, [r])
    assert g.status({"formula_id": "F", "formula_version": "1", "calib_id": "C", "qa_flag": "OK"}) == \
        "UNAPPROVED_CALIBRATION"
    bad = ct.calibration({"params": {"method": "cubic"}, "sign_mult": 2, "offset": "x"})
    assert len(bad["calibration_issues"]) == 3 and bad["sign_mult"] == 2 and bad["offset"] == "x"


def test_instrument_mapping_and_types():
    i = ct.instrument({"instrument_id": "A", "instrument_type": "VW_PIEZOMETER", "subtype": None,
                       "tip_el_m": 180.0, "top_el_m": None, "output_unit": "kPa"})
    assert (i["instrument_type"], i["subtype"], i["tip_el_m"], i["eng_unit"]) == ("piezometer", "vibrating_wire", 180.0, "kPa")
    assert i["source_instrument_type"] == "VW_PIEZOMETER" and i["computable"] and i["instrument_type_known"]
    i = ct.instrument({"instrument_id": "B", "instrument_type": "Thermometer", "subtype": "x"})
    assert i["instrument_type"] == "thermometer" and i["subtype"] == "x" and not i["computable"]
    assert ct.instrument({"instrument_id": "C", "instrument_type": None})["instrument_type"] is None
    u = ct.instrument({"instrument_id": "D", "instrument_type": "Mystery"})
    assert u["instrument_type"] == "mystery" and not u["instrument_type_known"] and not u["computable"]
    assert len(ct.INSTRUMENT_TYPES) == 14 and not ct.type_is_computable("strain_gauge")


def test_threshold_mapping():
    up = ct.threshold({"limit_type": "magnitude", "direction": "upper", "value": 5.0, "pfm_id": "P1",
                       "required_actions": "verify"})
    assert (up["rule"], up["upper_limit"], up["lower_limit"], up["pfm_ids"], up["response_action"], up["evaluated"]) == \
        ("value_above", 5.0, None, ["P1"], "verify", True)
    lo = ct.threshold({"limit_type": "MAGNITUDE", "direction": "lower", "value": 2.0, "pfm_id": None})
    assert lo["rule"] == "value_below" and lo["lower_limit"] == 2.0 and lo["pfm_ids"] == []
    ab = ct.threshold({"limit_type": "magnitude", "direction": "abs", "value": 3.0})
    assert ab["rule"] == "abs_above" and ab["abs_limit"] == 3.0 and ab["evaluated"] and "|value|" in ab["rule_label"]
    rt = ct.threshold({"limit_type": "rate", "direction": "upper", "value": 0.3})
    assert rt["rule"] == "rate_above" and rt["rate_limit_per_day"] == 0.3
    cy = ct.threshold({"limit_type": "cyclic", "direction": "abs", "value": 1})
    assert cy["rule"] == "cyclic" and cy["evaluated"] is False and "not evaluated" in cy["rule_label"]
    assert cy["upper_limit"] is None and cy["abs_limit"] is None
    legacy = ct.threshold({"rule": "value_above", "upper_limit": 4.0, "pfm_ids": ["P"], "response_action": "a"})
    assert (legacy["limit_type"], legacy["direction"], legacy["value"], legacy["pfm_id"], legacy["required_actions"]) == \
        ("magnitude", "upper", 4.0, "P", "a")
    assert ct.threshold({"limit_type": "weird"})["evaluated"] is False


def test_pfm_incl_formula_mapping():
    p = ct.pfm({"pfm_id": "P", "pfm_code": "PFM-1", "legacy_ch14_category": "II", "instruments": ["b", "a"],
                "title": "old", "category": "I"})
    assert p["pfm_code"] == "PFM-1" and p["legacy_ch14_category"] == "II" and p["instruments"] == ["a", "b"]
    assert "title" not in p and "category" not in p and all(k in p for k in ct.PFM_FIELDS)
    r = ct.incl_row({"cum_a": 3.0, "cum_b": 4.0, "resultant": 5.0, "inc_a": 1, "checksum_a": 20,
                     "qa_reason": "CHECKSUM,SPIKE"})
    assert r["a_cum_mm"] == 3.0 and r["resultant_mm"] == 5.0 and r["a_chk"] == 20 and r["qc_checksum_ok"] is False
    assert r["correction_basis"] == "uncorrected"
    assert ct.incl_row({"qa_reason": None})["qc_checksum_ok"] is True
    assert "proposed_bias" not in ct.incl_row({"qa_reason": None, "proposed_bias": {"a_bias_mm_per_interval": 1}})
    api = ct.incl_api_row(r)
    assert api["cum_a"] == 3.0 and api["resultant"] == 5.0 and "a_cum_mm" not in api
    f = ct.formula({"formula_id": "F", "formula_version": "1.0.0", "output_unit": "kPa", "reading_type": "vw"})
    assert f["unit"] == "kPa" and f["required_params"] == []


def test_incl_correction_rows_withheld_and_audit_fields():
    r = ct.incl_row({"cum_a": 3.0, "correction_record_ids": ["R1"], "formula_id": "F", "formula_version": "1",
                     "calib_id": "C", "qa_flag": "OK"})
    assert r["correction_basis"] == "corrected_unverified"
    g = Gate(F, [{"calib_id": "C", "approved_by": "x"}])
    out = g.apply(r, ("a_cum_mm",))
    assert out["a_cum_mm"] is None and out["display_status"] == ct.CORRECTION_UNVERIFIED
    assert len(ct.INCL_CORRECTION_FIELDS) == 23
    full = ct.incl_correction({k: k for k in ct.INCL_CORRECTION_FIELDS} | {"reviewed_by": "pe", "extra": 1})
    assert full["audit_complete"] and full["reviewed_by"] == "pe" and "extra" not in full
    part = ct.incl_correction({"record_id": "R1"})
    assert not part["audit_complete"] and len(part["missing_audit_fields"]) == 22


def test_alarm_episode_without_lineage_is_withheld():
    e = ct.alarm_event({"event_id": "T1", "instrument_id": "I", "value": 9.9, "value_limit": 9.0, "level": 2,
                        "limit_type": "magnitude", "direction": "upper", "unit": "m", "machine_state": "ACTION_UNACK"})
    assert e["limit"] == 9.0 and e["upper_limit"] == 9.0 and e["formula_id"] is None
    assert "ACTION_UNACK" in e["message"]
    g = Gate(F, [{"calib_id": "C", "approved_by": "x"}])
    out = g.apply_event(e)
    assert out["value"] is None and out["display_status"] == "LINEAGE_INCOMPLETE"


def test_alarm_episode_with_lineage_is_shown():
    """v1.4 §4/§5: ops.alarm_episode carries formula_id and qa_flag itself; no calibration / reading-qc join."""
    row = {"event_id": "T2", "instrument_id": "I", "value": 9.9, "value_limit": 9.0, "level": 1, "unit": "m",
           "limit_type": "magnitude", "direction": "upper", "reading_id": "R9", "raise_reading_id": "R1",
           "cal_version": 2, "raise_cal_version": 1, "mixed_calibration": True, "calib_id": "C",
           "formula_id": "F", "formula_version": "1", "qa_flag": "OK", "qa_reason": None}
    e = ct.alarm_event(row)
    assert (e["formula_id"], e["lineage_issue"], e["mixed_calibration"], e["raise_reading_id"]) == ("F", None, True, "R1")
    assert e["qa_flag"] == "OK" and "reading_is_erroneous" not in e
    g = Gate(F, [{"calib_id": "C", "approved_by": "x"}])
    out = g.apply_event(dict(e))
    assert out["value"] == 9.9 and out["display_status"] == "VERIFIED"
    # legacy join columns are ignored: formula_id / qa_flag come only from the episode
    legacy = ct.alarm_event({**row, "formula_id": None, "cal_formula_id": "F", "cal_formula_version": "1",
                             "reading_qc_flag": "OK"})
    assert legacy["formula_id"] is None and g.apply_event(legacy)["display_status"] == "LINEAGE_INCOMPLETE"
    assert ct.alarm_event({**row, "qa_flag": None, "reading_qc_flag": "BAD"})["qa_flag"] is None
    err = ct.alarm_event({**row, "reading_is_erroneous": True})
    assert err["qa_flag"] == "BAD" and "ERRONEOUS_MARK" in err["qa_reason"]
    assert g.apply_event(err)["display_status"] == "QC_BAD"
    assert g.apply_event(ct.alarm_event({**row, "qa_flag": "MISSING"}))["display_status"] == "MISSING"
    assert g.apply_event(ct.alarm_event({**row, "qa_flag": "BAD"}))["display_status"] == "QC_BAD"


def test_alarm_episode_null_value_is_withheld_defensively():
    """v1.4 §4: value is never NULL for a reading-driven episode; if one arrives anyway it is never VERIFIED."""
    row = {"event_id": "T3", "instrument_id": "I", "value": None, "value_limit": 9.0, "level": 1, "unit": "m",
           "limit_type": "magnitude", "direction": "upper", "calib_id": "C", "formula_id": "F",
           "formula_version": "1", "qa_flag": "OK"}
    out = Gate(F, [{"calib_id": "C", "approved_by": "x"}]).apply_event(ct.alarm_event(row))
    assert out["display_status"] == "MISSING" and out["value"] is None and out["verified"] is False
    assert "withheld" in out["message"]


def test_sql_alarm_query_has_no_formula_or_qc_join():
    from backend.repos.analytics import SqlAnalytics
    sql = SqlAnalytics.__new__(SqlAnalytics)
    sql.t = lambda layer, name: f"{layer}.{name}"
    q = sql._alarm_sql("TRUE")
    assert "ref.calibration" not in q and "qc_flag" not in q and "cal_formula" not in q
    assert "ops.alarm_episode e" in q and "r.is_erroneous" in q


def test_retired_calibration_is_not_approved():
    g = Gate(F, [{"calib_id": "C", "approved_by": "x", "is_retired": True}])
    assert g.status({"formula_id": "F", "formula_version": "1", "calib_id": "C"}) == "UNAPPROVED_CALIBRATION"


def test_join_reasons_comma():
    assert ct.join_reasons("SPIKE", None, "DRY") == "SPIKE,DRY" and ct.join_reasons(None, "") is None


def test_qa_reason_codes_v13():
    from backend.repos import contract as ct
    assert ct.qa_reason_codes(" BAD:TEXT, ,MEAN_CS_GT_100_A,SPIRAL_NOT_APPLIED ") == \
        ["BAD:TEXT", "MEAN_CS_GT_100_A", "SPIRAL_NOT_APPLIED"]
    assert ct.qa_reason_codes(None) == []
    d = {x["code"]: x for x in ct.qa_reason_detail("CORRECTION_ERROR:rotation,CORRECTION_RECORD_INVALID,NEW_CODE,"
                                                    "MISSING:PASS,BAD:RANGE,INDIVIDUAL_CS_B,BIAS_CS_SHIFT_B,BIAS_CROSSCHECK_A")}
    assert d["BIAS_CROSSCHECK_A"]["severity"] == "SUSPECT"
    q = ct.bias_qc([{"qa_reason": "BIAS_CROSSCHECK_A"}], [{"axis": "A", "applied": False, "auto_value": 1.0,
                                                          "detection_method": "between_set_bias_qc"}])
    # QA-030: a SUSPECT_CROSSCHECK proposal shows its ZA, record and evidence (UNVERIFIED, not applied)
    a = q["axes"]["A"]
    assert a["status"] == "SUSPECT_CROSSCHECK" and a["proposed_c"] == 1.0 and a["ranking"] == "SUSPECT"
    assert a["applied"] is False and a["display_status"] == "UNVERIFIED"
    q = ct.bias_qc([{"qa_reason": "BIAS_PROPOSED_A,BIAS_CROSSCHECK_A"}],
                   [{"record_id": "R9", "axis": "A", "applied": False, "auto_value": -6.0,
                     "detection_method": "between_set_bias_qc", "detection_statistic": "n=6;se=0.1"}])["axes"]["A"]
    assert (q["status"], q["proposed_c"], q["proposal_record_id"], q["evidence"]) == \
        ("SUSPECT_CROSSCHECK", -6.0, "R9", {"n": "6", "se": "0.1"})
    # no proposal record -> nothing to show; NO code / BAD_RESURVEY never shows a c even if a record exists
    assert ct.bias_qc([{"qa_reason": "BIAS_CROSSCHECK_A"}], [])["axes"]["A"]["proposed_c"] is None
    r = ct.bias_qc([{"qa_reason": "BIAS_BAD_RESURVEY_A"}], [{"record_id": "R1", "axis": "A", "applied": False,
                                                           "auto_value": 30.0, "detection_method": "between_set_bias_qc"}])
    assert r["axes"]["A"]["proposed_c"] is None and r["axes"]["A"]["proposal_record_id"] is None
    assert "no proposal" not in ct.QA_REASON_CODES["BIAS_CROSSCHECK_A"][0]
    assert d["CORRECTION_ERROR:rotation"]["severity"] == "BAD" and "rotation" in d["CORRECTION_ERROR:rotation"]["label"]
    assert d["CORRECTION_RECORD_INVALID"]["severity"] == "SUSPECT"
    assert d["NEW_CODE"] == {"code": "NEW_CODE", "label": None, "severity": None, "known": False}  # shown verbatim
    assert d["MISSING:PASS"]["severity"] == "MISSING" and d["BAD:RANGE"]["severity"] == "BAD"
    # checksum-type codes from the v1.3 writer mark the row's checksum as not OK; bias/info codes do not
    assert ct.incl_row({"qa_reason": "INDIVIDUAL_CS_A"})["qc_checksum_ok"] is False
    assert ct.incl_row({"qa_reason": "MEAN_CS_GT_100_B"})["qc_checksum_ok"] is False
    assert ct.incl_row({"qa_reason": "BIAS_PROPOSED_A,SPIRAL_NOT_APPLIED"})["qc_checksum_ok"] is True
    assert ct._parse_statistic("n=5;bad;x = 1") == {"n": "5", "x": "1"}
