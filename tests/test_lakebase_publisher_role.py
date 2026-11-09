"""v1.7 Lakebase publisher role and default privileges (app SCHEMA_CONTRACT §7 items 17 / 18; Genie 2026-11-09):
NOLOGIN dam_lakebase_publisher = USAGE + SELECT + UPDATE (status, message, applied_at) on the three publication
outboxes only; the extract role stays SELECT-only and gets SELECT on future tables by default; the app may only
INSERT / SELECT the outboxes. Executed on a real PostgreSQL (>= 15) when DAM_TEST_PG is set. Synthetic data only."""
import os
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
DDL_PATH = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
VAL_PATH = os.path.join(ROOT, "lakebase", "validate_append_only.sql")
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")
OUTBOXES = ("ref_change_publication", "onboarding_publication", "dssmp_registration_outbox")


def test_static_grants_are_column_level_and_never_default_for_the_publisher():
    ddl = open(DDL_PATH).read()
    assert "CREATE ROLE dam_lakebase_publisher NOLOGIN" in ddl
    assert ("GRANT SELECT, UPDATE (status, message, applied_at) ON ref_change_publication, onboarding_publication\n"
            "  TO dam_lakebase_publisher;") in ddl
    assert ("GRANT SELECT, UPDATE (status, reason_code, message, processed_at) ON dssmp_registration_outbox\n"
            "  TO dam_lakebase_publisher;") in ddl
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE dam_lakebase_owner IN SCHEMA dam_app GRANT SELECT ON TABLES" in ddl
    for line in ddl.splitlines():
        if "DEFAULT PRIVILEGES" in line:
            assert "publisher" not in line and "UPDATE" not in line
    import publish_ref_changes as pj
    import load_dssmp_registrations as lj
    assert pj.UPDATE_SQL.split(" SET ")[1].split(" WHERE ")[0] == "status = %s, message = %s, applied_at = %s"
    assert lj.UPDATE_SQL.split(" SET ")[1].split(" WHERE ")[0] == \
        "status = %s, reason_code = %s, message = %s, processed_at = %s"


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture(scope="module")
def env():
    tag = uuid.uuid4().hex[:8]
    db, app, ext, pub = f"lbpub_{tag}", f"lbpub_app_{tag}", f"lbpub_ext_{tag}", f"lbpub_login_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN",
                 "-c", f"CREATE ROLE {ext} LOGIN", "-c", f"CREATE ROLE {pub} LOGIN").returncode == 0
    conn = f"{PG} dbname={db}"
    opts = {"PGOPTIONS": f"-c dam.app_role={app} -c dam.extract_role={ext} -c dam.publisher_login={pub}"}
    for _ in range(2):                                                   # idempotent
        r = _psql(conn, "-f", DDL_PATH, env_extra=opts)
        assert r.returncode == 0, r.stderr
    e = {"conn": conn, "app": app, "ext": ext, "pub": pub, "opts": opts,
         "as_app": f"{conn} user={app}", "as_ext": f"{conn} user={ext}", "as_pub": f"{conn} user={pub}"}
    for t in OUTBOXES:
        assert _ins(e, t).returncode == 0
    yield e
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    for r in (app, ext, pub):
        _psql(PG, "-c", f"DROP ROLE IF EXISTS {r}")


_n = iter(range(1, 10_000))


def _ins(e, table):
    n = next(_n)
    vals = {"ref_change_publication": "(pub_id, kind, dam_id, payload, requested_by, requested_at, status) VALUES "
                                      f"('P-{n}', 'k', 'D1', '{{}}', 'a', 't', 'pending')",
            "onboarding_publication": "(pub_id, draft_id, dam_id, payload, requested_by, requested_at, status) VALUES "
                                      f"('O-{n}', 'DR', 'D1', '{{}}', 'a', 't', 'pending')",
            "dssmp_registration_outbox": "(outbox_id, dam_id, dssmp_version_id, version_no, effective_from, "
                                         "document_sha256, proposed_by, approved_by, approved_at, "
                                         f"approval_record_hash, status) VALUES ('OB-{n}', 'D1', 'DS-{n}', 1, 't', "
                                         "'h', 'a', 'c', 't', 'h', 'pending')"}[table]
    return _psql(e["as_app"], "-c", f"INSERT INTO dam_app.{table} {vals}")


def _violations(e, extra_opts=""):
    r = _psql(e["conn"], "-F", "|", "-f", VAL_PATH,
              env_extra={"PGOPTIONS": f"-c dam.app_role={e['app']} -c dam.extract_role={e['ext']} {extra_opts}"})
    assert r.returncode == 0, r.stderr
    return [tuple(l.split("|", 1)) for l in r.stdout.splitlines() if l.strip()]


@live
def test_good_setup_passes_the_validator_with_extract_and_publisher(env):
    assert _violations(env) == []


@live
@pytest.mark.parametrize("table", OUTBOXES)
def test_publisher_can_read_and_set_the_outcome_columns_only(env, table):
    dssmp = table == "dssmp_registration_outbox"
    key = "outbox_id" if dssmp else "pub_id"
    sets = ("status = 'loaded', reason_code = 'x', message = 'm', processed_at = 'now'" if dssmp
            else "status = 'applied', message = 'm', applied_at = 'now'")
    ok = _psql(env["as_pub"], "-c", f"SELECT count(*) FROM dam_app.{table}",
               "-c", f"UPDATE dam_app.{table} SET {sets} WHERE {key} IS NOT NULL")
    assert ok.returncode == 0, ok.stderr
    others = ("dam_id", "proposed_by", "approved_by", "approved_at", key) if dssmp else \
        ("dam_id", "requested_by", "requested_at", key)
    for col in others:
        r = _psql(env["as_pub"], "-c", f"UPDATE dam_app.{table} SET {col} = 'x'")
        assert r.returncode != 0 and "permission denied" in r.stderr, (col, r.stderr)
    for stmt in (f"DELETE FROM dam_app.{table}", f"TRUNCATE dam_app.{table}",
                 f"INSERT INTO dam_app.{table} ({key}) VALUES ('evil')"):
        r = _psql(env["as_pub"], "-c", stmt)
        assert r.returncode != 0 and "permission denied" in r.stderr, (stmt, r.stderr)


@live
def test_publisher_payload_columns_and_other_tables_are_closed(env):
    for stmt in ("UPDATE dam_app.ref_change_publication SET payload = '{}'",
                 "UPDATE dam_app.ref_change_publication SET kind = 'x'",
                 "UPDATE dam_app.dssmp_registration_outbox SET document_sha256 = 'x'",
                 "UPDATE dam_app.dssmp_registration_outbox SET approval_record_hash = 'x'",
                 "SELECT * FROM dam_app.review_signoff", "SELECT * FROM dam_app.app_user",
                 "DELETE FROM dam_app.extract_watermark", "CREATE TABLE dam_app.evil (x int)"):
        r = _psql(env["as_pub"], "-c", stmt)
        assert r.returncode != 0 and "permission denied" in r.stderr, (stmt, r.stderr)


@live
@pytest.mark.parametrize("table", OUTBOXES)
def test_app_inserts_and_reads_the_outboxes_but_never_updates_or_deletes(env, table):
    assert _ins(env, table).returncode == 0
    for stmt in (f"UPDATE dam_app.{table} SET status = 'applied'", f"DELETE FROM dam_app.{table}",
                 f"TRUNCATE dam_app.{table}"):
        r = _psql(env["as_app"], "-c", stmt)
        assert r.returncode != 0 and ("permission denied" in r.stderr or "must be owner" in r.stderr), (stmt, r.stderr)


@live
def test_extract_stays_select_only_and_sees_future_tables(env):
    for t in OUTBOXES:
        r = _psql(env["as_ext"], "-c", f"UPDATE dam_app.{t} SET status = 'x'")
        assert r.returncode != 0 and "permission denied" in r.stderr
    # a table the app creates after 001 ran (e.g. alarm_rule_flag_proposal on a first deploy), and one the owner adds
    assert _psql(env["as_app"], "-c", "CREATE TABLE dam_app.later_app (x int)",
                 "-c", "INSERT INTO dam_app.later_app VALUES (1)").returncode == 0
    assert _psql(env["conn"], "-c", "SET ROLE dam_lakebase_owner",
                 "-c", "CREATE TABLE dam_app.later_owner (x int)").returncode == 0
    r = _psql(env["as_ext"], "-c", "SELECT count(*) FROM dam_app.later_app",
              "-c", "SELECT count(*) FROM dam_app.later_owner")
    assert r.returncode == 0 and r.stdout.split() == ["1", "0"], r.stderr
    for stmt in ("UPDATE dam_app.later_app SET x = 2", "DELETE FROM dam_app.later_owner"):
        assert _psql(env["as_ext"], "-c", stmt).returncode != 0
    r = _psql(env["as_pub"], "-c", "SELECT * FROM dam_app.later_owner")       # no default privilege for publisher
    assert r.returncode != 0 and "permission denied" in r.stderr
    assert _violations(env) == []
    assert _psql(env["conn"], "-c", "DROP TABLE dam_app.later_app, dam_app.later_owner").returncode == 0


@live
@pytest.mark.parametrize("grant,revoke,check", [
    ("GRANT DELETE ON dam_app.ref_change_publication TO dam_lakebase_publisher",
     "REVOKE DELETE ON dam_app.ref_change_publication FROM dam_lakebase_publisher", "publisher_privilege"),
    ("GRANT UPDATE (payload) ON dam_app.ref_change_publication TO dam_lakebase_publisher",
     "REVOKE UPDATE (payload) ON dam_app.ref_change_publication FROM dam_lakebase_publisher", "publisher_privilege"),
    ("GRANT SELECT ON dam_app.review_signoff TO dam_lakebase_publisher",
     "REVOKE SELECT ON dam_app.review_signoff FROM dam_lakebase_publisher", "publisher_privilege"),
    ("ALTER DEFAULT PRIVILEGES FOR ROLE dam_lakebase_owner IN SCHEMA dam_app GRANT UPDATE ON TABLES TO "
     "dam_lakebase_publisher", "ALTER DEFAULT PRIVILEGES FOR ROLE dam_lakebase_owner IN SCHEMA dam_app REVOKE UPDATE "
     "ON TABLES FROM dam_lakebase_publisher", "publisher_default_privilege"),
    ("GRANT UPDATE (status) ON dam_app.ref_change_publication TO {ext}",
     "REVOKE UPDATE (status) ON dam_app.ref_change_publication FROM {ext}", "extract_privilege"),
    ("GRANT UPDATE ON dam_app.onboarding_publication TO {app}",
     "REVOKE UPDATE ON dam_app.onboarding_publication FROM {app}", "app_outbox_privilege"),
    ("GRANT dam_lakebase_publisher TO {app}", "REVOKE dam_lakebase_publisher FROM {app}", "app_member_of_publisher"),
    ("ALTER DEFAULT PRIVILEGES FOR ROLE dam_lakebase_owner IN SCHEMA dam_app REVOKE SELECT ON TABLES FROM {ext}",
     "ALTER DEFAULT PRIVILEGES FOR ROLE dam_lakebase_owner IN SCHEMA dam_app GRANT SELECT ON TABLES TO {ext}",
     "extract_default_select_missing"),
])
def test_validator_catches_each_widening(env, grant, revoke, check):
    f = dict(ext=env["ext"], app=env["app"])
    assert _psql(env["conn"], "-c", grant.format(**f)).returncode == 0
    try:
        assert check in {c for c, _ in _violations(env)}
    finally:
        assert _psql(env["conn"], "-c", revoke.format(**f)).returncode == 0
    assert _violations(env) == []


@live
def test_001_refuses_the_app_role_as_publisher_login(env):
    r = _psql(env["conn"], "-f", DDL_PATH, env_extra={
        "PGOPTIONS": f"-c dam.app_role={env['app']} -c dam.publisher_login={env['app']}"})
    assert r.returncode != 0 and "publisher login" in r.stderr
    r = _psql(env["conn"], "-f", DDL_PATH, env_extra=env["opts"])            # and the good run still works after
    assert r.returncode == 0, r.stderr
