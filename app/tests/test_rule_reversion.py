"""v1.7 bulk re-version of rules set under a superseded DSSMP version (Genie; SCHEMA_CONTRACT §7 item 21)."""
import copy
import dataclasses

import pytest
from conftest import ADMIN, CDSE, ENG, VIEWER, Client
from fastapi.testclient import TestClient

from backend.main import create_app

DAMADMIN = "damadmin.crd@demo.local"
REGS = "/api/dams/CRD/dssmp-registrations"
RV = "/api/dams/CRD/rule-reversions"
NEWV = "DSSMP-CRD-2027"


def _client(demo_settings, on=False):
    c = Client(TestClient(create_app(dataclasses.replace(demo_settings, cdse_flag_require_second_approver=on))))
    assert c.put("/api/admin/roles", user=ADMIN, json={"email": DAMADMIN, "dam_id": "CRD",
                                                         "role": "dam_admin"}).status_code == 200
    return c


@pytest.fixture()
def k(demo_settings):
    return _client(demo_settings)


def _an(c):
    return c.tc.app.state.analytics_factory(None)


def _st(c):
    return c.tc.app.state.store


def _rules(c):
    return {t["threshold_id"]: t for t in _an(c).thresholds("CRD")}


def register(c, user=DAMADMIN):
    return c.post(REGS, user=user, json={
        "dssmp_version_id": NEWV, "version_no": 2, "effective_from": "2026-11-01", "change_reason": "Annual review",
        "source_ref": "DSSMR-2026-14", "document_uri": "https://docs.example/d.pdf", "document_sha256": "ab" * 32,
        "rationale": "Revised"}).json()["registration_id"]


def test_approval_screen_shows_affected_rules(k):
    rid = register(k)
    reg = k.get(REGS, user=CDSE).json()["registrations"][0]
    assert reg["affected_count"] == len(_rules(k)) and sorted(reg["affected_rules"]) == sorted(_rules(k))
    assert k.get(RV, user=CDSE, params={"to": NEWV}).json()["affected_count"] == len(_rules(k))
    assert rid


def test_approve_with_bulk_reversion_end_to_end(k):
    before = copy.deepcopy(_rules(k))
    alarms_before = k.get("/api/alarms", user=ADMIN).json()
    rid = register(k)
    r = k.post(f"{REGS}/{rid}/approve", user=CDSE, json={"rationale": "Approved", "propose_reversion": True})
    assert r.status_code == 200, r.text
    rv = r.json()["reversion"]
    assert rv["status"] == "pending" and sorted(x["threshold_id"] for x in rv["rules"]) == sorted(before)
    # the 409 stays until the re-version is approved (no grace path)
    assert k.post("/api/dams/CRD/alarm-rules/CRD-PZ-2-T1/cdse-flag", user=CDSE,
                  json={"cdse_ack_required": True, "rationale": "x"}).status_code == 409
    a = k.post(f"{RV}/{rv['proposal_id']}/approve", user=CDSE, json={"rationale": "Re-version all"})
    assert a.status_code == 200, a.text
    pubs = [p for p in _st(k).ref_publications("CRD") if p["payload"].get("cause") == "dssmp_reversion"]
    assert len(pubs) == len(before) and len(a.json()["pub_ids"]) == len(before)
    after = _rules(k)
    for tid, old in before.items():                                       # N+1, only the DSSMP version changed
        new = after[tid]
        assert new["dssmp_version_id"] == NEWV and int(new["threshold_version"]) == int(old["threshold_version"]) + 1
        assert bool(new["cdse_ack_required"]) == bool(old["cdse_ack_required"])
        for key in ("tier", "deadband", "persistence_on", "persistence_off"):
            assert new.get(key) == old.get(key)
    acts = [x["action_type"] for x in _st(k).chain_rows("CRD")]
    assert acts == ["dssmp_version_propose", "dssmp_version_approve", "rule_reversion_propose",
                    "rule_reversion_approve"]
    assert all(p["payload"]["approval_record_hash"] == _st(k).chain_rows("CRD")[-1]["record_hash"] for p in pubs)
    assert k.get("/api/alarms", user=ADMIN).json() == alarms_before       # alarms / open episodes untouched
    assert k.post("/api/dams/CRD/alarm-rules/CRD-PZ-2-T1/cdse-flag", user=CDSE,
                  json={"cdse_ack_required": True, "rationale": "x"}).status_code == 200
    r = k.post(RV, user=CDSE, json={"to_dssmp_version_id": NEWV, "rationale": "again"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nothing_to_reversion"


def _approved_dssmp(k):
    rid = register(k)
    assert k.post(f"{REGS}/{rid}/approve", user=CDSE, json={"rationale": "ok"}).status_code == 200


@pytest.mark.parametrize("user,code", [(ENG, 403), (VIEWER, 403), (DAMADMIN, 200), (ADMIN, 200), (CDSE, 200)])
def test_who_may_propose(k, user, code):
    _approved_dssmp(k)
    assert k.post(RV, user=user, json={"to_dssmp_version_id": NEWV, "rationale": "x"}).status_code == code


@pytest.mark.parametrize("user", [DAMADMIN, ADMIN, ENG, "cdse.bfk@demo.local"])
def test_only_the_dams_cdse_decides(k, user):
    _approved_dssmp(k)
    pid = k.post(RV, user=DAMADMIN, json={"to_dssmp_version_id": NEWV, "rationale": "x"}).json()["proposal_id"]
    for verb in ("approve", "reject"):
        assert k.post(f"{RV}/{pid}/{verb}", user=user, json={"rationale": "x"}).status_code in (401, 403)
    assert k.post(f"{RV}/{pid}/approve", user=CDSE, json={"rationale": " "}).status_code == 422
    assert k.post(f"{RV}/{pid}/reject", user=CDSE, json={"rationale": "no"}).status_code == 200
    assert [p for p in _st(k).ref_publications("CRD") if p["payload"].get("cause")] == []


def test_target_must_be_loaded(k, monkeypatch):
    monkeypatch.setattr(_an(k), "load_dssmp_outbox", lambda row: ("pending", None, None))
    rid = register(k)
    r = k.post(f"{REGS}/{rid}/approve", user=CDSE, json={"rationale": "ok", "propose_reversion": True})
    assert r.json()["reversion"] is None and r.json()["reversion_error"]["code"] == "dssmp_not_found"
    r = k.post(RV, user=CDSE, json={"to_dssmp_version_id": NEWV, "rationale": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dssmp_not_found"


def test_stale_rule_refuses_the_whole_reversion(k):
    _approved_dssmp(k)
    pid = k.post(RV, user=DAMADMIN, json={"to_dssmp_version_id": NEWV, "rationale": "x"}).json()["proposal_id"]
    rule = dict(_rules(k)["CRD-PZ-2-T1"])
    rule.update(threshold_version=int(rule["threshold_version"]) + 1)      # changed behind the proposal
    _an(k).apply_threshold_version({"insert": {kk: v for kk, v in rule.items() if kk not in (
        "rule", "evaluated", "rule_label", "upper_limit", "lower_limit", "abs_limit", "rate_limit_per_day",
        "pfm_ids", "response_action")}})
    r = k.post(f"{RV}/{pid}/approve", user=CDSE, json={"rationale": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "reversion_stale"
    assert [p for p in _st(k).ref_publications("CRD") if p["payload"].get("cause")] == []


def test_self_approval_follows_the_provisional_setting(demo_settings):
    c = _client(demo_settings, True)
    rid = register(c)
    assert c.post(f"{REGS}/{rid}/approve", user=CDSE, json={"rationale": "ok"}).status_code == 200
    pid = c.post(RV, user=CDSE, json={"to_dssmp_version_id": NEWV, "rationale": "x"}).json()["proposal_id"]
    r = c.post(f"{RV}/{pid}/approve", user=CDSE, json={"rationale": "x"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "self_approval_forbidden"


def test_cdse_queue_lists_pending_reversions(k):
    _approved_dssmp(k)
    pid = k.post(RV, user=DAMADMIN, json={"to_dssmp_version_id": NEWV, "rationale": "x"}).json()["proposal_id"]
    assert [p["proposal_id"] for p in k.get("/api/rule-reversions/pending", user=CDSE).json()["proposals"]] == [pid]
    assert k.get("/api/rule-reversions/pending", user="cdse.bfk@demo.local").json()["proposals"] == []


def test_affected_count_prefers_the_da_function_with_a_fallback(k, monkeypatch):
    """ref.dssmp_affected_rules(dam, to) drives the preview / approval-screen count when it answers; the app's own
    computation is the fallback (function missing or unreadable). The proposal itself always uses current rules."""
    _approved_dssmp(k)
    calls = []
    def da(dam, to):
        calls.append((dam, to))
        return [{"threshold_id": "CRD-PZ-2-T1", "threshold_version": 1, "dssmp_version_id": None,
                 "cdse_ack_required": False}]
    monkeypatch.setattr(_an(k), "dssmp_affected_rules", da, raising=False)
    r = k.get(RV, user=CDSE, params={"to": NEWV}).json()
    assert (r["affected_count"], r["affected_source"], calls) == (1, "ref.dssmp_affected_rules", [("CRD", NEWV)])
    def boom(dam, to):
        raise RuntimeError("function missing")
    monkeypatch.setattr(_an(k), "dssmp_affected_rules", boom, raising=False)
    r = k.get(RV, user=CDSE, params={"to": NEWV}).json()
    assert (r["affected_count"], r["affected_source"]) == (len(_rules(k)), "app")


def test_rules_without_a_dssmp_are_re_versionable(k):
    """dssmp_missing rules are exactly what a re-version fixes: not refused as 'lacking' publication fields."""
    _approved_dssmp(k)
    for t in _an(k).d["thresholds"]:
        if t["threshold_id"] == "CRD-PZ-2-T1":
            t["dssmp_version_id"] = None
    r = k.post(RV, user=CDSE, json={"to_dssmp_version_id": NEWV, "rationale": "fix missing"})
    assert r.status_code == 200, r.text
    assert "CRD-PZ-2-T1" in [x["threshold_id"] for x in r.json()["rules"]]


def test_publisher_refusal_codes_are_exposed_per_proposal(k):
    rid = register(k)
    rv = k.post(f"{REGS}/{rid}/approve", user=CDSE, json={"rationale": "ok", "propose_reversion": True}).json()["reversion"]
    pub_ids = k.post(f"{RV}/{rv['proposal_id']}/approve", user=CDSE, json={"rationale": "go"}).json()["pub_ids"]
    st = _st(k)
    st.execute("UPDATE {s}ref_change_publication SET status = 'rejected_invalid', "
               "message = 'reversion_fields_changed: tier differs from version N' WHERE pub_id = %s", (pub_ids[0],))
    (p,) = [x for x in k.get(RV, user=CDSE).json()["proposals"] if x["proposal_id"] == rv["proposal_id"]]
    bad = [x for x in p["publications"] if x["pub_id"] == pub_ids[0]][0]
    assert (bad["status"], bad["code"]) == ("rejected_invalid", "reversion_fields_changed") and bad["threshold_id"]
    assert len(p["publications"]) == len(pub_ids)


def test_pub_code_parses_only_a_code_prefix():
    from backend.routers.rule_reversion import pub_code
    assert pub_code("payload_invalid: missing insert") == "payload_invalid"
    assert pub_code("Something went wrong: x") is None and pub_code(None) is None and pub_code("") is None
