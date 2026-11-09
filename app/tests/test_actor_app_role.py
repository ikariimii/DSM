"""v1.6 DA follow-up 1: actor_app_role in every NEW chained record, under a versioned canonical form.

canon 1 (canon_version NULL/absent) = CHAIN_FIELDS only: existing records and the DA's fixtures verify unchanged.
canon 2 (every new record) hashes CHAIN_FIELDS + actor_app_role + canon_version; a canon-2 record without a valid
actor_app_role fails verification. The role is derived server-side, never taken from the request."""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import test_audit_chain as tac
from conftest import ADMIN, CDSE, ENG

import canon2_vector as vec
from backend import audit_chain as ac

ROOT = Path(__file__).resolve().parents[2]
DA_FIXTURES = [ROOT / "tests" / "fixtures" / "review_chain_vector.json",
               ROOT / "tests" / "fixtures" / "review_chain_signed_vector.json"]
V1 = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment", "reviewer_id",
      "reviewer_pe_license", "signed_ts", "signature_hash", "action_type", "reviewer_email", "reviewer_sub",
      "original_value", "corrected_value", "chain_id", "chain_seq", "prev_hash")
recs = tac.recs  # reuse the inclinometer/alarm fixture


def _independent_hash(r):
    """Re-derivation from the contract text alone (no app code): sorted compact JSON, ints for seq/version."""
    canon = r.get("canon_version")
    fields = V1 if canon is None else V1 + ("actor_app_role", "canon_version")
    payload = {k: r.get(k) for k in fields}
    payload["chain_seq"] = int(r["chain_seq"])
    if canon is not None:
        payload["canon_version"] = int(canon)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


# ------------------------------------------------------------------ canon definition
def test_canon_versions_are_pinned():
    assert ac.CHAIN_FIELDS == V1                                   # canon 1 unchanged (QA SPEC_CHAIN_FIELDS, DA vector)
    assert ac.CANON_VERSION == 2 and ac.CANON_FIELDS == {1: V1, 2: V1 + ("actor_app_role", "canon_version")}
    assert ac.ACTOR_ROLES == ("viewer", "engineer", "dso", "dam_admin", "admin", "cdse")
    assert set(ac.HASH_EXCLUDED) == {"record_hash", "signature", "sig_alg", "key_id"}


@pytest.mark.parametrize("path", DA_FIXTURES, ids=lambda p: p.name)
def test_da_fixtures_still_verify_unchanged_as_canon_1(path):
    raw = path.read_bytes()
    v = json.loads(raw)
    rows = v["rows"]
    assert all("canon_version" not in r and "actor_app_role" not in r for r in rows)
    assert all(ac.canon_of(r) == 1 and ac.record_hash(r) == r["record_hash"] for r in rows)
    head = v.get("head") or {"chain_id": rows[-1]["chain_id"], "chain_seq": rows[-1]["chain_seq"],
                             "head_hash": rows[-1]["record_hash"]}
    signer = tac._VecHmacSigner(v["signer"]) if "signer" in v else None
    assert ac.verify_chain(copy.deepcopy(rows), head, signer)["ok"]
    assert path.read_bytes() == raw                                # never rewritten


# ------------------------------------------------------------------ app-side vector
def test_canon2_vector_file_is_reproducible():
    assert vec.FIXTURE.read_text() == vec.dumps(vec.build())


def test_canon2_vector_hashes_rederive_independently():
    v = json.loads(vec.FIXTURE.read_text())
    assert [r.get("canon_version") for r in v["rows"]] == [None, None, 2, 2, 2]
    assert [r.get("actor_app_role") for r in v["rows"]] == [None, None, "cdse,engineer", "admin", "dam_admin"]
    for r in v["rows"]:
        assert r["record_hash"] == _independent_hash(r) == ac.record_hash(r)
    assert v["canon_fields"] == {"1": list(V1), "2": list(V1) + ["actor_app_role", "canon_version"]}


@pytest.mark.parametrize("case", json.loads(vec.FIXTURE.read_text())["cases"], ids=lambda c: c["name"])
def test_canon2_vector_cases(case):
    got = ac.verify_chain(copy.deepcopy(case["rows"]), copy.deepcopy(case["head"]), vec.VecHmac())
    assert {k: got[k] for k in ("ok", "first_broken_seq", "reason")} == case["expect"]
    # unsigned view of the same rows gives the same chain verdict
    unsigned = [{**r, "signature": None, "sig_alg": None, "key_id": None} for r in case["rows"]]
    assert ac.verify_chain(unsigned, copy.deepcopy(case["head"]))["reason"] == case["expect"]["reason"]


def test_canon2_rejection_reasons_cover_the_contract():
    reasons = {c["name"]: c["expect"]["reason"] for c in json.loads(vec.FIXTURE.read_text())["cases"]}
    assert reasons["clean"] is None
    assert {"actor_role_missing", "actor_role_invalid", "canon_downgrade", "canon_unknown",
            "hash_mismatch"} <= set(reasons.values())


@pytest.mark.parametrize("role", [None, "", " ", "superuser", "engineer,cdse", "cdse,cdse", "CDSE"])
def test_finalizer_refuses_a_canon2_record_without_a_valid_role(role):
    base = {**vec._base(9, "alarm_ack", "acknowledge", "engineer"), "actor_app_role": role, "canon_version": 2}
    with pytest.raises(ValueError, match="actor_app_role"):
        ac.finalizer(base)


@pytest.mark.parametrize("role", ["viewer", "engineer", "dso", "dam_admin", "admin", "cdse", "admin,cdse",
                                  "cdse,engineer", "admin,cdse,dam_admin"])
def test_valid_roles(role):
    assert ac.actor_role_problem(role) is None


# ------------------------------------------------------------------ server-side derivation end to end
def test_every_new_record_is_canon2_with_the_server_derived_role(recs):
    c, sid, an = recs
    st = c.tc.app.state.store
    smuggle = {"actor_app_role": "cdse", "canon_version": 1}       # client-supplied values must be ignored
    r1 = c.post(f"/api/instruments/{tac.IID}/incl/surveys/{sid}/bias-proposals/B1/review", user=ENG,
                json={"decision": "approve", "rationale": "AGREE across 6 sets", **smuggle})
    r2 = c.post(f"/api/instruments/{tac.IID}/incl/surveys/{sid}/corrections/D1/review", user=ENG,
                json={"decision": "reject", "rationale": "not supported", **smuggle})
    eid = tac._crd_event(c)["event_id"]
    r3 = c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "checking", **smuggle})
    r4 = c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "field check OK", **smuggle})
    assert [r.status_code for r in (r1, r2, r3, r4)] == [200] * 4, [r.text for r in (r1, r2, r3, r4)]
    rows = st.chain_rows("CRD")
    assert [(r["action_type"], r["actor_app_role"], r["canon_version"]) for r in rows] == [
        ("bias_review", "engineer", 2), ("data_correction_review", "engineer", 2),
        ("alarm_ack", "cdse,engineer", 2), ("alarm_close", "admin", 2)]
    assert all(r["record_hash"] == _independent_hash(r) for r in rows)
    assert c.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()["ok"]
    # the store's CHECK refuses a canon-2 row without a role even for a privileged (trigger-bypassing) UPDATE
    tac._unguard(st)
    with pytest.raises(sqlite3.IntegrityError, match="ck_review_signoff_canon"):
        st.execute("UPDATE review_signoff SET actor_app_role = NULL WHERE chain_seq = 3")
    # a forged extract that blanks the role and recomputes every hash is still refused (actor_role_missing)
    rows = st.chain_rows("CRD")
    rows[2]["actor_app_role"] = None
    head = vec._rehash_from(rows, 2, ac.NoopSigner())
    v = ac.verify_chain(rows, head)
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 3, "actor_role_missing")


def test_rule_flag_records_carry_the_role(c):
    assert c.put("/api/admin/roles", user=ADMIN, json={"email": "damadmin.crd@demo.local", "dam_id": "CRD",
                                                         "role": "dam_admin"}).status_code == 200
    base = "/api/dams/CRD/alarm-rules"
    pid = c.post(f"{base}/CRD-PZ-2-T1/cdse-flag/proposals", user="damadmin.crd@demo.local",
                 json={"cdse_ack_required": True, "rationale": "trend", "actor_app_role": "cdse"}).json()["proposal_id"]
    assert c.post(f"{base}/proposals/{pid}/approve", user=CDSE, json={"rationale": "agreed"}).status_code == 200
    rows = c.tc.app.state.store.chain_rows("CRD")
    assert [(r["action_type"], r["actor_app_role"]) for r in rows] == [
        ("alarm_rule_flag_propose", "dam_admin"), ("alarm_rule_flag_apply", "cdse,engineer")]


def test_pre_v2_records_then_new_records_verify_together(c):
    st = c.tc.app.state.store
    for i in (1, 2):                                              # records written before canon 2 existed
        st.append_chained("CRD", ac.GENESIS, ac.finalizer(vec._base(i, "alarm_ack", "acknowledge", "engineer")))
    eid = tac._crd_event(c)["event_id"]
    assert c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "checking"}).status_code == 200
    rows = st.chain_rows("CRD")
    assert [r["canon_version"] for r in rows] == [None, None, 2]
    assert c.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()["ok"]


def test_store_check_refuses_canon2_without_role(tmp_path):
    st = tac._store()
    row = ac.finalizer(vec._base(1, "alarm_ack", "acknowledge", "engineer"))(1, ac.GENESIS)
    row.update(canon_version=2, actor_app_role=None)
    cols = st.SIGNOFF_COLS
    with pytest.raises(sqlite3.IntegrityError):
        st.execute(f"INSERT INTO review_signoff ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                   tuple(row.get(k) for k in cols))
