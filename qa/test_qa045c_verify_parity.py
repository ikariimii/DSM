"""QA-045c/d: engine verify_records vs app verify_chain agree on the same tampered inputs.

QA-045d closed the earlier residual: engine head=None with records → head_missing (same as the app).
Independent of App Dev / DA vector tests. Synthetic chain built with the app finalizer+HMAC signer.
"""
from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

from backend import audit_chain as app_ac  # noqa: E402
from dam_safety import audit_chain as eng_ac  # noqa: E402


class _Hmac:
    alg, key_id = "HS256-qa045c", "k-qa"

    def sign(self, record_hash):
        return hashlib.sha256(b"qa045c|" + record_hash.encode()).hexdigest()

    def verify(self, record_hash, signature, key_id):
        return key_id == self.key_id and signature == self.sign(record_hash)


def _base(n, dam="CRD"):
    return {"review_id": f"RS-{n}", "dam_id": dam, "subject_type": "alarm_event", "subject_id": f"E{n}",
            "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "uid-1",
            "reviewer_pe_license": None, "signed_ts": "2026-11-02T00:00:00.000000+00:00",
            "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
            "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}"}


def _signed_chain(n=3, signer=None):
    signer = signer or _Hmac()
    rows, prev = [], app_ac.GENESIS
    for i in range(1, n + 1):
        row = app_ac.finalizer(_base(i), signer)(i, prev)
        rows.append(row)
        prev = row["record_hash"]
    head = {"chain_id": "CRD", "chain_seq": n, "head_hash": prev}
    return rows, head, signer


def _verdict(v):
    return (v.get("ok"), v.get("first_broken_seq"), v.get("reason"), v.get("signed_records"),
            v.get("signatures_required"))


def _both(rows, head, signer):
    a = app_ac.verify_chain(copy.deepcopy(rows), copy.deepcopy(head), signer)
    # QA-045d: engine kw is `head` (expected_head removed); same semantics as the app
    e = eng_ac.verify_records(copy.deepcopy(rows), signer=signer, head=None if head is None else {
        "chain_seq": head["chain_seq"],
        "head_hash": head.get("head_hash", head.get("record_hash")),
        "chain_id": head.get("chain_id")})
    return a, e


def test_clean_signed_chain_agrees_and_ok():
    rows, head, signer = _signed_chain()
    a, e = _both(rows, head, signer)
    assert a["ok"] and e["ok"]
    assert _verdict(a) == _verdict(e)
    assert a["head_hash"] == e["head_hash"] == head["head_hash"]


def test_edited_field_hash_mismatch_agrees():
    rows, head, signer = _signed_chain()
    rows[1] = {**rows[1], "comment": "tampered"}
    a, e = _both(rows, head, signer)
    assert a["reason"] == e["reason"] == "hash_mismatch"
    assert _verdict(a) == _verdict(e)


def test_reordered_input_still_ok_because_both_sort_by_seq():
    """Both verifiers sort by chain_seq; list order alone cannot fork the chain."""
    rows, head, signer = _signed_chain()
    shuffled = [rows[2], rows[0], rows[1]]
    a, e = _both(shuffled, head, signer)
    assert a["ok"] and e["ok"] and _verdict(a) == _verdict(e)


def test_swapped_seq_content_prev_or_gap_agrees():
    """Adversarial: two records exchange chain_seq (and prev_hash left stale) → both fail the same way."""
    rows, head, signer = _signed_chain()
    a0, a1 = dict(rows[0]), dict(rows[1])
    a0["chain_seq"], a1["chain_seq"] = 2, 1
    rows = [a0, a1, rows[2]]
    a, e = _both(rows, head, signer)
    assert a["ok"] is False and e["ok"] is False
    assert a["reason"] == e["reason"]
    assert _verdict(a) == _verdict(e)


def test_deleted_middle_gap_agrees():
    rows, head, signer = _signed_chain()
    rows = [rows[0], rows[2]]
    a, e = _both(rows, head, signer)
    assert a["reason"] == e["reason"] == "gap"
    assert _verdict(a) == _verdict(e)


def test_truncated_tail_head_mismatch_agrees():
    rows, head, signer = _signed_chain()
    rows = rows[:-1]
    a, e = _both(rows, head, signer)
    assert a["reason"] == e["reason"] == "head_mismatch"
    assert _verdict(a) == _verdict(e)


def test_forged_head_seq_mismatch_agrees():
    rows, head, signer = _signed_chain()
    head = {**head, "chain_seq": 99}
    a, e = _both(rows, head, signer)
    assert a["reason"] == e["reason"] == "head_mismatch"
    assert _verdict(a) == _verdict(e)


def test_forged_head_hash_mismatch_agrees():
    rows, head, signer = _signed_chain()
    head = {**head, "head_hash": "f" * 64}
    a, e = _both(rows, head, signer)
    assert a["reason"] == e["reason"] == "head_mismatch"
    assert _verdict(a) == _verdict(e)


def test_stripped_signature_missing_agrees():
    rows, head, signer = _signed_chain()
    rows[0] = {**rows[0], "signature": None, "sig_alg": None, "key_id": None}
    a, e = _both(rows, head, signer)
    assert a["reason"] == e["reason"] == "signature_missing"
    assert _verdict(a) == _verdict(e)


def test_missing_sig_alg_after_signed_is_downgrade_agrees():
    rows, head, signer = _signed_chain()
    rows[1] = {**rows[1], "sig_alg": None}
    a, e = _both(rows, head, signer)
    # row 0 is signed; a later incomplete signature is a downgrade, not a first missing
    assert a["reason"] == e["reason"] == "signature_downgrade"
    assert _verdict(a) == _verdict(e)


def test_downgraded_after_signed_agrees():
    rows, head, signer = _signed_chain()
    rows[2] = {**rows[2], "signature": None, "sig_alg": None, "key_id": None}
    a, e = _both(rows, head, signer)
    assert a["reason"] == e["reason"] == "signature_downgrade"
    assert _verdict(a) == _verdict(e)


def test_borrowed_signature_invalid_agrees():
    rows, head, signer = _signed_chain()
    rows[1] = {**rows[1], "signature": rows[0]["signature"]}
    a, e = _both(rows, head, signer)
    assert a["reason"] == e["reason"] == "signature_invalid"
    assert _verdict(a) == _verdict(e)


def test_head_absent_both_report_head_missing():
    """QA-045d: residual disagreement closed — both say head_missing when head is None and records exist."""
    rows, head, signer = _signed_chain()
    a, e = _both(rows, None, signer)
    assert a["ok"] is False and e["ok"] is False
    assert a["reason"] == e["reason"] == "head_missing"
    assert _verdict(a) == _verdict(e)
    # With head supplied they still agree and ok
    a2, e2 = _both(rows, head, signer)
    assert a2["ok"] and e2["ok"] and _verdict(a2) == _verdict(e2)


def test_empty_chain_no_head_both_ok():
    a = app_ac.verify_chain([], None, None)
    e = eng_ac.verify_records([], signer=None, head=None)
    assert a["ok"] and e["ok"]


def test_noop_signer_rejects_carried_signature_agrees():
    rows, head, signer = _signed_chain()
    # strip to unsigned then claim a signature under NoopSigner
    for i, r in enumerate(rows):
        rows[i] = {**r, "signature": "deadbeef", "sig_alg": "HS256", "key_id": "k"}
    a = app_ac.verify_chain(copy.deepcopy(rows), copy.deepcopy(head), app_ac.NoopSigner())
    e = eng_ac.verify_records(copy.deepcopy(rows), signer=eng_ac.NoopSigner(), head=head)
    assert a["reason"] == e["reason"] == "signature_invalid"
    assert _verdict(a) == _verdict(e)
