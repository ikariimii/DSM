"""v1.7 Lakebase ownership hardening (QA-045b residual risks), lakebase/001_app_schema.sql split-ownership model and
lakebase/validate_append_only.sql (+ tools/validate_lakebase.py).

Static checks always run. Live checks need a REAL PostgreSQL (>= 15) and psql, e.g.
    DAM_TEST_PG="host=/tmp port=55432 user=postgres dbname=postgres" pytest tests/test_lakebase_ownership.py
They create a throwaway database and a throwaway LOGIN role (trust auth on the local test cluster), act AS that app
role, and prove: DISABLE TRIGGER, TRUNCATE, UPDATE/DELETE, SET session_replication_role, self-GRANT, DROP, function
replacement and SET ROLE to the owner all fail; triggers fire even for a superuser in replica mode (ENABLE ALWAYS);
the validator passes the good setup and names the exact check for each deliberately bad one.
Lakebase itself (version, deployer and app SP role attributes) is UNVERIFIED (docs/VERIFICATION.md LB-OWNER)."""
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL_PATH = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
VAL_PATH = os.path.join(ROOT, "lakebase", "validate_append_only.sql")
DDL, VAL = open(DDL_PATH).read(), open(VAL_PATH).read()
GUARDED = ("exceedance_status", "review_signoff", "manual_reading")


def _code(sql):
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


# ---------------------------------------------------------------- static
def test_owner_role_owns_schema_guarded_tables_and_trigger_functions():
    code = _code(DDL)
    assert "CREATE ROLE dam_lakebase_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in code
    assert "ALTER SCHEMA dam_app OWNER TO dam_lakebase_owner;" in code
    for t in GUARDED:
        assert f"ALTER TABLE {t} OWNER TO dam_lakebase_owner;" in code
        assert re.search(rf"ALTER TABLE {t} ENABLE ALWAYS TRIGGER t_noupd_{t},\s+ENABLE ALWAYS TRIGGER t_notrunc_{t};", code)
    assert "p.proname IN ('forbid_mutation', 'app_forbid_mutation', 'status_transition_guard')" in code
    assert "GRANT SELECT, INSERT ON exceedance_status, review_signoff, manual_reading, review_chain_head_log TO %I" in code
    assert not re.search(r"GRANT [^;]*(UPDATE|DELETE|TRUNCATE|TRIGGER)[^;]*ON exceedance_status, review_signoff", code)
    # ownership work sits inside the single locked transaction
    assert code.index("pg_advisory_xact_lock(4702045)") < code.index("dam_lakebase_owner") < code.rindex("COMMIT;")


def test_validator_is_read_only_and_covers_every_required_check():
    code = _code(VAL)
    no_literals = re.sub(r"'[^']*'", "''", code)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|GRANT|REVOKE|CREATE|DROP|SET)\b", no_literals)
    for chk in ("owner_role_missing", "owner_role_attrs", "schema_owner", "table_owner", "function_owner",
                "guard_trigger_missing", "guard_trigger_not_always", "trigger_disabled", "app_role_unset",
                "app_role_missing", "app_role_attrs", "app_member_of_owner", "app_owns", "app_privilege",
                "app_can_set_replication_role", "app_preset_replication_role"):
        assert f"'{chk}'" in code, chk


def test_ci_and_checklist_wire_the_validator():
    wf = open(os.path.join(ROOT, ".github", "workflows", "bundle.yml")).read()
    assert "postgres:17" in wf and "tests/test_lakebase_ownership.py" in wf and "DAM_TEST_PG" in wf
    assert "python tools/validate_lakebase.py" in wf
    readme = open(os.path.join(ROOT, "README.md")).read()
    assert "lakebase/validate_append_only.sql" in readme and "dam_lakebase_owner" in readme


# ---------------------------------------------------------------- live
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


def _apply(conn, app):
    r = _psql(conn, "-f", DDL_PATH, env_extra={"PGOPTIONS": f"-c dam.app_role={app}"})
    assert r.returncode == 0, r.stderr


def _violations(conn, app):
    r = _psql(conn, "-At", "-F", "|", "-f", VAL_PATH, env_extra={"PGOPTIONS": f"-c dam.app_role={app}"})
    assert r.returncode == 0, r.stderr
    return [tuple(l.split("|", 1)) for l in r.stdout.splitlines() if l.strip()]


@pytest.fixture(scope="module")
def env():
    tag = uuid.uuid4().hex[:8]
    db, app = f"lbown_{tag}", f"lbown_app_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN").returncode == 0
    conn = f"{PG} dbname={db}"
    # the app starts first and creates review_signoff itself (worst case: it owns it), then the deployer runs 001
    assert _psql(conn, "-c", f"GRANT CREATE ON DATABASE {db} TO {app}").returncode == 0
    assert _psql(f"{conn} user={app}", "-c", "CREATE SCHEMA dam_app",
                 "-c", "CREATE TABLE dam_app.review_signoff (review_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, "
                       "subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, review_level TEXT NOT NULL, decision TEXT "
                       "NOT NULL, comment TEXT, reviewer_id TEXT NOT NULL, reviewer_pe_license TEXT, signed_ts TEXT NOT "
                       "NULL, signature_hash TEXT NOT NULL)",
                 "-c", "CREATE TABLE dam_app.app_user (email TEXT PRIMARY KEY, display_name TEXT, is_admin BOOLEAN NOT "
                       "NULL DEFAULT FALSE, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TEXT NOT NULL, created_by TEXT)"
                 ).returncode == 0
    assert _psql(conn, "-c", f"REVOKE CREATE ON DATABASE {db} FROM {app}").returncode == 0
    _apply(conn, app)
    _apply(conn, app)                                    # idempotent
    yield {"conn": conn, "app": app, "as_app": f"{conn} user={app}", "db": db}
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}")


@live
def test_good_setup_passes_the_validator_sql_and_the_python_tool(env):
    assert _violations(env["conn"], env["app"]) == []
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "validate_lakebase.py"), "--app-role", env["app"],
                        "--dsn", env["conn"]], capture_output=True, text=True)
    if "ModuleNotFoundError" in r.stderr or "No module named 'psycopg'" in r.stdout + r.stderr:
        pytest.skip("psycopg not installed")
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


@live
def test_app_role_ownership_moved_and_it_keeps_its_mutable_tables(env):
    q = ("SELECT relname || ':' || relowner::regrole FROM pg_class WHERE relnamespace = 'dam_app'::regnamespace "
         "AND relname IN ('review_signoff', 'app_user', 'manual_reading', 'review_chain_head') ORDER BY 1")
    out = _psql(env["conn"], "-At", "-c", q).stdout.split()
    assert out == [f"app_user:{env['app']}", "manual_reading:dam_lakebase_owner",
                   f"review_chain_head:{env['app']}", "review_signoff:dam_lakebase_owner"]


ROW = ("INSERT INTO dam_app.review_signoff (review_id, dam_id, subject_type, subject_id, review_level, decision, "
       "comment, reviewer_id, signed_ts, signature_hash) VALUES ('RS-{n}','D1','incl_correction','C-1','engineer',"
       "'approve','ok','eng@example.org','2026-11-09T15:00:00Z','abc')")


@live
@pytest.mark.parametrize("stmt,err", [
    ("ALTER TABLE dam_app.review_signoff DISABLE TRIGGER t_notrunc_review_signoff", "must be owner"),
    ("ALTER TABLE dam_app.review_signoff DISABLE TRIGGER ALL", "must be owner"),
    ("ALTER TABLE dam_app.manual_reading DISABLE TRIGGER t_noupd_manual_reading", "must be owner"),
    ("TRUNCATE dam_app.review_signoff", "permission denied"),
    ("TRUNCATE dam_app.exceedance_status", "permission denied"),
    ("UPDATE dam_app.review_signoff SET decision = 'reject'", "permission denied"),
    ("DELETE FROM dam_app.review_signoff", "permission denied"),
    ("SET session_replication_role = replica", "permission denied to set parameter"),
    ("SET LOCAL session_replication_role = replica", "permission denied to set parameter"),
    # a non-owner GRANT is a no-op ("no privileges were granted"), so the TRUNCATE after it is still refused
    ("GRANT TRUNCATE ON dam_app.review_signoff TO CURRENT_USER; TRUNCATE dam_app.review_signoff", "permission denied"),
    ("DROP TABLE dam_app.review_signoff", "must be owner"),
    ("DROP TRIGGER t_notrunc_review_signoff ON dam_app.review_signoff", "must be owner"),
    ("CREATE OR REPLACE FUNCTION dam_app.forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS "
     "$$ BEGIN RETURN NULL; END $$", "must be owner"),
    ("ALTER TABLE dam_app.review_signoff OWNER TO CURRENT_USER", "must be owner"),
    ("SET ROLE dam_lakebase_owner", "permission denied"),
    ("DROP SCHEMA dam_app CASCADE", "must be owner"),
])
def test_app_role_cannot_bypass_the_guard(env, stmt, err):
    assert _psql(env["as_app"], "-c", ROW.format(n=uuid.uuid4().hex[:6])).returncode == 0   # INSERT is allowed
    r = _psql(env["as_app"], "-c", "BEGIN", "-c", stmt, "-c", "ROLLBACK")
    assert r.returncode != 0 and err in r.stderr, (stmt, r.stderr)


@live
def test_triggers_fire_even_for_a_superuser_in_replica_mode(env):
    """ENABLE ALWAYS: session_replication_role = replica no longer skips the guard (residual risk 2)."""
    r = _psql(env["conn"], "-c", "SET session_replication_role = replica", "-c", "TRUNCATE dam_app.review_signoff")
    assert r.returncode != 0 and "review_signoff is append-only (TRUNCATE refused)" in r.stderr, r.stderr
    r = _psql(env["conn"], "-c", "SET session_replication_role = replica",
              "-c", "DELETE FROM dam_app.manual_reading WHERE true", "-c", "SELECT 1")
    assert r.returncode == 0          # 0 rows: no row trigger to fire; TRUNCATE above is the binding proof


BAD = [  # (break as superuser, undo, expected violation check)
    ("ALTER TABLE dam_app.review_signoff OWNER TO {app}", "ALTER TABLE dam_app.review_signoff OWNER TO dam_lakebase_owner",
     "app_owns"),
    ("ALTER TABLE dam_app.manual_reading OWNER TO postgres",
     "ALTER TABLE dam_app.manual_reading OWNER TO dam_lakebase_owner", "table_owner"),
    ("GRANT TRUNCATE ON dam_app.review_signoff TO {app}", "REVOKE TRUNCATE ON dam_app.review_signoff FROM {app}",
     "app_privilege"),
    ("GRANT UPDATE ON dam_app.exceedance_status TO {app}", "REVOKE UPDATE ON dam_app.exceedance_status FROM {app}",
     "app_privilege"),
    ("GRANT UPDATE (decision) ON dam_app.review_signoff TO {app}",
     "REVOKE UPDATE (decision) ON dam_app.review_signoff FROM {app}", "app_privilege"),
    ("GRANT DELETE ON dam_app.manual_reading TO {app}", "REVOKE DELETE ON dam_app.manual_reading FROM {app}",
     "app_privilege"),
    ("ALTER ROLE {app} REPLICATION", "ALTER ROLE {app} NOREPLICATION", "app_role_attrs"),
    ("ALTER ROLE {app} SUPERUSER", "ALTER ROLE {app} NOSUPERUSER", "app_role_attrs"),
    ("ALTER ROLE {app} BYPASSRLS", "ALTER ROLE {app} NOBYPASSRLS", "app_role_attrs"),
    ("GRANT dam_lakebase_owner TO {app}", "REVOKE dam_lakebase_owner FROM {app}", "app_member_of_owner"),
    ("ALTER TABLE dam_app.review_signoff DISABLE TRIGGER t_notrunc_review_signoff",
     "ALTER TABLE dam_app.review_signoff ENABLE ALWAYS TRIGGER t_notrunc_review_signoff", "trigger_disabled"),
    ("ALTER TABLE dam_app.review_signoff ENABLE TRIGGER t_noupd_review_signoff",
     "ALTER TABLE dam_app.review_signoff ENABLE ALWAYS TRIGGER t_noupd_review_signoff", "guard_trigger_not_always"),
    ("DROP TRIGGER t_notrunc_manual_reading ON dam_app.manual_reading",
     "CREATE TRIGGER t_notrunc_manual_reading BEFORE TRUNCATE ON dam_app.manual_reading FOR EACH STATEMENT EXECUTE "
     "FUNCTION dam_app.forbid_mutation(); ALTER TABLE dam_app.manual_reading ENABLE ALWAYS TRIGGER t_notrunc_manual_reading",
     "guard_trigger_missing"),
    ("GRANT SET ON PARAMETER session_replication_role TO {app}",
     "REVOKE SET ON PARAMETER session_replication_role FROM {app}", "app_can_set_replication_role"),
    ("ALTER SCHEMA dam_app OWNER TO {app}", "ALTER SCHEMA dam_app OWNER TO dam_lakebase_owner", "schema_owner"),
    ("ALTER FUNCTION dam_app.forbid_mutation() OWNER TO {app}",
     "ALTER FUNCTION dam_app.forbid_mutation() OWNER TO dam_lakebase_owner", "function_owner"),
    ("ALTER ROLE dam_lakebase_owner LOGIN", "ALTER ROLE dam_lakebase_owner NOLOGIN", "owner_role_attrs"),
]


@live
@pytest.mark.parametrize("brk,undo,check", BAD, ids=[f"{b[2]}:{i}" for i, b in enumerate(BAD)])
def test_each_deliberately_bad_setup_fails_the_validator(env, brk, undo, check):
    app = env["app"]
    assert _psql(env["conn"], "-c", brk.format(app=app)).returncode == 0
    try:
        got = _violations(env["conn"], app)
        assert check in {c for c, _ in got}, got
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "validate_lakebase.py"), "--app-role", app,
                            "--dsn", env["conn"]], capture_output=True, text=True)
        if "psycopg" not in r.stderr:
            assert r.returncode == 1 and f"FAIL {check}" in r.stdout, r.stdout + r.stderr
    finally:
        for s in undo.format(app=app).split("; "):
            assert _psql(env["conn"], "-c", s).returncode == 0, s
    assert _violations(env["conn"], app) == []                        # restored


@live
def test_unset_or_unknown_app_role_fails_the_validator(env):
    r = _psql(env["conn"], "-At", "-F", "|", "-f", VAL_PATH)
    assert "app_role_unset" in r.stdout
    assert "app_role_missing" in {c for c, _ in _violations(env["conn"], "no_such_role_x")}


@live
def test_001_refuses_a_superuser_or_replication_app_role(env):
    app = env["app"]
    for attr, undo in (("REPLICATION", "NOREPLICATION"), ("SUPERUSER", "NOSUPERUSER")):
        assert _psql(env["conn"], "-c", f"ALTER ROLE {app} {attr}").returncode == 0
        try:
            r = _psql(env["conn"], "-f", DDL_PATH, env_extra={"PGOPTIONS": f"-c dam.app_role={app}"})
            assert r.returncode != 0 and f"must not be {attr}" in r.stderr, r.stderr
        finally:
            _psql(env["conn"], "-c", f"ALTER ROLE {app} {undo}")


@live
@pytest.mark.parametrize("canon,role,ok", [(None, None, True), (2, "cdse", True), (2, "admin,cdse", True),
                                           (2, None, False), (2, "  ", False), (3, "admin", False)])
def test_canon_v2_columns_and_check_exist_for_the_app_role(env, canon, role, ok):
    """canon v2 (App Dev c797151): 001 adds actor_app_role / canon_version and ck_review_signoff_canon to the
    owner-owned review_signoff, so the app (which may not ALTER it) can append canon-2 records."""
    _apply(env["conn"], env["app"])        # re-provision: earlier bad-setup tests undo their break, not every grant
    n = uuid.uuid4().hex[:8]
    lit = lambda v: "NULL" if v is None else (str(v) if isinstance(v, int) else f"'{v}'")
    stmt = (ROW.format(n=n).replace("signature_hash) VALUES", "signature_hash, canon_version, actor_app_role) VALUES")
            [:-1] + f", {lit(canon)}, {lit(role)})")
    r = _psql(env["as_app"], "-c", stmt)
    assert (r.returncode == 0) is ok, r.stderr
    if not ok:
        assert "ck_review_signoff_canon" in r.stderr
