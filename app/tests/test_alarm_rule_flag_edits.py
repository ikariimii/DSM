"""Live-dam edits of cdse_ack_required (Genie decision, v1.6): dam_admin/admin propose, only the dam's CDSE applies,
approves or rejects; rationale required; one chained record per action; apply = new ref.threshold version (old row
untouched); open episodes keep their protection; stale proposals refused."""
import json

import pytest
from conftest import ADMIN, CDSE, DSO, ENG, VIEWER

from backend import audit_chain as ac

DAMADMIN = "damadmin.crd@demo.local"
RULE = "CRD-PZ-2-T1"          # unflagged level-1 rule (EV-00001)
FLAGGED = "CRD-IN-1-T1"       # flagged level-1 rule (EV-00003, snapshot true)
BASE = "/api/dams/CRD/alarm-rules"


@pytest.fixture()
def crd(c):
    for email, role in ((VIEWER, "viewer"), (DSO, "dso"), (DAMADMIN, "dam_admin")):
        assert c.put("/api/admin/roles", user=ADMIN, json={"email": email, "dam_id": "CRD", "role": role}).status_code == 200
    return c


def _an(c):
    return c.tc.app.state.analytics_factory(None)


def _chain(c, dam="CRD"):
    return c.tc.app.state.store.chain_rows(dam)


def _props(c, dam="CRD", status=None):
    return c.tc.app.state.store.flag_proposals(dam, status)


def _propose(c, user, flag=True, rule=RULE, why="seepage trend warrants CDSE sign-off", base=BASE):
    return c.post(f"{base}/{rule}/cdse-flag/proposals", user=user, json={"cdse_ack_required": flag, "rationale": why})


def _apply(c, user, flag=True, rule=RULE, why="CDSE decision", base=BASE):
    return c.post(f"{base}/{rule}/cdse-flag", user=user, json={"cdse_ack_required": flag, "rationale": why})


def _decide(c, user, pid, verb="approve", why="reviewed", base=BASE):
    return c.post(f"{base}/proposals/{pid}/{verb}", user=user, json={"rationale": why})


def _current(c, rule=RULE):
    return next(t for t in _an(c).thresholds("CRD") if t["threshold_id"] == rule)


# ------------------------------------------------------------------ roles
@pytest.mark.parametrize("user", [VIEWER, ENG, DSO], ids=["viewer", "engineer", "dso"])
def test_viewer_engineer_dso_get_403_everywhere(crd, user):
    pid = _propose(crd, ADMIN).json()["proposal_id"]
    n = len(_chain(crd))
    assert _propose(crd, user).status_code == 403
    assert _apply(crd, user).status_code == 403
    assert _decide(crd, user, pid).status_code == 403
    assert _decide(crd, user, pid, "reject").status_code == 403
    assert len(_chain(crd)) == n and [p["status"] for p in _props(crd)] == ["pending"]
    view = crd.get(BASE, user=user).json()
    assert (view["can_propose"], view["can_apply"], view["is_cdse"]) == (False, False, False)
    assert not view["pending"][0]["can_decide"]


@pytest.mark.parametrize("user", [DAMADMIN, ADMIN], ids=["dam_admin", "admin"])
def test_dam_admin_and_admin_may_propose_but_never_apply_or_decide(crd, user):
    r = _propose(crd, user)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    pid = r.json()["proposal_id"]
    assert _apply(crd, user).status_code == 403                       # no direct set/clear
    assert _decide(crd, user, pid).status_code == 403                 # no self-approval
    assert _decide(crd, user, pid, "reject").status_code == 403
    assert _current(crd)["cdse_ack_required"] is False                # nothing applied
    assert [p["status"] for p in _props(crd)] == ["pending"]
    assert [r["action_type"] for r in _chain(crd)] == ["alarm_rule_flag_propose"]
    view = crd.get(BASE, user=user).json()
    assert view["can_propose"] and not view["can_apply"] and not view["pending"][0]["can_decide"]


def test_cdse_approves_an_admins_proposal(crd):
    pid = _propose(crd, ADMIN).json()["proposal_id"]
    view = crd.get(BASE, user=CDSE).json()
    assert view["is_cdse"] and view["can_apply"] and view["pending"][0]["can_decide"]
    r = _decide(crd, CDSE, pid)
    assert r.status_code == 200 and r.json()["status"] == "applied" and r.json()["new_threshold_version"] == 2
    p = crd.tc.app.state.store.flag_proposal(pid)
    assert (p["status"], p["proposed_by"], p["decided_by"], p["new_threshold_version"]) == ("applied", ADMIN, CDSE, 2)
    assert _current(crd)["cdse_ack_required"] is True
    assert _decide(crd, CDSE, pid, "reject").status_code == 409       # already decided


def test_cdse_rejects_a_proposal(crd):
    pid = _propose(crd, DAMADMIN).json()["proposal_id"]
    r = _decide(crd, CDSE, pid, "reject", "flag not justified by PFM")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert _current(crd)["cdse_ack_required"] is False and _current(crd)["threshold_version"] == 1
    assert crd.tc.app.state.store.ref_publications("CRD") == []
    assert _decide(crd, CDSE, pid).status_code == 409


def test_cdse_may_apply_directly_and_approve_their_own_proposal(crd):
    r = _apply(crd, CDSE)
    assert r.status_code == 200 and r.json()["new_threshold_version"] == 2
    pid = _propose(crd, CDSE, flag=False).json()["proposal_id"]       # their own proposal ...
    r = _decide(crd, CDSE, pid)                                         # ... they may apply it
    assert r.status_code == 200 and r.json()["new_threshold_version"] == 3
    assert _current(crd)["cdse_ack_required"] is False and _current(crd)["threshold_version"] == 3
    assert [p["proposed_by"] for p in _props(crd)] == [CDSE, CDSE]


def test_no_op_and_unknown_rule(crd):
    assert _propose(crd, ADMIN, flag=False).status_code == 409        # already false
    assert _apply(crd, CDSE, flag=True, rule=FLAGGED).status_code == 409
    assert _propose(crd, ADMIN, rule="NOPE").status_code == 404
    assert _decide(crd, CDSE, "RFP-nope").status_code == 404
    assert _chain(crd) == [] and _props(crd) == []


# ------------------------------------------------------------------ rationale
@pytest.mark.parametrize("why", ["", "   ", None], ids=["empty", "blank", "missing"])
def test_rationale_required_for_propose_apply_approve_reject(crd, why):
    body = {"cdse_ack_required": True} | ({} if why is None else {"rationale": why})
    assert crd.post(f"{BASE}/{RULE}/cdse-flag/proposals", user=ADMIN, json=body).status_code == 422
    assert crd.post(f"{BASE}/{RULE}/cdse-flag", user=CDSE, json=body).status_code == 422
    assert _chain(crd) == [] and _props(crd) == []
    pid = _propose(crd, ADMIN).json()["proposal_id"]
    dbody = {} if why is None else {"rationale": why}
    for verb in ("approve", "reject"):
        assert crd.post(f"{BASE}/proposals/{pid}/{verb}", user=CDSE, json=dbody).status_code == 422
    assert len(_chain(crd)) == 1 and [p["status"] for p in _props(crd)] == ["pending"]
    assert _current(crd)["threshold_version"] == 1


# ------------------------------------------------------------------ versioning
def test_apply_creates_a_new_threshold_version_and_never_edits_the_old_row(crd):
    an = _an(crd)
    before = an.threshold_versions(RULE)
    assert [t["threshold_version"] for t in before] == [1]
    r = _apply(crd, CDSE, why="CDSE sets the flag")
    after = an.threshold_versions(RULE)
    assert after[0] == before[0]                                       # v1 byte-for-byte unchanged
    v2 = after[1]
    assert (v2["threshold_version"], v2["cdse_ack_required"], v2["set_by"], v2["is_current"], v2["effective_to"]) == \
        (2, True, CDSE, True, None)
    assert v2["effective_from"] > before[0]["effective_from"]
    for k in ("dam_id", "instrument_id", "level", "limit_type", "direction", "value", "unit", "required_actions"):
        assert v2[k] == before[0][k], k
    assert "rule" not in v2 and "upper_limit" not in v2                # canonical columns only
    (pub,) = crd.tc.app.state.store.ref_publications("CRD")
    assert (pub["kind"], pub["status"], pub["proposal_id"], pub["pub_id"]) == \
        ("ref.threshold.new_version", "applied", r.json()["proposal_id"], r.json()["publication_id"])
    assert pub["payload"]["supersedes_version"] == 1 and pub["payload"]["insert"]["threshold_version"] == 2


# ------------------------------------------------------------------ chain records
def test_each_action_writes_one_chained_record_with_original_and_new_values(crd):
    p1 = _propose(crd, ADMIN).json()["proposal_id"]
    assert _decide(crd, CDSE, p1, "reject", "not now").status_code == 200
    p2 = _propose(crd, DAMADMIN).json()["proposal_id"]
    assert _decide(crd, CDSE, p2, "approve", "agreed").status_code == 200
    rows = _chain(crd)
    assert [(r["action_type"], r["decision"], r["review_level"], r["reviewer_email"]) for r in rows] == [
        ("alarm_rule_flag_propose", "propose", "engineer", ADMIN),
        ("alarm_rule_flag_reject", "reject", "CDSE", CDSE),
        ("alarm_rule_flag_propose", "propose", "engineer", DAMADMIN),
        ("alarm_rule_flag_apply", "apply", "CDSE", CDSE)]
    assert all(r["subject_type"] == "alarm_rule" and r["subject_id"] == RULE and r["comment"].strip() for r in rows)
    assert set(r["action_type"] for r in rows) <= set(ac.APP_ACTION_TYPES)
    for r in rows:
        assert json.loads(r["original_value"]) == {"threshold_id": RULE, "threshold_version": 1,
                                                   "cdse_ack_required": False}
    new = [json.loads(r["corrected_value"]) for r in rows]
    assert new[0] == {"cdse_ack_required": True, "proposal_id": p1, "state": "pending"}
    assert new[1]["state"] == "rejected" and new[1]["proposed_by"] == ADMIN
    assert (new[3]["cdse_ack_required"], new[3]["threshold_version"], new[3]["proposal_id"], new[3]["proposed_by"]) == \
        (True, 2, p2, DAMADMIN)
    v = crd.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()
    assert v["ok"] and v["records"] == 4
    acts = [e["action"] for e in crd.get("/api/admin/audit?dam_id=CRD").json()["entries"]]
    assert {"alarm_rule_flag_propose", "alarm_rule_flag_reject", "alarm_rule_flag_apply"} <= set(acts)


# ------------------------------------------------------------------ open-episode protection
def test_clearing_the_flag_does_not_weaken_an_open_episode(crd):
    assert crd.get("/api/alarms/EV-00003", user=ENG).json()["requires_cdse"]
    assert _apply(crd, CDSE, flag=False, rule=FLAGGED, why="rule no longer critical").status_code == 200
    assert _current(crd, FLAGGED)["cdse_ack_required"] is False and _current(crd, FLAGGED)["threshold_version"] == 2
    ev = crd.get("/api/alarms/EV-00003", user=ENG).json()
    assert ev["requires_cdse"] and not ev["can_ack"]                  # snapshot taken at open still applies
    n = len(_chain(crd))
    assert crd.post("/api/alarms/EV-00003/ack", user=ENG, json={"rationale": "x"}).status_code == 403
    assert len(_chain(crd)) == n
    assert crd.post("/api/alarms/EV-00003/ack", user=CDSE, json={"rationale": "CDSE ack"}).status_code == 200


def test_setting_the_flag_applies_to_an_already_open_episode(crd):
    assert not crd.get("/api/alarms/EV-00001", user=ENG).json()["requires_cdse"]
    pid = _propose(crd, ADMIN).json()["proposal_id"]
    assert _decide(crd, CDSE, pid).status_code == 200
    assert crd.get("/api/alarms/EV-00001", user=ENG).json()["requires_cdse"]
    assert crd.post("/api/alarms/EV-00001/ack", user=ENG, json={"rationale": "x"}).status_code == 403


# ------------------------------------------------------------------ CDSE of another dam
def test_cdse_of_another_dam_is_refused(crd):
    """cdse.crd is CRD's CDSE and an engineer on HLD: on HLD they can neither apply nor decide (nor propose)."""
    hbase = "/api/dams/HLD/alarm-rules"
    pid = _propose(crd, ADMIN, rule="HLD-IN-1-T1", base=hbase).json()["proposal_id"]
    assert crd.get(hbase, user=CDSE).json()["is_cdse"] is False
    assert _apply(crd, CDSE, rule="HLD-IN-1-T1", base=hbase).status_code == 403
    assert _decide(crd, CDSE, pid, base=hbase).status_code == 403
    assert _decide(crd, CDSE, pid, "reject", base=hbase).status_code == 403
    assert _propose(crd, CDSE, rule="HLD-IN-1-T1", base=hbase).status_code == 403
    assert [p["status"] for p in _props(crd, "HLD")] == ["pending"]
    assert len(_chain(crd, "HLD")) == 1
    # and CRD's CDSE cannot decide an HLD proposal through the CRD URL either
    assert _decide(crd, CDSE, pid).status_code == 404


# ------------------------------------------------------------------ stale proposals
def test_stale_proposal_is_refused_after_the_rule_changed(crd):
    p1 = _propose(crd, ADMIN).json()["proposal_id"]
    p2 = _propose(crd, DAMADMIN).json()["proposal_id"]               # both against v1 (false -> true)
    assert _decide(crd, CDSE, p1).status_code == 200                    # v2 = true
    n = len(_chain(crd))
    r = _decide(crd, CDSE, p2)
    assert r.status_code == 409 and "stale" in r.json()["detail"]
    assert len(_chain(crd)) == n and crd.tc.app.state.store.flag_proposal(p2)["status"] == "pending"
    assert _current(crd)["threshold_version"] == 2
    assert _decide(crd, CDSE, p2, "reject", "superseded by v2").status_code == 200   # stale ones can be rejected


def test_stale_when_an_applied_change_is_still_waiting_to_be_published(crd, monkeypatch):
    """Prod: the new version appears only after the DA job runs. Until then a second proposal against the same base
    version is stale too (and a direct CDSE change is refused)."""
    monkeypatch.setattr(_an(crd), "apply_threshold_version", lambda payload: None)   # publication not yet applied
    p1 = _propose(crd, ADMIN).json()["proposal_id"]
    p2 = _propose(crd, DAMADMIN).json()["proposal_id"]
    assert _decide(crd, CDSE, p1).status_code == 200
    assert _current(crd)["threshold_version"] == 1                      # UC not updated yet
    r = _decide(crd, CDSE, p2)
    assert r.status_code == 409 and "waiting to be published" in r.json()["detail"]
    assert _apply(crd, CDSE).status_code == 409
