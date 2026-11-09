"""QA-039 v2: hash-chained, append-only audit of every reviewer action.

Covered actions (ACTION_TYPES): bias-proposal review, data-correction review (approve / reject), alarm acknowledge
(the CDSE acknowledgement) and alarm close. Each action is ONE new row in Lakebase `review_signoff`; nothing is
updated or deleted (DB triggers refuse it) and the original value is stored next to the corrected value (Ch.14
§14.4.1), never overwritten.

Chain scope: ONE CHAIN PER DAM (chain_id = dam_id). A dam's records are verifiable on their own, so the per-dam
verify endpoint can be scoped by the dam ACL without exposing other dams. Genesis: prev_hash of chain_seq 1 is
GENESIS ("0" * 64).

record_hash = sha256(canonical JSON of every stored column except record_hash, signature, sig_alg, key_id) -
i.e. it covers the content, identity, timestamp, original/corrected values, the engine-compatible content hash
(signature_hash), chain_id, chain_seq and prev_hash. Changing, deleting, inserting or reordering any record
therefore breaks the chain at or after that record; deleting the LAST record is caught by review_chain_head.

The token is never stored or logged: only its unverified 'sub' claim (identity only) is kept.

Future key-based signing: nullable signature / sig_alg / key_id columns and the Signer interface below
(NoopSigner by default; FERC confirmed no in-app crypto signature is needed - document signing is a backlog item,
docs/BACKLOG.md)."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Protocol

from .signoff import _canon, canonical_value

GENESIS = "0" * 64
ACTION_TYPES = ("bias_review", "data_correction_review", "alarm_ack", "alarm_close")
# v1.6 live alarm-rule flag edits (subject_type 'alarm_rule', decisions propose | apply | reject). The DA's UC
# ck_review_record_subject_decision and the engine's ACTION_TYPES must add them before the extract carries these rows
# (SCHEMA_CONTRACT §7 item 13).
RULE_FLAG_ACTION_TYPES = ("alarm_rule_flag_propose", "alarm_rule_flag_apply", "alarm_rule_flag_reject")
# v1.7 DSSMP version registration (subject_type 'dssmp_version', decisions propose | approve | reject). The DA's UC
# CHECKs and the engine's action types must add them before the extract carries these rows (SCHEMA_CONTRACT §7 19).
DSSMP_ACTION_TYPES = ("dssmp_version_propose", "dssmp_version_approve", "dssmp_version_reject")
# v1.7 first-CDSE bootstrap and CDSE changes (subject_type 'dam') and the bulk re-version of rules under a superseded
# DSSMP version (subject_type 'rule_reversion'); SCHEMA_CONTRACT §7 items 20 / 21 (DA CHECKs + engine).
CDSE_ACTION_TYPES = ("dam_cdse_assign", "dam_cdse_change_propose", "dam_cdse_change_approve", "dam_cdse_change_reject")
REVERSION_ACTION_TYPES = ("rule_reversion_propose", "rule_reversion_approve", "rule_reversion_reject")
APP_ACTION_TYPES = (ACTION_TYPES + RULE_FLAG_ACTION_TYPES + DSSMP_ACTION_TYPES + CDSE_ACTION_TYPES
                    + REVERSION_ACTION_TYPES)
HASH_EXCLUDED = ("record_hash", "signature", "sig_alg", "key_id")
CHAIN_FIELDS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash", "action_type",
                "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id", "chain_seq",
                "prev_hash")
# Versioned canonical record form (v1.6, DA follow-up 1). canon_version NULL/absent = canon 1: exactly CHAIN_FIELDS,
# so every existing record and the DA's fixtures (review_chain_vector.json, review_chain_signed_vector.json) hash
# unchanged. canon 2 (every NEW app record) also hashes actor_app_role and canon_version itself, so stripping either
# breaks record_hash. A canon-2 record without a valid actor_app_role fails verification (actor_role_missing /
# actor_role_invalid); a canon-1 record after a canon-2 record is canon_downgrade; an unknown version is canon_unknown.
CANON_VERSION = 2
ACTOR_ROLE_FIELD = "actor_app_role"
CANON_FIELD = "canon_version"
CHAIN_FIELDS_V2 = CHAIN_FIELDS + (ACTOR_ROLE_FIELD, CANON_FIELD)
CANON_FIELDS = {1: CHAIN_FIELDS, 2: CHAIN_FIELDS_V2}
# actor_app_role: the RAW app roles of the acting user on that dam at the time of the action, derived server-side
# (routers/signoffs.actor_app_role), never client-supplied. One token, or several sorted and comma-joined
# (e.g. "admin,cdse", "cdse,engineer"). "cdse" = the dam's named CDSE via the reviewer-authority lookup.
ACTOR_ROLES = ("viewer", "engineer", "dso", "dam_admin", "admin", "cdse")
# app role -> review_level (Genie-confirmed 2026-11-09; DA SCHEMA_CONTRACT v1.6 ROLE-LEVEL). An app role NEVER
# confers CDSE: dso, dam_admin and platform admin get no review elevation. CDSE_LEVEL is recorded only for the
# principal named by ref.dam.cdse_id (or a direct ref.project_membership CDSE grant) for that dam - FERC Ch.14 App K
# 14-K-4 treats the Chief Dam Safety Engineer as a named person (routers/signoffs.is_cdse). Records written under the
# earlier proposal keep their stored review_level and still verify (only new records change).
CDSE_LEVEL = "CDSE"
ROLE_LEVEL = {"engineer": "engineer", "dso": "engineer", "dam_admin": "engineer", "admin": "engineer"}


class Signer(Protocol):
    """Pluggable key-based signer for a future decision. sign() returns the signature text or None (unsigned)."""
    alg: str | None
    key_id: str | None

    def sign(self, record_hash: str) -> str | None: ...

    def verify(self, record_hash: str, signature: str, key_id: str | None) -> bool: ...


class NoopSigner:
    """Default: records are hash-chained but not key-signed (signature/sig_alg/key_id stay NULL)."""
    alg = None
    key_id = None

    def sign(self, record_hash: str) -> str | None:
        return None

    def verify(self, record_hash: str, signature: str, key_id: str | None) -> bool:
        return False  # cannot vouch for a signature it did not make


SIGNATURE_FIELDS = ("signature", "sig_alg", "key_id")


def is_enforcing(signer) -> bool:
    """A real (non-no-op) signer makes signatures mandatory on every record (QA-045a)."""
    return signer is not None and not isinstance(signer, NoopSigner)


def _present(v) -> bool:
    return v is not None and str(v).strip() != ""


def canon_json(obj) -> str:
    """Canonical JSON text for original_value / corrected_value (stored as text so the hash is stable)."""
    return _canon({k: canonical_value(v) for k, v in obj.items()} if isinstance(obj, dict) else canonical_value(obj))


def canon_of(row: dict) -> int | None:
    """Canonical form of a stored record: 1 when canon_version is NULL/absent (pre-v2), else its int value
    (None when it is not an integer)."""
    v = row.get(CANON_FIELD)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return 1
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def actor_role_problem(value) -> str | None:
    """None when value is a valid actor_app_role, else 'actor_role_missing' / 'actor_role_invalid'."""
    if value is None or not str(value).strip():
        return "actor_role_missing"
    parts = str(value).split(",")
    if any(p not in ACTOR_ROLES for p in parts) or parts != sorted(set(parts)):
        return "actor_role_invalid"
    return None


def record_hash(row: dict) -> str:
    canon = canon_of(row)
    fields = CANON_FIELDS.get(canon, CHAIN_FIELDS_V2)  # unknown canon: hashed as v2, verify reports canon_unknown
    payload = {k: canonical_value(row.get(k)) for k in fields}
    payload["chain_seq"] = None if row.get("chain_seq") is None else int(row["chain_seq"])
    if CANON_FIELD in fields:
        payload[CANON_FIELD] = canon
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


def finalizer(base: dict, signer: Signer | None = None):
    """Returns finalize(seq, prev_hash) for StateStore.append_chained: completes the row under the chain lock."""
    signer = signer or NoopSigner()

    if canon_of(base) == CANON_VERSION and actor_role_problem(base.get(ACTOR_ROLE_FIELD)):
        raise ValueError(f"a canon-{CANON_VERSION} record needs a valid server-derived {ACTOR_ROLE_FIELD}")

    def finalize(seq: int, prev_hash: str) -> dict:
        row = {**base, "chain_id": base["dam_id"], "chain_seq": seq, "prev_hash": prev_hash}
        row["record_hash"] = record_hash(row)
        sig = signer.sign(row["record_hash"])
        row.update(signature=sig, sig_alg=signer.alg if sig else None, key_id=signer.key_id if sig else None)
        return row
    return finalize


def verify_chain(rows: list[dict], head: dict | None, signer: Signer | None = None) -> dict:
    """Recompute a whole chain. rows: the chain's records (any order). Reports ok, or the FIRST broken seq and its
    reason: gap (missing / duplicate seq), prev_mismatch (prev_hash != previous record_hash, or genesis), hash_mismatch
    (content changed), signature_missing / signature_downgrade (a real signer is configured but signature, sig_alg or
    key_id is empty or absent; downgrade = after an earlier signed record), signature_invalid, head_missing, head_mismatch (head does not point at the last record, e.g. the
    tail was deleted)."""
    signer = signer or NoopSigner()
    rows = sorted(rows, key=lambda r: (int(r["chain_seq"]), str(r["review_id"])))
    chain_id = rows[0]["chain_id"] if rows else (head or {}).get("chain_id")
    out = {"chain_id": chain_id, "records": len(rows), "ok": False, "first_broken_seq": None, "reason": None,
           "detail": None, "signed_records": 0}

    def broken(seq, reason, detail):
        out.update(first_broken_seq=seq, reason=reason, detail=detail)
        return out

    enforce = is_enforcing(signer)
    out["signatures_required"] = enforce
    expected, prev, signed_seen, v2_seen = 1, GENESIS, 0, 0
    for r in rows:
        seq = int(r["chain_seq"])
        if seq != expected:
            return broken(expected, "gap", f"expected chain_seq {expected}, found {seq}")
        if r.get("prev_hash") != prev:
            return broken(seq, "prev_mismatch", "prev_hash does not match the previous record_hash"
                          if seq > 1 else "first record does not start at GENESIS")
        canon = canon_of(r)
        if canon not in CANON_FIELDS:
            return broken(seq, "canon_unknown", f"unknown canon_version {r.get(CANON_FIELD)!r}")
        if record_hash(r) != r.get("record_hash"):
            return broken(seq, "hash_mismatch", "recomputed record_hash differs from the stored one")
        if canon >= 2:
            bad = actor_role_problem(r.get(ACTOR_ROLE_FIELD))
            if bad:
                return broken(seq, bad, f"canon-{canon} record has {ACTOR_ROLE_FIELD} {r.get(ACTOR_ROLE_FIELD)!r}")
            v2_seen = v2_seen or seq
        elif v2_seen:
            return broken(seq, "canon_downgrade", f"canon-1 record (no {ACTOR_ROLE_FIELD}) after canon-2 record(s) "
                          f"(first at seq {v2_seen})")
        if enforce:
            # QA-045a: signature/sig_alg/key_id sit outside record_hash, so stripping them leaves the hash chain intact.
            # With a real signer every record must carry all three and verify; nothing unsigned is accepted.
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
        elif r.get("signature"):  # NoopSigner: unchanged behaviour - it cannot vouch for any signature
            if not signer.verify(r["record_hash"], r["signature"], r.get("key_id")):
                return broken(seq, "signature_invalid", f"signature ({r.get('sig_alg')}) does not verify")
            out["signed_records"] += 1
        prev, expected = r["record_hash"], seq + 1
    last = expected - 1
    if head is None:
        if rows:
            return broken(last + 1, "head_missing", "records exist but the chain head row is missing")
    elif int(head["chain_seq"]) != last or head["head_hash"] != prev:
        return broken(last + 1 if int(head["chain_seq"]) > last else int(head["chain_seq"]), "head_mismatch",
                      f"chain head at seq {head['chain_seq']} but the last verifiable record is seq {last} "
                      "(records deleted from the tail, or head altered)")
    out["ok"] = True
    out["head_seq"] = last
    out["head_hash"] = prev
    return out


def token_sub(token: str | None) -> str | None:
    """'sub' claim of the forwarded user access token, base64url-decoded WITHOUT signature verification - identity
    labelling only, never authorization. Returns None on anything malformed. The token itself is never stored or
    logged (no logging in this function)."""
    if not token or token.count(".") < 2:
        return None
    try:
        seg = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        sub = claims.get("sub") if isinstance(claims, dict) else None
        return str(sub) if sub not in (None, "") else None
    except Exception:  # noqa: BLE001 - any malformed token -> no claim
        return None
