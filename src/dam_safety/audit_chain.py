"""v1.6 (QA-039 v2): engine-side re-verification of the app's per-dam hash chain of reviewer actions, so the UC copy
(audit.review_record, extracted by dam_lakebase_extract) can be checked without the app. Rules (SCHEMA_CONTRACT
§5.5.2), identical to the app's backend/audit_chain.py (interop vector: tests/fixtures/review_chain_vector.json):

* one chain per dam (chain_id = dam_id), chain_seq 1, 2, 3 ..., prev_hash of seq 1 = GENESIS ("0" * 64);
* record_hash = sha256(UTF-8(json.dumps({f: canonical_value(row[f]) for f in CHAIN_FIELDS}, sort_keys=True,
  separators=(',', ':'), ensure_ascii=False))) with chain_seq as an integer; i.e. every column except record_hash,
  signature, sig_alg, key_id (and the UC-only `source`). canonical_value is corrections.canonical_value (timestamps
  -> UTC ISO-8601 with microseconds and '+00:00', so a Lakebase ISO text and the UC TIMESTAMP hash identically).

The chain HEAD (Lakebase review_chain_head) is not extracted, so a deleted tail is detectable only in the app
(GET /api/audit/signoffs/verify), not from the UC copy alone."""
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


def verify_records(rows) -> dict:
    """Recompute one chain from its records (any order). Returns {chain_id, records, ok, first_broken_seq, reason}
    with reason gap | prev_mismatch | hash_mismatch (same names as the app). No head check (see module doc)."""
    rows = sorted(rows, key=lambda r: (int(r["chain_seq"]), str(r["review_id"])))
    out = {"chain_id": rows[0]["chain_id"] if rows else None, "records": len(rows), "ok": False,
           "first_broken_seq": None, "reason": None}
    expected, prev = 1, GENESIS
    for r in rows:
        seq = int(r["chain_seq"])
        if seq != expected:
            out.update(first_broken_seq=expected, reason="gap")
            return out
        if r.get("prev_hash") != prev:
            out.update(first_broken_seq=seq, reason="prev_mismatch")
            return out
        if record_hash(r) != r.get("record_hash"):
            out.update(first_broken_seq=seq, reason="hash_mismatch")
            return out
        prev, expected = r["record_hash"], seq + 1
    out["ok"] = True
    return out
