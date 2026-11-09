"""Live PostgreSQL: the app as a RESTRICTED LOGIN role on the DA's split-ownership Lakebase model (lakebase/001,
v1.7: NOLOGIN dam_lakebase_owner owns the schema and the append-only tables; the app role has SELECT + INSERT on
them and owns its mutable tables). Runs only with DAM_TEST_PG (superuser conninfo used to provision a scratch DB):
    DAM_TEST_PG="host=/tmp port=55432 user=postgres dbname=postgres" pytest app/tests/test_lakebase_owner_live.py
lakebase/001 (DA e8cc437) carries the canon-2 columns and ck_review_signoff_canon itself; the app runs no DDL on
owner-owned objects."""
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from backend import audit_chain as ac
from backend.repos.state import StateStore

PG = os.environ.get("DAM_TEST_PG")
pytestmark = pytest.mark.skipif(not PG or not shutil.which("psql"),
                                reason="set DAM_TEST_PG (superuser libpq conninfo) and have psql to run")
LB001 = Path(__file__).resolve().parents[2] / "lakebase" / "001_app_schema.sql"


def _psql(conn, *args, env=None):
    r = subprocess.run(["psql", conn, "-X", "-q", "-v", "ON_ERROR_STOP=1", *args], capture_output=True, text=True,
                       env=dict(os.environ, **(env or {})))
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture()
def lakebase():
    """Scratch DB provisioned by lakebase/001 alone (as superuser = deployer); yields (su, app) conninfo."""
    tag = uuid.uuid4().hex[:8]
    db, role = f"lbown_{tag}", f"lbown_app_{tag}"
    _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {role} LOGIN NOSUPERUSER NOREPLICATION NOBYPASSRLS")
    su = f"{PG} dbname={db}"
    try:
        _psql(su, "-f", str(LB001), env={"PGOPTIONS": f"-c dam.app_role={role}"})
        yield su, f"{PG} dbname={db} user={role}"
    finally:
        _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)", "-c", f"DROP ROLE IF EXISTS {role}")


def _store(conn):
    pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    pool = ConnectionPool(conn, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)
    return StateStore("postgres", schema="dam_app", pg_pool=pool, retries=1), pool


def _base(n, **kw):
    return {"review_id": f"RS-LB-{n}", "dam_id": "D1", "subject_type": "alarm_event", "subject_id": f"E{n}",
            "review_level": "engineer", "decision": "acknowledge", "comment": "why", "reviewer_id": "uid",
            "reviewer_pe_license": None, "signed_ts": "2026-11-09T16:00:00.000000+00:00", "signature_hash": "a" * 64,
            "action_type": "alarm_ack", "reviewer_email": "e@x", "reviewer_sub": None, "original_value": "{}",
            "corrected_value": "{}", "actor_app_role": "engineer", "canon_version": 2, **kw}


def test_app_role_is_restricted_and_init_runs_no_owner_ddl(lakebase):
    su, app = lakebase
    role = app.split("user=")[-1]
    attrs = _psql(su, "-At", "-c", "SELECT rolsuper, rolreplication, rolbypassrls, "
                  f"pg_has_role(rolname, 'dam_lakebase_owner', 'MEMBER') FROM pg_roles WHERE rolname = '{role}'")
    assert attrs.strip() == "f|f|f|f"                                  # restricted: not owner, not superuser
    st, pool = _store(app)
    try:
        st2, pool2 = _store(app)                                       # second init: idempotent, still no DDL errors
        pool2.close()
        owner = st.execute("SELECT pg_get_userbyid(relowner) AS o FROM pg_class WHERE oid = 'dam_app.review_signoff'::regclass")
        assert owner[0]["o"] == "dam_lakebase_owner"
        mine = st.execute("SELECT pg_get_userbyid(relowner) AS o FROM pg_class "
                          "WHERE oid = 'dam_app.alarm_rule_flag_proposal'::regclass")
        assert mine[0]["o"] == role                   # app-created mutable table: app-owned
    finally:
        pool.close()


def test_owner_model_startup_runs_no_owner_ddl(lakebase, monkeypatch):
    """Under the owner model the app's own review_signoff ALTERs / CHECK / index / guard DDL are neither needed nor
    attempted: capture every statement of a full init as the app role."""
    _, app = lakebase
    seen = []
    orig = StateStore.execute

    def spy(self, sql, params=()):
        seen.append(sql)
        return orig(self, sql, params)
    monkeypatch.setattr(StateStore, "execute", spy)
    st, pool = _store(app)
    pool.close()
    ddl = [q for q in seen if q.lstrip().upper().startswith(("ALTER", "CREATE", "DROP", "GRANT", "REVOKE", "COMMENT"))]
    owner_objects = ("review_signoff", "exceedance_status", "manual_reading", "extract_watermark", "forbid_mutation",
                     "CREATE SCHEMA")
    assert not [q for q in ddl if any(o in q for o in owner_objects) and not q.lstrip().upper().startswith(
        "CREATE TABLE IF NOT EXISTS")], ddl
    assert not [q for q in ddl if "review_signoff" in q and not q.lstrip().upper().startswith("CREATE TABLE IF NOT EXISTS")]
    # the only trigger DDL is the app's own guard on its app-owned DSSMP outbox (v1.7), never an owner table
    assert {q.split(" ON ")[1].split()[0] for q in ddl if q.lstrip().upper().startswith(("CREATE TRIGGER", "CREATE OR REPLACE TRIGGER"))} <= {"{s}dssmp_registration_outbox"}


def test_append_verify_and_guards_as_app_role(lakebase):
    su, app = lakebase
    st, pool = _store(app)
    try:
        st.append_chained("D1", ac.GENESIS, ac.finalizer(_base(1)))
        st.append_chained("D1", ac.GENESIS, ac.finalizer(_base(2, actor_app_role="cdse,engineer")))
        rows = st.chain_rows("D1")
        assert [r["canon_version"] for r in rows] == [2, 2]
        assert ac.verify_chain(rows, st.chain_head("D1"))["ok"]
        for stmt in ("UPDATE dam_app.review_signoff SET comment = 'x'", "DELETE FROM dam_app.review_signoff",
                     "TRUNCATE dam_app.review_signoff", "ALTER TABLE dam_app.review_signoff DISABLE TRIGGER ALL",
                     "ALTER TABLE dam_app.review_signoff DROP CONSTRAINT ck_review_signoff_canon"):
            with pytest.raises(Exception) as e:
                st.execute(stmt)
            assert getattr(e.value, "sqlstate", None) in ("42501",), (stmt, e.value)   # insufficient_privilege
        row = ac.finalizer(_base(3))(3, rows[-1]["record_hash"])
        row["actor_app_role"] = None                                    # canon 2 without role: DB CHECK refuses
        cols = st.SIGNOFF_COLS
        with pytest.raises(Exception) as e:
            st.execute(f"INSERT INTO dam_app.review_signoff ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                       tuple(row.get(k) for k in cols))
        assert getattr(e.value, "sqlstate", None) == "23514"            # check_violation
        assert len(st.chain_rows("D1")) == 2
    finally:
        pool.close()


def test_mutable_app_tables_still_work_as_app_role(lakebase):
    """Proposal status, chain head, publications: app-owned mutable tables (the DA model), not INSERT-only."""
    _, app = lakebase
    st, pool = _store(app)
    try:
        row = {"proposal_id": "P1", "dam_id": "D1", "threshold_id": "T1", "base_threshold_version": 1,
               "base_flag": False, "new_flag": True, "proposed_by": "a@x", "proposed_by_id": "a",
               "proposed_at": "2026-11-09T16:00:00Z", "rationale": "r", "status": "pending"}
        with st.transaction() as q:
            st.insert_flag_proposal(q, row)
        with st.transaction() as q:
            st.decide_flag_proposal(q, "P1", "rejected", "c@x", "c", "2026-11-09T16:01:00Z", "no")
            st.insert_ref_publication(q, "PUB1", "ref.threshold.new_version", "D1", "P1", {"k": 1}, "c@x", "pending")
        assert st.flag_proposal("P1")["status"] == "rejected"
        assert st.ref_publications("D1")[0]["status"] == "pending"
        st.upsert_user("x@y", "X", False, "admin@x")
    finally:
        pool.close()


@pytest.mark.parametrize("sabotage,needle", [
    ("ALTER TABLE dam_app.review_signoff DROP CONSTRAINT ck_review_signoff_canon", "ck_review_signoff_canon False"),
    ("ALTER TABLE dam_app.review_signoff DISABLE TRIGGER t_notrunc_review_signoff", "truncate guard False"),
    ("ALTER TABLE dam_app.review_signoff DISABLE TRIGGER t_noupd_review_signoff", "row guard False"),
    ("ALTER TABLE dam_app.review_signoff DROP COLUMN actor_app_role", "actor_app_role"),
])
def test_init_fails_closed_when_owner_table_is_not_provisioned(lakebase, sabotage, needle):
    su, app = lakebase
    _psql(su, "-c", sabotage)
    with pytest.raises(RuntimeError, match="re-run lakebase/001_app_schema.sql") as e:
        _store(app)
    assert needle in str(e.value)
