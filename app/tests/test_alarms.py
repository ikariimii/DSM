from conftest import ADMIN, CDSE, DSO, ENG, NOBODY, VIEWER


def _crd_event(c, user=ENG):
    al = c.get("/api/alarms?dam_id=CRD", user=user).json()["alarms"]
    return next(a for a in al if a["instrument_id"] == "CRD-PZ-2")


def test_list_scoped_and_gated(rc):
    al = rc.get("/api/alarms").json()["alarms"]
    assert {a["dam_id"] for a in al} == {"CRD", "HLD", "PHR"}
    assert all({"state", "level_name", "display_status", "unit", "cal_version", "qa_flag"} <= set(a) for a in al)
    assert {a["dam_id"] for a in rc.get("/api/alarms", user=ENG).json()["alarms"]} == {"CRD", "HLD"}
    assert rc.get("/api/alarms", user=VIEWER).json()["alarms"] == []
    assert rc.get("/api/alarms", user=NOBODY).json()["alarms"] == []
    assert rc.get("/api/alarms?dam_id=PHR", user=ENG).status_code == 403
    nd = next(a for a in al if a["rule"] == "no_data")
    assert nd["value"] is None and nd["display_status"] == "LINEAGE_INCOMPLETE"  # no calib/formula lineage -> nothing to show
    assert all(a["level"] >= 2 for a in rc.get("/api/alarms?min_level=2").json()["alarms"])
    # open first, highest level first
    states = [a["state"] for a in al]
    assert states == sorted(states, key=["open", "acknowledged", "closed"].index)


def test_ack_close_workflow(c):
    ev = _crd_event(c)
    eid = ev["event_id"]
    assert ev["state"] == "open" and ev["level"] >= 2 and ev["requires_cdse"] and not ev["can_close"]
    assert not ev["can_ack"] and not ev["is_cdse"]          # ENG is not CRD's CDSE
    assert _crd_event(c, CDSE)["can_ack"] and _crd_event(c, CDSE)["is_cdse"]
    # engineer cannot close; must ack first anyway
    assert c.post(f"/api/alarms/{eid}/close", user=ENG, json={"reason": "fine"}).status_code == 403
    assert c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "fine"}).status_code == 409
    r = c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"comment": "checking"})
    assert r.status_code == 200 and r.json()["state"] == "acknowledged"
    assert c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "again"}).status_code == 409
    a = c.get(f"/api/alarms/{eid}", user=ENG).json()
    assert a["acknowledged_by"] == CDSE and a["comments"][0]["body"] == "checking"
    assert not a["can_ack"] and not a["can_close"]
    assert c.get(f"/api/alarms/{eid}", user=ADMIN).json()["can_close"]
    r = c.post(f"/api/alarms/{eid}/comment", user=ENG, json={"body": "reading verified in field"})
    assert len(r.json()["comments"]) == 2
    assert c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "x"}).status_code == 422
    r = c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "Field check OK; rainfall response"})
    assert r.status_code == 200 and r.json()["state"] == "closed"
    assert c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "again"}).status_code == 409
    assert c.get("/api/alarms?dam_id=CRD&state=closed", user=ENG).json()["alarms"][0]["event_id"] == eid
    acts = [e["action"] for e in c.get("/api/admin/audit?dam_id=CRD").json()["entries"]]
    assert {"alarm_ack", "alarm_comment", "alarm_close"} <= set(acts)


def test_alarm_acl(c):
    al = c.get("/api/alarms?dam_id=PHR", user=DSO).json()["alarms"]
    eid = al[0]["event_id"]
    assert c.get(f"/api/alarms/{eid}", user=ENG).status_code == 403
    assert c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "x"}).status_code == 403
    assert c.post(f"/api/alarms/{eid}/comment", user=VIEWER, json={"body": "x"}).status_code == 403
    assert c.get("/api/alarms/EV-NOPE").status_code == 404
    assert c.post(f"/api/alarms/{eid}/ack", user=DSO, json={"rationale": "piezometer spike reviewed"}).status_code == 200


def test_unverified_alarm_value_withheld(c):
    """An alarm whose triggering value is not verifiable stays visible but its value is hidden."""
    an = c.tc.app.state.analytics_factory(None)
    an.d["alarm_events"].append(dict(
        event_id="EV-T1", dam_id="STC", instrument_id="STC-PZ-3", threshold_id=None, level=1, rule="value_above",
        value=999.0, limit=1.0, unit="m", event_ts="2026-10-30T00:00:00Z", cleared_ts=None, pfm_ids=[], cal_version=1,
        calib_id="STC-PZ-3-C1", formula_id="VW_PIEZO", formula_version="1.0.0", qa_flag="OK", message="t"))
    a = c.get("/api/alarms/EV-T1", user=DSO).json()
    assert a["value"] is None and a["display_status"] == "UNAPPROVED_CALIBRATION" and a["state"] == "open"


def test_incl_alarm_gated_by_installation_version(rc):
    """v1.5: inclinometer episodes have calib_id NULL; approved via installation_version (row valid at event_ts)."""
    al = [a for a in rc.get("/api/alarms?dam_id=CRD").json()["alarms"] if a["instrument_id"] == "CRD-IN-1"]
    assert al and all(a["calib_id"] is None and a["installation_version"] for a in al)
    assert all(a["display_status"] == "VERIFIED" and a["value"] is not None and a["metric_value"] is not None
               for a in al)
    assert all("PEAK" not in a["reading_id"] for a in al)
