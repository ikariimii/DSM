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
    assert p["unit"] == "mm" and p["cal_version"] is None and p["bias_applied"] is False
    # bias proposal suppressed from API output (QA-020 owner decision): not at profile level, not in any row
    assert all("proposed_bias" not in q and all("proposed_bias" not in x for x in q["rows"]) for q in r["profiles"])
    assert set(p["display_status"]) == {"VERIFIED"}
    assert max(abs(x["cum_a"]) for x in p["rows"]) > 20  # CRD-IN-1 L3 movement (contract name, §5.5)
    assert all("a_cum_mm" not in x and {"cum_b", "inc_a", "resultant"} <= set(x) for x in p["rows"])
    assert p["correction_basis"] == ["uncorrected"] and "Uncorrected" in p["correction_note"]
    assert p["corrections"] == []
    assert rc.get("/api/instruments/CRD-PZ-2/incl/surveys").status_code == 400
    assert rc.get("/api/instruments/CRD-PZ-2/incl/profile?survey_ids=x").status_code == 400
    assert rc.get("/api/instruments/CRD-IN-1/incl/profile?survey_ids=NOPE").status_code == 404


def test_formula_check(rc):
    r = rc.get("/api/formulas/WEIR_VNOTCH/1.0.0/check").json()
    assert r["verified"] and all(x["pass"] for x in r["results"])
    r = rc.get("/api/formulas/SETTLEMENT/1.0.0/check").json()
    assert r["verified"] is False and r["results"] == []
    assert rc.get("/api/formulas/NOPE/1.0.0/check").json()["known"] is False


def test_incl_rows_with_corrections_are_withheld(rc, monkeypatch):
    from backend.repos import analytics as an_mod
    from backend.repos import contract as ct
    orig = an_mod.MockAnalytics.incl_profile

    def with_corr(self, iid, sid):
        rows = orig(self, iid, sid)
        for r in rows:
            r.update(correction_record_ids=["RC-1"], correction_basis="corrected_unverified")
        return rows
    monkeypatch.setattr(an_mod.MockAnalytics, "incl_profile", with_corr)
    monkeypatch.setattr(an_mod.MockAnalytics, "incl_corrections",
                        lambda self, ids: [ct.incl_correction({"record_id": i}) for i in ids])
    sid = rc.get("/api/instruments/CRD-IN-1/incl/surveys").json()["surveys"][-1]["survey_id"]
    p = rc.get(f"/api/instruments/CRD-IN-1/incl/profile?survey_ids={sid}").json()["profiles"][0]
    assert set(p["display_status"]) == {"CORRECTION_UNVERIFIED"} and p["correction_basis"] == ["corrected_unverified"]
    assert all(x["cum_a"] is None and x["resultant"] is None for x in p["rows"])
    assert p["corrections"][0]["record_id"] == "RC-1" and p["corrections"][0]["audit_complete"] is False


def test_incl_bias_qc_shown_unverified_never_applied(rc, monkeypatch):
    """QA-020 (engine): suspect flag + proposed c are shown UNVERIFIED, never applied; values stay uncorrected."""
    from backend.repos import analytics as an_mod
    from backend.repos import contract as ct
    orig = an_mod.MockAnalytics.incl_profile
    sid = rc.get("/api/instruments/CRD-IN-1/incl/surveys").json()["surveys"][-1]["survey_id"]
    base = rc.get(f"/api/instruments/CRD-IN-1/incl/profile?survey_ids={sid}").json()["profiles"][0]
    assert base["bias_qc"]["axes"]["A"] == {**base["bias_qc"]["axes"]["A"], "suspect_flag": False, "status": None,
                                            "proposed_c": None, "applied": False, "display_status": "UNVERIFIED"}

    def flagged(self, iid, s):
        rows = orig(self, iid, s)
        for r in rows:
            r.update(qa_flag="SUSPECT", qa_reason="BIAS_CS_SHIFT_A,BIAS_PROPOSED_A,STABLE_ZONE_ASSUMED,BIAS_BAD_RESURVEY_B")
        return rows
    rec = {"record_id": "BP-1", "instrument_id": "CRD-IN-1", "survey_id": sid, "axis": "A", "correction_type": "bias",
           "parameter_name": "c", "auto_value": 3.4, "detection_method": "between_set_bias_qc",
           "detection_statistic": "n=12;mean_dD=3.4000;sd=1.1000;spread=3.0000;equal_opposite=False",
           "applied": False, "superseded_by": None, "timestamp_utc": "2026-11-02T00:00:00Z"}
    stale = {**rec, "record_id": "BP-0", "auto_value": 9.9, "timestamp_utc": "2026-11-01T00:00:00Z"}
    applied = {**rec, "record_id": "BP-X", "auto_value": 99.0, "applied": True, "timestamp_utc": "2026-11-03T00:00:00Z"}
    monkeypatch.setattr(an_mod.MockAnalytics, "incl_profile", flagged)
    monkeypatch.setattr(an_mod.MockAnalytics, "incl_bias_proposals",
                        lambda self, iid, s: [ct.incl_correction(x) for x in (rec, stale, applied)])
    p = rc.get(f"/api/instruments/CRD-IN-1/incl/profile?survey_ids={sid}").json()["profiles"][0]
    a, b = p["bias_qc"]["axes"]["A"], p["bias_qc"]["axes"]["B"]
    assert a["suspect_flag"] and a["status"] == "PROPOSED" and a["proposed_c"] == 3.4 and a["proposal_record_id"] == "BP-1"
    assert a["display_status"] == "UNVERIFIED" and a["applied"] is False and a["evidence"]["n"] == "12"
    assert "reading units" in a["proposed_c_unit"]
    assert b["status"] == "BAD_RESURVEY" and b["proposed_c"] is None and not b["suspect_flag"]
    assert p["bias_qc"]["stable_zone_assumed"] and "NEVER applied" in p["bias_qc"]["note"]
    assert p["bias_applied"] is False and p["correction_basis"] == ["uncorrected"]
    # values are unchanged by the proposal (still the uncorrected engine values; SUSPECT rows stay visible)
    assert [r["cum_a"] for r in p["rows"]] == [r["cum_a"] for r in base["rows"]]
    assert "proposed_bias" not in str(p)
    codes = {x["code"]: x for x in p["qa_reasons"]}
    assert codes["BIAS_PROPOSED_A"]["severity"] == "SUSPECT" and codes["BIAS_BAD_RESURVEY_B"]["severity"] == "BAD"
    assert codes["STABLE_ZONE_ASSUMED"]["known"]
