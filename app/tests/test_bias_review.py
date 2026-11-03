"""QA-039: bias-proposal approval is an append-only, content-signed sign-off (dam_app.review_signoff)."""
import sqlite3

import pytest

from backend import signoff as so
from backend.repos import contract as ct
from backend.repos.state import StateStore
from conftest import ADMIN, DSO, ENG, VIEWER

IID = "CRD-IN-1"


def _rec(rid, status="PROPOSED", user="engine:reduce_inclinometers", **kw):
    stat = (f"rule=QA021-v2;ZA=-6.0000;abs_dD=6.0000;abs_bs=6.0000;crosscheck_verdict=AGREE;status={status};"
            "requires_review=True")
    base = {k: None for k in ct.INCL_CORRECTION_FIELDS}
    base.update(record_id=rid, dam_id="CRD", instrument_id=IID, survey_id=None, axis="A", auto_value=-6.0,
                detection_method="between_set_bias_qc", detection_statistic=stat, applied=False,
                timestamp_utc="2026-11-02T00:00:00Z", user=user)
    return {**base, **kw}


@pytest.fixture()
def setup(c):
    sid = c.get(f"/api/instruments/{IID}/incl/surveys").json()["surveys"][-1]["survey_id"]
    an = c.tc.app.state.analytics_factory(None)
    recs = [_rec("P1"), _rec("P2", user=ENG), _rec("SUP", superseded_by="P1"),
            _rec("BADR", status="BAD_RESURVEY"),
            _rec("APP", applied=True, reviewed_by="rev", reviewed_utc="2026-11-02T01:00:00Z"),
            _rec("ANOM", applied=True)]
    for r in recs:
        r["survey_id"] = sid
    an.d.setdefault("incl_corrections", []).extend(recs)
    return c, sid, an


def _url(sid, rid):
    return f"/api/instruments/{IID}/incl/surveys/{sid}/bias-proposals/{rid}/review"


def test_approve_writes_one_signed_append_only_row(setup):
    c, sid, an = setup
    r = c.post(_url(sid, "P1"), user=ENG, json={"decision": "approve", "rationale": "  AGREE, 6 sets  ",
                                                "reviewer_pe_license": "PE-123"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["reviewer_id"] == f"uid-{ENG}"          # X-Forwarded-User, not the email
    assert (row["subject_type"], row["subject_id"], row["decision"], row["comment"], row["review_level"]) == \
        ("incl_correction", "P1", "approve", "AGREE, 6 sets", "engineer")
    assert row["dam_id"] == "CRD" and row["reviewer_pe_license"] == "PE-123" and row["signed_ts"].endswith("+00:00")
    assert len(row["signature_hash"]) == 64 and row["signature_verified"] and row["applied_by_app"] is False
    st = c.tc.app.state.store
    stored = st.review_signoffs("incl_correction", ["P1"])
    assert len(stored) == 1 and stored[0]["signature_hash"] == row["signature_hash"]
    # the app changed nothing on the record itself
    rec = next(x for x in an.d["incl_corrections"] if x["record_id"] == "P1")
    assert rec["applied"] is False and rec.get("reviewed_by") is None
    # a second decision is a NEW row, never an update
    assert c.post(_url(sid, "P1"), user=ADMIN, json={"decision": "reject", "rationale": "no"}).status_code == 200
    assert [x["decision"] for x in st.review_signoffs("incl_correction", ["P1"])] == ["approve", "reject"]
    audit = [a for a in st.audit_entries("CRD") if a["action"] == "bias_proposal_review"]
    assert audit[-1]["detail"]["signature_hash"] == row["signature_hash"]
    # the profile lists the sign-offs, re-verified against the current record
    p = c.get(f"/api/instruments/{IID}/incl/profile?survey_ids={sid}", user=ENG).json()
    so_rows = p["profiles"][0]["bias_signoffs"]
    assert [(x["subject_id"], x["signature_verified"]) for x in so_rows] == [("P1", True), ("P1", True)]
    rec["detection_statistic"] += ";tampered=1"           # record content changed after signing
    p = c.get(f"/api/instruments/{IID}/incl/profile?survey_ids={sid}", user=ENG).json()
    assert [x["signature_verified"] for x in p["profiles"][0]["bias_signoffs"]] == [False, False]


def test_database_refuses_update_and_delete(setup):
    c, sid, _ = setup
    assert c.post(_url(sid, "P1"), user=ENG, json={"decision": "approve", "rationale": "ok"}).status_code == 200
    st = c.tc.app.state.store
    for sql in ("UPDATE review_signoff SET decision = 'reject'", "DELETE FROM review_signoff"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            st.execute(sql)
    assert len(st.review_signoffs("incl_correction", ["P1"])) == 1
    assert not hasattr(st, "update_review_signoff") and not hasattr(st, "delete_review_signoff")


def test_reviewer_must_not_be_author(setup):
    c, sid, _ = setup
    r = c.post(_url(sid, "P2"), user=ENG, json={"decision": "approve", "rationale": "mine"})
    assert r.status_code == 403 and "author" in r.json()["detail"]
    assert c.post(_url(sid, "P2"), user=ADMIN, json={"decision": "approve", "rationale": "ok"}).status_code == 200


@pytest.mark.parametrize("rid,code", [("SUP", 409), ("BADR", 409), ("APP", 409), ("ANOM", 409), ("NOPE", 404)])
def test_only_open_valid_proposals_can_be_reviewed(setup, rid, code):
    c, sid, _ = setup
    r = c.post(_url(sid, rid), user=ENG, json={"decision": "approve", "rationale": "x"})
    assert r.status_code == code
    if rid == "ANOM":
        assert "APPLIED_WITHOUT_VALID_REVIEW" in r.json()["detail"]
    assert c.tc.app.state.store.review_signoffs("incl_correction", [rid]) == []


def test_role_input_and_instrument_checks(setup):
    c, sid, _ = setup
    body = {"decision": "approve", "rationale": "x"}
    assert c.post(_url(sid, "P1"), user=VIEWER, json=body).status_code == 403   # viewer elsewhere / not engineer
    assert c.post(_url(sid, "P1"), user=DSO, json=body).status_code == 403      # no role on CRD
    assert c.post(_url(sid, "P1"), user=ENG, json={**body, "decision": "applied"}).status_code == 422
    assert c.post(_url(sid, "P1"), user=ENG, json={**body, "rationale": "   "}).status_code == 422
    assert c.post(_url(sid, "P1"), user=ENG, json={"decision": "approve"}).status_code == 422
    assert c.post(f"/api/instruments/CRD-PZ-1/incl/surveys/{sid}/bias-proposals/P1/review", user=ENG,
                  json=body).status_code == 400
    assert c.post(f"/api/instruments/NOPE/incl/surveys/{sid}/bias-proposals/P1/review", user=ENG,
                  json=body).status_code == 404
    assert c.tc.app.state.store.review_signoffs("incl_correction", ["P1"]) == []


def test_demo_persona_without_proxy_id_is_labelled(setup):
    c, sid, _ = setup
    r = c.tc.post(_url(sid, "P1"), headers={"x-demo-user": ENG}, json={"decision": "approve", "rationale": "x"})
    assert r.status_code == 200 and r.json()["reviewer_id"] == f"demo:{ENG}"


def test_signature_is_deterministic_and_binds_every_field():
    rec = _rec("P1", survey_id="S")
    row = so.build(rec, "CRD", "approve", "ok", "uid-1", None, "2026-11-02T00:00:00+00:00")
    assert row["review_id"].startswith("RS-") and so.verify(row, rec)
    assert so.signature(row, rec) == so.signature(dict(row), dict(rec))
    for k in so.SIGNED_FIELDS:
        assert not so.verify({**row, k: "changed"}, rec), k
    for k in so.SUBJECT_FIELDS:
        assert not so.verify(row, {**rec, k: "changed"}), k
    assert not so.verify(row, None) and not so.verify({**row, "signature_hash": "0" * 64}, rec)
    assert so.build(rec, "CRD", "approve", "ok", "uid-1", None, "t")["review_id"] != row["review_id"]


def test_same_person():
    assert so.same_person(("uid-1", "A@x"), "a@x") and so.same_person(("uid-1", ""), " UID-1 ")
    assert not so.same_person(("uid-1", "a@x"), "b@x") and not so.same_person(("uid-1",), None)
    assert not so.same_person(("", None), "")


def test_review_signoffs_empty_and_columns():
    st = StateStore("sqlite")
    st.init_schema()
    st.init_schema()   # idempotent, triggers included
    assert st.review_signoffs("incl_correction", []) == []
    cols = [r["name"] for r in st.execute("PRAGMA table_info(review_signoff)")]
    assert tuple(cols) == StateStore._SIGNOFF_COLS


def test_canonical_value():
    from datetime import date, datetime, timezone
    iso = "2026-11-02T00:00:00.000000+00:00"
    assert so.canonical_value("2026-11-02T00:00:00Z") == iso == so.canonical_value(datetime(2026, 11, 2))
    assert so.canonical_value(datetime(2026, 11, 1, 19, tzinfo=timezone(__import__("datetime").timedelta(hours=-5)))) == iso
    assert so.canonical_value("2026-11-02 00:00:00") == iso and so.canonical_value("2026-11-02T99:00:00x") == \
        "2026-11-02T99:00:00x"
    assert so.canonical_value(float("nan")) is None and so.canonical_value(None) is None
    assert so.canonical_value(date(2026, 1, 2)) == "2026-01-02" and so.canonical_value((1.0, 2.0)) == "1.0..2.0"
    assert so.canonical_value(-6.0) == -6.0 and so.canonical_value("abc") == "abc"
    rec = _rec("P1", survey_id="S")
    row = so.build(rec, "CRD", "approve", "ok", "uid-1", None, "2026-11-02T00:00:00+00:00")
    assert so.verify({**row, "signed_ts": "2026-11-02T00:00:00Z"}, {**rec, "timestamp_utc": datetime(2026, 11, 2)})


def test_signature_matches_engine_review_signature_when_available(monkeypatch):
    """Cross-layer interop: the engine must verify what the app signs (same canonical JSON)."""
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "src"))
    corr = pytest.importorskip("dam_safety.inclinometer.corrections")
    if not hasattr(corr, "review_signature"):
        pytest.skip("engine review_signature not present")
    assert set(corr.AUDIT_FIELDS) == set(so.SUBJECT_FIELDS)
    assert tuple(corr.REVIEW_SIGNED_FIELDS) == so.SIGNED_FIELDS and corr.REVIEW_SUBJECT_TYPE == so.SUBJECT_TYPE
    rec = _rec("P1", survey_id="S", depth_range="0.5..10.0", reviewed_by=None, supersedes=None, review_id=None)
    row = so.build(rec, "CRD", "approve", "ok ü", "uid-1", "PE-1", "2026-11-02T00:00:00+00:00")
    assert corr.review_signature(row, rec) == row["signature_hash"]
