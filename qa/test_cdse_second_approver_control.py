"""Independent checks for CDSE_FLAG_REQUIRE_SECOND_APPROVER (f0dd9c8 / 23c4a42 equivalent).

Default off ≡ QA-048 today (CDSE may self-approve / direct-apply). On: self-approve 403, direct apply 403.
Setting is deploy-time (env/app.yaml), not a client-togglable audited flip.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

from backend import main as app_main  # noqa: E402
from backend.config import Settings, approval_policy  # noqa: E402

ADMIN, ENG, CDSE = "admin@demo.local", "engineer.crd@demo.local", "cdse.crd@demo.local"
RULE = "CRD-IN-1-T2"


def _h(email):
    return {"x-forwarded-user": f"uid-{email.lower()}", "x-forwarded-email": email}


def _app(tmp_path, second: bool):
    s = Settings(demo_mode=True, static_dir=str(tmp_path / "nodist"),
                 cdse_flag_require_second_approver=second)
    app = app_main.create_app(s)
    st = app.state.store
    if st.get_user(CDSE) is None:
        st.upsert_user(CDSE, "Chris CDSE", False, "qa054")
        st.set_role(CDSE, "CRD", "engineer", "qa054")
    return TestClient(app), app


@pytest.fixture()
def off(tmp_path):
    tc, app = _app(tmp_path, False)
    yield tc, app
    app.state.store._conn.close()


@pytest.fixture()
def on(tmp_path):
    tc, app = _app(tmp_path, True)
    yield tc, app
    app.state.store._conn.close()


def test_default_setting_is_off(monkeypatch):
    monkeypatch.delenv("CDSE_FLAG_REQUIRE_SECOND_APPROVER", raising=False)
    assert Settings().cdse_flag_require_second_approver is False
    pol = approval_policy(Settings())
    assert pol["cdse_flag_require_second_approver"] is False and pol["provisional"] is True


def test_off_matches_qa048_cdse_may_self_approve_and_direct_apply(off):
    tc, app = off
    an = app.state.analytics_factory(None)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    target = not bool(cur.get("cdse_ack_required"))
    # direct apply allowed
    r = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag", headers=_h(CDSE),
                json={"cdse_ack_required": target, "rationale": "direct"})
    assert r.status_code == 200, r.text
    # self-approve of own proposal allowed when off
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    target2 = not bool(cur.get("cdse_ack_required"))
    p = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag/proposals", headers=_h(CDSE),
                json={"cdse_ack_required": target2, "rationale": "self prop"})
    assert p.status_code == 200, p.text
    a = tc.post(f"/api/dams/CRD/alarm-rules/proposals/{p.json()['proposal_id']}/approve",
                headers=_h(CDSE), json={"rationale": "self approve"})
    assert a.status_code == 200, a.text


def test_on_blocks_direct_apply_and_self_approve(on):
    tc, app = on
    an = app.state.analytics_factory(None)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    target = not bool(cur.get("cdse_ack_required"))
    r = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag", headers=_h(CDSE),
                json={"cdse_ack_required": target, "rationale": "direct"})
    assert r.status_code == 403
    body = r.json()["detail"]
    assert body["code"] == "second_approver_required"
    # propose then self-approve forbidden
    p = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag/proposals", headers=_h(CDSE),
                json={"cdse_ack_required": target, "rationale": "prop"})
    assert p.status_code == 200, p.text
    pid = p.json()["proposal_id"]
    a = tc.post(f"/api/dams/CRD/alarm-rules/proposals/{pid}/approve",
                headers=_h(CDSE), json={"rationale": "self"})
    assert a.status_code == 403 and a.json()["detail"]["code"] == "self_approval_forbidden"
    # admin propose + CDSE approve works
    p2 = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag/proposals", headers=_h(ADMIN),
                 json={"cdse_ack_required": target, "rationale": "admin prop"})
    # may 409 if already pending for same flag from above — reject first
    tc.post(f"/api/dams/CRD/alarm-rules/proposals/{pid}/reject", headers=_h(CDSE),
            json={"rationale": "withdraw"})
    p2 = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag/proposals", headers=_h(ADMIN),
                 json={"cdse_ack_required": target, "rationale": "admin prop2"})
    assert p2.status_code == 200, p2.text
    a2 = tc.post(f"/api/dams/CRD/alarm-rules/proposals/{p2.json()['proposal_id']}/approve",
                 headers=_h(CDSE), json={"rationale": "cdse approves admin"})
    assert a2.status_code == 200, a2.text


def test_setting_not_client_togglable_and_not_request_body(on):
    tc, app = on
    # forging body/query cannot turn the rule off for this process
    r = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag",
                headers={**_h(CDSE), "x-cdse-flag-require-second-approver": "false"},
                json={"cdse_ack_required": True, "rationale": "x",
                      "cdse_flag_require_second_approver": False})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "second_approver_required"
    # no public write API for the setting
    assert not any("/approval_policy" in (getattr(rt, "path", "") or "")
                   for rt in app.routes if getattr(rt, "methods", None) and "POST" in rt.methods)


def test_pending_proposal_survives_policy_read_flags(on):
    """Flipping the setting is deploy-time; a pending proposal under 'on' exposes can_approve=false for owner."""
    tc, app = on
    an = app.state.analytics_factory(None)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    target = not bool(cur.get("cdse_ack_required"))
    p = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag/proposals", headers=_h(CDSE),
                json={"cdse_ack_required": target, "rationale": "pending"})
    assert p.status_code == 200
    listing = tc.get("/api/dams/CRD/alarm-rules", headers=_h(CDSE)).json()
    assert listing["approval_policy"]["cdse_flag_require_second_approver"] is True
    assert listing["can_apply"] is False
    mine = next(x for x in listing["pending"] if x["proposal_id"] == p.json()["proposal_id"])
    assert mine["own"] is True and mine["can_approve"] is False and mine["can_reject"] is True


def test_me_exposes_policy_readonly(off, on):
    for tc, flag in ((off[0], False), (on[0], True)):
        me = tc.get("/api/me", headers=_h(ENG)).json()
        assert me["approval_policy"]["cdse_flag_require_second_approver"] is flag
        assert me["approval_policy"]["provisional"] is True
