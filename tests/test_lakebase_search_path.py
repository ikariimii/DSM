"""QA item 4: the Lakebase jobs use unqualified table names, so the shared connection helper
(src/jobs/lakebase_extract._connect -> _session) sets search_path to exactly the configured schema (bundle variable
lakebase_schema -> --lb_schema, default dam_app), verifies it, and only then switches to REPEATABLE READ.
Static checks + live PostgreSQL (DAM_TEST_PG). Lakebase itself: UNVERIFIED (runbook)."""
import os
import re
import shutil
import sys
import types
import uuid

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
import lakebase_extract as lx  # noqa: E402

JOBS = ["lakebase_extract.py", "load_dssmp_registrations.py", "publish_ref_changes.py", "apply_cdse_assignments.py",
        "apply_lifecycle_transitions.py"]
SRC = {j: open(os.path.join(ROOT, "src", "jobs", j)).read() for j in JOBS}


def test_every_job_that_opens_lakebase_is_listed():
    d = os.path.join(ROOT, "src", "jobs")
    opening = sorted(f for f in os.listdir(d) if f.endswith(".py") and "_connect(" in open(os.path.join(d, f)).read()
                     and f != "lakebase_extract.py")
    assert opening == sorted(j for j in JOBS if j != "lakebase_extract.py")


@pytest.mark.parametrize("job", JOBS)
def test_every_lakebase_job_passes_the_configured_schema(job):
    s = SRC[job]
    assert '("lb_schema", ' in s
    calls = re.findall(r"_connect\(([^)]*)\)", s.split("def main", 1)[1])
    assert calls == ["a.lb_project, a.lb_branch, a.lb_endpoint, a.lb_schema"], calls
    assert "SET search_path" not in s.split("def _session", 1)[-1].split("def _connect", 1)[-1]


def test_bundle_passes_lakebase_schema_to_every_lakebase_task():
    jobs = yaml.safe_load(open(os.path.join(ROOT, "resources", "jobs.yml")))["resources"]["jobs"]
    seen = []
    for job in jobs.values():
        for t in job.get("tasks", []):
            f = (t.get("spark_python_task") or {}).get("python_file", "")
            if os.path.basename(f) in JOBS:
                p = t["spark_python_task"]["parameters"]
                assert p[p.index("--lb_schema") + 1] == "${var.lakebase_schema}", t["task_key"]
                seen.append(os.path.basename(f))
    assert sorted(seen) == sorted(JOBS)
    var = yaml.safe_load(open(os.path.join(ROOT, "databricks.yml")))["variables"]["lakebase_schema"]
    assert var["default"] == lx.DEFAULT_LB_SCHEMA == "dam_app"


@pytest.mark.parametrize("bad", ["", "Dam_app", "dam_app; DROP TABLE x", '"dam_app"', "dam-app", "public,dam_app",
                                 "dam_app\u200b", None, 1, "a" * 64])
def test_invalid_schema_names_are_refused_before_any_sql(bad):
    class NoSQL:
        def execute(self, *a, **k):
            pytest.fail("executed SQL")
    with pytest.raises(ValueError):
        lx._session(NoSQL(), bad)


PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


@pytest.fixture()
def db():
    import psycopg
    name, role = f"lbsp_{uuid.uuid4().hex[:8]}", f"lbsp_r_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
        c.execute(f"CREATE ROLE {role} LOGIN")
    conninfo = f"{PG} dbname={name}"
    with psycopg.connect(conninfo, autocommit=True) as c:
        c.execute("CREATE SCHEMA dam_app; CREATE SCHEMA other")
        for s in ("dam_app", "public", "other"):
            c.execute(f"CREATE TABLE {s}.extract_watermark (table_name text, owner_schema text)")
            c.execute(f"INSERT INTO {s}.extract_watermark VALUES ('t', '{s}')")
        c.execute(f"GRANT USAGE ON SCHEMA other TO {role}; GRANT SELECT ON ALL TABLES IN SCHEMA other TO {role}")
    yield conninfo, role
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c.execute(f"DROP ROLE IF EXISTS {role}")


def _owner(conn):
    return conn.execute("SELECT owner_schema FROM extract_watermark").fetchone()[0]


@live
def test_without_the_session_setup_unqualified_names_resolve_to_public(db):
    import psycopg
    with psycopg.connect(db[0]) as c:
        assert _owner(c) == "public"                     # the hazard QA flagged


@live
def test_session_resolves_unqualified_names_in_the_configured_schema_only(db):
    import psycopg
    with lx._session(psycopg.connect(db[0])) as c:
        assert c.execute("SHOW search_path").fetchone()[0] == "dam_app"
        assert c.execute("SELECT current_schemas(false)").fetchone()[0] == ["dam_app"]
        assert c.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
        assert c.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
        assert _owner(c) == "dam_app"
        c.commit()
        assert c.execute("SHOW search_path").fetchone()[0] == "dam_app"      # session-level, survives commit
        assert c.execute("SELECT count(*) FROM pg_class").fetchone()[0] > 0   # pg_catalog still implicit
    with lx._session(psycopg.connect(db[0]), "other") as c:
        assert _owner(c) == "other"


@live
@pytest.mark.parametrize("schema", ["missing_schema", "dam_app"])
def test_missing_or_inaccessible_schema_fails_closed(db, schema):
    import psycopg
    conninfo, role = db                                  # role has USAGE on `other` only
    with psycopg.connect(f"{conninfo} user={role}") as c:
        with pytest.raises(RuntimeError, match="missing or not accessible"):
            lx._session(c, schema)


@live
def test_connect_uses_the_session_setup_and_closes_on_failure(db, monkeypatch):
    """_connect with the Databricks SDK faked and psycopg.connect redirected to the test database."""
    import psycopg
    real = psycopg.connect
    opened = []

    def fake_connect(**kw):
        assert kw["dbname"] == "databricks_postgres" and kw["sslmode"] == "require"
        c = real(db[0])
        opened.append(c)
        return c

    class W:
        postgres = types.SimpleNamespace(
            generate_database_credential=lambda endpoint: types.SimpleNamespace(token="tok"),
            get_endpoint=lambda name: types.SimpleNamespace(
                status=types.SimpleNamespace(hosts=types.SimpleNamespace(host="h"))))
        current_user = types.SimpleNamespace(me=lambda: types.SimpleNamespace(user_name="u"))
    sdk = types.ModuleType("databricks.sdk")
    sdk.WorkspaceClient = W
    monkeypatch.setitem(sys.modules, "databricks", types.ModuleType("databricks"))
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with lx._connect("p", "b", "e") as c:
        assert _owner(c) == "dam_app"
    with pytest.raises(RuntimeError):
        lx._connect("p", "b", "e", "missing_schema")
    assert opened[-1].closed
