"""v1.6 (QA-039 v2): the engine re-verifies the app's per-dam hash chain from the UC copy. Interop vector
tests/fixtures/review_chain_vector.json was generated with the App Developer's app/backend/audit_chain.py
(branch app/v1.5-audit-chain, read only). Synthetic data."""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
sys.path.insert(0, os.path.join(ROOT, "src"))
import lakebase_extract as lx  # noqa: E402
from dam_safety import audit_chain as ac  # noqa: E402

VEC = json.load(open(os.path.join(ROOT, "tests", "fixtures", "review_chain_vector.json"), encoding="utf-8"))


def test_engine_hash_matches_app_vector():
    for r in VEC["rows"]:
        assert ac.record_hash(r) == r["record_hash"], r["review_id"]
    assert ac.verify_records(VEC["rows"])["ok"]
    assert VEC["rows"][-1]["record_hash"] == VEC["head"]["head_hash"]


def test_chain_fields_and_columns_agree():
    assert set(ac.CHAIN_FIELDS) | {"record_hash", "signature", "sig_alg", "key_id"} == set(VEC["rows"][0])
    assert set(ac.CHAIN_FIELDS) | {"record_hash", "signature", "sig_alg", "key_id", "source"} == set(lx.REVIEW_RECORD_COLS)
    assert ac.ACTION_TYPES == ("bias_review", "data_correction_review", "alarm_ack", "alarm_close")


def test_uc_copy_reverifies_after_extract():
    recs = lx.review_rows_to_records(list(reversed(copy.deepcopy(VEC["rows"]))))
    assert all(r["signed_ts"].tzinfo is not None for r in recs)            # TIMESTAMP, not text
    assert [r["chain_seq"] for r in recs] == [1, 2, 3]                     # chain order
    res = ac.verify_records(recs)
    assert res == {"chain_id": "DAM-TEST-1", "records": 3, "ok": True, "first_broken_seq": None, "reason": None}


def test_tamper_gap_and_relink_are_detected():
    rows = copy.deepcopy(VEC["rows"])
    rows[1]["comment"] = "edited"
    assert ac.verify_records(rows)[ "reason"] == "hash_mismatch" and ac.verify_records(rows)["first_broken_seq"] == 2
    rows = copy.deepcopy(VEC["rows"])
    del rows[1]
    assert (ac.verify_records(rows)["reason"], ac.verify_records(rows)["first_broken_seq"]) == ("gap", 2)
    rows = copy.deepcopy(VEC["rows"])
    rows[2]["prev_hash"] = ac.GENESIS
    assert ac.verify_records(rows)["reason"] == "prev_mismatch"
