"""App-side canon-2 audit-chain vector (v1.6 DA follow-up 1: actor_app_role). NOT the DA's fixtures - those
(tests/fixtures/review_chain_vector.json, review_chain_signed_vector.json) stay canon 1 and untouched.

Regenerate: python app/tests/canon2_vector.py  (writes app/tests/fixtures/review_chain_canon2_vector.json).
test_actor_app_role.py asserts the committed file equals build() byte for byte, so the canon is pinned."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from backend import audit_chain as ac  # noqa: E402

FIXTURE = HERE / "fixtures" / "review_chain_canon2_vector.json"


class VecHmac:
    """Deterministic test signer (NOT a real key): sig = sha256('canon2-vec|' + record_hash)."""
    alg, key_id = "HS256-canon2-vector", "k-canon2-vector"

    def sign(self, record_hash):
        return hashlib.sha256(b"canon2-vec|" + record_hash.encode()).hexdigest()

    def verify(self, record_hash, signature, key_id):
        return key_id == self.key_id and signature == self.sign(record_hash)


def _base(n, action, decision, level, role=None, canon=None):
    b = {"review_id": f"RS-CANON2-{n:02d}", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": f"EV-{n:05d}",
         "review_level": level, "decision": decision, "comment": f"vector rationale {n}", "reviewer_id": f"uid-{n}",
         "reviewer_pe_license": None, "signed_ts": f"2026-11-09T16:{n:02d}:00.000000+00:00",
         "signature_hash": hashlib.sha256(f"content-{n}".encode()).hexdigest(), "action_type": action,
         "reviewer_email": f"user{n}@demo.local", "reviewer_sub": None, "original_value": '{"state":"open"}',
         "corrected_value": '{"state":"acknowledged"}'}
    if canon is not None:
        b.update(actor_app_role=role, canon_version=canon)
    return b


BASES = [  # a chain that migrates: two pre-v2 (canon 1) records, then canon-2 records only
    _base(1, "alarm_ack", "acknowledge", "engineer"),
    _base(2, "alarm_close", "close", "engineer"),
    _base(3, "alarm_ack", "acknowledge", "CDSE", "cdse,engineer", 2),
    _base(4, "alarm_close", "close", "engineer", "admin", 2),
    _base(5, "alarm_ack", "acknowledge", "engineer", "dam_admin", 2),
]


def _chain(signer):
    rows, prev = [], ac.GENESIS
    for i, b in enumerate(BASES, 1):
        rows.append(ac.finalizer(b, signer)(i, prev))
        prev = rows[-1]["record_hash"]
    return rows, {"chain_id": "CRD", "chain_seq": len(rows), "head_hash": prev}


def _rehash_from(rows, i, signer):
    """Privileged full recompute from index i (models an attacker with DB write access and the key)."""
    prev = rows[i - 1]["record_hash"] if i else ac.GENESIS
    for r in rows[i:]:
        r["prev_hash"] = prev
        r["record_hash"] = ac.record_hash(r)
        r["signature"] = signer.sign(r["record_hash"])
        prev = r["record_hash"]
    return {"chain_id": "CRD", "chain_seq": len(rows), "head_hash": prev}


def _case(name, mutate, rehash=None):
    signer = VecHmac()
    rows, head = _chain(signer)
    mutate(rows)
    if rehash is not None:
        head = _rehash_from(rows, rehash, signer)
    v = ac.verify_chain(copy.deepcopy(rows), copy.deepcopy(head), signer)
    return {"name": name, "rows": rows, "head": head,
            "expect": {"ok": v["ok"], "first_broken_seq": v["first_broken_seq"], "reason": v["reason"]}}


def _set(i, **kw):
    return lambda rows: rows[i].update(kw)


def _drop(i, *keys):
    return lambda rows: [rows[i].pop(k) for k in keys]


def build() -> dict:
    signer = VecHmac()
    rows, head = _chain(signer)
    cases = [
        _case("clean", lambda rows: None),
        _case("actor_role_edited", _set(2, actor_app_role="engineer")),
        _case("actor_role_stripped", _set(2, actor_app_role=None)),
        _case("canon_version_stripped", _set(3, canon_version=None)),
        _case("both_stripped_to_look_canon1", _drop(3, "actor_app_role", "canon_version")),
        _case("actor_role_missing_rehashed", _set(2, actor_app_role=None), rehash=2),
        _case("actor_role_blank_rehashed", _set(2, actor_app_role="  "), rehash=2),
        _case("actor_role_invalid_rehashed", _set(3, actor_app_role="superuser"), rehash=3),
        _case("actor_role_unsorted_rehashed", _set(2, actor_app_role="engineer,cdse"), rehash=2),
        _case("canon_downgrade_rehashed", _drop(4, "actor_app_role", "canon_version"), rehash=4),
        _case("canon_unknown_rehashed", _set(4, canon_version=3), rehash=4),
        _case("canon1_field_edited", _set(0, comment="tampered")),
    ]
    return {
        "description": "App-side canon-2 review chain vector (actor_app_role). Records 1-2 are canon 1 (no "
                       "canon_version: hash over CHAIN_FIELDS only, as in the DA fixtures); records 3-5 are canon 2: "
                       "hash over CHAIN_FIELDS + actor_app_role + canon_version (int). Signer: deterministic test HMAC "
                       "stand-in sha256('canon2-vec|' + record_hash), alg HS256-canon2-vector, key k-canon2-vector.",
        "canon_version": ac.CANON_VERSION,
        "canon_fields": {str(k): list(v) for k, v in ac.CANON_FIELDS.items()},
        "hash_excluded": list(ac.HASH_EXCLUDED),
        "actor_roles": list(ac.ACTOR_ROLES),
        "rows": rows, "head": head, "cases": cases,
    }


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    FIXTURE.write_text(dumps(build()))
    print(FIXTURE)
