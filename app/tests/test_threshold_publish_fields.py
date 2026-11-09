"""v1.6 DA follow-up 2: the go-live and flag-apply (N+1) threshold rows carry the ref.threshold NOT NULL columns
dssmp_version_id, tier, deadband, persistence_on, persistence_off. N+1 copies them from version N; go-live takes them
from the wizard, which requires them. Nothing is ever defaulted."""
import copy

import pytest
from conftest import ADMIN, CDSE
from test_onboarding import _fill, good_draft

from backend import onboarding as ob

FIELDS = ("dssmp_version_id", "tier", "deadband", "persistence_on", "persistence_off")
RULE = "CRD-PZ-2-T1"
BASE = "/api/dams/CRD/alarm-rules"


def _an(c):
    return c.tc.app.state.analytics_factory(None)


def _errors(d):
    return [i["message"] for i in ob.validate(d, set(), {}) if i["severity"] == "error" and i["step"] == "thresholds"]


def test_contract_field_list():
    assert ob.PUBLISH_REQUIRED == FIELDS


def test_good_draft_publishes_exactly_what_was_entered():
    d = good_draft()
    assert _errors(d) == []
    ths = ob.golive_payload(d)["thresholds"]
    assert [tuple(t[k] for k in FIELDS) for t in ths] == [
        ("DSSMP-NEW-2026", "AL1", 0.05, 2, 3), ("DSSMP-NEW-2026", "AL2", 0.05, 2, 3), ("DSSMP-NEW-2026", "AL1", 0.1, 2, 3)]


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("empty", ["drop", None, "", "  "])
def test_each_missing_field_is_a_go_live_error(field, empty):
    d = good_draft()
    if empty == "drop":
        d["thresholds"][0].pop(field)
    else:
        d["thresholds"][0][field] = empty
    errs = _errors(d)
    assert len(errs) == 1 and field in errs[0], errs
    with pytest.raises(ValueError, match=field):                 # payload builder never defaults either
        ob.golive_payload(d)


@pytest.mark.parametrize("field,value", [
    ("tier", "L1"), ("tier", "AL0"), ("tier", "al1"), ("deadband", -0.1), ("deadband", "x"), ("deadband", True),
    ("persistence_on", 0), ("persistence_on", 1.5), ("persistence_on", True), ("persistence_off", -2),
    ("persistence_off", "two")])
def test_invalid_values_are_errors(field, value):
    d = good_draft()
    d["thresholds"][0][field] = value
    errs = _errors(d)
    assert len(errs) == 1 and field in errs[0], errs


@pytest.mark.parametrize("tier", ["DESIGN_BASIS", "THRESHOLD", "AL1", "AL12"])
def test_valid_tiers(tier):
    d = good_draft()
    d["thresholds"][0]["tier"] = tier
    assert _errors(d) == []


def test_error_messages_show_units():
    d = good_draft()
    for k in FIELDS[2:]:
        d["thresholds"][0].pop(k)
    errs = _errors(d)
    assert "deadband (m) is required" in errs
    assert "persistence_on (consecutive readings to raise) is required" in errs
    assert "persistence_off (consecutive readings to clear) is required" in errs


def test_go_live_refused_without_the_fields_then_publishes_them(c):
    did = c.post("/api/onboarding/drafts").json()["draft_id"]
    d = good_draft()
    d["thresholds"][1].pop("persistence_off")
    _fill(c, did, d)
    r = c.post(f"/api/onboarding/drafts/{did}/golive")
    assert r.status_code == 422
    assert any("persistence_off" in i["message"] for i in r.json()["detail"]["issues"])
    assert "NEW" not in [x["dam_id"] for x in c.get("/api/portfolio").json()["dams"]]
    _fill(c, did, good_draft())
    assert c.post(f"/api/onboarding/drafts/{did}/golive").status_code == 200
    live = sorted(_an(c).thresholds("NEW"), key=lambda t: (t["instrument_id"], t["level"]))
    assert [(t["tier"], t["deadband"], t["persistence_on"], t["persistence_off"], t["dssmp_version_id"])
            for t in live] == [("AL1", 0.05, 2, 3, "DSSMP-NEW-2026"), ("AL2", 0.05, 2, 3, "DSSMP-NEW-2026"),
                               ("AL1", 0.1, 2, 3, "DSSMP-NEW-2026")]


def test_seeded_demo_rules_carry_the_fields(c):
    for dam in ("CRD", "HLD", "PHR", "BFK"):
        for t in _an(c).thresholds(dam):
            assert all(t.get(k) not in (None, "") for k in FIELDS), (dam, t["threshold_id"])


def test_flag_apply_copies_the_fields_from_version_n(c):
    before = next(t for t in _an(c).thresholds("CRD") if t["threshold_id"] == RULE)
    r = c.post(f"{BASE}/{RULE}/cdse-flag", user=CDSE, json={"cdse_ack_required": True, "rationale": "CDSE decision"})
    assert r.status_code == 200, r.text
    (pub,) = c.tc.app.state.store.ref_publications("CRD")
    ins = pub["payload"]["insert"]
    assert ins["threshold_version"] == 2
    assert {k: ins[k] for k in FIELDS} == {k: before[k] for k in FIELDS}
    after = next(t for t in _an(c).thresholds("CRD") if t["threshold_id"] == RULE)
    assert {k: after[k] for k in FIELDS} == {k: before[k] for k in FIELDS}


@pytest.mark.parametrize("field", FIELDS)
def test_flag_apply_refused_when_version_n_lacks_a_field(c, field):
    an = _an(c)
    for t in an.d["thresholds"]:
        if t["threshold_id"] == RULE:
            t[field] = None
    st = c.tc.app.state.store
    n_chain = len(st.chain_rows("CRD"))
    r = c.post(f"{BASE}/{RULE}/cdse-flag", user=CDSE, json={"cdse_ack_required": True, "rationale": "CDSE decision"})
    assert r.status_code == 409 and field in r.json()["detail"] and "no defaults" in r.json()["detail"]
    assert len(st.chain_rows("CRD")) == n_chain and st.ref_publications("CRD") == []
    # the proposal route is refused the same way at approval, and the proposal stays pending
    pid = c.post(f"{BASE}/{RULE}/cdse-flag/proposals", user=ADMIN,
                 json={"cdse_ack_required": True, "rationale": "trend"}).json()["proposal_id"]
    n_chain = len(st.chain_rows("CRD"))
    assert c.post(f"{BASE}/proposals/{pid}/approve", user=CDSE, json={"rationale": "ok"}).status_code == 409
    assert len(st.chain_rows("CRD")) == n_chain and st.ref_publications("CRD") == []
    assert [p["status"] for p in st.flag_proposals("CRD")] == ["pending"]
    assert all(t.get(field) is None for t in copy.deepcopy(an.d["thresholds"]) if t["threshold_id"] == RULE)
