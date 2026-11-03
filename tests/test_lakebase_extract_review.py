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


def _row(**kw):
    r = dict(VEC["review"], signed_ts="2026-03-02T12:00:00Z", signature_hash=VEC["signature_hash"])
    r.update(kw)
    return r


def test_review_signoff_is_not_in_the_app_extract_envelope():
    assert "review_signoff" not in [t for t, _, _ in lx.IMMUTABLE]
    src = open(os.path.join(ROOT, "src", "jobs", "lakebase_extract.py")).read()
    assert re.search(r"saveAsTable\(f\"\{a\.catalog\}\.audit\.review_record\"\)", src)


def test_rows_map_to_review_record_columns_and_signature_still_verifies():
    ddl = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    body = ddl.split("CREATE TABLE IF NOT EXISTS audit.review_record (")[1].split("CONSTRAINT pk_review")[0]
    cols = tuple(re.match(r"\s*(\w+)", l).group(1) for l in body.strip().splitlines())
    assert cols == lx.REVIEW_RECORD_COLS
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
