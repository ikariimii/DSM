"""v1.7 canon v2 (App Dev c797151, app SCHEMA_CONTRACT §7 item 14): actor_app_role + canon_version in the hashed
record. The engine (dam_safety.audit_chain) must give the app's verdict on the app-generated canon-2 vector
(app/tests/fixtures/review_chain_canon2_vector.json) and still on the canon-1 DA vectors; the extract carries both
columns (24 -> 26); verify_review_chain SELECTs them; the UC CHECK is the contract's; §7.13 authority uses the
recorded role. Synthetic data only."""
import copy
import hashlib
import hmac
import importlib
import json
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import audit_chain as ac  # noqa: E402
from dam_safety import rule_flag_authority as rfa  # noqa: E402
import lakebase_extract as lx  # noqa: E402
import verify_review_chain as vj  # noqa: E402

VEC = json.load(open(os.path.join(ROOT, "app", "tests", "fixtures", "review_chain_canon2_vector.json")))
SIGNED = json.load(open(os.path.join(ROOT, "tests", "fixtures", "review_chain_signed_vector.json")))
PLAIN = json.load(open(os.path.join(ROOT, "tests", "fixtures", "review_chain_vector.json")))


class VecSigner:
    """The vector's deterministic test stand-in (app/tests/canon2_vector.py VecHmac): sha256('canon2-vec|' + hash)."""
    alg, key_id = "HS256-canon2-vector", "k-canon2-vector"

    def sign(self, record_hash):
        return hashlib.sha256(b"canon2-vec|" + record_hash.encode()).hexdigest()

    def verify(self, record_hash, signature, key_id):
        return key_id == self.key_id and hmac.compare_digest(str(signature), self.sign(record_hash))


class SignedVecSigner:
    def __init__(self, s):
        self.alg, self.key_id, self._k = s["alg"], s["key_id"], s["key_utf8"].encode()

    def sign(self, record_hash):
        return hmac.new(self._k, record_hash.encode(), hashlib.sha256).hexdigest()

    def verify(self, record_hash, signature, key_id):
        return key_id == self.key_id and hmac.compare_digest(str(signature), self.sign(record_hash))


def _verdict(v):
    return {"ok": v["ok"], "reason": v["reason"], "first_broken_seq": v["first_broken_seq"]}


# ---------------------------------------------------------------- engine == app vector
def test_constants_match_the_vector():
    assert list(ac.ACTOR_ROLES) == VEC["actor_roles"] and ac.CANON_VERSION == VEC["canon_version"]
    assert list(ac.CANON_FIELDS[1]) == VEC["canon_fields"]["1"] and list(ac.CANON_FIELDS[2]) == VEC["canon_fields"]["2"]
    assert set(VEC["hash_excluded"]) == {"record_hash", *ac.SIGNATURE_FIELDS}


def test_clean_vector_hashes_identically_v1_and_v2_rows():
    canons = [ac.canon_of(r) for r in VEC["rows"]]
    assert canons == [1, 1, 2, 2, 2]
    for r in VEC["rows"]:
        assert ac.record_hash(r) == r["record_hash"], r["chain_seq"]


@pytest.mark.parametrize("case", VEC["cases"], ids=[c["name"] for c in VEC["cases"]])
def test_engine_gives_the_app_verdict_on_every_canon2_case(case):
    v = ac.verify_records(copy.deepcopy(case["rows"]), VecSigner(), copy.deepcopy(case["head"]))
    assert _verdict(v) == case["expect"]


def test_canon1_vectors_still_verify():
    v = ac.verify_records(copy.deepcopy(PLAIN["rows"]), None, PLAIN["head"])
    assert v["ok"], v
    for name, c in SIGNED["cases"].items():
        v = ac.verify_records(copy.deepcopy(c["rows"]), SignedVecSigner(SIGNED["signer"]), c["head"])
        assert {k: v[k] for k in c["expected"]} == c["expected"], name


def _app_audit_chain():
    sys.path.insert(0, os.path.join(ROOT, "app"))
    try:
        return importlib.import_module("backend.audit_chain")
    except Exception as e:  # pragma: no cover - app deps missing
        pytest.skip(f"app backend not importable: {e}")
    finally:
        sys.path.remove(os.path.join(ROOT, "app"))


def test_engine_and_live_app_module_agree_on_every_vector():
    """Not just the frozen expectations: the app's own verify_chain/record_hash (read only) on the same inputs."""
    app = _app_audit_chain()
    for c in VEC["cases"]:
        a = app.verify_chain(copy.deepcopy(c["rows"]), copy.deepcopy(c["head"]), VecSigner())
        e = ac.verify_records(copy.deepcopy(c["rows"]), VecSigner(), copy.deepcopy(c["head"]))
        assert _verdict(a) == _verdict(e), c["name"]
        for r in c["rows"]:
            assert app.record_hash(r) == ac.record_hash(r)
    for name, c in SIGNED["cases"].items():
        s = SignedVecSigner(SIGNED["signer"])
        assert _verdict(app.verify_chain(copy.deepcopy(c["rows"]), c["head"], s)) == \
            _verdict(ac.verify_records(copy.deepcopy(c["rows"]), s, c["head"])), name


def test_canon_version_as_text_or_int_hashes_the_same_and_garbage_is_canon_unknown():
    r = copy.deepcopy(VEC["rows"][2])
    assert ac.record_hash({**r, "canon_version": "2"}) == r["record_hash"]
    rows = copy.deepcopy(VEC["rows"])
    rows[4]["canon_version"] = "two"
    assert _verdict(ac.verify_records(rows, VecSigner(), VEC["head"]))["reason"] == "canon_unknown"


# ---------------------------------------------------------------- extract + job
def test_extract_carries_both_columns_26_in_ddl_order_and_still_verifies():
    ddl = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    body = ddl.split("CREATE TABLE IF NOT EXISTS audit.review_record (")[1].split("CONSTRAINT pk_review")[0]
    cols = tuple(re.match(r"\s*(\w+)", l).group(1) for l in body.strip().splitlines() if not l.strip().startswith("--"))
    assert cols == lx.REVIEW_RECORD_COLS and len(cols) == 26 and cols[-2:] == ("actor_app_role", "canon_version")
    assert "actor_app_role" not in lx.REVIEW_REQUIRED and "canon_version" not in lx.REVIEW_REQUIRED
    rows = copy.deepcopy(VEC["rows"])
    rows[3]["canon_version"] = "2"                                     # Lakebase text/int: extract normalises to int
    recs = lx.review_rows_to_records(rows)
    assert [r["canon_version"] for r in recs] == [None, None, 2, 2, 2]
    assert [r["actor_app_role"] for r in recs] == [r.get("actor_app_role") for r in VEC["rows"]]
    assert ac.verify_records(recs, VecSigner(), VEC["head"])["ok"]    # TIMESTAMP signed_ts, int canon: same hashes


def test_verify_job_selects_both_canon2_columns():
    cols = [c.strip() for c in vj.review_record_select_cols().split(",")]
    assert {"actor_app_role", "canon_version", "record_hash", "signature", "sig_alg", "key_id"} <= set(cols)
    assert set(ac.CHAIN_FIELDS_V2) <= set(cols)
    src = open(os.path.join(ROOT, "src", "jobs", "verify_review_chain.py")).read()
    assert "cols = review_record_select_cols()" in src


# ---------------------------------------------------------------- UC CHECK
def _check_body():
    ddl = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    return re.search(r"ADD CONSTRAINT ck_review_record_canon CHECK \((.*?)\);", ddl, re.S).group(1)


@pytest.mark.parametrize("canon,role,ok", [(None, None, True), (None, "admin", True), (2, "cdse", True),
                                           (2, "admin,cdse", True), (2, None, False), (2, "", False),
                                           (2, "   ", False), (1, "admin", False), (3, "admin", False)])
def test_uc_canon_check(canon, role, ok):
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE t (canon_version INT, actor_app_role TEXT, CHECK ({_check_body()}))")
    try:
        db.execute("INSERT INTO t VALUES (?, ?)", (canon, role))
        assert ok
    except sqlite3.IntegrityError:
        assert not ok


# ---------------------------------------------------------------- §7.13 authority from actor_app_role
D, CDSE, ADMIN, DAMADMIN, ENG = "SYN-DAM-01", "cdse1@example.org", "admin@example.org", "da@example.org", "eng@example.org"
AUTH = [{"dam_id": D, "principal": CDSE, "granted_ts": None, "revoked_ts": None}]
_n = iter(range(1, 10_000))


def rec(action, who, role, cv=None, canon=2):
    r = {"review_id": f"RS-{next(_n)}", "dam_id": D, "chain_id": D, "chain_seq": next(_n), "subject_type": "alarm_rule",
         "subject_id": "T-1", "action_type": f"alarm_rule_flag_{action}", "decision": action, "comment": "synthetic",
         "reviewer_id": who, "reviewer_email": who, "review_level": "CDSE" if who == CDSE else "engineer",
         "signed_ts": "2026-11-09T15:00:00Z", "corrected_value": json.dumps(cv or {})}
    if canon == 2:
        r.update(canon_version=2, actor_app_role=role)
    return r


def codes(recs, app_roles=None):
    return [(p["code"], p["severity"]) for p in rfa.rule_flag_authority_problems(recs, AUTH, app_roles)]


def test_admin_apply_is_apply_by_admin_from_the_record_alone():
    assert codes([rec("apply", ADMIN, "admin", {"proposal_id": "P1", "proposed_by": ADMIN})]) == \
        [("apply_by_admin", "error")]
    assert codes([rec("reject", DAMADMIN, "dam_admin", {"proposal_id": "P2", "proposed_by": ENG})])[0] == \
        ("reject_by_admin", "error")


def test_propose_role_is_a_real_check_for_canon2():
    assert codes([rec("propose", ENG, "engineer", {"proposal_id": "P3"})]) == [("propose_no_authority", "error")]
    assert codes([rec("propose", DAMADMIN, "dam_admin", {"proposal_id": "P4"})]) == []
    assert codes([rec("propose", ADMIN, "admin,engineer", {"proposal_id": "P5"})]) == []
    # 'cdse' recorded by the app but not confirmed by ref.v_dam_reviewer_authority grants nothing
    assert codes([rec("propose", ENG, "cdse", {"proposal_id": "P6"})]) == [("propose_no_authority", "error")]
    assert codes([rec("apply", ENG, "cdse,engineer", {"proposal_id": "P7", "proposed_by": ENG})]) == \
        [("apply_not_cdse", "error")]


def test_cdse_who_is_also_admin_may_apply_and_canon1_stays_unverified():
    assert codes([rec("apply", CDSE, "admin,cdse", {"proposal_id": "P8", "proposed_by": CDSE})]) == []
    assert codes([rec("propose", ENG, None, {"proposal_id": "P9"}, canon=1)]) == [("propose_role_unverified", "warn")]
    # canon 1 still accepts the optional app_roles callable
    assert codes([rec("propose", ENG, None, {"proposal_id": "P10"}, canon=1)], lambda d, p, t: "dam_admin") == []


def test_each_new_reason_code_has_an_engine_case_matching_the_app():
    """QA-050: the four canon-2 reason codes (names from app c797151 verify_chain) each fire in the engine, on the
    app vector's case for that code."""
    want = {"actor_role_missing", "actor_role_invalid", "canon_downgrade", "canon_unknown"}
    seen = {}
    for c in VEC["cases"]:
        v = ac.verify_records(copy.deepcopy(c["rows"]), VecSigner(), copy.deepcopy(c["head"]))
        if v["reason"] in want:
            seen.setdefault(v["reason"], c["name"])
    assert set(seen) == want, seen
    app_src = open(os.path.join(ROOT, "app", "backend", "audit_chain.py")).read()
    for code in want:
        assert f'"{code}"' in app_src, code
