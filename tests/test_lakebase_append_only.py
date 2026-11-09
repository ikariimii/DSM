"""QA-045b: the Lakebase append-only guard (lakebase/001_app_schema.sql) refuses TRUNCATE as well as UPDATE/DELETE.

Row-level BEFORE UPDATE OR DELETE triggers do not fire on TRUNCATE; PostgreSQL only allows TRUNCATE triggers
FOR EACH STATEMENT (PostgreSQL 17 docs, CREATE TRIGGER). The static tests always run. The live tests run the DDL
against a REAL PostgreSQL through psql and are skipped unless DAM_TEST_PG is set, e.g.
    DAM_TEST_PG="host=/tmp port=55432 user=postgres dbname=postgres" pytest tests/test_lakebase_append_only.py
They use a throwaway database and synthetic rows only. Lakebase itself (version, role model) is UNVERIFIED here.
"""
import os
import re
import shutil
import subprocess
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL_PATH = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
DDL = open(DDL_PATH).read()
APPEND_ONLY = ("exceedance_status", "review_signoff", "manual_reading")


def _code(sql):
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_every_append_only_table_has_row_and_truncate_guards(table):
    code = _code(DDL)
    assert re.search(rf"DROP TRIGGER IF EXISTS t_noupd_{table} ON {table};", code)
    assert re.search(rf"BEFORE UPDATE OR DELETE ON {table}\s+FOR EACH ROW EXECUTE FUNCTION forbid_mutation\(\)", code)
    assert re.search(rf"DROP TRIGGER IF EXISTS t_notrunc_{table} ON {table};", code)
    assert re.search(rf"BEFORE TRUNCATE ON {table}\s+FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation\(\)", code)


def test_guard_function_is_idempotent_and_raises():
    code = _code(DDL)
    assert "CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger" in code
    assert re.search(r"RAISE EXCEPTION '% is append-only \(% refused\)', TG_TABLE_NAME, TG_OP", code)
    assert "REVOKE TRUNCATE ON exceedance_status, review_signoff, manual_reading FROM %I" in code


# ---------------------------------------------------------------- live PostgreSQL
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture(scope="module")
def db():
    name = f"qa045b_{uuid.uuid4().hex[:8]}"
    assert _psql(PG, "-c", f"CREATE DATABASE {name}").returncode == 0
    conn = f"{PG} dbname={name}"
    for _ in range(2):  # applying twice proves idempotence
        r = _psql(conn, "-f", DDL_PATH)
        assert r.returncode == 0, r.stderr
    yield conn
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    _psql(PG, "-c", "DROP ROLE IF EXISTS qa045b_app")


def _sql(conn, sql, **kw):
    return _psql(conn, "-c", "SET search_path TO dam_app", "-c", sql, **kw)


ROW = ("INSERT INTO review_signoff VALUES ('RS-{n}','D1','incl_correction','C-1','CDSE','approve','ok',"
       "'eng@example.org',NULL,'2026-11-09T15:00:00Z','abc')")


@live
def test_insert_works_and_update_delete_truncate_are_refused(db):
    assert _sql(db, ROW.format(n=1)).returncode == 0
    for stmt, op in [("TRUNCATE review_signoff", "TRUNCATE"), ("TRUNCATE review_signoff CASCADE", "TRUNCATE"),
                     ("UPDATE review_signoff SET decision = 'reject'", "UPDATE"),
                     ("DELETE FROM review_signoff", "DELETE")]:
        r = _sql(db, stmt)
        assert r.returncode != 0 and f"review_signoff is append-only ({op} refused)" in r.stderr, (stmt, r.stderr)
    for t in ("exceedance_status", "manual_reading"):
        r = _sql(db, f"TRUNCATE {t}")
        assert f"{t} is append-only (TRUNCATE refused)" in r.stderr
    out = _psql(db, "-At", "-c", "SELECT count(*) FROM dam_app.review_signoff")
    assert out.stdout.strip() == "1"


@live
def test_revoke_truncate_from_the_named_app_role(db):
    assert _psql(db, "-c", "DROP ROLE IF EXISTS qa045b_app", "-c", "CREATE ROLE qa045b_app",
                 "-c", "GRANT ALL ON ALL TABLES IN SCHEMA dam_app TO qa045b_app").returncode == 0
    r = _psql(db, "-f", DDL_PATH, env_extra={"PGOPTIONS": "-c dam.app_role=qa045b_app"})
    assert r.returncode == 0, r.stderr
    q = ("SELECT bool_or(has_table_privilege('qa045b_app', 'dam_app.' || t, 'TRUNCATE')), "
         "bool_and(has_table_privilege('qa045b_app', 'dam_app.' || t, 'INSERT')) "
         "FROM unnest(array['exceedance_status','review_signoff','manual_reading']) t")
    assert _psql(db, "-At", "-c", q).stdout.strip() == "f|t"
    # an unknown role name is a no-op, not an error
    r = _psql(db, "-f", DDL_PATH, env_extra={"PGOPTIONS": "-c dam.app_role=no_such_role"})
    assert r.returncode == 0, r.stderr
