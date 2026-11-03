"""v1.5 QA-039: the review signature rules (SCHEMA_CONTRACT §5.5.1) and the shared interop vector.

tests/fixtures/review_signature_vector.json is SYNTHETIC (no restricted data). The engine
(dam_safety.inclinometer.corrections.review_signature) must reproduce it, and so must the app
(app/backend/signoff.signature, App Dev commit a9e7d56; imported read-only, skipped if the app is absent)."""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import pytest

from dam_safety.inclinometer import corrections as C

ROOT = os.path.join(os.path.dirname(__file__), "..")
VEC = json.load(open(os.path.join(ROOT, "tests", "fixtures", "review_signature_vector.json"), encoding="utf-8"))


def test_engine_reproduces_vector():
    assert C.review_signature(VEC["review"], VEC["subject"]) == VEC["signature_hash"]


def test_rules_spelled_out_reproduce_vector():
    """The published rule, independently of the engine code: sha256(utf-8(JSON({"signoff": {10 signed fields},
    "subject": {every ref.incl_correction column}}))) with sorted keys, separators (',', ':'), ensure_ascii=False."""
    signoff = {k: VEC["review"].get(k) for k in C.REVIEW_SIGNED_FIELDS}
    subject = {k: VEC["subject"].get(k) for k in C.AUDIT_FIELDS}
    payload = json.dumps({"signoff": signoff, "subject": subject}, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    assert hashlib.sha256(payload.encode("utf-8")).hexdigest() == VEC["signature_hash"]
    assert len(C.AUDIT_FIELDS) == 27 and C.AUDIT_FIELDS[-4:] == ("reviewed_by", "reviewed_utc", "supersedes", "review_id")


def test_timestamp_forms_and_nan_canonicalise():
    s = dict(VEC["subject"], timestamp_utc=datetime(2026, 3, 1, tzinfo=timezone.utc), before_value=float("nan"))
    r = dict(VEC["review"], signed_ts="2026-03-02T12:00:00Z")
    assert C.review_signature(r, s) == VEC["signature_hash"]
    assert C.review_signature(dict(r, comment="x"), s) != VEC["signature_hash"]


def test_app_signoff_reproduces_vector():
    if not os.path.isfile(os.path.join(ROOT, "app", "backend", "signoff.py")):
        pytest.skip("app/backend/signoff.py not present")
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.join(ROOT, "app"))
    try:
        S = pytest.importorskip("backend.signoff")
    finally:
        sys.path.pop(0)
    assert set(S.SUBJECT_FIELDS) == set(C.AUDIT_FIELDS) and tuple(S.SIGNED_FIELDS) == C.REVIEW_SIGNED_FIELDS
    assert S.signature(VEC["review"], VEC["subject"]) == VEC["signature_hash"]
