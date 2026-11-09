"""QA-045b: the app's Postgres append-only guard refuses TRUNCATE as well as UPDATE/DELETE, on a REAL PostgreSQL.

Row-level triggers do not fire on TRUNCATE; PostgreSQL allows TRUNCATE triggers only FOR EACH STATEMENT
(PG 17 docs, CREATE TRIGGER). Runs only when DAM_TEST_PG (libpq conninfo) is set, e.g.
    DAM_TEST_PG="host=/tmp port=55432 user=postgres dbname=postgres" pytest app/tests/test_state_pg_append_only.py
Each test uses its own throwaway schema, dropped afterwards."""
import os
import uuid

import pytest

from backend import audit_chain as ac
from backend.repos.state import APPEND_ONLY, StateStore

PG = os.environ.get("DAM_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="set DAM_TEST_PG (libpq conninfo) to run against a real PostgreSQL")


@pytest.fixture
def pg_store():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    schema = f"qa045b_{uuid.uuid4().hex[:10]}"
    pool = ConnectionPool(PG, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)
    st = StateStore("postgres", schema=schema, pg_pool=pool, retries=1)
    try:
        yield st, pool, psycopg
    finally:
        with pool.connection() as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pool.close()


def _row(n):
    return {"review_id": f"RS-{n}", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": f"E{n}",
            "review_level": "engineer", "decision": "acknowledge", "comment": "rationale", "reviewer_id": "uid-1",
            "reviewer_pe_license": None, "signed_ts": "2026-11-02T00:00:00.000000+00:00", "signature_hash": "c" * 64,
            "action_type": "alarm_ack", "reviewer_email": "a@x", "reviewer_sub": None, "original_value": "{}",
            "corrected_value": "{}"}


@pytest.mark.parametrize("stmt", ["TRUNCATE {s}review_signoff",
                                  "UPDATE {s}review_signoff SET comment = 'x'",
                                  "DELETE FROM {s}review_signoff"])
def test_review_signoff_refuses_truncate_update_delete(pg_store, stmt):
    st, _, psycopg = pg_store
    st.append_chained("CRD", ac.GENESIS, ac.finalizer(_row(0)))
    with pytest.raises(psycopg.errors.RaiseException, match="review_signoff is append-only"):
        st.execute(stmt)
    assert len(st.execute("SELECT * FROM {s}review_signoff")) == 1
    assert ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"))["ok"]


def test_guard_init_is_idempotent_and_keeps_one_trigger_each(pg_store):
    st, _, psycopg = pg_store
    st.init_schema()          # second run (app restart) must not fail or duplicate triggers
    st._append_only_guards()  # third run
    for t in APPEND_ONLY:
        rows = st.execute(
            "SELECT tgname, tgtype FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = %s::regclass "
            "AND tgname LIKE 't_app_%%' ORDER BY tgname", (f'"{st.schema}".{t}',))
        names = [r["tgname"] for r in rows]
        assert names == sorted([f"t_app_noupd_{t}", f"t_app_notrunc_{t}"])
        trunc = next(r["tgtype"] for r in rows if r["tgname"] == f"t_app_notrunc_{t}")
        assert trunc & 32 and not trunc & 1  # TRIGGER_TYPE_TRUNCATE, FOR EACH STATEMENT (not ROW)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        st.execute("TRUNCATE {s}review_signoff")
