"""v1.6 (QA-039 v2): engine-side re-verification of the app's per-dam hash chain of reviewer actions, so the UC copy
(audit.review_record, extracted by dam_lakebase_extract) can be checked without the app. Rules (SCHEMA_CONTRACT
§5.5.2), identical to the app's backend/audit_chain.py (interop vector: tests/fixtures/review_chain_vector.json):

* one chain per dam (chain_id = dam_id), chain_seq 1, 2, 3 ..., prev_hash of seq 1 = GENESIS ("0" * 64);
* record_hash = sha256(UTF-8(json.dumps({f: canonical_value(row[f]) for f in CHAIN_FIELDS}, sort_keys=True,
  separators=(',', ':'), ensure_ascii=False))) with chain_seq as an integer; i.e. every column except record_hash,
  signature, sig_alg, key_id (and the UC-only `source`). canonical_value is corrections.canonical_value (timestamps
  -> UTC ISO-8601 with microseconds and '+00:00', so a Lakebase ISO text and the UC TIMESTAMP hash identically).

The chain HEAD (Lakebase review_chain_head) is not extracted, so a deleted tail is detectable from the UC copy only
when the caller passes an `expected_head` (chain_seq + head_hash/record_hash, e.g. read from Lakebase or recorded at
an earlier verification); without it, tail truncation is detectable only in the app (GET /api/audit/signoffs/verify).

QA-045c: signature rules mirror the app's backend/audit_chain.py verify_chain after QA-045a (App Dev 113060c,
read only), with the same reason codes. signature / sig_alg / key_id are outside record_hash, so with a real signer
every record must carry all three and verify: signature_missing (first record(s) unsigned), signature_downgrade
(unsigned after an earlier signed record), signature_invalid (sig_alg differs from the signer's, or verify() fails).
With no signer (or a NoopSigner) a record that carries a signature is signature_invalid, as in the app (a no-op
signer cannot vouch for a signature it did not make)."""
from __future__ import annotations

import hashlib
import json

from dam_safety.inclinometer.corrections import canonical_value

GENESIS = "0" * 64
ACTION_TYPES = ("bias_review", "data_correction_review", "alarm_ack", "alarm_close")
CHAIN_FIELDS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash", "action_type",
                "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id", "chain_seq",
                "prev_hash")


def record_hash(row: dict) -> str:
    payload = {k: canonical_value(row.get(k)) for k in CHAIN_FIELDS}
    payload["chain_seq"] = None if row.get("chain_seq") is None else int(row["chain_seq"])
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SIGNATURE_FIELDS = ("signature", "sig_alg", "key_id")


class NoopSigner:
    """Same contract as the app's NoopSigner: signs nothing and vouches for no signature."""
    alg = None
    key_id = None

    def sign(self, record_hash: str) -> str | None:
        return None

    def verify(self, record_hash: str, signature: str, key_id: str | None) -> bool:
        return False


def is_enforcing(signer) -> bool:
    """A real (non-no-op) signer makes signatures mandatory on every record (app rule, QA-045a)."""
    return signer is not None and not isinstance(signer, NoopSigner)


def _present(v) -> bool:
    return v is not None and str(v).strip() != ""


def verify_records(rows, signer=None, expected_head: dict | None = None) -> dict:
    """Recompute one chain from its records (any order).

    signer: object with .alg, .key_id and .verify(record_hash, signature, key_id) (the app's Signer protocol);
    None / NoopSigner = signatures not required. expected_head: optional {chain_seq, head_hash} (record_hash is
    accepted for head_hash); None = no head check (the UC copy has no head; see module doc).

    Returns {chain_id, records, ok, first_broken_seq, reason, detail, signed_records, signatures_required} (+ head_seq,
    head_hash when ok). reason, as in the app: gap | prev_mismatch | hash_mismatch | signature_missing |
    signature_downgrade | signature_invalid | head_mismatch. Reordered/replayed records surface as gap (duplicate or
    out-of-place chain_seq) or prev_mismatch/hash_mismatch (a record moved to another seq)."""
    signer = signer or NoopSigner()
    rows = sorted(rows, key=lambda r: (int(r["chain_seq"]), str(r["review_id"])))
    enforce = is_enforcing(signer)
    out = {"chain_id": rows[0]["chain_id"] if rows else (expected_head or {}).get("chain_id"), "records": len(rows),
           "ok": False, "first_broken_seq": None, "reason": None, "detail": None, "signed_records": 0,
           "signatures_required": enforce}

    def broken(seq, reason, detail):
        out.update(first_broken_seq=seq, reason=reason, detail=detail)
        return out

    expected, prev, signed_seen = 1, GENESIS, 0
    for r in rows:
        seq = int(r["chain_seq"])
        if seq != expected:
            return broken(expected, "gap", f"expected chain_seq {expected}, found {seq}")
        if r.get("prev_hash") != prev:
            return broken(seq, "prev_mismatch", "prev_hash does not match the previous record_hash"
                          if seq > 1 else "first record does not start at GENESIS")
        if record_hash(r) != r.get("record_hash"):
            return broken(seq, "hash_mismatch", "recomputed record_hash differs from the stored one")
        if enforce:
            missing = [f for f in SIGNATURE_FIELDS if not _present(r.get(f))]
            if missing:
                if signed_seen:
                    return broken(seq, "signature_downgrade", "unsigned record after signed record(s) "
                                  f"(first signed seq {signed_seen}); missing {', '.join(missing)}")
                return broken(seq, "signature_missing", f"real signer configured but record lacks {', '.join(missing)}")
            if r["sig_alg"] != signer.alg:
                return broken(seq, "signature_invalid", f"sig_alg {r['sig_alg']!r} does not match the configured "
                              f"signer ({signer.alg!r})")
            if not signer.verify(r["record_hash"], r["signature"], r["key_id"]):
                return broken(seq, "signature_invalid", f"signature ({r['sig_alg']}, key {r['key_id']}) does not verify")
            out["signed_records"] += 1
            signed_seen = signed_seen or seq
        elif r.get("signature"):  # no-op signer cannot vouch for any signature (app behaviour)
            if not signer.verify(r["record_hash"], r["signature"], r.get("key_id")):
                return broken(seq, "signature_invalid", f"signature ({r.get('sig_alg')}) does not verify")
            out["signed_records"] += 1
        prev, expected = r["record_hash"], seq + 1
    last = expected - 1
    if expected_head is not None:
        h_seq = int(expected_head["chain_seq"])
        h_hash = expected_head.get("head_hash", expected_head.get("record_hash"))
        if h_seq != last or h_hash != prev:
            return broken(last + 1 if h_seq > last else h_seq, "head_mismatch",
                          f"chain head at seq {h_seq} but the last verifiable record is seq {last} "
                          "(records deleted from the tail, or head altered)")
    out["ok"] = True
    out["head_seq"] = last
    out["head_hash"] = prev
    return out
