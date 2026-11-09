"""dssmp_version_id must be a ref.dssmp_version row of the SAME dam (fail closed): 422 on go-live (and an error in
validate), 409 on the flag-apply N+1. Unknown id, another dam's id and an unreadable table are all refused."""
import pytest
from conftest import ADMIN, CDSE
from test_onboarding import _fill, good_draft, register_dssmp

from backend import onboarding as ob

RULE = "CRD-PZ-2-T1"
BASE = "/api/dams/CRD/alarm-rules"


def _an(c):
    return c.tc.app.state.analytics_factory(None)


def _boom(ids):
    raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")


# ------------------------------------------------------------------ pure check
def test_problems_unknown_other_dam_and_ok():
    rows = [{"dssmp_version_id": "D-A", "dam_id": "A"}, {"dssmp_version_id": "D-B", "dam_id": "B"}]
    look = lambda ids: [r for r in rows if r["dssmp_version_id"] in ids]  # noqa: E731
    assert ob.dssmp_problems(look, "A", ["D-A", " D-A "]) == {}
    p = ob.dssmp_problems(look, "A", ["D-A", "D-B", "D-X"])
    assert set(p) == {"D-B", "D-X"}
    assert "belongs to dam B, not A" in p["D-B"] and "does not exist in ref.dssmp_version" in p["D-X"]
    assert ob.dssmp_problems(look, "A", [None, "", "  "]) == {}       # missing ids: the NOT NULL check reports them


def test_problems_fail_closed_when_lookup_fails():
    p = ob.dssmp_problems(_boom, "A", ["D-A"])
    assert list(p) == ["D-A"] and "cannot be verified" in p["D-A"] and "RuntimeError" in p["D-A"]


# ------------------------------------------------------------------ go-live: 422
def _draft_id(c, d, dssmp=True):
    did = c.post("/api/onboarding/drafts").json()["draft_id"]
    _fill(c, did, d, dssmp=dssmp)
    return did


def test_go_live_refused_when_dssmp_version_not_registered(c):
    did = _draft_id(c, good_draft(), dssmp=False)
    v = c.post(f"/api/onboarding/drafts/{did}/validate").json()
    assert not v["ok"] and any("DSSMP-NEW-2026 does not exist" in i["message"] for i in v["issues"])
    r = c.post(f"/api/onboarding/drafts/{did}/golive")
    assert r.status_code == 422
    assert {i["step"] for i in r.json()["detail"]["issues"] if "dssmp" in i["message"]} == {"thresholds"}
    assert "NEW" not in [x["dam_id"] for x in c.get("/api/portfolio").json()["dams"]]
    register_dssmp(c)
    assert c.post(f"/api/onboarding/drafts/{did}/golive").status_code == 200


def test_go_live_refused_for_another_dams_dssmp_version(c):
    d = good_draft()
    d["thresholds"][0]["dssmp_version_id"] = "DSSMP-CRD-2026"           # exists, but for CRD
    did = _draft_id(c, d)
    r = c.post(f"/api/onboarding/drafts/{did}/golive")
    assert r.status_code == 422
    assert any("belongs to dam CRD, not NEW" in i["message"] for i in r.json()["detail"]["issues"])


def test_go_live_fails_closed_when_ref_dssmp_version_unreadable(c, monkeypatch):
    did = _draft_id(c, good_draft())
    monkeypatch.setattr(_an(c), "dssmp_versions", _boom)
    r = c.post(f"/api/onboarding/drafts/{did}/golive")
    assert r.status_code == 422 and any("cannot be verified" in i["message"] for i in r.json()["detail"]["issues"])


# ------------------------------------------------------------------ flag apply N+1: 409
def _set(c, value):
    for t in _an(c).d["thresholds"]:
        if t["threshold_id"] == RULE:
            t["dssmp_version_id"] = value


@pytest.mark.parametrize("value,msg", [("DSSMP-XX-1", "does not exist"), ("DSSMP-HLD-2026", "belongs to dam HLD")])
def test_flag_apply_refused_when_dssmp_version_invalid(c, value, msg):
    _set(c, value)
    st = c.tc.app.state.store
    r = c.post(f"{BASE}/{RULE}/cdse-flag", user=CDSE, json={"cdse_ack_required": True, "rationale": "decision"})
    assert r.status_code == 409 and msg in r.json()["detail"], r.text
    assert st.chain_rows("CRD") == [] and st.ref_publications("CRD") == []
    pid = c.post(f"{BASE}/{RULE}/cdse-flag/proposals", user=ADMIN,
                 json={"cdse_ack_required": True, "rationale": "trend"}).json()["proposal_id"]
    assert c.post(f"{BASE}/proposals/{pid}/approve", user=CDSE, json={"rationale": "ok"}).status_code == 409
    assert st.flag_proposal(pid)["status"] == "pending" and st.ref_publications("CRD") == []


def test_flag_apply_fails_closed_when_lookup_fails(c, monkeypatch):
    monkeypatch.setattr(_an(c), "dssmp_versions", _boom)
    r = c.post(f"{BASE}/{RULE}/cdse-flag", user=CDSE, json={"cdse_ack_required": True, "rationale": "decision"})
    assert r.status_code == 409 and "cannot be verified" in r.json()["detail"]
    assert c.tc.app.state.store.ref_publications("CRD") == []


def test_seeded_rules_reference_their_own_dams_dssmp_version(c):
    an = _an(c)
    for s in an.sites():
        ids = {t["dssmp_version_id"] for t in an.thresholds(s["dam_id"])}
        assert ob.dssmp_problems(an.dssmp_versions, s["dam_id"], ids) == {}, s["dam_id"]
