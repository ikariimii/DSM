"""v1.6 (QA-039 v2): engine-side re-verification of the app's per-dam hash chain of reviewer actions, so the UC copy
(audit.review_record, extracted by dam_lakebase_extract) can be checked without the app. Rules (SCHEMA_CONTRACT
§5.5.2), identical to the app's backend/audit_chain.py (interop vector: tests/fixtures/review_chain_vector.json):

* one chain per dam (chain_id = dam_id), chain_seq 1, 2, 3 ..., prev_hash of seq 1 = GENESIS ("0" * 64);
* record_hash = sha256(UTF-8(json.dumps({f: canonical_value(row[f]) for f in CHAIN_FIELDS}, sort_keys=True,
  separators=(',', ':'), ensure_ascii=False))) with chain_seq as an integer; i.e. every column except record_hash,
  signature, sig_alg, key_id (and the UC-only `source`). canonical_value is corrections.canonical_value (timestamps
  -> UTC ISO-8601 with microseconds and '+00:00', so a Lakebase ISO text and the UC TIMESTAMP hash identically).

QA-045d: the chain HEAD is mandatory, as in the app. `dam_lakebase_extract` copies the app's Lakebase
review_chain_head into the append-only UC table audit.chain_head (same REPEATABLE READ snapshot as the records), and
`verify_with_chain_heads` reads the head from those rows; callers never supply an expected head. A chain with records
and no head row is head_missing, never ok (app verify_chain semantics). Every stored head is also a checkpoint: a
later deletion of records at or below any stored head's seq, a head row rewound below an earlier one, a forged
head_hash or a head row for another chain/dam is reported. Truncation is detectable from the first head row on.

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
# v1.6 §7.13 (app 6f7a1d5 RULE_FLAG_ACTION_TYPES): live alarm-rule cdse_ack_required edits, subject_type 'alarm_rule'
RULE_FLAG_ACTION_TYPES = ("alarm_rule_flag_propose", "alarm_rule_flag_apply", "alarm_rule_flag_reject")
ACTION_TYPES = ("bias_review", "data_correction_review", "alarm_ack", "alarm_close") + RULE_FLAG_ACTION_TYPES
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


# ---------------------------------------------------------------- prod fail-closed (Genie 2026-11-09)
PROD_CATALOGS = ("dam_prod",)
NO_SIGNER = ("", "none", "noop")


class SignerConfigError(SystemExit):
    """Raised at job start; a SystemExit so the task fails before reading any data."""


def load_signer(spec: str | None):
    """Signer from the job configuration (--chain_signer / env DAM_CHAIN_SIGNER). 'none' / unset -> NoopSigner. No real
    signer implementation exists yet (FERC requires no in-app crypto signature; VERIFICATION QA-045c), so any other
    value is refused instead of being silently treated as no-op."""
    s = (spec or "").strip().lower()
    if s in NO_SIGNER:
        return NoopSigner()
    raise SignerConfigError(f"chain signer {spec!r} is not implemented (known: none); refusing to start")


def is_real_signer(signer) -> bool:
    """Enforcing, with a non-empty alg and key_id, a callable verify, and not a TEST-ONLY signer (alg '*-test')."""
    alg, kid = getattr(signer, "alg", None), getattr(signer, "key_id", None)
    return (is_enforcing(signer) and callable(getattr(signer, "verify", None)) and bool(alg and str(alg).strip())
            and bool(kid and str(kid).strip()) and not str(alg).lower().endswith("-test"))


def require_signer_for_catalog(catalog: str, signer) -> None:
    """Fail closed: a job that verifies chains must not run against a production catalog without a real signer,
    because a fully stripped chain verifies under the no-op signer (QA-045c). Call at job start, before any read."""
    if catalog in PROD_CATALOGS and not is_real_signer(signer):
        raise SignerConfigError(
            f"refusing to verify reviewer chains in {catalog}: no real (non-no-op) signer is configured "
            f"(got {type(signer).__name__}, alg={getattr(signer, 'alg', None)!r}); configure --chain_signer first")


def _present(v) -> bool:
    return v is not None and str(v).strip() != ""


def verify_records(rows, signer=None, head: dict | None = None) -> dict:
    """Recompute one chain from its records (any order) against its head. Mirrors the app's
    backend/audit_chain.py verify_chain(rows, head, signer) (App Dev, read only), reason for reason.

    signer: object with .alg, .key_id and .verify(record_hash, signature, key_id) (the app's Signer protocol);
    None / NoopSigner = signatures not required. head: {chain_seq, head_hash} (record_hash accepted for head_hash) or
    None. None with records => head_missing (QA-045d: never ok); None with no records => ok (empty chain). A head
    that names another chain_id is head_mismatch (the app looks its head up by chain_id, so it cannot get one).

    Returns {chain_id, records, ok, first_broken_seq, reason, detail, signed_records, signatures_required} (+ head_seq,
    head_hash when ok). reason, as in the app: gap | prev_mismatch | hash_mismatch | signature_missing |
    signature_downgrade | signature_invalid | head_missing | head_mismatch. Reordered/replayed records surface as gap
    (duplicate or out-of-place chain_seq) or prev_mismatch/hash_mismatch (a record moved to another seq)."""
    signer = signer or NoopSigner()
    rows = sorted(rows, key=lambda r: (int(r["chain_seq"]), str(r["review_id"])))
    enforce = is_enforcing(signer)
    out = {"chain_id": rows[0]["chain_id"] if rows else (head or {}).get("chain_id"), "records": len(rows),
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
    if head is None:
        if rows:   # app: records exist but the chain head row is missing
            return broken(last + 1, "head_missing", "records exist but the chain head row is missing")
    else:
        h_seq = int(head["chain_seq"])
        h_hash = head.get("head_hash", head.get("record_hash"))
        if rows and head.get("chain_id") not in (None, out["chain_id"]):
            return broken(last + 1 if h_seq > last else h_seq, "head_mismatch",
                          f"head row is for chain {head.get('chain_id')!r}, "
                          f"not {out['chain_id']!r}")
        if h_seq != last or h_hash != prev:
            return broken(last + 1 if h_seq > last else h_seq, "head_mismatch",
                          f"chain head at seq {h_seq} but the last verifiable record is seq {last} "
                          "(records deleted from the tail, or head altered)")
    out["ok"] = True
    out["head_seq"] = last
    out["head_hash"] = prev
    return out


# ---------------------------------------------------------------- QA-045d: heads from audit.chain_head
CHAIN_HEAD_COLS = ("dam_id", "chain_id", "chain_seq", "head_hash", "computed_utc", "job_run_id", "source")


def latest_chain_head(head_rows, dam_id: str, chain_id: str) -> dict | None:
    """The latest audit.chain_head row of (dam_id, chain_id): max computed_utc, ties broken by chain_seq then
    head_hash (deterministic). Rows of other chains/dams are ignored (they are never this chain's head)."""
    mine = [h for h in head_rows if h.get("dam_id") == dam_id and h.get("chain_id") == chain_id]
    if not mine:
        return None
    return max(mine, key=lambda h: (str(h["computed_utc"]), int(h["chain_seq"]), str(h["head_hash"])))


def verify_with_chain_heads(rows, head_rows, dam_id: str, chain_id: str | None = None, signer=None) -> dict:
    """Verify one chain against the heads the job stored in audit.chain_head (no caller-supplied head).

    1. records of another chain or dam in `rows` are a caller bug (the job filters by dam), so ValueError.
    2. latest head (latest_chain_head) -> verify_records(rows, signer, head). No head row => head_missing.
    3. checkpoints: every stored head of this chain must still match the record at its seq (a head above the
       current last record = tail truncated after it was recorded; a different hash = forged head or rewritten
       record), and the latest head must not be below an earlier one (rewound head) => head_mismatch."""
    chain_id = chain_id or dam_id
    rows = list(rows)
    stray = {(r.get("dam_id"), r.get("chain_id")) for r in rows} - {(dam_id, chain_id)}
    if stray:
        raise ValueError(f"verify_with_chain_heads({dam_id}/{chain_id}) got records of {sorted(stray, key=str)}")
    heads = [h for h in head_rows if h.get("dam_id") == dam_id and h.get("chain_id") == chain_id]
    head = latest_chain_head(heads, dam_id, chain_id)
    res = verify_records(rows, signer, head)
    res["heads_checked"] = len(heads)
    if not res["ok"]:
        return res
    by_seq = {int(r["chain_seq"]): r["record_hash"] for r in rows}
    top = max(int(h["chain_seq"]) for h in heads) if heads else 0
    if head is not None and int(head["chain_seq"]) < top:
        res.update(ok=False, first_broken_seq=int(head["chain_seq"]) + 1, reason="head_mismatch",
                   detail=f"latest head (seq {head['chain_seq']}) is below an earlier head (seq {top}): "
                          "head rewound, records after it may have been deleted")
        return res
    for h in sorted(heads, key=lambda h: (int(h["chain_seq"]), str(h["computed_utc"]))):
        s = int(h["chain_seq"])
        if s == 0:
            continue
        if by_seq.get(s) != h["head_hash"]:
            res.update(ok=False, first_broken_seq=s, reason="head_mismatch",
                       detail=f"stored head seq {s} @ {h['computed_utc']} ({h.get('job_run_id')}) does not match the "
                              "record at that seq (forged head, or the record was rewritten)")
            return res
    return res
