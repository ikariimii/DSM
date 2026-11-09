"""v1.7 DSSMP version registration (SCHEMA_CONTRACT §7 item 19): roles, rationale, CDSE-only decisions, chain records,
the upload hash, the dssmp_registration_outbox row (DA contract) and the go-live block (dssmp_not_found / _draft /
_superseded, approved-but-not-loaded fails closed)."""
import base64
import dataclasses
import hashlib
import sqlite3

import pytest
from conftest import ADMIN, CDSE, DSO, ENG, NOBODY, VIEWER, Client
from fastapi.testclient import TestClient
from test_onboarding import _fill, good_draft

from backend import audit_chain as ac
from backend.main import create_app

DAMADMIN = "damadmin.crd@demo.local"
OTHER_CDSE = "cdse.bfk@demo.local"
NEW_CDSE = "cdse.new@demo.local"
REGS = "/api/dams/{d}/dssmp-registrations"


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


def body(vid="DSSMP-CRD-2027", **kw):
    b = {"dssmp_version_id": vid, "version_no": 2, "effective_from": "2026-11-01", "change_reason": "Annual review",
         "source_ref": "DSSMR-2026-14", "document_uri": "https://docs.example/dssmp-crd-2027.pdf",
         "rationale": "Revised per 2026 DSSMR"}
    b.update(kw)
    return {k: v for k, v in b.items() if v is not None}


def propose(c, dam="CRD", user=DAMADMIN, **kw):
    return c.post(REGS.format(d=dam), user=user, json=body(**kw))


def decide(c, rid, what="approve", dam="CRD", user=CDSE, rationale="Checked against the DSSMR"):
    return c.post(f"{REGS.format(d=dam)}/{rid}/{what}", user=user,
                  json={"rationale": rationale} if rationale is not None else {})


# ------------------------------------------------------------------ roles
@pytest.mark.parametrize("user,code", [(VIEWER, 403), (ENG, 403), (DSO, 403), (CDSE, 403), (NOBODY, 403),
                                       (DAMADMIN, 200), (ADMIN, 200)])
def test_who_may_propose(k, user, code):
    r = propose(k, user=user)
    assert r.status_code in ((code, 401) if user == NOBODY else (code,)), r.text


@pytest.mark.parametrize("user", [DAMADMIN, ADMIN, ENG, VIEWER, OTHER_CDSE])
def test_only_the_dams_cdse_decides(k, user):
    rid = propose(k).json()["registration_id"]
    for what in ("approve", "reject"):
        assert decide(k, rid, what, user=user).status_code in (401, 403)
    assert _st(k).dssmp_registration(rid)["status"] == "pending" and _st(k).dssmp_outbox("CRD") == []


def test_rationale_required(k):
    assert propose(k, rationale=None).status_code == 422
    assert propose(k, rationale="   ").status_code == 422
    rid = propose(k).json()["registration_id"]
    for what in ("approve", "reject"):
        assert decide(k, rid, what, rationale=None).status_code == 422
        assert decide(k, rid, what, rationale="  ").status_code == 422


@pytest.mark.parametrize("kw,msg", [
    ({"version_no": None}, "version_no"), ({"version_no": 0}, "version_no"),
    ({"effective_from": None}, "effective_from"), ({"effective_from": "next week"}, "effective_from"),
    ({"change_reason": " "}, "change_reason"), ({"source_ref": None}, "source_ref"),
    ({"document_uri": None}, "exactly one"), ({"document_sha256": "XYZ"}, "64 lowercase hex"),
    ({"vid": "bad id!"}, "dssmp_version_id")])
def test_required_fields(k, kw, msg):
    r = propose(k, **kw)
    assert r.status_code == 422 and msg in r.text, r.text


def test_existing_ids_refused(k):
    r = propose(k, vid="DSSMP-CRD-2026")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dssmp_exists"
    r = propose(k, vid="DSSMP-BFK-2026")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dssmp_other_dam"
    assert propose(k).status_code == 200 and propose(k).status_code == 409   # one open registration per id


# ------------------------------------------------------------------ approve: chain, outbox, demo load
def test_approve_writes_chain_outbox_and_loads(k):
    rid = propose(k).json()["registration_id"]
    r = decide(k, rid)
    assert r.status_code == 200, r.text
    rows = _st(k).chain_rows("CRD")
    assert [x["action_type"] for x in rows] == ["dssmp_version_propose", "dssmp_version_approve"]
    for x, role in zip(rows, ("dam_admin", "cdse,engineer")):
        assert x["subject_type"] == "dssmp_version" and x["subject_id"] == "DSSMP-CRD-2027"
        assert x[ac.CANON_FIELD] == 2 and x[ac.ACTOR_ROLE_FIELD] == role
    assert ac.verify_chain(rows, _st(k).chain_head("CRD"))["ok"]
    (o,) = _st(k).dssmp_outbox("CRD")
    assert o["approval_record_hash"] == rows[1]["record_hash"] == r.json()["approval_record_hash"]
    assert {kk: o[kk] for kk in ("dam_id", "dssmp_version_id", "version_no", "effective_from", "change_reason",
                                 "source_ref", "document_uri", "proposed_by", "approved_by")} == {
        "dam_id": "CRD", "dssmp_version_id": "DSSMP-CRD-2027", "version_no": 2,
        "effective_from": "2026-11-01T00:00:00Z", "change_reason": "Annual review", "source_ref": "DSSMR-2026-14",
        "document_uri": "https://docs.example/dssmp-crd-2027.pdf", "proposed_by": DAMADMIN, "approved_by": CDSE}
    assert o["status"] == "loaded" and o["processed_at"]           # demo stand-in for the DA load job
    ref = {v["dssmp_version_id"]: v for v in _an(k).dssmp_versions_for_dam("CRD")}
    assert ref["DSSMP-CRD-2027"]["status"] == "approved" and ref["DSSMP-CRD-2027"]["loaded_from_outbox_id"] == o["outbox_id"]
    assert ref["DSSMP-CRD-2026"]["status"] == "superseded"           # the load job closes the previous version
    assert r.json()["outbox"]["status"] == "loaded" and r.json()["ref"]["status"] == "approved"
    assert decide(k, rid).status_code == 409                          # decided once


def test_reject_writes_chain_and_no_outbox(k):
    rid = propose(k).json()["registration_id"]
    assert decide(k, rid, "reject").status_code == 200
    assert [x["action_type"] for x in _st(k).chain_rows("CRD")] == ["dssmp_version_propose", "dssmp_version_reject"]
    assert _st(k).dssmp_outbox("CRD") == [] and _st(k).dssmp_registration(rid)["status"] == "rejected"


def test_outbox_rejection_is_surfaced(k, monkeypatch):
    monkeypatch.setattr(_an(k), "load_dssmp_outbox", lambda row: ("rejected_invalid", "dssmp_other_dam", "clash"))
    rid = propose(k).json()["registration_id"]
    o = decide(k, rid).json()["outbox"]
    assert (o["status"], o["reason_code"], o["message"]) == ("rejected_invalid", "dssmp_other_dam", "clash")
    reg = k.get(REGS.format(d="CRD"), user=ENG).json()["registrations"][0]
    assert reg["outbox"]["reason_code"] == "dssmp_other_dam" and reg["ref"] is None


def test_outbox_rows_are_immutable_except_bookkeeping(k, monkeypatch):
    monkeypatch.setattr(_an(k), "load_dssmp_outbox", lambda row: ("pending", None, None))
    decide(k, propose(k).json()["registration_id"])
    st = _st(k)
    (o,) = st.dssmp_outbox("CRD")
    assert o["status"] == "pending" and o["processed_at"] is None
    for sql in ("UPDATE {s}dssmp_registration_outbox SET dssmp_version_id = 'X'",
                "UPDATE {s}dssmp_registration_outbox SET approval_record_hash = 'X'",
                "DELETE FROM {s}dssmp_registration_outbox"):
        with pytest.raises(sqlite3.IntegrityError):
            st.execute(sql)
    st.demo_publish_outbox(o["outbox_id"], "loaded", None, None)      # the publisher's columns only
    assert st.dssmp_outbox("CRD")[0]["status"] == "loaded"


# ------------------------------------------------------------------ upload
def test_upload_hash_and_use(k):
    raw = b"%PDF-1.7 demo DSSMP\n" * 50
    up = k.post("/api/dams/CRD/dssmp-documents", user=DAMADMIN,
                json={"filename": "dssmp.pdf", "content_type": "application/pdf",
                      "content_b64": base64.b64encode(raw).decode()})
    assert up.status_code == 200, up.text
    d = up.json()
    assert d["sha256"] == hashlib.sha256(raw).hexdigest() and d["size_bytes"] == len(raw)
    assert "content_b64" not in d
    dl = k.get(f"/api/dams/CRD/dssmp-documents/{d['document_id']}", user=ENG)
    assert dl.content == raw and dl.headers["x-content-sha256"] == d["sha256"]
    assert propose(k, document_uri=None, document_id=d["document_id"], document_sha256="0" * 64).status_code == 422
    rid = propose(k, document_uri=None, document_id=d["document_id"]).json()["registration_id"]
    decide(k, rid)
    o = _st(k).dssmp_outbox("CRD")[0]
    assert o["document_sha256"] == d["sha256"] and o["document_uri"].endswith(d["document_id"])
    assert k.post("/api/dams/CRD/dssmp-documents", user=DAMADMIN,
                  json={"filename": "x", "content_b64": "not base64!"}).status_code == 422
    assert k.post("/api/dams/CRD/dssmp-documents", user=ENG,
                  json={"filename": "x", "content_b64": "eA=="}).status_code == 403


# ------------------------------------------------------------------ provisional self-approval (same switch)
@pytest.mark.parametrize("on", [False, True])
def test_self_approval_follows_the_provisional_setting(demo_settings, on):
    c = _client(demo_settings, on)
    assert c.put("/api/admin/roles", user=ADMIN, json={"email": CDSE, "dam_id": "CRD",
                                                         "role": "dam_admin"}).status_code == 200
    rid = propose(c, user=CDSE).json()["registration_id"]
    r = decide(c, rid)
    if not on:
        assert r.status_code == 200
    else:
        assert r.status_code == 403 and r.json()["detail"]["code"] == "self_approval_forbidden"
        assert decide(c, rid, "reject").status_code == 200        # can only be rejected (withdrawn)
    assert c.get(REGS.format(d="CRD"), user=CDSE).json()["approval_policy"]["cdse_may_self_approve"] is (not on)


def test_cdse_queue(k):
    propose(k)
    assert [r["dssmp_version_id"] for r in k.get("/api/dssmp-registrations/pending", user=CDSE).json()[
        "registrations"]] == ["DSSMP-CRD-2027"]
    assert k.get("/api/dssmp-registrations/pending", user=OTHER_CDSE).json()["registrations"] == []


# ------------------------------------------------------------------ go-live block
def _new_draft(c):
    did = c.post("/api/onboarding/drafts").json()["draft_id"]
    _fill(c, did, good_draft(), dssmp=False)
    return did


def _codes(c, did):
    r = c.post(f"/api/onboarding/drafts/{did}/golive")
    return r.status_code, {i.get("code") for i in r.json()["detail"]["issues"]} if r.status_code == 422 else set()


def _ref(c, **row):
    _an(c).d["dssmp_versions"].append({"dssmp_version_id": "DSSMP-NEW-2026", "dam_id": "NEW", "version_no": 1,
                                       "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
                                       "status": "approved", **row})


@pytest.mark.parametrize("row,code", [({"status": "draft"}, "dssmp_draft"),
                                      ({"status": "superseded"}, "dssmp_superseded"),
                                      ({"effective_to": "2026-02-01T00:00:00Z"}, "dssmp_superseded"),
                                      ({"status": "withdrawn"}, "dssmp_status_unknown"),
                                      ({"effective_from": "2099-01-01T00:00:00Z"}, "dssmp_not_yet_effective"),
                                      ({"dam_id": "CRD"}, "dssmp_other_dam")])
def test_golive_blocked_by_ref_state(k, row, code):
    did = _new_draft(k)
    _ref(k, **row)
    status, codes = _codes(k, did)
    assert status == 422 and code in codes, codes


def test_golive_new_dam_end_to_end_and_not_loaded_fails_closed(k, monkeypatch):
    did = _new_draft(k)
    assert _codes(k, did) == (422, {"dssmp_not_found"})                     # missing
    _an(k).d["dam_cdse"]["NEW"] = NEW_CDSE                                   # DA exposes the new dam's CDSE
    rid = propose(k, dam="NEW", user=ADMIN, vid="DSSMP-NEW-2026", version_no=1).json()["registration_id"]
    v = k.post(f"/api/onboarding/drafts/{did}/validate").json()
    assert any("pending CDSE approval" in i["message"] for i in v["issues"])  # unapproved
    assert _codes(k, did)[0] == 422
    assert decide(k, rid, dam="NEW", user=CDSE).status_code == 403          # another dam's CDSE
    monkeypatch.setattr(_an(k), "load_dssmp_outbox", lambda row: ("pending", None, None))
    assert decide(k, rid, dam="NEW", user=NEW_CDSE).status_code == 200
    v = k.post(f"/api/onboarding/drafts/{did}/validate").json()
    assert any("waiting for the DA load job" in i["message"] for i in v["issues"])  # approved but not loaded
    assert _codes(k, did) == (422, {"dssmp_not_found"})
    (o,) = _st(k).dssmp_outbox("NEW")
    monkeypatch.undo()
    st, code, msg = _an(k).load_dssmp_outbox(o)                              # the DA job loads it
    _st(k).demo_publish_outbox(o["outbox_id"], st, code, msg)
    assert k.post(f"/api/onboarding/drafts/{did}/golive").status_code == 200


def test_superseded_dssmp_blocks_flag_apply(k):
    decide(k, propose(k).json()["registration_id"])                          # CRD-2026 now superseded
    r = k.post("/api/dams/CRD/alarm-rules/CRD-PZ-2-T1/cdse-flag", user=CDSE,
               json={"cdse_ack_required": True, "rationale": "x"})
    assert r.status_code == 409 and "dssmp_superseded" in r.text
