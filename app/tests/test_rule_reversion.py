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
