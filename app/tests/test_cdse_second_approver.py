"""PROVISIONAL approval rule for live cdse_ack_required edits (Genie, v1.6): setting CDSE_FLAG_REQUIRE_SECOND_APPROVER.
false (default) = current behaviour (CDSE sets/clears directly, may approve own proposal). true = proposals only,
approver must be the dam's CDSE and not the proposer; dam_admin / admin still only propose."""
import dataclasses

import pytest
from conftest import ADMIN, CDSE, ENG, Client
from fastapi.testclient import TestClient

from backend.config import Settings, approval_policy
from backend.main import create_app

DAMADMIN = "damadmin.crd@demo.local"
RULE = "CRD-PZ-2-T1"
BASE = "/api/dams/CRD/alarm-rules"


def _client(demo_settings, on):
    c = Client(TestClient(create_app(dataclasses.replace(demo_settings, cdse_flag_require_second_approver=on))))
    assert c.put("/api/admin/roles", user=ADMIN, json={"email": DAMADMIN, "dam_id": "CRD",
                                                         "role": "dam_admin"}).status_code == 200
    return c


@pytest.fixture()
def on(demo_settings):
    return _client(demo_settings, True)


@pytest.fixture()
def off(demo_settings):
    return _client(demo_settings, False)


def _an(c):
    return c.tc.app.state.analytics_factory(None)


def _flag(c, rule=RULE):
    return next(t for t in _an(c).thresholds("CRD") if t["threshold_id"] == rule)["cdse_ack_required"]


def _chain(c):
    return [r["action_type"] for r in c.tc.app.state.store.chain_rows("CRD")]


def _propose(c, user, flag=True):
    return c.post(f"{BASE}/{RULE}/cdse-flag/proposals", user=user, json={"cdse_ack_required": flag, "rationale": "why"})


def _apply(c, user, flag=True):
    return c.post(f"{BASE}/{RULE}/cdse-flag", user=user, json={"cdse_ack_required": flag, "rationale": "why"})


def _decide(c, user, pid, verb="approve"):
    return c.post(f"{BASE}/proposals/{pid}/{verb}", user=user, json={"rationale": "reviewed"})


def _status(c, pid):
    return c.tc.app.state.store.flag_proposal(pid)["status"]


# ------------------------------------------------------------------ setting
def test_setting_defaults_off_and_reads_the_env(monkeypatch):
    monkeypatch.delenv("CDSE_FLAG_REQUIRE_SECOND_APPROVER", raising=False)
    assert Settings().cdse_flag_require_second_approver is False
    for v, want in (("true", True), ("1", True), ("on", True), ("false", False), ("0", False)):
        monkeypatch.setenv("CDSE_FLAG_REQUIRE_SECOND_APPROVER", v)
        assert Settings().cdse_flag_require_second_approver is want


@pytest.mark.parametrize("flag", [False, True])
def test_policy_exposed_on_me_and_rules_and_marked_provisional(demo_settings, flag):
    c = _client(demo_settings, flag)
    pol = c.get("/api/me", user=ENG).json()["approval_policy"]
    assert pol == approval_policy(c.tc.app.state.settings)
    assert pol["provisional"] is True and pol["cdse_flag_require_second_approver"] is flag
    assert (pol["cdse_may_apply_directly"], pol["cdse_may_self_approve"]) == (not flag, not flag)
    assert pol["note"].startswith("Approval rule provisional")
    if not flag:
        assert pol["note"] == "Approval rule provisional: currently the dam CDSE may self-approve"
    assert c.get(BASE, user=CDSE).json()["approval_policy"] == pol


# ------------------------------------------------------------------ off (default): current behaviour kept
def test_off_cdse_applies_directly_and_self_approves(off):
    assert _apply(off, CDSE).status_code == 200 and _flag(off) is True
    pid = _propose(off, CDSE, flag=False).json()["proposal_id"]
    view = off.get(BASE, user=CDSE).json()
    assert view["can_apply"] is True
    (p,) = view["pending"]
    assert (p["own"], p["can_approve"], p["can_reject"]) == (True, True, True)
    assert _decide(off, CDSE, pid).status_code == 200 and _status(off, pid) == "applied" and _flag(off) is False
    assert _chain(off) == ["alarm_rule_flag_apply", "alarm_rule_flag_propose", "alarm_rule_flag_apply"]


@pytest.mark.parametrize("user", [DAMADMIN, ADMIN])
def test_off_admins_still_only_propose(off, user):
    pid = _propose(off, user).json()["proposal_id"]
    assert _apply(off, user).status_code == 403 and _decide(off, user, pid).status_code == 403


# ------------------------------------------------------------------ on: second approver required
def test_on_cdse_cannot_apply_directly(on):
    r = _apply(on, CDSE)
    assert r.status_code == 403
    d = r.json()["detail"]
    assert d["code"] == "second_approver_required" and d["provisional"] is True
    assert d["setting"] == "CDSE_FLAG_REQUIRE_SECOND_APPROVER" and "proposal" in d["message"]
    assert _flag(on) is False and _chain(on) == [] and on.tc.app.state.store.ref_publications("CRD") == []
    assert on.get(BASE, user=CDSE).json()["can_apply"] is False


def test_on_cdse_own_proposal_can_only_be_rejected_withdrawn(on):
    r = _propose(on, CDSE)
    assert r.status_code == 200
    pid = r.json()["proposal_id"]
    view = on.get(BASE, user=CDSE).json()
    (p,) = view["pending"]
    assert (p["own"], p["can_approve"], p["can_reject"]) == (True, False, True)
    r = _decide(on, CDSE, pid)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "self_approval_forbidden"
    assert _status(on, pid) == "pending" and _flag(on) is False and _chain(on) == ["alarm_rule_flag_propose"]
    assert _decide(on, CDSE, pid, "reject").status_code == 200 and _status(on, pid) == "rejected"
    assert _chain(on) == ["alarm_rule_flag_propose", "alarm_rule_flag_reject"] and _flag(on) is False


def test_on_self_approval_matched_case_insensitively(on):
    pid = _propose(on, CDSE).json()["proposal_id"]
    r = _decide(on, CDSE.upper(), pid)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "self_approval_forbidden"
    assert _status(on, pid) == "pending"


@pytest.mark.parametrize("proposer", [DAMADMIN, ADMIN])
def test_on_cdse_approves_someone_elses_proposal(on, proposer):
    pid = _propose(on, proposer).json()["proposal_id"]
    (p,) = on.get(BASE, user=CDSE).json()["pending"]
    assert (p["own"], p["can_approve"]) == (False, True)
    r = _decide(on, CDSE, pid)
    assert r.status_code == 200, r.text
    assert _status(on, pid) == "applied" and _flag(on) is True
    assert _chain(on) == ["alarm_rule_flag_propose", "alarm_rule_flag_apply"]
    pub = on.tc.app.state.store.ref_publications("CRD")[0]
    assert pub["payload"]["insert"]["set_by"] == CDSE


@pytest.mark.parametrize("user", [DAMADMIN, ADMIN])
def test_on_admins_still_only_propose_and_never_approve(on, user):
    pid = _propose(on, user).json()["proposal_id"]
    r = _apply(on, user)
    assert r.status_code == 403 and "dam_admin and admin may only propose" in r.json()["detail"]
    assert _decide(on, user, pid).status_code == 403
    assert _status(on, pid) == "pending" and _flag(on) is False


def test_on_approver_must_still_be_the_dams_cdse(on):
    pid = _propose(on, CDSE).json()["proposal_id"]
    assert _decide(on, ENG, pid).status_code == 403                 # engineer, not a CDSE
    assert _decide(on, "cdse.hld@demo.local", pid).status_code == 403  # another dam's CDSE
    # a second CDSE grant for the same dam (ref.project_membership role CDSE) is a valid second approver
    _an(on).d.setdefault("project_membership", []).append({"dam_id": "CRD", "principal": ENG, "role": "CDSE"})
    r = _decide(on, ENG, pid)
    assert r.status_code == 200, r.text
    assert _status(on, pid) == "applied" and _flag(on) is True
