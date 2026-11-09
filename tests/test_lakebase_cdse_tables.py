"""v1.7 app SCHEMA_CONTRACT §7 items 20 / 21 in lakebase/001 (single schema source): dam_cdse_change,
dam_cdse_outbox, rule_reversion_proposal with the app's DDL verbatim; the outbox owner-owned with the publisher's
column-level UPDATE; Lakebase migration v1_7_005 + rollback (nothing dropped). Live parts on PostgreSQL (DAM_TEST_PG):
fresh 001, the upgrade path from the previous 001 (git dfc08bb) + migration, rollback and re-apply with rows kept,
and the app's own StateStore writing the three tables as the app role. Synthetic data only."""
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL_PATH = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
VALIDATOR = os.path.join(ROOT, "lakebase", "validate_append_only.sql")
MIG = os.path.join(ROOT, "lakebase", "migrations", "v1_7_005_cdse_reversion_tables.sql")
DOWN = os.path.join(ROOT, "lakebase", "migrations", "v1_7_005_cdse_reversion_tables_down.sql")
STATE = os.path.join(ROOT, "app", "backend", "repos", "state.py")
TABLES = ("dam_cdse_change", "dam_cdse_outbox", "rule_reversion_proposal")


def _block(path):
    s = open(path).read()
    return s[s.index("-- BEGIN v1_7_005 block"):s.index("-- END v1_7_005 block")]


def test_001_carries_the_app_ddl_verbatim_and_the_migration_the_same_block():
    st = open(STATE).read()
    blk = _block(DDL_PATH)
    for t in TABLES:
        app = re.search(r'"""(CREATE TABLE IF NOT EXISTS \{s\}' + t + r' \(.*?\))"""', st, re.S).group(1)
        assert app.replace("{s}", "") + ";" in blk, t
    assert _block(MIG) == blk
    lb = open(DDL_PATH).read()
    assert "ALTER TABLE dam_cdse_outbox OWNER TO dam_lakebase_owner;" in lb
    code = "\n".join(l for l in open(DOWN).read().splitlines() if not l.lstrip().startswith("--"))
    assert not re.search(r"\bDROP (TABLE|COLUMN)\b|\bDELETE\b|\bTRUNCATE\b", code)


PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture()
def env(tmp_path):
    tag = uuid.uuid4().hex[:8]
    db, app, pub = f"lbcd_{tag}", f"lbcd_app_{tag}", f"lbcd_pub_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN",
                 "-c", f"CREATE ROLE {pub} LOGIN").returncode == 0
    old = tmp_path / "001_dfc08bb.sql"
    old.write_text(subprocess.run(["git", "-C", ROOT, "show", "dfc08bb:lakebase/001_app_schema.sql"],
                                  capture_output=True, text=True, check=True).stdout)
    yield {"conn": f"{PG} dbname={db}", "app": app, "pub": pub, "old": str(old),
           "opts": {"PGOPTIONS": f"-c dam.app_role={app} -c dam.publisher_login={pub}"}}
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}", "-c", f"DROP ROLE IF EXISTS {pub}")


def _valid(e):
    r = _psql(e["conn"], "-f", VALIDATOR, env_extra=e["opts"])
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _app_writes(e):
    code = f"""
import sys; sys.path.insert(0, {os.path.join(ROOT, 'app')!r})
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from backend.repos.state import StateStore
pool = ConnectionPool({e['conn'] + ' user=' + e['app']!r}, kwargs={{"row_factory": dict_row}}, open=True)
st = StateStore("postgres", schema="dam_app", pg_pool=pool)
n = {uuid.uuid4().hex[:6]!r}
with st.transaction() as q:
    st.insert_row(q, "dam_cdse_change", {{"change_id": "DCC-" + n, "dam_id": "NEW", "kind": "assign",
        "previous_cdse_id": None, "new_cdse_id": "c@x.org", "change_reason_code": "initial_appointment",
        "rationale": "r", "proposed_by": "a@x.org", "proposed_by_id": None, "proposed_at": "2026-11-09T17:00:00Z",
        "status": "approved", "decided_by": "a@x.org", "decided_by_id": None, "decided_at": "2026-11-09T17:00:00Z",
        "decision_rationale": "r", "outbox_id": "DCO-" + n, "approval_record_hash": "ab" * 32}})
    st.insert_row(q, "dam_cdse_outbox", {{"outbox_id": "DCO-" + n, "dam_id": "NEW", "kind": "assign",
        "previous_cdse_id": None, "new_cdse_id": "c@x.org", "change_reason_code": "initial_appointment",
        "proposed_by": "a@x.org", "approved_by": "a@x.org", "approved_at": "2026-11-09T17:00:00Z",
        "approval_record_hash": "ab" * 32, "status": "pending", "reason_code": None, "message": None,
        "processed_at": None}})
    st.insert_row(q, "rule_reversion_proposal", {{"proposal_id": "RRV-" + n, "dam_id": "NEW",
        "to_dssmp_version_id": "DS-2", "rules": "[]", "proposed_by": "a@x.org", "proposed_by_id": None,
        "proposed_at": "2026-11-09T17:00:00Z", "rationale": "r", "status": "pending", "decided_by": None,
        "decided_by_id": None, "decided_at": None, "decision_rationale": None, "pub_ids": None}})
pool.close()
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if "No module named" in r.stderr:
        pytest.skip(r.stderr.strip().splitlines()[-1])
    assert r.returncode == 0, r.stderr[-2000:]


def _counts(e):
    return [_psql(e["conn"], "-c", f"SELECT count(*) FROM dam_app.{t}").stdout.strip() for t in TABLES]


def _pub_update(e):
    return _psql(f"{e['conn']} user={e['pub']}", "-c", "UPDATE dam_app.dam_cdse_outbox SET status = 'loaded', "
                 "reason_code = NULL, message = 'm', processed_at = 'now' WHERE status = 'pending'")


@live
def test_fresh_001_validates_and_the_app_writes_all_three_tables(env):
    r = _psql(env["conn"], "-f", DDL_PATH, env_extra=env["opts"])
    assert r.returncode == 0, r.stderr
    assert _valid(env) == ""
    _app_writes(env)
    assert _counts(env) == ["1", "1", "1"]
    assert _pub_update(env).returncode == 0
    for stmt in ("UPDATE dam_app.dam_cdse_outbox SET new_cdse_id = 'evil@x.org'",
                 "DELETE FROM dam_app.dam_cdse_outbox", "SELECT * FROM dam_app.dam_cdse_change"):
        r = _psql(f"{env['conn']} user={env['pub']}", "-c", stmt)
        assert r.returncode != 0 and "permission denied" in r.stderr, stmt
    r = _psql(f"{env['conn']} user={env['app']}", "-c", "UPDATE dam_app.dam_cdse_outbox SET status = 'loaded'")
    assert r.returncode != 0 and "permission denied" in r.stderr


@live
def test_upgrade_by_migration_then_rollback_keeps_every_row_and_reapply_restores(env):
    r = _psql(env["conn"], "-f", env["old"], env_extra=env["opts"])
    assert r.returncode == 0, r.stderr
    for m in (MIG, MIG):                                                    # idempotent
        r = _psql(env["conn"], "-f", m, env_extra=env["opts"])
        assert r.returncode == 0, r.stderr
    assert _valid(env) == ""
    _app_writes(env)
    for _ in range(2):
        r = _psql(env["conn"], "-f", DOWN, env_extra=env["opts"])
        assert r.returncode == 0, r.stderr
    assert _counts(env) == ["1", "1", "1"]                                  # nothing dropped
    assert _pub_update(env).returncode != 0                                 # publisher grants reverted
    assert _psql(env["conn"], "-f", MIG, env_extra=env["opts"]).returncode == 0
    assert _valid(env) == "" and _pub_update(env).returncode == 0 and _counts(env) == ["1", "1", "1"]
    # re-running the full current 001 on top converges to the same state
    assert _psql(env["conn"], "-f", DDL_PATH, env_extra=env["opts"]).returncode == 0 and _valid(env) == ""
