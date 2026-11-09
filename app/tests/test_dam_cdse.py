"""v1.7 first-CDSE bootstrap and CDSE changes (Genie; SCHEMA_CONTRACT §7 item 20): platform admin assigns the first
CDSE of a not-live dam with no CDSE; later changes are admin proposals the CURRENT CDSE approves or rejects. Chained,
reason_code + rationale required, outbox to the DA (the app never writes ref.dam)."""
import dataclasses
import json
import sqlite3

import pytest
from conftest import ADMIN, CDSE, ENG, VIEWER, Client
from fastapi.testclient import TestClient
from test_onboarding import _fill, good_draft

from backend import audit_chain as ac
from backend.main import create_app

DAMADMIN = "damadmin.crd@demo.local"
NEW_CDSE = "cdse.new@demo.local"
NEXT_CDSE = "cdse2.crd@demo.local"


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


def assign(c, dam="NEW", user=ADMIN, **kw):
    b = {"cdse_id": NEW_CDSE, "reason_code": "initial_appointment", "rationale": "Named by the owner's board"}
    b.update(kw)
    return c.post(f"/api/dams/{dam}/cdse/assign", user=user, json={k: v for k, v in b.items() if v is not None})


def change(c, dam="CRD", user=ADMIN, **kw):
    b = {"cdse_id": NEXT_CDSE, "reason_code": "retirement", "rationale": "Retiring at year end"}
    b.update(kw)
    return c.post(f"/api/dams/{dam}/cdse/changes", user=user, json={k: v for k, v in b.items() if v is not None})


# ------------------------------------------------------------------ first CDSE
def test_admin_assigns_first_cdse_and_go_live_then_passes_the_check(k):
    did = k.post("/api/onboarding/drafts").json()["draft_id"]
    _fill(k, did, good_draft(), dssmp=False)
    _an(k).d["dssmp_versions"].append({"dssmp_version_id": "DSSMP-NEW-2026", "dam_id": "NEW", "version_no": 1,
                                       "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
                                       "status": "approved"})
    r = k.post(f"/api/onboarding/drafts/{did}/golive")
    # NEW is not in ref.dam and has no assignment: no ref.v_dam_go_live_readiness row -> blocked (fail closed)
    assert r.status_code == 422 and {i.get("code") for i in r.json()["detail"]["issues"]} == {"go_live_readiness_missing"}
    s = k.get("/api/dams/NEW/cdse", user=ADMIN).json()
    assert s["can_assign"] and s["live"] is False and s["current_cdse"] == []
    r = assign(k)
    assert r.status_code == 200, r.text
    (rec,) = _st(k).chain_rows("NEW")
    assert (rec["action_type"], rec["subject_type"], rec["subject_id"], rec["decision"], rec[ac.ACTOR_ROLE_FIELD]) == (
        "dam_cdse_assign", "dam", "NEW", "assign", "admin")
    (o,) = _st(k).rows("dam_cdse_outbox", "outbox_id", dam_id="NEW")
    assert (o["kind"], o["new_cdse_id"], o["change_reason_code"], o["approval_record_hash"], o["status"]) == (
        "assign", NEW_CDSE, "initial_appointment", rec["record_hash"], "loaded")   # demo load job
    assert _an(k).cdse_principals("NEW") == [NEW_CDSE]
    assert k.get("/api/dams/NEW/cdse", user=ADMIN).json()["can_assign"] is False
    assert k.post(f"/api/onboarding/drafts/{did}/golive").status_code == 200


@pytest.mark.parametrize("user", [DAMADMIN, ENG, VIEWER, CDSE, NEW_CDSE])
def test_only_platform_admin_assigns(k, user):
    assert assign(k, user=user).status_code in (401, 403)
    assert _st(k).chain_rows("NEW") == []


def test_assign_refused_when_live_or_has_cdse(k):
    r = assign(k, dam="CRD")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dam_live"
    _an(k).d["dam_cdse"]["NEW"] = "someone@demo.local"
    r = assign(k)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cdse_exists"
    assert _st(k).chain_rows("NEW") == [] and _st(k).chain_rows("CRD") == []


@pytest.mark.parametrize("kw,code", [({"reason_code": None}, 422), ({"reason_code": "bored"}, 422),
                                     ({"rationale": None}, 422), ({"rationale": "  "}, 422),
                                     ({"cdse_id": "not-an-email"}, 422)])
def test_assign_requires_reason_code_and_rationale(k, kw, code):
    r = assign(k, **kw)
    assert r.status_code == code, r.text
    if "reason_code" in kw:
        assert r.json()["detail"]["code"] == "reason_code_invalid"


def test_assign_pending_load_blocks_a_second_assignment_and_lookup_fails_closed(k, monkeypatch):
    monkeypatch.setattr(_an(k), "load_cdse_outbox", lambda row: ("pending", None, None))
    assert assign(k).status_code == 200
    r = assign(k, cdse_id="other@demo.local")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cdse_assign_pending"
    def boom(d):
        raise RuntimeError("view down")
    monkeypatch.setattr(_an(k), "cdse_principals", boom)
    r = assign(k, dam="NEW2")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cdse_unverifiable"


# ------------------------------------------------------------------ changes
@pytest.mark.parametrize("user", [DAMADMIN, ENG, VIEWER, CDSE])
def test_only_platform_admin_proposes_a_change(k, user):
    assert change(k, user=user).status_code in (401, 403)


def test_change_approved_by_current_cdse(k):
    r = change(k)
    assert r.status_code == 200, r.text
    cid = r.json()["change_id"]
    assert r.json()["previous_cdse_id"] == CDSE and r.json()["status"] == "pending"
    assert change(k).json()["detail"]["code"] == "cdse_change_pending"
    for user in (ADMIN, DAMADMIN, NEXT_CDSE, "cdse.bfk@demo.local"):        # not the CURRENT CDSE
        assert k.post(f"/api/dams/CRD/cdse/changes/{cid}/approve", user=user,
                      json={"rationale": "ok"}).status_code in (401, 403)
    assert k.post(f"/api/dams/CRD/cdse/changes/{cid}/approve", user=CDSE, json={"rationale": " "}).status_code == 422
    assert [x["change_id"] for x in k.get("/api/cdse-changes/pending", user=CDSE).json()["changes"]] == [cid]
    r = k.post(f"/api/dams/CRD/cdse/changes/{cid}/approve", user=CDSE, json={"rationale": "Agreed handover"})
    assert r.status_code == 200, r.text
    acts = [x["action_type"] for x in _st(k).chain_rows("CRD")]
    assert acts == ["dam_cdse_change_propose", "dam_cdse_change_approve"]
    assert r.json()["outbox"]["status"] == "loaded" and _an(k).cdse_principals("CRD") == [NEXT_CDSE]
    assert k.post(f"/api/dams/CRD/cdse/changes/{cid}/approve", user=NEXT_CDSE,
                  json={"rationale": "x"}).status_code == 409                # decided once


def test_change_rejected_and_stale(k):
    cid = change(k).json()["change_id"]
    assert k.post(f"/api/dams/CRD/cdse/changes/{cid}/reject", user=CDSE, json={"rationale": "No"}).status_code == 200
    assert _st(k).rows("dam_cdse_outbox", "outbox_id", dam_id="CRD") == []
    cid = change(k).json()["change_id"]
    _an(k).d["dam_cdse"]["CRD"] = "interim@demo.local"                      # changed outside the proposal
    assert k.post(f"/api/dams/CRD/cdse/changes/{cid}/approve", user="interim@demo.local",
                  json={"rationale": "x"}).json()["detail"]["code"] == "cdse_stale"


def test_previous_cdse_id_is_the_single_designated_cdse_not_the_authority_principals(k):
    """§7.20: previous_cdse_id = the dam's ONE designated CDSE (ref.dam.cdse_id / assignment), never the list of
    reviewer-authority principals (which also holds MEMBERSHIP_CDSE grants)."""
    _an(k).d.setdefault("project_membership", []).append({"dam_id": "CRD", "principal": "extra@demo.local",
                                                          "role": "CDSE"})
    assert len(_an(k).cdse_principals("CRD")) > 1
    r = change(k)
    assert r.status_code == 200, r.text
    (row,) = _st(k).rows("dam_cdse_change", "change_id", dam_id="CRD")
    assert row["previous_cdse_id"] == CDSE and "," not in row["previous_cdse_id"]
    (rec,) = _st(k).chain_rows("CRD")
    val = lambda v: json.loads(v) if isinstance(v, str) else v  # noqa: E731
    assert val(rec["original_value"]) == {"cdse_id": CDSE}
    assert val(rec["corrected_value"])["previous_cdse_id"] == CDSE
    k.post(f"/api/dams/CRD/cdse/changes/{r.json()['change_id']}/approve", user=CDSE, json={"rationale": "ok"})
    (o,) = _st(k).rows("dam_cdse_outbox", "outbox_id", dam_id="CRD")
    assert o["previous_cdse_id"] == CDSE and o["status"] == "loaded"


def test_designated_cdse_is_re_read_under_the_dam_lock(k, monkeypatch):
    """The designated CDSE changing between the route's read and the locked check -> 409 cdse_stale, nothing written."""
    seq = iter([CDSE, "interim@demo.local"])
    monkeypatch.setattr(_an(k), "designated_cdse", lambda dam: next(seq))
    r = change(k)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cdse_stale"
    assert _st(k).chain_rows("CRD") == [] and _st(k).rows("dam_cdse_change", "change_id", dam_id="CRD") == []
    monkeypatch.setattr(_an(k), "designated_cdse", lambda dam: (_ for _ in ()).throw(RuntimeError("down")))
    assert change(k).json()["detail"]["code"] == "cdse_unverifiable"           # fail closed


def test_assign_refused_when_a_designated_cdse_exists_without_authority_rows(k, monkeypatch):
    monkeypatch.setattr(_an(k), "designated_cdse", lambda dam: "legacy@demo.local")
    r = assign(k)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cdse_exists"
    assert _st(k).chain_rows("NEW") == []


def test_change_needs_a_current_cdse_and_a_different_one(k):
    assert change(k, dam="NEW").json()["detail"]["code"] == "cdse_authority_missing"
    assert change(k, cdse_id=CDSE).json()["detail"]["code"] == "cdse_unchanged"
    assert change(k, reason_code=None).status_code == 422


def test_cdse_outbox_rows_are_immutable(k, monkeypatch):
    monkeypatch.setattr(_an(k), "load_cdse_outbox", lambda row: ("pending", None, None))
    assign(k)
    st = _st(k)
    for sql in ("UPDATE {s}dam_cdse_outbox SET new_cdse_id = 'x@y.z'", "DELETE FROM {s}dam_cdse_outbox"):
        with pytest.raises(sqlite3.IntegrityError):
            st.execute(sql)


# ------------------------------------------------------------------ QA-056 (demo / sqlite; live PG: test_qa056_*)
def test_qa056_concurrent_assign_demo_exactly_one_wins(demo_settings):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    for trial in range(15):
        c = _client(demo_settings)
        barrier = threading.Barrier(2)

        def go(n, c=c, barrier=barrier):
            barrier.wait()
            return assign(c, cdse_id=f"cdse.race{n}@demo.local").status_code
        with ThreadPoolExecutor(2) as ex:
            codes = sorted(ex.map(go, (1, 2)))
        assert codes == [200, 409], (trial, codes)
        assert len(_st(c).chain_rows("NEW")) == 1
        assert len(_st(c).rows("dam_cdse_outbox", "outbox_id", dam_id="NEW")) == 1


def test_qa056_sqlite_has_every_unique_guard_and_a_violation_is_a_409(k, monkeypatch):
    from backend.repos import state as sm
    st = _st(k)
    names = {r["name"] for r in st.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {n for n, _, _ in sm.UNIQUE_GUARDS} | {sm.PUB_GUARD[0]} <= names
    # layer 1 off: the in-transaction check passes, the unique index alone refuses the second pending change
    assert change(k).status_code == 200
    from backend.routers import dam_cdse
    monkeypatch.setattr(dam_cdse, "_propose_check", lambda dam_id, *a: None)
    monkeypatch.setattr(st, "rows", lambda table, order, **w: [] if w.get("status") == "pending" else
                        sm.StateStore.rows(st, table, order, **w))
    r = change(k, cdse_id="other@demo.local")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "concurrent_write"
    assert [x["action_type"] for x in sm.StateStore.chain_rows(st, "CRD")] == ["dam_cdse_change_propose"]
