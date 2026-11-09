"""v1.6 (QA-039 v2): the engine re-verifies the app's per-dam hash chain from the UC copy. Interop vector
tests/fixtures/review_chain_vector.json was generated with the App Developer's app/backend/audit_chain.py
(branch app/v1.5-audit-chain, read only). Synthetic data."""
import copy
import hashlib
import hmac
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
sys.path.insert(0, os.path.join(ROOT, "src"))
import lakebase_extract as lx  # noqa: E402
import pytest  # noqa: E402
from dam_safety import audit_chain as ac  # noqa: E402

VEC = json.load(open(os.path.join(ROOT, "tests", "fixtures", "review_chain_vector.json"), encoding="utf-8"))
# QA-045c: signed vector + adversarial cases with the APP's verify_chain verdicts (tools/gen_review_chain_signed_vector.py)
SVEC = json.load(open(os.path.join(ROOT, "tests", "fixtures", "review_chain_signed_vector.json"), encoding="utf-8"))


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
    assert {k: res[k] for k in ("chain_id", "records", "ok", "first_broken_seq", "reason")} == {
        "chain_id": "DAM-TEST-1", "records": 3, "ok": True, "first_broken_seq": None, "reason": None}
    assert ac.verify_records(recs, expected_head=VEC["head"])["ok"]       # head check on the UC copy


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


# ============================================================ QA-045c: signatures + chain head (mirror app QA-045a)
class _TestHmacSigner:
    """TEST ONLY: same signer as tools/gen_review_chain_signed_vector.py (public key stored in the vector)."""
    alg, key_id = SVEC["signer"]["alg"], SVEC["signer"]["key_id"]
    _key = SVEC["signer"]["key_utf8"].encode("utf-8")

    def sign(self, record_hash):
        return hmac.new(self._key, record_hash.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, record_hash, signature, key_id):
        return key_id == self.key_id and hmac.compare_digest(str(signature), self.sign(record_hash))


SIGNER = _TestHmacSigner()


def _verdict(v):
    return {"ok": v["ok"], "reason": v["reason"], "first_broken_seq": v["first_broken_seq"]}


def test_signed_vector_rows_hash_and_sign_like_the_app():
    for r in SVEC["rows"]:
        assert ac.record_hash(r) == r["record_hash"]
        assert SIGNER.sign(r["record_hash"]) == r["signature"] and r["sig_alg"] == SIGNER.alg
    v = ac.verify_records(SVEC["rows"], signer=SIGNER, expected_head=SVEC["head"])
    assert v["ok"] and v["signed_records"] == 3 and v["signatures_required"] and v["head_seq"] == 3


@pytest.mark.parametrize("case", sorted(SVEC["cases"]))
def test_engine_verdict_equals_app_verdict_with_signer(case):
    c = SVEC["cases"][case]
    assert _verdict(ac.verify_records(copy.deepcopy(c["rows"]), SIGNER, c["head"])) == c["expected"]


@pytest.mark.parametrize("case", sorted(SVEC["cases"]))
def test_engine_verdict_equals_app_verdict_without_signer(case):
    c = SVEC["cases"][case]
    assert _verdict(ac.verify_records(copy.deepcopy(c["rows"]), None, c["head"])) == c["expected_noop_signer"]


# explicit adversarial expectations (independent of the generator's recorded verdicts)
@pytest.mark.parametrize("case,reason,seq", [
    ("stripped_first", "signature_missing", 1), ("stripped_all", "signature_missing", 1),
    ("downgrade_seq2", "signature_downgrade", 2), ("downgrade_key_id_only", "signature_downgrade", 3),
    ("forged_signature", "signature_invalid", 2), ("signature_of_other_record", "signature_invalid", 2),
    ("bad_sig_alg_none", "signature_invalid", 2), ("bad_sig_alg_unknown", "signature_invalid", 1),
    ("wrong_key_id", "signature_invalid", 2), ("truncated_tail", "head_mismatch", 3),
    ("truncated_tail_and_head_rewound_wrong_hash", "head_mismatch", 2), ("head_altered_hash", "head_mismatch", 3),
    ("reordered_seq_swap", "prev_mismatch", 2), ("replayed_duplicate_seq2", "gap", 3),
    ("replayed_seq1_as_seq4", "hash_mismatch", 4)])
def test_adversarial_signed_chain_is_rejected(case, reason, seq):
    c = SVEC["cases"][case]
    v = ac.verify_records(copy.deepcopy(c["rows"]), SIGNER, c["head"])
    assert (v["ok"], v["reason"], v["first_broken_seq"]) == (False, reason, seq)


def test_truncated_unsigned_tail_needs_the_expected_head():
    rows = copy.deepcopy(VEC["rows"])[:2]
    assert ac.verify_records(rows)["ok"]                                    # documented limit: no head, no detection
    v = ac.verify_records(rows, expected_head=VEC["head"])
    assert (v["reason"], v["first_broken_seq"]) == ("head_mismatch", 3)
    alias = {"chain_seq": VEC["head"]["chain_seq"], "record_hash": VEC["head"]["head_hash"]}
    assert ac.verify_records(VEC["rows"], expected_head=alias)["ok"]       # record_hash accepted for head_hash


def test_stripping_every_signature_passes_only_when_no_signer_is_configured():
    """Mirrors the app: signatures are enforced only by a real signer; a fully stripped chain verifies under the
    no-op signer. Verification jobs must pass the signer whenever signing is enabled (UNVERIFIED: no signer exists)."""
    c = SVEC["cases"]["stripped_all"]
    assert ac.verify_records(copy.deepcopy(c["rows"]), None, c["head"])["ok"]
    assert ac.verify_records(copy.deepcopy(c["rows"]), SIGNER, c["head"])["reason"] == "signature_missing"
