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


# ------------------------------------------------------------------ QA-045c: race-free init (one locked transaction)
def _concurrent_inits(schema, n, hammer_rounds=0):
    """n StateStores (own pools, as n app replicas) init the same schema at once while, optionally, an attacker
    connection keeps trying UPDATE / DELETE / TRUNCATE on review_signoff. Returns (init errors, mutations through)."""
    import threading

    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    errors, through, stop = [], [], threading.Event()
    start = threading.Barrier(n + (1 if hammer_rounds else 0))
    pools = [ConnectionPool(PG, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True) for _ in range(n)]

    def init(pool):
        start.wait()
        try:
            StateStore("postgres", schema=schema, pg_pool=pool, retries=1)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def hammer():
        with psycopg.connect(PG, autocommit=True) as conn:
            start.wait()
            for i in range(hammer_rounds):
                for stmt in (f'UPDATE "{schema}".review_signoff SET comment = \'tampered\'',
                             f'DELETE FROM "{schema}".review_signoff', f'TRUNCATE "{schema}".review_signoff'):
                    try:
                        before = conn.execute(f'SELECT count(*) FROM "{schema}".review_signoff').fetchone()[0]
                        cur = conn.execute(stmt)
                        if stmt.startswith("TRUNCATE") and before or cur.rowcount and cur.rowcount > 0:
                            through.append((i, stmt))
                    except psycopg.Error:
                        pass   # refused by the guard (or the table is being created): not a mutation
                if stop.is_set() and i > 20:
                    break

    threads = [threading.Thread(target=init, args=(p,)) for p in pools]
    if hammer_rounds:
        threads.append(threading.Thread(target=hammer))
    for t in threads:
        t.start()
    for t in threads[:n]:
        t.join(120)
    stop.set()
    for t in threads[n:]:
        t.join(120)
    for p in pools:
        p.close()
    return errors, through


def _drop(schema):
    import psycopg
    with psycopg.connect(PG, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_qa045c_concurrent_inits_on_fresh_db_have_no_errors():
    pytest.importorskip("psycopg")
    schema = f"qa045c_{uuid.uuid4().hex[:10]}"
    try:
        errors, _ = _concurrent_inits(schema, 8)
        assert errors == []
        import psycopg
        with psycopg.connect(PG) as conn:
            names = sorted(r[0] for r in conn.execute(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = %s::regclass",
                (f'"{schema}".review_signoff',)))
        assert names == ["t_app_notrunc_review_signoff", "t_app_noupd_review_signoff"]
    finally:
        _drop(schema)


def test_qa045c_concurrent_reinits_never_open_an_unguarded_window(pg_store):
    """Existing DB with a chained record: 8 concurrent re-inits (CREATE OR REPLACE TRIGGER inside one transaction
    under pg_advisory_xact_lock) while an attacker hammers UPDATE / DELETE / TRUNCATE: 0 errors, 0 mutations."""
    st, _, _ = pg_store
    st.append_chained("CRD", ac.GENESIS, ac.finalizer(_row(0)))
    for _ in range(3):
        errors, through = _concurrent_inits(st.schema, 8, hammer_rounds=200)
        assert errors == [] and through == []
    rows = st.chain_rows("CRD")
    assert len(rows) == 1 and rows[0]["comment"] == "rationale"
    assert ac.verify_chain(rows, st.chain_head("CRD"))["ok"]


def test_qa045c_init_takes_the_shared_advisory_lock(pg_store):
    """While another session holds pg_advisory_xact_lock(4702045) (e.g. lakebase/001), the app init waits."""
    import threading
    import time

    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from backend.repos.state import INIT_LOCK_KEY
    st, _, _ = pg_store
    assert INIT_LOCK_KEY == 4702045
    done = threading.Event()
    with psycopg.connect(PG) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (INIT_LOCK_KEY,))
        pool = ConnectionPool(PG, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)
        t = threading.Thread(target=lambda: (StateStore("postgres", schema=st.schema, pg_pool=pool, retries=1),
                                             done.set()))
        t.start()
        time.sleep(1.0)
        assert not done.is_set()          # blocked on the shared lock
        holder.commit()                   # releases the xact lock
    t.join(30)
    pool.close()
    assert done.is_set()


def test_rule_flag_proposal_and_publication_commit_with_the_chain_record(pg_store):
    """Live-dam cdse_ack_required edits on real Postgres: proposal + decision + outbox row commit atomically with the
    chained record; a second decision of the same proposal fails inside the transaction and writes nothing."""
    st, _, _ = pg_store
    row = {"proposal_id": "RFP-1", "dam_id": "CRD", "threshold_id": "T1", "base_threshold_version": 1,
           "base_flag": False, "new_flag": True, "proposed_by": "a@x", "proposed_by_id": "uid-a", "proposed_at": "t0",
           "rationale": "why", "status": "pending"}
    st.append_chained("CRD", ac.GENESIS, ac.finalizer(_row(1)), lambda q: st.insert_flag_proposal(q, row))

    def decide(q):
        st.decide_flag_proposal(q, "RFP-1", "applied", "cdse@x", "uid-c", "t1", "ok", "PUB-1", 2)
        st.insert_ref_publication(q, "PUB-1", "ref.threshold.new_version", "CRD", "RFP-1", {"insert": {"v": 2}},
                                  "cdse@x", "pending")
    st.append_chained("CRD", ac.GENESIS, ac.finalizer(_row(2)), decide)
    p = st.flag_proposal("RFP-1")
    assert (p["status"], p["base_flag"], p["new_flag"], p["new_threshold_version"]) == ("applied", False, True, 2)
    assert st.ref_publications("CRD")[0]["payload"] == {"insert": {"v": 2}}
    with pytest.raises(ValueError, match="no longer pending"):
        st.append_chained("CRD", ac.GENESIS, ac.finalizer(_row(3)), decide)
    assert len(st.chain_rows("CRD")) == 2 and len(st.ref_publications("CRD")) == 1
    assert ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"))["ok"]
