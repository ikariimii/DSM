"""v1.8 UC schema (sql/ddl/14_dam_lifecycle.sql = sql/migrations/v1_8_001_dam_lifecycle.sql): identical table DDL,
CHECKs and seeds; the 15 citation codes / 6 suspension reasons as data with a per-row verification status; no
go_live_at column (derived view); ref.dam mirror columns; review_record accepts the dam_lifecycle actions; the CHECK
bodies executed on PostgreSQL (DAM_TEST_PG) against good and bad rows. Static + PG only: UNVERIFIED on Databricks."""
import os
import re

import pytest

from dam_safety import lifecycle as L
from lifecycle_fixtures import seed_citations, seed_reasons

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL = open(os.path.join(ROOT, "sql", "ddl", "14_dam_lifecycle.sql")).read()
MIG = open(os.path.join(ROOT, "sql", "migrations", "v1_8_001_dam_lifecycle.sql")).read()
TABLES = ("ref.lifecycle_suspension_reason", "ref.lifecycle_citation", "ref.dam_lifecycle_state",
          "audit.lifecycle_event")
CKS = ("ck_lifecycle_citation", "ck_dam_lifecycle_state", "ck_lifecycle_event")


def _create(s, t):
    m = re.search(r"CREATE TABLE IF NOT EXISTS " + re.escape(t) + r" \(.*?\n\)[^;]*;", s, re.S)
    assert m, t
    return m.group(0)


def _ck(s, name):
    m = re.search(r"ADD CONSTRAINT " + name + r" CHECK \((.*?)\);\n", s, re.S)
    assert m, name
    return m.group(1)


def _seed(s, t):
    i = s.index(f"INSERT INTO {t} ")
    return s[i:s.index(";\n", s.index("WHERE NOT EXISTS", i))]


def test_tables_checks_and_seeds_are_identical_in_ddl_and_migration():
    for t in TABLES:
        assert _create(DDL, t) == _create(MIG, t), t
    for c in CKS:
        assert _ck(DDL, c) == _ck(MIG, c), c
    for t in ("ref.lifecycle_suspension_reason", "ref.lifecycle_citation"):
        assert _seed(DDL, t) == _seed(MIG, t)
        assert "WHERE NOT EXISTS" in _seed(DDL, t)                  # data changes are never overwritten
    assert "delta.appendOnly = true" in _create(DDL, "audit.lifecycle_event")
    for st in L.STATES:
        for c in CKS:
            assert f"'{st}'" in _ck(DDL, c)


def test_event_table_carries_every_hashed_column():
    body = _create(DDL, "audit.lifecycle_event")
    cols = re.findall(r"^  (\w+) ", body, re.M)
    assert set(L.EVENT_COLS) <= set(cols) and "go_live_at" not in cols
    assert sorted(c for c in cols if c != "CONSTRAINT") == sorted(L.EVENT_COLS)


def test_enum_rows():
    assert len(seed_citations()) == 15 and len(seed_reasons()) == 6
    assert all(r["active"] for r in seed_citations())
    assert sorted({r["verification_status"] for r in seed_citations()}) == ["UNVERIFIED-pending", "VERIFIED"]


def test_no_go_live_at_column_and_the_mirror_columns():
    for f in os.listdir(os.path.join(ROOT, "sql", "ddl")):
        code = "\n".join(l.split("--")[0] for l in open(os.path.join(ROOT, "sql", "ddl", f)))
        assert not re.search(r"^\s*go_live_at\s+TIMESTAMP", code, re.M), f
    assert "AS go_live_at FROM ref.dam_lifecycle_state" in DDL and "CREATE OR REPLACE VIEW ref.v_dam_go_live" in DDL
    assert "CREATE OR REPLACE VIEW ref.v_dam_lifecycle_current" in DDL
    ref = open(os.path.join(ROOT, "sql", "ddl", "01_ref_hierarchy.sql")).read()
    assert re.search(r"^\s+lifecycle_state\s+STRING", ref, re.M)
    assert re.search(r"^\s+lifecycle_effective_from TIMESTAMP", ref, re.M)
    assert "ALTER TABLE ref.dam ADD COLUMNS (lifecycle_state STRING)" in MIG
    assert "ALTER TABLE ref.dam ADD COLUMNS (lifecycle_effective_from TIMESTAMP)" in MIG


def test_review_record_accepts_the_lifecycle_chain_actions():
    a = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    for c in ("ck_review_record_subject_decision", "ck_review_record_chain"):
        assert _ck(a, c) == _ck(MIG, c), c
    for act in ("lifecycle_transition_propose", "lifecycle_transition_approve", "lifecycle_transition_reject"):
        assert act in _ck(a, "ck_review_record_subject_decision") + _ck(a, "ck_review_record_chain")
    assert "'dam_lifecycle'" in _ck(a, "ck_review_record_subject_decision")


# ---------------------------------------------------------------- CHECK bodies on PostgreSQL
PG = os.environ.get("DAM_TEST_PG")


def _pg(s):
    for a, b in ((r"ARRAY<STRING>", "TEXT[]"), (r"\bSTRING\b", "TEXT"), (r"\bTIMESTAMP\b", "TIMESTAMPTZ"),
                 (r"\bsize\(", "cardinality("), (r"\bRLIKE\b", "~"), (r"COMMENT '(?:[^']|'')*'", ""),
                 (r"TBLPROPERTIES \([^)]*\)", ""), (r"CONSTRAINT pk_\w+ PRIMARY KEY \((\w+)\)", r"PRIMARY KEY (\1)")):
        s = re.sub(a, b, s)
    return s


GOOD_EVENT = {"event_id": "'e1'", "dam_id": "'D1'", "from_state": "'live'", "to_state": "'pending_transfer'",
              "trigger_code": "'t'", "authorizer_role": "'cdse'", "evidence_uri": "ARRAY['uri:1']",
              "effective_ts": "now()", "created_by": "'job'", "created_ts": "now()", "citation_code": "'c'",
              "prev_event_hash": "repeat('0', 64)", "event_hash": "repeat('a', 64)"}


@pytest.mark.skipif(not PG, reason="set DAM_TEST_PG")
def test_check_bodies_accept_good_rows_and_refuse_bad_ones():
    import uuid

    import psycopg
    db = "lcuc_" + uuid.uuid4().hex[:8]
    with psycopg.connect(PG, autocommit=True) as root:
        root.execute(f"CREATE DATABASE {db}")
    try:
        _checks(psycopg, f"{PG} dbname={db}")
    finally:
        with psycopg.connect(PG, autocommit=True) as root:
            root.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _checks(psycopg, dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE SCHEMA ref; CREATE SCHEMA audit")
        if True:
            for t in TABLES:
                c.execute(_pg(_create(DDL, t)).replace("IF NOT EXISTS ", ""))
            for t, ck in (("ref.lifecycle_citation", CKS[0]), ("ref.dam_lifecycle_state", CKS[1]),
                          ("audit.lifecycle_event", CKS[2])):
                c.execute(f"ALTER TABLE {t} ADD CONSTRAINT {ck} CHECK ({_pg(_ck(DDL, ck))})")
            for t in ("ref.lifecycle_suspension_reason", "ref.lifecycle_citation"):
                seed = _pg(_seed(DDL, t)).replace("FROM VALUES", "FROM (VALUES").replace(") AS s(", ")) AS s(")
                seed = re.sub(r"split\(('[^']*'), ','\)", r"string_to_array(\1, ',')", seed)
                seed = seed.replace("CAST(NULL AS TEXT[])", "CAST(NULL AS TEXT[])")
                c.execute(seed)
                c.execute(seed)                                      # idempotent
            assert c.execute("SELECT count(*) FROM ref.lifecycle_citation").fetchone()[0] == 15
            assert c.execute("SELECT count(*) FROM ref.lifecycle_suspension_reason").fetchone()[0] == 6

            def ins(**over):
                row = dict(GOOD_EVENT, **over)
                c.execute(f"INSERT INTO audit.lifecycle_event ({', '.join(row)}) VALUES ({', '.join(row.values())})")

            ins()
            ins(event_id="'e2'", to_state="'suspended_monitoring_modified'", suspension_reason="'drawdown'")
            for over in ({"to_state": "'active'"}, {"evidence_uri": "ARRAY[]::TEXT[]"},
                         {"event_hash": "'ABC'"}, {"citation_code": "' '"},
                         {"to_state": "'suspended_monitoring_modified'"}, {"suspension_reason": "'drawdown'"}):
                with pytest.raises(psycopg.errors.CheckViolation):
                    ins(event_id="'bad'", **over)
            c.execute("INSERT INTO ref.dam_lifecycle_state VALUES ('e1', 'D1', 'live', now(), NULL, 'e1')")
            for bad in ("('e9', 'D1', 'live', now(), NULL, 'other')", "('e9', 'D1', 'gone', now(), NULL, 'e9')",
                        "('e9', 'D1', 'live', now(), now() - interval '1 day', 'e9')"):
                with pytest.raises(psycopg.errors.CheckViolation):
                    c.execute(f"INSERT INTO ref.dam_lifecycle_state VALUES {bad}")
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("UPDATE ref.lifecycle_citation SET verification_status = 'verified' "
                          "WHERE citation_code = 'surrender_filed_6_1'")
