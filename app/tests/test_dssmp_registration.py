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
from backend import onboarding as ob
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
         "document_sha256": "ab" * 32, "rationale": "Revised per 2026 DSSMR"}
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
    ({"document_uri": None}, "exactly one"), ({"document_sha256": "XYZ"}, "document_sha256"),
    ({"document_sha256": None}, "document_sha256"),
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
    monkeypatch.setattr(_an(k), "load_dssmp_outbox", lambda row: ("rejected", "dssmp_other_dam", "dssmp_other_dam: clash"))
    rid = propose(k).json()["registration_id"]
    o = decide(k, rid).json()["outbox"]
    assert (o["status"], o["reason_code"], o["message"]) == ("rejected", "dssmp_other_dam", "dssmp_other_dam: clash")
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
    rid = propose(k, document_uri=None, document_sha256=None, document_id=d["document_id"]).json()["registration_id"]
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
    assert _codes(k, did) == (422, {"dssmp_not_found", "go_live_readiness_missing"})  # missing, no CDSE yet
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


def test_superseded_dssmp_blocks_flag_apply_naming_version_and_rules(k):
    on_old = sorted(t["threshold_id"] for t in _an(k).thresholds("CRD") if t["dssmp_version_id"] == "DSSMP-CRD-2026")
    assert "CRD-PZ-2-T1" in on_old and len(on_old) > 1
    decide(k, propose(k).json()["registration_id"])                          # CRD-2026 now superseded
    r = k.post("/api/dams/CRD/alarm-rules/CRD-PZ-2-T1/cdse-flag", user=CDSE,
               json={"cdse_ack_required": True, "rationale": "x"})
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "dssmp_superseded" and d["superseded_dssmp_version_id"] == "DSSMP-CRD-2026"
    assert d["affected_rules"] == on_old and d["threshold_id"] == "CRD-PZ-2-T1"
    assert "must be re-versioned" in d["message"] and "DSSMP-CRD-2026" in d["message"]
    assert _st(k).chain_rows("CRD")[-1]["action_type"] == "dssmp_version_approve"   # nothing recorded for the flag
    # no grace path: a proposal approval hits the same 409
    pid = k.post("/api/dams/CRD/alarm-rules/CRD-PZ-2-T1/cdse-flag/proposals", user=DAMADMIN,
                 json={"cdse_ack_required": True, "rationale": "x"}).json()["proposal_id"]
    r = k.post(f"/api/dams/CRD/alarm-rules/proposals/{pid}/approve", user=CDSE, json={"rationale": "ok"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dssmp_superseded"


# ------------------------------------------------------------------ CDSE direct registration (Genie)
@pytest.mark.parametrize("user", [DAMADMIN, ADMIN, ENG, VIEWER, OTHER_CDSE])
def test_only_the_dams_cdse_registers_directly(k, user):
    assert k.post(REGS.format(d="CRD") + "/direct", user=user, json=body()).status_code in (401, 403)
    assert _st(k).dssmp_registrations("CRD") == [] and _st(k).chain_rows("CRD") == []


def test_cdse_direct_registration(k, monkeypatch):
    assert k.post(REGS.format(d="CRD") + "/direct", user=CDSE, json=body(rationale=" ")).status_code == 422
    monkeypatch.setattr(_an(k), "load_dssmp_outbox", lambda row: ("pending", None, None))
    assert k.get(REGS.format(d="CRD"), user=CDSE).json()["can_register_directly"] is True
    r = k.post(REGS.format(d="CRD") + "/direct", user=CDSE, json=body())
    assert r.status_code == 200, r.text
    (rec,) = _st(k).chain_rows("CRD")                                        # ONE approve record, no proposal
    assert (rec["action_type"], rec["decision"], rec[ac.ACTOR_ROLE_FIELD]) == (
        "dssmp_version_approve", "approve", "cdse,engineer")
    reg = _st(k).dssmp_registration(r.json()["registration_id"])
    assert reg["status"] == "approved" and reg["proposed_by"] == reg["decided_by"] == CDSE
    (o,) = _st(k).dssmp_outbox("CRD")
    assert (o["status"], o["approval_record_hash"], o["approved_at"], o["proposed_by"]) == (
        "pending", rec["record_hash"], rec["signed_ts"], CDSE)
    import sys
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src"
    if (src / "dam_safety" / "dssmp_authority.py").is_file():
        sys.path.insert(0, str(src))
        try:
            from dam_safety.dssmp_authority import dssmp_authority_problems
        finally:
            sys.path.remove(str(src))
        assert dssmp_authority_problems([rec], [{"principal": CDSE, "dam_id": "CRD"}]) == []
    assert k.post(REGS.format(d="CRD") + "/direct", user=CDSE, json=body()).status_code == 409   # already registered


def test_direct_registration_refused_with_second_approver(demo_settings):
    c = _client(demo_settings, True)
    r = c.post(REGS.format(d="CRD") + "/direct", user=CDSE, json=body())
    assert r.status_code == 403 and r.json()["detail"]["code"] == "second_approver_required"
    assert c.get(REGS.format(d="CRD"), user=CDSE).json()["can_register_directly"] is False
    assert _st(c).chain_rows("CRD") == []
    assert propose(c).status_code == 200                                     # dam_admin proposal still works


# ------------------------------------------------------------------ go-live readiness: ref.v_dam_go_live_readiness
def test_golive_needs_the_new_dams_cdse_in_the_authority_view(k, monkeypatch):
    did = _new_draft(k)
    _ref(k)                                                                  # DSSMP loaded, approved
    status, codes = _codes(k, did)
    assert status == 422 and codes == {"go_live_readiness_missing"}          # no readiness row at all
    def boom(dam_id):
        raise RuntimeError("view unreadable")
    _an(k).d["dam_cdse"]["NEW"] = NEW_CDSE
    monkeypatch.setattr(_an(k), "go_live_readiness", boom)                   # fail closed on a lookup error
    assert _codes(k, did) == (422, {"go_live_readiness_unverifiable"})
    monkeypatch.undo()
    assert k.post(f"/api/onboarding/drafts/{did}/golive").status_code == 200


def test_golive_gates_on_the_readiness_view_ready_flag_and_blocking_reason(k, monkeypatch):
    """ready=false -> blocked with blocking_reason as the code; ready=true still needs the app's DSSMP check;
    the view's has_current_dssmp=true does not override the app's own check."""
    did = _new_draft(k)
    _ref(k)
    row = {"dam_id": "NEW", "ready": False, "has_current_dssmp": True, "blocking_reason": "no_cdse_in_reviewer_authority"}
    monkeypatch.setattr(_an(k), "go_live_readiness", lambda dam: dict(row))
    status, codes = _codes(k, did)
    assert status == 422 and codes == {"no_cdse_in_reviewer_authority"}
    row["blocking_reason"] = None                                            # false with no reason: still blocked
    assert _codes(k, did) == (422, {"go_live_not_ready"})
    row.update(ready=None)                                                   # NULL is not TRUE
    assert _codes(k, did) == (422, {"go_live_not_ready"})
    row.update(ready=True)
    _an(k).d["dssmp_versions"] = [v for v in _an(k).d["dssmp_versions"] if v["dam_id"] != "NEW"]
    status, codes = _codes(k, did)                                           # view ready, app DSSMP check fails
    assert status == 422 and "dssmp_not_found" in codes and not codes & {"no_cdse_in_reviewer_authority"}
    monkeypatch.setattr(_an(k), "go_live_readiness", None, raising=False)   # no lookup at all: fail closed
    assert "go_live_readiness_unverifiable" in _codes(k, did)[1]


def test_go_live_no_longer_uses_the_direct_authority_check(k, monkeypatch):
    """The deprecated cdse_authority_issues shim is not on the go-live path (readiness view only)."""
    did = _new_draft(k)
    _ref(k)
    _an(k).d["dam_cdse"]["NEW"] = NEW_CDSE
    monkeypatch.setattr(ob, "cdse_authority_issues", lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    assert k.post(f"/api/onboarding/drafts/{did}/golive").status_code == 200


def test_threshold_without_dssmp_is_coded_dssmp_missing():
    """DA publisher code dssmp_missing is mirrored as a coded go-live issue (not just a message)."""
    d = good_draft()
    d["thresholds"][0]["dssmp_version_id"] = None
    iss = [i for i in ob.validate(d, set()) if i.get("code") == "dssmp_missing"]
    assert len(iss) == 1 and iss[0]["message"].startswith("dssmp_missing: ") and iss[0]["step"] == "thresholds"


# ------------------------------------------------------------------ cross-check with the DA engine (86993d0)
def test_records_pass_the_engine_dssmp_authority_check(k):
    import sys
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src"
    if not (src / "dam_safety" / "dssmp_authority.py").is_file():
        pytest.skip("engine dssmp_authority not in this checkout")
    sys.path.insert(0, str(src))
    try:
        from dam_safety.dssmp_authority import dssmp_authority_problems
    finally:
        sys.path.remove(str(src))
    rid = propose(k).json()["registration_id"]
    decide(k, rid)
    rid2 = propose(k, vid="DSSMP-CRD-2028", version_no=3).json()["registration_id"]
    decide(k, rid2, "reject")
    rows = _st(k).chain_rows("CRD")
    assert [r["action_type"] for r in rows] == ["dssmp_version_propose", "dssmp_version_approve",
                                                "dssmp_version_propose", "dssmp_version_reject"]
    authority = [{"principal": CDSE, "dam_id": "CRD", "granted_ts": None, "revoked_ts": None}]
    assert dssmp_authority_problems(rows, authority) == []
    o = _st(k).dssmp_outbox("CRD")[0]                   # load job: approved_at / approved_by match the record
    assert o["approved_at"] == rows[1]["signed_ts"] and o["approved_by"] == rows[1]["reviewer_email"]
    assert o["approval_record_hash"] == rows[1]["record_hash"]
    # engine view of a forged approve by the dam_admin: refused
    bad = dict(rows[1], reviewer_id="demo:" + DAMADMIN, reviewer_email=DAMADMIN, actor_app_role="dam_admin")
    assert {p["code"] for p in dssmp_authority_problems([rows[0], bad], authority)} == {"dssmp_approve_by_admin"}


def test_document_hash_mismatch_rejection_is_explained(k, monkeypatch):
    did = _new_draft(k)
    _an(k).d["dam_cdse"]["NEW"] = NEW_CDSE
    monkeypatch.setattr(_an(k), "load_dssmp_outbox",
                        lambda row: ("rejected", "dssmp_document_hash_mismatch",
                                             "dssmp_document_hash_mismatch: sha256 differs"))
    r = k.post(REGS.format(d="NEW") + "/direct", user=NEW_CDSE,
               json=body(vid="DSSMP-NEW-2026", version_no=1))
    assert r.json()["outbox"]["reason_code"] == "dssmp_document_hash_mismatch"
    v = k.post(f"/api/onboarding/drafts/{did}/validate").json()
    assert any("dssmp_document_hash_mismatch" in i["message"] and "register the version again" in i["message"]
               for i in v["issues"])
