from conftest import DSO, ENG, VIEWER


def test_registry(rc):
    r = rc.get("/api/dams/STC/instruments", user=DSO).json()
    pz3 = next(i for i in r["instruments"] if i["instrument_id"] == "STC-PZ-3")
    assert pz3["current_calibration"]["approved"] is False
    assert pz3["latest"]["display_status"] == "UNAPPROVED_CALIBRATION"
    assert pz3["latest"]["corrected_value"] is None
    assert rc.get("/api/dams/STC/instruments", user=ENG).status_code == 403
    assert rc.get("/api/dams/ZZZ/instruments").status_code == 404


def test_detail(rc):
    r = rc.get("/api/instruments/BFK-PZ-1", user=VIEWER).json()
    assert [c["cal_version"] for c in r["calibrations"]] == [1, 2]
    assert r["formula"]["verified"]
    assert rc.get("/api/instruments/NOPE").status_code == 404
    assert rc.get("/api/instruments/CRD-PZ-2", user=VIEWER).status_code == 403


def test_timeseries_lineage_units_and_qc(rc):
    r = rc.get("/api/instruments/CRD-PZ-2/timeseries").json()
    assert r["unit"] == "m"
    for p in r["points"]:
        for k in ("unit", "eng_unit", "cal_version", "calib_id", "qa_flag", "display_status", "formula_version"):
            assert k in p
    assert r["summary"]["n_withheld"] == 0 and r["summary"]["n"] == len(r["points"])


def test_timeseries_bad_spike_withheld(rc):
    r = rc.get("/api/instruments/HLD-PZ-1/timeseries").json()
    bad = [p for p in r["points"] if p["qa_flag"] == "BAD"]
    assert len(bad) == 1 and bad[0]["value"] is None and bad[0]["display_status"] == "QC_BAD"
    assert bad[0]["raw_value"] is not None  # raw is always passed through
    assert r["summary"]["n_withheld"] == 1


def test_timeseries_unverified_and_unapproved(rc):
    r = rc.get("/api/instruments/CRD-SM-1/timeseries").json()
    assert all(p["value"] is None and p["display_status"] == "UNVERIFIED_FORMULA" for p in r["points"])
    r = rc.get("/api/instruments/STC-PZ-3/timeseries").json()
    assert all(p["value"] is None and p["display_status"] == "UNAPPROVED_CALIBRATION" for p in r["points"])


def test_timeseries_recalibration(rc):
    r = rc.get("/api/instruments/BFK-PZ-1/timeseries").json()
    vers = [p["cal_version"] for p in r["points"]]
    assert vers == sorted(vers) and set(vers) == {1, 2}
    assert {c["cal_version"] for c in r["calibration_versions"]} == {1, 2}


def test_timeseries_window_and_incl_rejected(rc):
    full = rc.get("/api/instruments/CRD-PZ-2/timeseries").json()["points"]
    start, end = full[10]["ts_utc"], full[20]["ts_utc"]
    part = rc.get(f"/api/instruments/CRD-PZ-2/timeseries?start={start}&end={end}").json()["points"]
    assert len(part) == 11 and part[0]["ts_utc"] == start
    assert rc.get("/api/instruments/CRD-IN-1/timeseries").status_code == 400


def test_incl_surveys_and_profile(rc):
    sv = rc.get("/api/instruments/CRD-IN-1/incl/surveys").json()["surveys"]
    assert sv[0]["is_baseline"]
    ids = ",".join(s["survey_id"] for s in sv[-2:])
    r = rc.get(f"/api/instruments/CRD-IN-1/incl/profile?survey_ids={ids}").json()
    assert len(r["profiles"]) == 2
    p = r["profiles"][-1]
    assert p["unit"] == "mm" and p["cal_version"] == 1 and p["bias_applied"] is False
    # bias proposal suppressed from API output (QA-020 owner decision): not at profile level, not in any row
    assert all("proposed_bias" not in q and all("proposed_bias" not in x for x in q["rows"]) for q in r["profiles"])
    assert set(p["display_status"]) == {"VERIFIED"}
    assert max(abs(x["a_cum_mm"]) for x in p["rows"]) > 20  # CRD-IN-1 L3 movement
    assert rc.get("/api/instruments/CRD-PZ-2/incl/surveys").status_code == 400
    assert rc.get("/api/instruments/CRD-PZ-2/incl/profile?survey_ids=x").status_code == 400
    assert rc.get("/api/instruments/CRD-IN-1/incl/profile?survey_ids=NOPE").status_code == 404


def test_formula_check(rc):
    r = rc.get("/api/formulas/WEIR_VNOTCH/1.0.0/check").json()
    assert r["verified"] and all(x["pass"] for x in r["results"])
    r = rc.get("/api/formulas/SETTLEMENT/1.0.0/check").json()
    assert r["verified"] is False and r["results"] == []
    assert rc.get("/api/formulas/NOPE/1.0.0/check").json()["known"] is False
