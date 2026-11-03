"""SCHEMA_CONTRACT v1.0 adapter (warehouse column names -> app model)."""
from backend.repos import contract as ct
from backend.verify import Gate


def test_calibration_params_from_string_map():
    c = ct.calibration({"calib_id": "X-C1", "certificate_uri": "/Volumes/c.pdf",
                        "params": {"G": "-0.1795", "R0": "9082", "method": "linear", "vented": "false",
                                   "top_el_m": "200.5", "tip_elev_m": "1", "tip_el_m": "2"}})
    p = c["params"]
    assert p["G"] == -0.1795 and p["R0"] == 9082.0 and p["vented"] is False and p["method"] == "linear"
    assert p["top_elev_m"] == 200.5 and p["tip_elev_m"] == 1  # explicit app spelling wins
    assert c["method"] == "linear" and c["certificate"] == "/Volumes/c.pdf"
    c = ct.calibration({"calib_id": "Y", "params": None})
    assert c["params"] == {} and c["method"] is None


def test_instrument_mapping():
    i = ct.instrument({"instrument_id": "A", "instrument_type": "VW_PIEZOMETER", "subtype": None,
                       "tip_el_m": 180.0, "top_el_m": None, "output_unit": "kPa"})
    assert (i["instrument_type"], i["subtype"], i["tip_elev_m"], i["eng_unit"]) == ("piezometer", "vibrating_wire", 180.0, "kPa")
    assert i["source_instrument_type"] == "VW_PIEZOMETER"
    i = ct.instrument({"instrument_id": "B", "instrument_type": "Thermometer", "subtype": "x"})
    assert i["instrument_type"] == "thermometer" and i["subtype"] == "x"
    assert ct.instrument({"instrument_id": "C", "instrument_type": None})["instrument_type"] is None


def test_threshold_mapping():
    up = ct.threshold({"limit_type": "magnitude", "direction": "upper", "value": 5.0, "pfm_id": "P1",
                       "required_actions": "verify"})
    assert (up["rule"], up["upper_limit"], up["lower_limit"], up["pfm_ids"], up["response_action"]) == \
        ("value_above", 5.0, None, ["P1"], "verify")
    lo = ct.threshold({"limit_type": "MAGNITUDE", "direction": "lower", "value": 2.0, "pfm_id": None})
    assert lo["rule"] == "value_below" and lo["lower_limit"] == 2.0 and lo["pfm_ids"] == []
    rt = ct.threshold({"limit_type": "rate", "direction": "upper", "value": 0.3})
    assert rt["rule"] == "rate_above" and rt["rate_limit_per_day"] == 0.3
    cy = ct.threshold({"limit_type": "cyclic", "direction": "abs", "value": 1})
    assert cy["rule"] == "unsupported:cyclic/abs" and cy["upper_limit"] is None


def test_pfm_incl_formula_mapping():
    p = ct.pfm({"pfm_id": "P", "pfm_code": "PFM-1", "legacy_ch14_category": "II", "instruments": ["b", "a"]})
    assert p["title"] == "PFM-1" and p["category"] == "II" and p["instruments"] == ["a", "b"]
    assert ct.pfm({"pfm_id": "Q", "instruments": None})["title"] == "Q"
    r = ct.incl_row({"cum_a": 3.0, "cum_b": 4.0, "resultant": 5.0, "inc_a": 1, "checksum_a": 20,
                     "qa_reason": "CHECKSUM;SPIKE"})
    assert r["a_cum_mm"] == 3.0 and r["resultant_mm"] == 5.0 and r["a_chk"] == 20 and r["qc_checksum_ok"] is False
    assert ct.incl_row({"qa_reason": None})["qc_checksum_ok"] is True
    assert "proposed_bias" not in ct.incl_row({"qa_reason": None, "proposed_bias": {"a_bias_mm_per_interval": 1}})
    f = ct.formula({"formula_id": "F", "formula_version": "1.0.0", "output_unit": "kPa", "reading_type": "vw"})
    assert f["unit"] == "kPa" and f["required_params"] == []


def test_alarm_episode_without_lineage_is_withheld():
    e = ct.alarm_event({"event_id": "T1", "instrument_id": "I", "value": 9.9, "value_limit": 9.0, "level": 2,
                        "limit_type": "magnitude", "direction": "upper", "unit": "m", "machine_state": "ACTION_UNACK"})
    assert e["limit"] == 9.0 and e["upper_limit"] == 9.0 and e["formula_id"] is None
    assert "ACTION_UNACK" in e["message"]
    g = Gate([{"formula_id": "F", "formula_version": "1", "verified": True}], [{"calib_id": "C", "approved_by": "x"}])
    out = g.apply_event(e)
    assert out["value"] is None and out["display_status"] == "UNVERIFIED_FORMULA"


def test_retired_calibration_is_not_approved():
    g = Gate([{"formula_id": "F", "formula_version": "1", "verified": True}],
             [{"calib_id": "C", "approved_by": "x", "is_retired": True}])
    assert g.status({"formula_id": "F", "formula_version": "1", "calib_id": "C"}) == "UNAPPROVED_CALIBRATION"


def test_calibration_sign_mult_offset_columns_win():
    r = ct.calibration({"params": {"G": "0.1795", "sign_mult": "1"}, "sign_mult": -1, "offset": "0.5"})
    assert r["params"]["sign_mult"] == -1 and r["params"]["offset"] == 0.5 and r["params"]["G"] == 0.1795
