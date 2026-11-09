"""v1.8 Lakebase dam_app.lifecycle_transition_outbox (lakebase/001 block v1_8_001 = lakebase/migrations/
v1_8_001_lifecycle_outbox.sql): owned by dam_lakebase_owner; the app has EXACTLY SELECT + INSERT (no UPDATE of any
column, no DELETE / TRUNCATE / TRIGGER / REFERENCES); dam_lakebase_publisher SELECT + UPDATE (status, reason_code,
message, processed_at); status pending -> loaded | rejected only (ENABLE ALWAYS); one pending row per dam; the INSERT
needs the dam lock (4702047, hashtext('dam_app.dam:' || dam_id)) held by the inserting transaction (lock order dam ->
check -> chain 4702046). Validator coverage (owner_not_dam_lakebase_owner, outbox_owner, app_outbox_privilege,
status_guard_missing, lifecycle_lock_guard_missing). Live parts on PostgreSQL (DAM_TEST_PG). Synthetic data only."""
import os
import re
import shutil
import subprocess
import uuid

import pytest

from gitrev import git_show_or_skip  # noqa: E402
from lbmig import later_than  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
VAL = os.path.join(ROOT, "lakebase", "validate_append_only.sql")
MIG = os.path.join(ROOT, "lakebase", "migrations", "v1_8_001_lifecycle_outbox.sql")
DOWN = os.path.join(ROOT, "lakebase", "migrations", "v1_8_001_lifecycle_outbox_down.sql")
T = "dam_app.lifecycle_transition_outbox"
APP_GRANT = "GRANT SELECT, INSERT ON dam_app.lifecycle_transition_outbox TO %I"
PUB_COLS = ("status", "reason_code", "message", "processed_at")
COLS = ("outbox_id", "dam_id", "from_state", "to_state", "trigger_code", "authorizer_role", "authorizer_user_id",
        "evidence_uri", "ferc_docket_or_order", "effective_ts", "monitoring_rule", "notification_checklist",
        "retention_action", "rationale", "citation_code", "proposed_by", "proposed_at", "approved_by", "approved_at",
        "second_approved_by", "proposal_record_hash", "approval_record_hash", "dssmp_version_id",
        "transition_reason_code", "suspension_reason", "incident_report_uri", "sworn_verification_12_13",
        "sworn_verification_uri", "cdse_basis", "cdse_basis_by", "re_directive_uri", "withdrawal_filed_date",
        "counterpart_dam_id", "created_at", "status", "reason_code", "message", "processed_at")


def _block(path):
    s = open(path).read()
    return s[s.index("-- BEGIN v1_8_001 block"):s.index("-- END v1_8_001 block")]


def test_001_and_the_migration_carry_the_same_block_with_the_exact_grants():
    blk = _block(DDL)
    assert _block(MIG) == blk
    assert APP_GRANT in blk and blk.count("GRANT ") == 3              # app, publisher, extract (SELECT)
    assert f"GRANT SELECT, UPDATE ({', '.join(PUB_COLS)}) ON dam_app.lifecycle_transition_outbox" in blk
    assert "ALTER TABLE dam_app.lifecycle_transition_outbox OWNER TO dam_lakebase_owner;" in blk
    assert "status_transition_guard('loaded', 'rejected')" in blk and "ENABLE ALWAYS TRIGGER t_lock_" in blk
    cols = re.findall(r"(\w+) (?:TEXT|BOOLEAN)", blk.split("CREATE TABLE IF NOT EXISTS")[1].split("CONSTRAINT")[0])
    assert tuple(cols) == COLS
    code = "\n".join(l for l in open(DOWN).read().splitlines() if not l.lstrip().startswith("--"))
    assert not re.search(r"\bDROP TABLE\b|\bDELETE\b|\bTRUNCATE\b|OWNER TO", code)


def test_the_loader_reads_exactly_the_outbox_columns():
    from dam_safety.lifecycle_load import OUTBOX_COLS
    assert OUTBOX_COLS == COLS


PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")
H1, H2 = "a" * 64, "b" * 64


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture()
def env():
    tag = uuid.uuid4().hex[:8]
    db, app, pub = f"lblc_{tag}", f"lblc_app_{tag}", f"lblc_pub_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN",
                 "-c", f"CREATE ROLE {pub} LOGIN").returncode == 0
    e = {"su": f"{PG} dbname={db}", "app": f"{PG} dbname={db} user={app}", "pub": f"{PG} dbname={db} user={pub}",
         "app_role": app, "opts": {"PGOPTIONS": f"-c dam.app_role={app} -c dam.publisher_login={pub}"}}
    yield e
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}", "-c", f"DROP ROLE IF EXISTS {pub}")


def _val(e):
    r = _psql(e["su"], "-f", VAL, env_extra=e["opts"])
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _ins_sql(oid, dam, h=H1, to="suspended_monitoring_modified", reason="'drawdown'", evidence="'[\"uri:1\"]'"):
    return (f"INSERT INTO {T} (outbox_id, dam_id, from_state, to_state, trigger_code, authorizer_role, "
            "authorizer_user_id, evidence_uri, effective_ts, citation_code, proposed_by, proposed_at, approved_by, "
            "approved_at, proposal_record_hash, approval_record_hash, suspension_reason, created_at, status) VALUES "
            f"('{oid}', '{dam}', 'live', '{to}', 'ops', 'cdse', 'u1', {evidence}, '2026-11-09T19:00:00Z', "
            f"'suspend_ops_drawdown_internal', 'p@example.org', '2026-11-09T18:00:00Z', 'c@example.org', '2026-11-09T18:30:00Z', "
            f"'{H2}', '{h}', {reason}, '2026-11-09T18:30:00Z', 'pending')")


def _locked(e, sql, lock_dam, cls=4702047, conn="app"):
    return _psql(e[conn], "-c", "BEGIN", "-c", f"SELECT pg_advisory_xact_lock({cls}, hashtext('dam_app.dam:{lock_dam}'))",
                 "-c", sql, "-c", "COMMIT")


@live
def test_fresh_001_privileges_lock_order_status_guard_and_checks(env):
    r = _psql(env["su"], "-f", DDL, env_extra=env["opts"])
    assert r.returncode == 0, r.stderr
    assert _val(env) == ""
    assert _psql(env["su"], "-c", f"SELECT relowner::regrole FROM pg_class WHERE oid = '{T}'::regclass"
                 ).stdout.strip() == "dam_lakebase_owner"
    # the app's exact privileges: SELECT and INSERT, nothing else at table or column level
    q = (f"SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p "
         f"WHERE has_table_privilege('{env['app_role']}', '{T}', p) ORDER BY 1")
    assert _psql(env["su"], "-c", q).stdout.split() == ["INSERT", "SELECT"]
    q = (f"SELECT count(*) FROM pg_attribute a WHERE a.attrelid = '{T}'::regclass AND a.attnum > 0 AND "
         f"(has_column_privilege('{env['app_role']}', a.attrelid, a.attname, 'UPDATE') OR "
         f"has_column_privilege('{env['app_role']}', a.attrelid, a.attname, 'REFERENCES'))")
    assert _psql(env["su"], "-c", q).stdout.strip() == "0"
    # lock order: no dam lock / another dam / the chain class -> refused; the dam lock -> accepted
    r = _psql(env["app"], "-c", _ins_sql("O0", "D1"))
    assert r.returncode != 0 and "take the dam lock" in r.stderr
    for lock_dam, cls in (("D2", 4702047), ("D1", 4702046)):
        r = _locked(env, _ins_sql("O0", "D1"), lock_dam, cls)
        assert r.returncode != 0 and "take the dam lock" in r.stderr, (lock_dam, cls)
    assert _locked(env, _ins_sql("O1", "D1"), "D1").returncode == 0
    r = _locked(env, _ins_sql("O2", "D1", h=H2), "D1")                 # one pending row per dam
    assert r.returncode != 0 and "lifecycle_outbox_one_pending" in r.stderr
    for bad in (_ins_sql("O3", "D3", h="c" * 64, reason="NULL"),      # into suspended needs a reason
                _ins_sql("O3", "D3", h="c" * 64, to="pending_surrender"),   # a reason on another edge
                _ins_sql("O3", "D3", h="c" * 64, evidence="'[]'"),
                _ins_sql("O3", "D3", h="XYZ")):
        r = _locked(env, bad, "D3")
        assert r.returncode != 0 and "violates check constraint" in r.stderr, (bad, r.stderr)
    for stmt, err in ((f"UPDATE {T} SET status = 'loaded'", "permission denied"),
                      (f"UPDATE {T} SET reason_code = 'x'", "permission denied"),
                      (f"DELETE FROM {T}", "permission denied"), (f"TRUNCATE {T}", "permission denied"),
                      (f"ALTER TABLE {T} DISABLE TRIGGER ALL", "must be owner"),
                      (f"ALTER TABLE {T} DISABLE TRIGGER t_lock_lifecycle_transition_outbox", "must be owner"),
                      (f"DROP TRIGGER t_status_lifecycle_transition_outbox ON {T}", "must be owner"),
                      ("CREATE OR REPLACE FUNCTION dam_app.lifecycle_outbox_lock_guard() RETURNS trigger "
                       "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$", "must be owner")):
        r = _psql(env["app"], "-c", stmt)
        assert r.returncode != 0 and err in r.stderr, (stmt, r.stderr)
    # publisher: the four bookkeeping columns only; terminal is final
    assert _psql(env["pub"], "-c", f"UPDATE {T} SET status = 'loaded', reason_code = NULL, message = 'ok', "
                                   "processed_at = '2026-11-09T20:00:00Z' WHERE outbox_id = 'O1'").returncode == 0
    r = _psql(env["pub"], "-c", f"UPDATE {T} SET status = 'pending' WHERE outbox_id = 'O1'")
    assert r.returncode != 0 and "status transition" in r.stderr
    r = _psql(env["pub"], "-c", f"UPDATE {T} SET dam_id = 'X' WHERE outbox_id = 'O1'")
    assert r.returncode != 0 and "permission denied" in r.stderr
    r = _psql(env["pub"], "-c", f"INSERT INTO {T} (outbox_id) VALUES ('Z')")
    assert r.returncode != 0 and "permission denied" in r.stderr
    assert _locked(env, _ins_sql("O4", "D1", h=H2), "D1").returncode == 0   # D1 has no pending row any more


@live
def test_validator_reports_owner_privilege_and_guard_regressions(env):
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0
    a = env["app_role"]
    cases = ((f"GRANT UPDATE (status) ON {T} TO {a}", f"app_outbox_privilege|{a} has column UPDATE on {T}",
              f"REVOKE UPDATE (status) ON {T} FROM {a}"),
             (f"GRANT DELETE ON {T} TO {a}", f"app_outbox_privilege|{a} has DELETE on {T}",
              f"REVOKE DELETE ON {T} FROM {a}"),
             (f"ALTER TABLE {T} DISABLE TRIGGER t_lock_lifecycle_transition_outbox",
              "lifecycle_lock_guard_missing|t_lock_lifecycle_transition_outbox",
              f"ALTER TABLE {T} ENABLE ALWAYS TRIGGER t_lock_lifecycle_transition_outbox"),
             (f"ALTER TABLE {T} ENABLE TRIGGER t_status_lifecycle_transition_outbox",
              "status_guard_missing|t_status_lifecycle_transition_outbox",
              f"ALTER TABLE {T} ENABLE ALWAYS TRIGGER t_status_lifecycle_transition_outbox"),
             (f"ALTER TABLE {T} OWNER TO {a}", f"owner_not_dam_lakebase_owner|{T} owned by {a}",
              f"ALTER TABLE {T} OWNER TO dam_lakebase_owner"),
             ("ALTER FUNCTION dam_app.lifecycle_outbox_lock_guard() OWNER TO " + a,
              "function_owner|dam_app.lifecycle_outbox_lock_guard() owned by " + a,
              "ALTER FUNCTION dam_app.lifecycle_outbox_lock_guard() OWNER TO dam_lakebase_owner"))
    for do, expect, undo in cases:
        assert _psql(env["su"], "-c", do).returncode == 0, do
        assert expect in _val(env), (do, _val(env))
        assert _psql(env["su"], "-c", undo).returncode == 0, undo
        assert _val(env) == "", undo


@live
def test_upgrade_from_0e11d01_down_keeps_rows_and_reapply(env, tmp_path):
    old = tmp_path / "001_0e11d01.sql"
    old.write_text(git_show_or_skip(ROOT, "0e11d01", "lakebase/001_app_schema.sql"))
    assert _psql(env["su"], "-f", str(old), env_extra=env["opts"]).returncode == 0
    for m in later_than("v1_7_010") * 2:                               # idempotent
        r = _psql(env["su"], "-f", m, env_extra=env["opts"])
        assert r.returncode == 0, (m, r.stderr)
    assert _val(env) == ""
    assert _locked(env, _ins_sql("O1", "D1"), "D1").returncode == 0
    for _ in range(2):
        r = _psql(env["su"], "-f", DOWN, env_extra=env["opts"])
        assert r.returncode == 0, r.stderr
    assert _psql(env["su"], "-c", f"SELECT count(*) FROM {T}").stdout.strip() == "1"     # nothing dropped
    assert _psql(env["su"], "-c", f"SELECT relowner::regrole FROM pg_class WHERE oid = '{T}'::regclass"
                 ).stdout.strip() == "dam_lakebase_owner"                                 # never handed to the app
    assert "status_guard_missing|t_status_lifecycle_transition_outbox" in _val(env)
    assert _psql(env["su"], "-f", MIG, env_extra=env["opts"]).returncode == 0
    assert _val(env) == ""
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0 and _val(env) == ""


# ---------------------------------------------------------------- the QA-059 runner with the v1.8 file
import importlib.util  # noqa: E402
import sys  # noqa: E402

RUNNER = os.path.join(ROOT, "tools", "run_migration.py")
_spec = importlib.util.spec_from_file_location("run_migration_v18", RUNNER)
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)


def test_runner_orders_migrations_numerically():
    names = ["v1_10_001_x.sql", "v1_8_001_lifecycle_outbox.sql", "v1_7_011_migration_ledger.sql", "v1_9_002_y.sql"]
    assert sorted(names, key=rm.mig_key) == ["v1_7_011_migration_ledger.sql", "v1_8_001_lifecycle_outbox.sql",
                                             "v1_9_002_y.sql", "v1_10_001_x.sql"]
    assert rm.lakebase_files()[-1] == "v1_8_001_lifecycle_outbox.sql"
    lb = os.path.join(ROOT, "lakebase", "migrations")
    ok = rm.decide_lakebase(os.path.join(lb, "v1_8_001_lifecycle_outbox.sql"),
                            [("v1_7_011_migration_ledger.sql", "up")])
    assert ok["rerun"] is False
    with pytest.raises(rm.Refused) as e:
        rm.decide_lakebase(os.path.join(lb, "v1_7_011_migration_ledger.sql"),
                           [("v1_7_010_rule_reversion_owner.sql", "up"), ("v1_8_001_lifecycle_outbox.sql", "up")])
    assert e.value.code == 3 and "older than the newest applied migration v1_8_001" in str(e.value)


@live
def test_runner_applies_the_v18_file_refuses_a_rerun_and_rolls_back_in_order(env, tmp_path):
    old = tmp_path / "001_0e11d01.sql"
    old.write_text(git_show_or_skip(ROOT, "0e11d01", "lakebase/001_app_schema.sql"))
    assert _psql(env["su"], "-f", str(old), env_extra=env["opts"]).returncode == 0
    pub = env["pub"].split("user=")[1]

    def run(*args):
        return subprocess.run([sys.executable, RUNNER, "--lakebase", "--dsn", env["su"], "--app-role",
                               env["app_role"], "--publisher-login", pub, *args], capture_output=True, text=True)

    lb = lambda f: os.path.join(ROOT, "lakebase", "migrations", f)
    for f in later_than("v1_7_010"):                   # 0e11d01's 001 already carries 004..010 (probed)
        r = run("--file", f)
        assert r.returncode == 0, (f, r.stdout, r.stderr)
    assert _val(env) == ""
    r = run("--file", MIG)
    assert r.returncode == 3 and "already applied" in r.stderr
    r = run("--file", lb("v1_7_011_migration_ledger_down.sql"))
    assert r.returncode == 3 and "not the newest applied" in r.stderr
    r = run("--file", lb("v1_7_005_cdse_reversion_tables.sql"), "--rerun", "operator insists on it")
    assert r.returncode == 3 and "no flag overrides" in r.stderr
    r = run("--file", DOWN)
    assert r.returncode == 5 and "status_guard_missing" in (r.stdout + r.stderr)    # down leaves an unguarded table
    r = run("--file", MIG)
    assert r.returncode == 0, r.stderr
    assert _val(env) == ""
    led = _psql(env["su"], "-c", "SELECT migration || ':' || direction FROM dam_app.schema_migration_ledger "
                                 "WHERE migration LIKE 'v1_8_001%' ORDER BY ledger_id").stdout.split()
    assert led == ["v1_8_001_lifecycle_outbox.sql:up", "v1_8_001_lifecycle_outbox_down.sql:down",
                   "v1_8_001_lifecycle_outbox.sql:up"]


def test_split_pending_only_forgives_tables_a_later_migration_creates():
    old = {f for f in rm.lakebase_files() if f < "v1_8_001"}
    bad = [("outbox_missing", "dam_app.lifecycle_transition_outbox"),
           ("lifecycle_lock_guard_missing", "t_lock_lifecycle_transition_outbox on dam_app.lifecycle_transition_outbox"),
           ("outbox_missing", "dam_app.dam_cdse_outbox"),                        # created by an applied file
           ("app_outbox_privilege", "x has DELETE on dam_app.lifecycle_transition_outbox"),
           ("owner_role_missing", "role dam_lakebase_owner does not exist")]
    pending, real = rm.split_pending(bad, old, lambda t: False)
    assert pending == bad[:2] + bad[3:4] and real == [bad[2], bad[4]]
    pending, real = rm.split_pending(bad, old, lambda t: True)                    # it exists: never pending
    assert pending == [] and real == bad
    pending, real = rm.split_pending(bad, old | {"v1_8_001_lifecycle_outbox.sql"}, lambda t: False)
    assert pending == []
