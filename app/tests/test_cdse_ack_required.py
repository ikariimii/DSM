"""Per-dam, per-alarm-rule cdse_ack_required (Genie decision, v1.6; DA-owned column ref.threshold.cdse_ack_required
BOOLEAN NOT NULL DEFAULT false, exposed on ops.alarm_episode, plus the episode snapshot
ops.alarm_episode.cdse_ack_required_at_open). A flagged rule needs the dam's CDSE at ANY level; unflagged rules keep
the level rule (levels 2 Evaluate / 3 Act = FERC high/critical -> CDSE; level 1 Verify -> engineer+). The flag counts
when the snapshot OR the episode view's rule flag OR the current rule row is true; absent columns are false."""
import copy
import logging

import pytest
from conftest import ADMIN, CDSE, ENG, VIEWER
from test_onboarding import _fill, good_draft

from backend.routers import alarms as alarms_router

FLAGGED_L1 = "EV-00003"      # CRD-IN-1-T1, flagged in the demo seed (snapshot true at open)
UNFLAGGED_L1 = "EV-00001"    # CRD-PZ-2-T1
HLD_L1 = "EV-00006"          # HLD-IN-1-T1


def _an(c):
    return c.tc.app.state.analytics_factory(None)


def _rule(c, threshold_id):
    return next(t for t in _an(c).d["thresholds"] if t["threshold_id"] == threshold_id)


def _episode(c, event_id):
    return next(e for e in _an(c).d["alarm_events"] if e["event_id"] == event_id)


def _view(c, eid, user):
    return c.get(f"/api/alarms/{eid}", user=user).json()


def _nothing_written(c, eid, dam="CRD"):
    st = c.tc.app.state.store
    assert st.chain_rows(dam) == []
    assert st.ack_states([eid]).get(eid) is None
    assert not [e for e in c.get(f"/api/admin/audit?dam_id={dam}").json()["entries"] if e["action"] == "alarm_ack"]


def _ack(c, eid, user):
    return c.post(f"/api/alarms/{eid}/ack", user=user, json={"rationale": "reviewed in the field"})


def test_demo_seed_flags_one_level1_rule():
    from backend import seed
    d = seed.build()
    assert {t["threshold_id"] for t in d["thresholds"] if t["cdse_ack_required"]} == {"CRD-IN-1-T1"}
    assert all(isinstance(t["cdse_ack_required"], bool) for t in d["thresholds"])
    ev = next(e for e in d["alarm_events"] if e["event_id"] == FLAGGED_L1)
    assert ev["level"] == 1 and ev["cdse_ack_required_at_open"] is True


def test_flagged_level1_needs_the_cdse(c):
    v = _view(c, FLAGGED_L1, CDSE)
    assert v["level"] == 1 and v["cdse_ack_required"] and v["requires_cdse"] and v["is_cdse"] and v["can_ack"]
    listed = next(a for a in c.get("/api/alarms?dam_id=CRD", user=ENG).json()["alarms"] if a["event_id"] == FLAGGED_L1)
    assert listed["cdse_ack_required"] and listed["requires_cdse"] and not listed["can_ack"]
    r = _ack(c, FLAGGED_L1, CDSE)
    assert r.status_code == 200
    (row,) = c.tc.app.state.store.chain_rows("CRD")
    assert (row["review_level"], row["reviewer_email"], row["subject_id"]) == ("CDSE", CDSE, FLAGGED_L1)


@pytest.mark.parametrize("user", [ENG, ADMIN, VIEWER], ids=["engineer", "admin", "viewer"])
def test_flagged_level1_by_non_cdse_is_403_with_nothing_written(c, user):
    if user != VIEWER:                     # viewer.bfk has no CRD access at all (403 before the CDSE check)
        assert _view(c, FLAGGED_L1, user)["can_ack"] is False
    r = _ack(c, FLAGGED_L1, user)
    assert r.status_code == 403, r.text
    if user != VIEWER:
        assert "cdse_ack_required" in r.json()["detail"]
    _nothing_written(c, FLAGGED_L1)


def test_unflagged_level1_works_for_an_engineer(c):
    v = _view(c, UNFLAGGED_L1, ENG)
    assert v["level"] == 1 and not v["cdse_ack_required"] and not v["requires_cdse"] and v["can_ack"]
    assert _ack(c, UNFLAGGED_L1, ENG).status_code == 200
    assert [r["review_level"] for r in c.tc.app.state.store.chain_rows("CRD")] == ["engineer"]


@pytest.mark.parametrize("level", [2, 3], ids=["HIGH", "CRITICAL"])
@pytest.mark.parametrize("flag", [False, True], ids=["unflagged", "flagged"])
def test_level2_3_need_the_cdse_regardless_of_flag(c, level, flag):
    ev = next(e for e in _an(c).d["alarm_events"] if e["dam_id"] == "CRD" and e["level"] == level)
    _rule(c, ev["threshold_id"])["cdse_ack_required"] = flag
    ev["cdse_ack_required_at_open"] = flag
    assert _view(c, ev["event_id"], ENG)["requires_cdse"]
    for user in (ENG, ADMIN):
        assert _ack(c, ev["event_id"], user).status_code == 403
    _nothing_written(c, ev["event_id"])
    assert _ack(c, ev["event_id"], CDSE).status_code == 200


def test_cdse_of_another_dam_is_refused_on_a_flagged_rule(c):
    """cdse.crd is CRD's CDSE and an engineer on HLD; flag HLD's level-1 rule: they (and ENG) are refused there."""
    _rule(c, "HLD-IN-1-T1")["cdse_ack_required"] = True
    v = _view(c, HLD_L1, CDSE)
    assert v["level"] == 1 and v["requires_cdse"] and not v["is_cdse"] and not v["can_ack"]
    for user in (CDSE, ENG, ADMIN):
        r = _ack(c, HLD_L1, user)
        assert r.status_code == 403 and "HLD" in r.json()["detail"], (user, r.text)
    _nothing_written(c, HLD_L1, "HLD")


def test_unflagging_a_rule_mid_episode_does_not_weaken_the_open_episode(c):
    """The episode opened while CRD-IN-1-T1 was flagged (snapshot true). Un-flagging the rule afterwards (current
    ref.threshold row and the episode view's rule flag both false) still requires the CDSE for THAT episode."""
    _rule(c, "CRD-IN-1-T1")["cdse_ack_required"] = False
    _episode(c, FLAGGED_L1)["cdse_ack_required"] = False          # the view's current-rule column, now false
    assert _episode(c, FLAGGED_L1)["cdse_ack_required_at_open"] is True
    assert _view(c, FLAGGED_L1, ENG)["requires_cdse"]
    assert _ack(c, FLAGGED_L1, ENG).status_code == 403
    _nothing_written(c, FLAGGED_L1)
    assert _ack(c, FLAGGED_L1, CDSE).status_code == 200


def test_flagging_a_rule_mid_episode_strengthens_the_open_episode(c):
    """Snapshot false, rule flagged later: the current flag is OR-ed in, so the CDSE is now needed."""
    assert _episode(c, UNFLAGGED_L1)["cdse_ack_required_at_open"] is False
    _rule(c, "CRD-PZ-2-T1")["cdse_ack_required"] = True
    assert _view(c, UNFLAGGED_L1, ENG)["requires_cdse"]
    assert _ack(c, UNFLAGGED_L1, ENG).status_code == 403
    _nothing_written(c, UNFLAGGED_L1)


def test_episode_view_rule_flag_alone_is_enough(c):
    ep = _episode(c, UNFLAGGED_L1)
    ep["cdse_ack_required"] = True                                  # DA's view column only
    assert _ack(c, UNFLAGGED_L1, ENG).status_code == 403


def test_missing_columns_default_to_false_and_log_once(c, caplog, monkeypatch):
    """An older schema without ref.threshold.cdse_ack_required and the episode columns: flag = false (logged once);
    the level rule still applies."""
    an = _an(c)
    for t in an.d["thresholds"]:
        t.pop("cdse_ack_required", None)
    for e in an.d["alarm_events"]:
        e.pop("cdse_ack_required_at_open", None)
        e.pop("cdse_ack_required", None)
    monkeypatch.setattr(alarms_router, "_absent_logged", set())
    caplog.set_level(logging.WARNING, logger="backend.routers.alarms")
    for _ in range(3):
        listed = c.get("/api/alarms?dam_id=CRD", user=ENG).json()["alarms"]
    flag = {a["event_id"]: a for a in listed}
    assert flag[FLAGGED_L1]["cdse_ack_required"] is False and not flag[FLAGGED_L1]["requires_cdse"]
    assert all(a["requires_cdse"] == (a["level"] >= 2) for a in listed)
    msgs = [r.getMessage() for r in caplog.records if "cdse_ack_required" in r.getMessage()]
    assert len([m for m in msgs if "ref.threshold" in m]) == 1
    assert len([m for m in msgs if "ops.alarm_episode" in m]) == 1
    assert _ack(c, FLAGGED_L1, ENG).status_code == 200


def test_rule_flag_values_are_strict():
    t = alarms_router._truthy
    assert [t(v) for v in (True, "true", "TRUE", "t", 1, "1")] == [True] * 6
    assert [t(v) for v in (False, None, "false", "", 0, "no", "yes?")] == [False] * 7


# ------------------------------------------------------------------ onboarding wizard
def test_wizard_saves_the_flag_defaults_off_and_audits_changes(c):
    did = c.post("/api/onboarding/drafts").json()["draft_id"]
    d = good_draft()
    _fill(c, did, d)
    saved = c.get(f"/api/onboarding/drafts/{did}").json()["data"]["thresholds"]
    assert all("cdse_ack_required" not in t for t in saved)                   # default off (absent)
    audit = lambda: [e for e in c.get("/api/admin/audit").json()["entries"]  # noqa: E731
                     if e["action"] == "alarm_rule_cdse_ack_required"]
    assert audit() == []
    flagged = copy.deepcopy(d["thresholds"])
    flagged[0]["cdse_ack_required"] = True
    assert c.put(f"/api/onboarding/drafts/{did}/steps/thresholds", json=flagged).status_code == 200
    assert c.get(f"/api/onboarding/drafts/{did}").json()["data"]["thresholds"][0]["cdse_ack_required"] is True
    (a,) = audit()
    det = a["detail"]
    assert (a["actor"], a["dam_id"], det["from"], det["to"], det["instrument_id"], det["level"]) == \
        (ADMIN, "NEW", False, True, "NEW-PZ-1", 1)
    # re-saving unchanged is not a change; un-flagging is
    assert c.put(f"/api/onboarding/drafts/{did}/steps/thresholds", json=flagged).status_code == 200
    assert len(audit()) == 1
    off = copy.deepcopy(flagged)
    off[0]["cdse_ack_required"] = False
    assert c.put(f"/api/onboarding/drafts/{did}/steps/thresholds", json=off).status_code == 200
    assert len(audit()) == 2
    assert c.put(f"/api/onboarding/drafts/{did}/steps/thresholds", json=flagged).status_code == 200
    # go-live publishes ref.threshold.cdse_ack_required (bool, default false) and the CDSE is then needed at L1
    assert c.post(f"/api/onboarding/drafts/{did}/validate").json()["ok"]
    r = c.post(f"/api/onboarding/drafts/{did}/golive")
    assert r.status_code == 200
    rules = {(t["instrument_id"], t["level"]): t["cdse_ack_required"] for t in _an(c).d["thresholds"]
             if t["dam_id"] == "NEW"}
    assert rules == {("NEW-PZ-1", 1): True, ("NEW-PZ-1", 2): False, ("NEW-SP-1", 1): False}
    golive = next(e for e in c.get("/api/admin/audit").json()["entries"] if e["action"] == "onboarding_golive")
    assert golive["detail"]["cdse_ack_required_rules"] == ["NEW-PZ-1-T1-1"]


def test_wizard_rejects_a_non_boolean_flag(c):
    from backend import onboarding as ob
    d = good_draft()
    d["thresholds"][0]["cdse_ack_required"] = "yes"
    errs = [i["message"] for i in ob.validate(d, {"CRD"}) if i["severity"] == "error"]
    assert "cdse_ack_required must be true or false" in errs


def test_wizard_flag_needs_the_draft_owner_or_admin(c):
    did = c.post("/api/onboarding/drafts").json()["draft_id"]
    r = c.put(f"/api/onboarding/drafts/{did}/steps/thresholds", user=ENG, json=[{"cdse_ack_required": True}])
    assert r.status_code == 403
    assert not [e for e in c.get("/api/admin/audit").json()["entries"] if e["action"] == "alarm_rule_cdse_ack_required"]
