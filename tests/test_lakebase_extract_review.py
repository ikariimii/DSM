"""v1.5 (QA-039, App Dev follow-up 1): Lakebase review_signoff -> audit.review_record (not audit.app_extract).
Row shape from lakebase/001_app_schema.sql review_signoff (signed_ts ISO TEXT, 'Z', second precision)."""
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
sys.path.insert(0, os.path.join(ROOT, "src"))
import lakebase_extract as lx  # noqa: E402
from dam_safety.inclinometer import corrections as C  # noqa: E402

VEC = json.load(open(os.path.join(ROOT, "tests", "fixtures", "review_signature_vector.json"), encoding="utf-8"))


CHAIN = dict(action_type="data_correction_review", reviewer_email=None, reviewer_sub=None,
             original_value='{"bias_A":0.0}', corrected_value='{"bias_A":0.1}', chain_id=None, chain_seq=1,
             prev_hash="0" * 64, record_hash="f" * 64, signature=None, sig_alg=None, key_id=None)


def _row(**kw):
    r = dict(VEC["review"], signed_ts="2026-03-02T12:00:00Z", signature_hash=VEC["signature_hash"])
    r.update(CHAIN, chain_id=r["dam_id"])
    r.update(kw)
    return r


def test_review_signoff_is_not_in_the_app_extract_envelope():
    assert "review_signoff" not in [t for t, _, _ in lx.IMMUTABLE]
    src = open(os.path.join(ROOT, "src", "jobs", "lakebase_extract.py")).read()
    assert re.search(r"saveAsTable\(f\"\{a\.catalog\}\.audit\.review_record\"\)", src)


def test_rows_map_to_review_record_columns_and_signature_still_verifies():
    ddl = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    body = ddl.split("CREATE TABLE IF NOT EXISTS audit.review_record (")[1].split("CONSTRAINT pk_review")[0]
    cols = tuple(re.match(r"\s*(\w+)", l).group(1) for l in body.strip().splitlines() if not l.strip().startswith("--"))
    assert cols == lx.REVIEW_RECORD_COLS and len(cols) == 24                               # v1.6: 12 -> 24
    assert lx.REVIEW_RECORD_COLS[12:] == lx.CHAIN_COLS
    [rec] = lx.review_rows_to_records([_row()])
    assert rec["source"] == lx.REVIEW_SOURCE and rec["signed_ts"].tzinfo is not None
    assert C.review_signature(rec, VEC["subject"]) == rec["signature_hash"]        # TIMESTAMP vs text: same hash


def test_dedup_on_review_id_and_missing_fields_raise():
    a, b = _row(), _row(review_id="RS-SYN-2")
    assert [r["review_id"] for r in lx.review_rows_to_records([a, a, b], known_ids={"RS-SYN-2"})] == ["RS-SYN-1"]
    with pytest.raises(ValueError):
        lx.review_rows_to_records([_row(signature_hash="")])


def test_review_record_check_allows_incl_correction_approve_reject_only():
    import sqlite3
    ddl = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    expr = re.search(r"ADD CONSTRAINT ck_review_record_subject_decision CHECK \((.*?)\);\n", ddl, re.S).group(1)
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE t (subject_type TEXT, decision TEXT, comment TEXT, CHECK ({expr}))")
    for ok in [("incl_correction", "approve", "why"), ("incl_correction", "reject", "why"), ("dssmr", "APPROVED", None)]:
        db.execute("INSERT INTO t VALUES (?,?,?)", ok)
    for bad in [("incl_correction", "APPROVE", "why"), ("incl_correction", "approve", " "),
                ("incl_correction", "approve", None), ("other", "approve", "x")]:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO t VALUES (?,?,?)", bad)


# ---- v1.6 (QA-039 v2, app SCHEMA_CONTRACT §7 10b) ----
REQUIRED_CHAIN = ("action_type", "original_value", "corrected_value", "chain_id", "chain_seq", "prev_hash", "record_hash")
NULLABLE_CHAIN = ("reviewer_email", "reviewer_sub", "signature", "sig_alg", "key_id")


def test_v16_required_chain_fields_raise_when_missing():
    assert set(REQUIRED_CHAIN) <= set(lx.REVIEW_REQUIRED)
    assert not set(NULLABLE_CHAIN) & set(lx.REVIEW_REQUIRED)
    for c in REQUIRED_CHAIN:
        with pytest.raises(ValueError, match=c):
            lx.review_rows_to_records([_row(**{c: None})])


def test_v16_nullable_chain_fields_pass_through():
    [rec] = lx.review_rows_to_records([_row(reviewer_email="a@b.org", signature=None, sig_alg=None, key_id=None)])
    assert rec["reviewer_email"] == "a@b.org" and rec["signature"] is None and rec["chain_seq"] == 1


def test_v16_output_and_query_in_chain_order():
    rows = [_row(review_id="R3", chain_id="B", chain_seq=1, dam_id="B"), _row(review_id="R2", chain_seq="2"),
            _row(review_id="R1", chain_seq=1)]
    out = lx.review_rows_to_records(rows)
    assert [(r["chain_id"], r["chain_seq"]) for r in out] == sorted([(rows[1]["chain_id"], 1), (rows[1]["chain_id"], 2), ("B", 1)])
    src = open(os.path.join(ROOT, "src", "jobs", "lakebase_extract.py")).read()
    assert lx.REVIEW_ORDER_SQL == "ORDER BY chain_id, chain_seq"
    assert '"SELECT * FROM review_signoff WHERE signed_ts >= %s " + REVIEW_ORDER_SQL' in src


def test_v16_alarm_event_rows_extract_without_approval_rule():
    a = _row(review_id="AE-1", subject_type="alarm_event", subject_id="ep-1", decision="acknowledge",
             action_type="alarm_ack", comment="ack")
    [rec] = lx.review_rows_to_records([a])
    assert rec["subject_type"] == "alarm_event"
    src = open(os.path.join(ROOT, "src", "jobs", "lakebase_extract.py")).read()
    assert "approval_problem(" not in src


def _check(name):
    ddl = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    return re.search(rf"ADD CONSTRAINT {name} CHECK \((.*?)\);\n", ddl, re.S).group(1)


def test_v16_subject_decision_check_adds_alarm_event_acknowledge_close():
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE t (subject_type TEXT, decision TEXT, comment TEXT, "
               f"CHECK ({_check('ck_review_record_subject_decision')}))")
    for ok in [("alarm_event", "acknowledge", "why"), ("alarm_event", "close", "why"),
               ("incl_correction", "approve", "why"), ("incl_correction", "reject", "why")]:
        db.execute("INSERT INTO t VALUES (?,?,?)", ok)
    for bad in [("alarm_event", "approve", "why"), ("alarm_event", "close", " "), ("alarm_event", "ACKNOWLEDGE", "x"),
                ("incl_correction", "close", "why"), ("alarm_events", "close", "x")]:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO t VALUES (?,?,?)", bad)


def test_v16_chain_check():
    import sqlite3
    cols = ("dam_id", "subject_type", "decision") + REQUIRED_CHAIN
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE t ({', '.join(cols)}, CHECK ({_check('ck_review_record_chain')}))")
    good = dict(dam_id="D", subject_type="alarm_event", decision="close", action_type="alarm_close",
                original_value="{}", corrected_value="{}", chain_id="D", chain_seq=1, prev_hash="0" * 64,
                record_hash="f" * 64)
    ins = lambda **kw: db.execute(f"INSERT INTO t VALUES ({','.join('?' * len(cols))})",
                                  tuple(dict(good, **kw)[c] for c in cols))
    ins()
    ins(subject_type="incl_correction", decision="approve", action_type="bias_review")
    ins(subject_type="dssmr", decision="APPROVED", **{c: None for c in REQUIRED_CHAIN})   # non-chained legacy row
    for bad in [dict(action_type=None), dict(chain_seq=0), dict(chain_id="E"), dict(record_hash=None),
                dict(action_type="alarm_ack"), dict(action_type="bias_review"), dict(action_type="other"),
                dict(subject_type="incl_correction", decision="approve", action_type="alarm_close")]:
        with pytest.raises(sqlite3.IntegrityError):
            ins(**bad)
