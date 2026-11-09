"""Outbox contract fix (App Dev blocker, 2026-11-09): dam_app.dssmp_registration_outbox has exactly the columns of app
SCHEMA_CONTRACT §7 item 19 (= the app's state.py DDL and the INSERT in app/backend/routers/dssmp_registration.py);
lakebase/migrations/v1_7_004 renames an older DA-shaped table in place; the rollback keeps all data. Live parts run on
PostgreSQL when DAM_TEST_PG is set, including the app's own StateStore INSERT as the app role. Synthetic data only."""
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import dssmp_registration as dr  # noqa: E402
import load_dssmp_registrations as job  # noqa: E402

DDL_PATH = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
MIG = os.path.join(ROOT, "lakebase", "migrations", "v1_7_004_dssmp_outbox_contract.sql")
DOWN = os.path.join(ROOT, "lakebase", "migrations", "v1_7_004_dssmp_outbox_contract_down.sql")
STATE = os.path.join(ROOT, "app", "backend", "repos", "state.py")
ROUTER = os.path.join(ROOT, "app", "backend", "routers", "dssmp_registration.py")


def _cols(create_sql):
    body = create_sql[create_sql.index("(") + 1:]
    return tuple(m.group(1) for m in re.finditer(r"(?:^|,)\s*(\w+) (?:TEXT|INTEGER)", body))


def test_columns_match_the_app_ddl_the_app_insert_and_001():
    st = open(STATE).read()
    app_ddl = re.search(r"CREATE TABLE IF NOT EXISTS \{s\}dssmp_registration_outbox \((.*?)\)\"\"\"", st, re.S).group(1)
    assert _cols("(" + app_ddl) == dr.OUTBOX_COLS
    lb = open(DDL_PATH).read()
    lb_ddl = re.search(r"CREATE TABLE IF NOT EXISTS dssmp_registration_outbox \((.*?)\);", lb, re.S).group(1)
    assert _cols("(" + lb_ddl) == dr.OUTBOX_COLS
    router = open(ROUTER).read()
    ins = router[router.index("st.insert_dssmp_outbox(q, {"):]
    ins = ins[:ins.index("})")]
    assert tuple(re.findall(r'"(\w+)":', ins)) == dr.OUTBOX_COLS
    assert "status IN ('pending', 'loaded', 'rejected', 'rejected_invalid')" in lb
    assert re.search(r"status TEXT NOT NULL CHECK \(status IN \('pending', 'loaded', 'rejected', 'rejected_invalid'\)\)",
                     st)
    assert "document_sha256 TEXT NOT NULL" in lb_ddl


def test_001_inlines_exactly_the_migration_block_and_the_job_writes_contract_columns():
    mig = open(MIG).read()
    blk = mig[mig.index("-- BEGIN v1_7_004 block"):mig.index("-- END v1_7_004 block")]
    assert blk in open(DDL_PATH).read()
    for bad in ("requested_by", "requested_at", "applied_at", "supersedes_dssmp_version_id"):
        assert bad not in job.OUTBOX_SQL and bad not in job.UPDATE_SQL
    down = open(DOWN).read()
    code = "\n".join(l for l in down.splitlines() if not l.lstrip().startswith("--"))
    assert "DROP COLUMN" not in code and "DROP TABLE" not in code and "DELETE" not in code
    assert "RENAME COLUMN proposed_by TO requested_by" in code and "RENAME COLUMN processed_at TO applied_at" in code


# ---------------------------------------------------------------- live
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")
OLD_SHAPE = ("CREATE TABLE dam_app.dssmp_registration_outbox (outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, "
             "dssmp_version_id TEXT NOT NULL, version_no INTEGER NOT NULL, effective_from TEXT NOT NULL, "
             "supersedes_dssmp_version_id TEXT, change_reason TEXT, source_ref TEXT, document_uri TEXT, "
             "document_sha256 TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL, "
             "approval_record_hash TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL, "
             "status TEXT NOT NULL DEFAULT 'pending', applied_at TEXT, message TEXT, CONSTRAINT ck_dssmp_outbox_status "
             "CHECK (status IN ('pending', 'applied', 'rejected_stale', 'rejected_invalid')))")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


def _cols_live(conn):
    r = _psql(conn, "-c", "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns "
                          "WHERE table_schema = 'dam_app' AND table_name = 'dssmp_registration_outbox'")
    return tuple(r.stdout.strip().split(","))


@pytest.fixture()
def env():
    tag = uuid.uuid4().hex[:8]
    db, app, pub = f"lbobx_{tag}", f"lbobx_app_{tag}", f"lbobx_pub_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN",
                 "-c", f"CREATE ROLE {pub} LOGIN").returncode == 0
    conn = f"{PG} dbname={db}"
    yield {"conn": conn, "app": app, "pub": pub,
           "opts": {"PGOPTIONS": f"-c dam.app_role={app} -c dam.publisher_login={pub}"}}
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}", "-c", f"DROP ROLE IF EXISTS {pub}")


@live
def test_old_da_shape_is_renamed_in_place_by_001_and_the_app_insert_works(env):
    assert _psql(env["conn"], "-c", "CREATE SCHEMA dam_app", "-c", OLD_SHAPE).returncode == 0
    r = _psql(env["conn"], "-f", DDL_PATH, env_extra=env["opts"])
    assert r.returncode == 0, r.stderr
    assert set(_cols_live(env["conn"])) == set(dr.OUTBOX_COLS)
    assert _psql(env["conn"], "-f", DDL_PATH, env_extra=env["opts"]).returncode == 0      # idempotent
    # the app's own StateStore, connected as the app role, inserts exactly the router's row
    code = f"""
import sys; sys.path.insert(0, {os.path.join(ROOT, 'app')!r})
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from backend.repos.state import StateStore
pool = ConnectionPool({env['conn'] + ' user=' + env['app']!r}, kwargs={{"row_factory": dict_row}}, open=True)
st = StateStore("postgres", schema="dam_app", pg_pool=pool)
row = {{"outbox_id": "DSO-1", "dam_id": "D1", "dssmp_version_id": "DS-2", "version_no": 2,
        "effective_from": "2026-12-01T00:00:00Z", "change_reason": "annual", "source_ref": "DSSMR-1",
        "document_uri": "lakebase:dam_app.dssmp_document/DOC-1", "document_sha256": "ab" * 32,
        "proposed_by": "damadmin@x.org", "approved_by": "cdse@x.org", "approved_at": "2026-11-09T15:00:00Z",
        "approval_record_hash": "c1" * 32, "status": "pending", "reason_code": None, "message": None,
        "processed_at": None}}
with st.transaction() as q:
    st.insert_dssmp_outbox(q, row)
print(len(st.dssmp_outbox(outbox_id="DSO-1")))
pool.close()
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if "No module named" in r.stderr:
        pytest.skip(r.stderr.strip().splitlines()[-1])
    assert r.returncode == 0 and r.stdout.split() == ["1"], r.stderr[-2000:]
    # the load job's UPDATE as the publisher login: outcome columns only
    upd = job.UPDATE_SQL.replace("%s", "'{}'").format("rejected", "dssmp_draft", "dssmp_draft: x", "now", "DSO-1")
    r = _psql(f"{env['conn']} user={env['pub']}", "-c", "SET search_path TO dam_app", "-c", upd)
    assert r.returncode == 0, r.stderr
    r = _psql(f"{env['conn']} user={env['pub']}", "-c",
              "UPDATE dam_app.dssmp_registration_outbox SET proposed_by = 'x'")
    assert r.returncode != 0 and "permission denied" in r.stderr
    r = _psql(f"{env['conn']} user={env['app']}", "-c",
              "UPDATE dam_app.dssmp_registration_outbox SET status = 'loaded'")
    assert r.returncode != 0 and "permission denied" in r.stderr
    out = _psql(env["conn"], "-c", "SELECT status || ':' || reason_code FROM dam_app.dssmp_registration_outbox").stdout
    assert out.split() == ["rejected:dssmp_draft"]


@live
def test_old_rows_with_values_are_never_dropped_and_rollback_keeps_data(env):
    assert _psql(env["conn"], "-c", "CREATE SCHEMA dam_app", "-c", OLD_SHAPE, "-c",
                 "INSERT INTO dam_app.dssmp_registration_outbox (outbox_id, dam_id, dssmp_version_id, version_no, "
                 "effective_from, document_sha256, approved_by, approved_at, approval_record_hash, requested_by, "
                 "requested_at, status) VALUES ('OB-1','D1','DS-1',1,'t','h','c','t','h','r','t','applied')"
                 ).returncode == 0
    r = _psql(env["conn"], "-1", "-f", MIG)
    assert r.returncode != 0 and "requested_at holds values" in r.stderr
    assert "requested_by" in _cols_live(env["conn"])                       # all-or-nothing: nothing renamed
    # clear the old-only values by hand (the documented decision), then the migration and its rollback round-trip
    assert _psql(env["conn"], "-c", "ALTER TABLE dam_app.dssmp_registration_outbox ALTER COLUMN requested_at "
                                    "DROP NOT NULL", "-c", "UPDATE dam_app.dssmp_registration_outbox SET "
                                    "requested_at = NULL").returncode == 0
    assert _psql(env["conn"], "-1", "-f", MIG).returncode == 0
    assert set(_cols_live(env["conn"])) == set(dr.OUTBOX_COLS)
    st = _psql(env["conn"], "-c", "SELECT status || ':' || proposed_by FROM dam_app.dssmp_registration_outbox").stdout
    assert st.split() == ["loaded:r"]
    assert _psql(env["conn"], "-c", "UPDATE dam_app.dssmp_registration_outbox SET reason_code = 'kept'").returncode == 0
    for _ in range(2):
        r = _psql(env["conn"], "-1", "-f", DOWN)
        assert r.returncode == 0, r.stderr
    out = _psql(env["conn"], "-c", "SELECT status || ':' || requested_by || ':' || requested_at || ':' || reason_code "
                                   "FROM dam_app.dssmp_registration_outbox").stdout
    assert out.split() == ["applied:r:t:kept"]                             # nothing lost, reason_code kept
