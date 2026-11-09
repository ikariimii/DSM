import copy

import pytest

from backend import onboarding as ob
from conftest import ADMIN, DSO, ENG

CSV = """instrument_id,structure_id,instrument_type,subtype,formula_id,formula_version,eng_unit,output_name,station_m,offset_m,tip_el_m,top_el_m,install_date
NEW-PZ-1,NEW-MAIN,piezometer,vibrating_wire,VW_PIEZO,1.0.0,m,piezometric_elev_m,100,5,180.5,,2026-06-01
NEW-SP-1,NEW-MAIN,piezometer,standpipe,STANDPIPE_DTW,1.0.0,m,piezometric_elev_m,120,5,175,200,2026-06-01
"""


def _pub(tier, deadband, on=2, off=3):
    """ref.threshold NOT NULL publication columns the wizard collects (DA follow-up 2)."""
    return {"dssmp_version_id": "DSSMP-NEW-2026", "tier": tier, "deadband": deadband, "persistence_on": on,
            "persistence_off": off}


def good_draft():
    return {
        "details": {"dam_id": "NEW", "name": "New Dam", "dam_type": "Earthfill", "hazard_class": "High",
                    "owner": "Owner", "datum": "NAVD88", "lat": 35.0, "lon": -84.0, "crest_elev_m": 220,
                    "normal_pool_m": 210, "height_m": 40, "nid_id": "XX0001", "state": "TN"},
        "structures": [{"structure_id": "NEW-MAIN", "name": "Main embankment", "structure_type": "embankment"}],
        "instruments": ob.parse_instrument_csv(CSV)["rows"],
        "calibrations": [
            {"instrument_id": "NEW-PZ-1", "cal_version": 1, "effective_from": "2026-06-01T00:00:00Z",
             "sign_mult": -1, "offset": 0.0, "method": "linear",
             "params": {"G": 0.1795, "R0": 9082, "T0": 20, "K": 0.1, "tip_el_m": 180.5},
             "approved_by": "eor@x.org", "certificate_uri": "CERT-1"},
            {"instrument_id": "NEW-SP-1", "cal_version": 1, "effective_from": "2026-06-01T00:00:00Z",
             "params": {"top_el_m": 200, "tip_el_m": 175}, "approved_by": "eor@x.org", "certificate_uri": "survey"},
        ],
        "thresholds": [
            {"instrument_id": "NEW-PZ-1", "level": 1, "limit_type": "magnitude", "direction": "upper", "value": 200,
             "unit": "m", "required_actions": "verify", **_pub("AL1", 0.05)},
            {"instrument_id": "NEW-PZ-1", "level": 2, "limit_type": "magnitude", "direction": "upper", "value": 205,
             "unit": "m", "required_actions": "evaluate", **_pub("AL2", 0.05)},
            {"instrument_id": "NEW-SP-1", "level": 1, "limit_type": "magnitude", "direction": "lower", "value": 176,
             "unit": "m", "required_actions": "check dry", **_pub("AL1", 0.1)},
        ],
        "data_sources": [
            {"instrument_id": "NEW-PZ-1", "source_type": "logger_file", "path": "/Volumes/x/new/*.dat",
             "column": "PZ1_Digits", "raw_unit": "digits"},
            {"instrument_id": "NEW-SP-1", "source_type": "manual", "raw_unit": "m"},
        ],
        "users": [{"email": "owner@new.org", "role": "dam_admin"}, {"email": "eng@new.org", "role": "engineer"}],
    }


def errs(d, existing=frozenset({"CRD"})):
    return [i for i in ob.validate(d, set(existing)) if i["severity"] == "error"]


def test_csv_parse():
    r = ob.parse_instrument_csv("\ufeff" + CSV)
    assert len(r["rows"]) == 2 and r["issues"] == []
    assert r["rows"][0]["tip_el_m"] == 180.5 and r["rows"][0]["top_el_m"] is None
    old = ob.parse_instrument_csv(CSV.replace("tip_el_m,top_el_m", "tip_elev_m,top_elev_m"))  # legacy spelling
    assert old["issues"] == [] and old["rows"][1]["top_el_m"] == 200.0
    bad = CSV + "NEW-PZ-1,NEW-MAIN,piezometer,,VW_PIEZO,1.0.0,m,,abc,,,,2026-13-01\n,,,,,,,,,,,,\nbad id,NEW-MAIN,piezometer,,VW_PIEZO,1.0.0,m,,,,,,\n"
    r = ob.parse_instrument_csv(bad)
    msgs = " ".join(i["message"] for i in r["issues"])
    assert "duplicate instrument_id" in msgs and "station_m must be a number" in msgs
    assert "install_date" in msgs and "instrument_id is required" in msgs and "invalid instrument_id" in msgs
    assert len(r["rows"]) == 2
    r = ob.parse_instrument_csv("instrument_id,foo\nA,1\n")
    assert r["rows"] == [] and "missing columns" in r["issues"][0]["message"]
    r = ob.parse_instrument_csv(CSV.replace("install_date", "install_date,extra").replace("2026-06-01\n", "2026-06-01,1\n"))
    assert any("ignored columns: extra" in i["message"] for i in r["issues"])
    r = ob.parse_instrument_csv(CSV, max_rows=1)
    assert any("more than 1 rows" in i["message"] for i in r["issues"])


def test_validate_good_draft():
    assert errs(good_draft()) == []
    warn = [i for i in ob.validate(good_draft(), set()) if i["severity"] == "warning"]
    assert warn == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["details"].update(dam_id="new"), "dam_id must be"),
    (lambda d: d["details"].update(dam_id="CRD"), "already exists"),
    (lambda d: d["details"].pop("name"), "name is required"),
    (lambda d: d["details"].update(hazard_class="Extreme"), "hazard_class"),
    (lambda d: d["details"].update(lat=95), "lat must be"),
    (lambda d: d["details"].update(lon="x"), "lon must be"),
    (lambda d: d.update(structures=[]), "at least one structure"),
    (lambda d: d["structures"].append(dict(d["structures"][0])), "duplicate structure_id"),
    (lambda d: d["structures"].append({"structure_id": "bad id"}), "invalid structure_id"),
    (lambda d: d.update(instruments=[]), "at least one instrument"),
    (lambda d: d["instruments"][0].update(structure_id="X"), "unknown structure_id"),
    (lambda d: d["instruments"][0].update(instrument_type="sonar"), "unsupported instrument_type"),
    (lambda d: d["instruments"][0].update(instrument_type="strain_gauge", eng_unit="microstrain"), "has no engine formula"),
    (lambda d: d["instruments"][0].update(eng_unit="ft"), "not valid for piezometer"),
    (lambda d: d["instruments"][0].update(formula_id="NOPE"), "unknown formula"),
    (lambda d: d["calibrations"].pop(0), "no calibration"),
    (lambda d: d["calibrations"][0]["params"].pop("G"), "missing parameter G"),
    (lambda d: d["calibrations"][0].update(method="poly"), "missing parameter A"),
    (lambda d: d["calibrations"][0]["params"].update(sign_mult=-1), "belongs in the calibration's sign_mult field"),
    (lambda d: d["calibrations"][0].update(method="cubic"), "method must be one of linear|poly"),
    (lambda d: d["calibrations"][0]["params"].update(R0="abc"), "not numeric"),
    (lambda d: d["calibrations"][0].pop("effective_from"), "effective_from is required"),
    (lambda d: d["calibrations"].append(dict(d["calibrations"][0])), "duplicate cal_version"),
    (lambda d: d["thresholds"].append({"instrument_id": "GHOST", "level": 1}), "unknown instrument"),
    (lambda d: d["thresholds"][0].update(unit="ft"), "differs from instrument unit"),
    (lambda d: d["thresholds"][0].update(level=4), "level must be"),
    (lambda d: d["thresholds"][0].update(limit_type="magic"), "limit_type must be one of"),
    (lambda d: d["thresholds"][0].update(direction="sideways"), "direction must be one of"),
    (lambda d: d["thresholds"][0].update(value="x"), "limit value must be numeric"),
    (lambda d: d["thresholds"][1].update(value=150), "not monotonic"),
    (lambda d: d["thresholds"].append(dict(d["thresholds"][0])), "duplicate magnitude upper level"),
    (lambda d: d["thresholds"].append({"instrument_id": "NEW-SP-1", "level": 2, "limit_type": "magnitude",
                                       "direction": "lower", "value": 180, "unit": "m", "required_actions": "x"}),
     "not monotonic"),
    (lambda d: d["thresholds"].append({"instrument_id": "NEW-PZ-1", "level": 3, "limit_type": "magnitude",
                                       "direction": "abs", "value": -1, "unit": "m", "required_actions": "x"}),
     "abs limit must be >= 0"),
    (lambda d: d["thresholds"][0].update(limit_type=None, rule="magic"), "limit_type must be one of"),
    (lambda d: d["data_sources"].pop(0), "no data source mapping"),
    (lambda d: d["data_sources"][0].update(source_type="ftp"), "source_type must be"),
    (lambda d: d["data_sources"][0].pop("path"), "path or table is required"),
    (lambda d: d["data_sources"][0].pop("column"), "logger column is required"),
    (lambda d: d["data_sources"][0].pop("raw_unit"), "raw_unit is required"),
    (lambda d: d.update(users=[{"email": "a@b.org", "role": "engineer"}]), "at least one dam_admin"),
    (lambda d: d["users"].append({"email": "OWNER@new.org", "role": "viewer"}), "duplicate user email"),
    (lambda d: d["users"].append({"email": "not-an-email", "role": "viewer"}), "invalid email"),
    (lambda d: d["users"].append({"email": "x@y.org", "role": "god"}), "invalid role"),
])
def test_validate_errors(mutate, needle):
    d = copy.deepcopy(good_draft())
    mutate(d)
    msgs = [i["message"] for i in errs(d)]
    assert any(needle in m for m in msgs), msgs


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["details"].pop("crest_elev_m"), "crest_elev_m not set"),
    (lambda d: d["calibrations"][0].pop("approved_by"), "values will be withheld"),
    (lambda d: d["calibrations"][0].pop("certificate_uri"), "no calibration certificate"),
    (lambda d: d["thresholds"][0].pop("required_actions"), "no required actions"),
    (lambda d: d["thresholds"].append({"instrument_id": "NEW-PZ-1", "level": 1, "limit_type": "cyclic",
                                       "direction": "abs", "value": 2, "unit": "m", "required_actions": "x"}),
     "cyclic limit is not evaluated"),
    (lambda d: d.update(thresholds=[t for t in d["thresholds"] if t["instrument_id"] != "NEW-SP-1"]), "no thresholds"),
    (lambda d: d["data_sources"].append({"instrument_id": "GHOST", "source_type": "manual", "raw_unit": "m"}),
     "mapping for unknown instrument"),
])
def test_validate_warnings(mutate, needle):
    d = copy.deepcopy(good_draft())
    mutate(d)
    issues = ob.validate(d, set())
    assert any(needle in i["message"] and i["severity"] == "warning" for i in issues)


def test_validate_unverified_formula_warning_and_engine_catalog():
    d = good_draft()
    d["instruments"].append({**d["instruments"][0], "instrument_id": "NEW-SM-1", "instrument_type": "settlement",
                             "formula_id": "SETTLEMENT", "eng_unit": "mm"})
    d["calibrations"].append({"instrument_id": "NEW-SM-1", "cal_version": 1, "effective_from": "2026-06-01",
                              "params": {"EL0": 200}, "approved_by": "x", "certificate_uri": "c"})
    issues = ob.validate(d, set())
    assert any("SETTLEMENT v1.0.0 is not engine-verified" in i["message"] for i in issues)
    # engine catalog is authoritative: a formula unknown to the app but verified in ref.formula passes
    d2 = good_draft()
    d2["instruments"][0]["formula_id"] = "ENGINE_ONLY"
    d2["calibrations"] = d2["calibrations"][1:]
    issues = ob.validate(d2, set(), [{"formula_id": "ENGINE_ONLY", "formula_version": "1.0.0", "verified": True}])
    assert not any("unknown formula" in i["message"] for i in issues)
    assert not any("ENGINE_ONLY" in i["message"] and "not engine-verified" in i["message"] for i in issues)
    # stored check that claims verified while re-run fails is an error
    d3 = good_draft()
    d3["instruments"][0]["formula_id"] = "SETTLEMENT"
    d3["formulas"] = {"checks": {"SETTLEMENT:1.0.0": {"verified": True}}}
    assert any("stored check disagrees" in i["message"] for i in errs(d3))


def test_golive_payload():
    p = ob.golive_payload(good_draft())
    assert p["dam_id"] == "NEW" and p["sites"][0]["status"] == "ACTIVE"
    cal = p["calibrations"][0]
    assert cal["calib_id"] == "NEW-PZ-1-C1" and cal["is_current"] and cal["params"]["G"] == 0.1795
    assert cal["sign_mult"] == -1 and cal["offset"] == 0.0 and "sign_mult" not in cal["params"]   # §10 Q3 columns
    assert cal["method"] == "linear" and cal["formula_version"] == "1.0.0" and cal["certificate_uri"] == "CERT-1"
    assert cal["is_retired"] is False and p["calibrations"][1]["sign_mult"] == 1
    t = p["thresholds"][0]
    assert (t["limit_type"], t["direction"], t["value"], t["dam_id"], t["required_actions"]) == \
        ("magnitude", "upper", 200.0, "NEW", "verify")
    assert "upper_limit" not in t and "rule" not in t
    assert p["instruments"][0]["tip_el_m"] == 180.5
    # a legacy-shaped draft threshold is converted to canonical columns
    d = good_draft()
    d["thresholds"][0] = {"instrument_id": "NEW-PZ-1", "level": 1, "rule": "value_above", "upper_limit": 200,
                          "unit": "m", "pfm_ids": ["P"], "response_action": "v", **_pub("AL1", 0.05)}
    t = ob.golive_payload(d)["thresholds"][0]
    assert (t["limit_type"], t["direction"], t["value"], t["pfm_id"], t["required_actions"]) == \
        ("magnitude", "upper", 200.0, "P", "v")
    assert p["data_source_map"][0]["dam_id"] == "NEW"


def register_dssmp(c, dam_id="NEW", dssmp_version_id="DSSMP-NEW-2026", status="approved"):
    """Demo stand-in for the data layer: the DSSMP version a new project's thresholds are set under must exist in
    ref.dssmp_version for that dam before go-live (onboarding.dssmp_issues)."""
    an = c.tc.app.state.analytics_factory(None)
    an.d.setdefault("dssmp_versions", []).append({"dssmp_version_id": dssmp_version_id, "dam_id": dam_id,
                                                  "version_no": 1, "status": status})


def _fill(c, did, d, user=ADMIN, dssmp=True):
    if dssmp:
        register_dssmp(c)
    for step in ob.EDITABLE:
        if step == "formulas":
            continue
        r = c.put(f"/api/onboarding/drafts/{did}/steps/{step}", user=user, json=d[step])
        assert r.status_code == 200, r.text


def test_wizard_end_to_end(c):
    assert c.get("/api/onboarding/steps").json()["steps"][-1] == "golive"
    did = c.post("/api/onboarding/drafts").json()["draft_id"]
    r = c.post("/api/onboarding/parse-csv", json={"csv": CSV}).json()
    assert len(r["rows"]) == 2
    _fill(c, did, good_draft())
    chk = c.post(f"/api/onboarding/drafts/{did}/formulas/check").json()["checks"]
    assert chk["VW_PIEZO:1.0.0"]["verified"] and chk["STANDPIPE_DTW:1.0.0"]["engine_verified"]
    v = c.post(f"/api/onboarding/drafts/{did}/validate").json()
    assert v["ok"], v
    r = c.post(f"/api/onboarding/drafts/{did}/golive")
    assert r.status_code == 200 and r.json()["publication"]["status"] == "applied"
    # project is live in demo mode
    assert "NEW" in [d["dam_id"] for d in c.get("/api/portfolio").json()["dams"]]
    assert c.get("/api/me", user="owner@new.org").json()["roles"] == {"NEW": "dam_admin"}
    assert c.get("/api/dams/NEW/instruments", user="eng@new.org").status_code == 200
    assert c.post(f"/api/onboarding/drafts/{did}/golive").status_code == 409
    assert c.put(f"/api/onboarding/drafts/{did}/steps/details", json={}).status_code == 409
    assert c.get(f"/api/onboarding/drafts/{did}").json()["status"] == "published"
    assert any(e["action"] == "onboarding_golive" for e in c.get("/api/admin/audit").json()["entries"])


def test_golive_blocked_by_errors_and_roles(c):
    did = c.post("/api/onboarding/drafts", user=DSO).json()["draft_id"]  # dso.phr is dam_admin on PHR
    assert c.post(f"/api/onboarding/drafts/{did}/golive", user=DSO).status_code == 403
    assert c.get(f"/api/onboarding/drafts/{did}", user=ENG).status_code == 403
    assert c.get(f"/api/onboarding/drafts/{did}").status_code == 200  # admin can see all
    r = c.post(f"/api/onboarding/drafts/{did}/golive")
    assert r.status_code == 422 and r.json()["detail"]["issues"]
    assert [d["draft_id"] for d in c.get("/api/onboarding/drafts", user=DSO).json()["drafts"]] == [did]


def test_onboarding_permissions_and_input(c):
    assert c.post("/api/onboarding/drafts", user=ENG).status_code == 403
    assert c.get("/api/onboarding/drafts", user=ENG).status_code == 403
    assert c.post("/api/onboarding/parse-csv", user=ENG, json={"csv": CSV}).status_code == 403
    assert c.get("/api/onboarding/drafts/nope").status_code == 404
    did = c.post("/api/onboarding/drafts").json()["draft_id"]
    assert c.put(f"/api/onboarding/drafts/{did}/steps/golive", json={}).status_code == 404
    assert c.put(f"/api/onboarding/drafts/{did}/steps/details", json=[]).status_code == 422
    assert c.put(f"/api/onboarding/drafts/{did}/steps/structures", json={}).status_code == 422
    r = c.tc.put(f"/api/onboarding/drafts/{did}/steps/details", content=b"not json",
                 headers={"x-forwarded-email": ADMIN, "content-type": "application/json"})
    assert r.status_code == 422


def test_rate_threshold_and_formula_check_dedupe(c):
    d = good_draft()
    d["thresholds"].append({"instrument_id": "NEW-PZ-1", "level": 1, "limit_type": "rate", "direction": "upper",
                            "value": 0.5, "unit": "m/day", "required_actions": "inspect"})
    assert not [e for e in errs(d) if "rate" in e["message"]]
    d["thresholds"][-1]["value"] = None
    assert errs(d)
    d = good_draft()
    d["instruments"].append({**d["instruments"][0], "instrument_id": "NEW-PZ-2"})
    did = c.post("/api/onboarding/drafts").json()["draft_id"]
    _fill(c, did, d)
    chk = c.post(f"/api/onboarding/drafts/{did}/formulas/check").json()["checks"]
    assert set(chk) == {"VW_PIEZO:1.0.0", "STANDPIPE_DTW:1.0.0"}


def test_type_units_match_contract_vocab():
    from backend.repos import contract as ct
    assert ob.TYPE_UNITS == {t: set(spec["units"]) for t, spec in ct.INSTRUMENT_TYPES.items()}
