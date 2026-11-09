"""QA-045c: concurrent Postgres init under pg_advisory_xact_lock creates one clean schema.

Requires DAM_TEST_PG. Spawns parallel StateStore('postgres') constructors against distinct schemas
(fresh) and repeated re-inits against one shared schema.
"""
from __future__ import annotations

import os
import uuid
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

PG = os.environ.get("DAM_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="set DAM_TEST_PG for live PG")


@pytest.fixture
def pool():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    p = ConnectionPool(PG, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
    yield p, psycopg
    p.close()


def test_parallel_fresh_inits_succeed_without_duplicate_heads(pool):
    from backend.repos.state import StateStore
    p, psycopg = pool
    schemas = [f"qa045c_{uuid.uuid4().hex[:8]}_{i}" for i in range(4)]

    def one(schema):
        st = StateStore("postgres", schema=schema, pg_pool=p, retries=1)
        heads = st.execute("SELECT * FROM {s}review_chain_head")
        signoffs = st.execute("SELECT count(*) AS n FROM {s}review_signoff")
        return schema, len(heads), signoffs[0]["n"], st

    errs = []
    stores = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(one, s) for s in schemas]
        for f in as_completed(futs):
            try:
                schema, n_heads, n_so, st = f.result()
                stores.append((schema, st))
                assert n_heads == 0  # no genesis until first append
                assert n_so == 0
            except Exception as e:  # noqa: BLE001
                errs.append(e)
    assert errs == [], errs
    # Drop schemas
    with p.connection() as conn:
        for schema, _ in stores:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_parallel_reinits_of_same_schema_are_idempotent(pool):
    from backend.repos.state import StateStore, INIT_LOCK_KEY
    p, psycopg = pool
    schema = f"qa045c_re_{uuid.uuid4().hex[:8]}"
    st0 = StateStore("postgres", schema=schema, pg_pool=p, retries=1)

    def reinit():
        st = StateStore("postgres", schema=schema, pg_pool=p, retries=1)
        # triggers present once each
        rows = st.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = %s::regclass "
            "AND tgname LIKE 't_app_%%' ORDER BY tgname",
            (f'"{schema}".review_signoff',))
        return [r["tgname"] for r in rows]

    out = []
    errs = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(reinit) for _ in range(4)]):
            try:
                out.append(f.result())
            except Exception as e:  # noqa: BLE001
                errs.append(e)
    assert errs == [], errs
    assert all(names == sorted(["t_app_noupd_review_signoff", "t_app_notrunc_review_signoff"]) for names in out)
    # lock key constant matches lakebase
    assert INIT_LOCK_KEY == 4702045
    with p.connection() as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_parallel_first_append_creates_single_head(pool):
    """Race on first append_chained: exactly one head row, contiguous seq, verify ok."""
    from backend import audit_chain as ac
    from backend.repos.state import StateStore
    p, psycopg = pool
    schema = f"qa045c_ap_{uuid.uuid4().hex[:8]}"
    # init once
    StateStore("postgres", schema=schema, pg_pool=p, retries=1)

    def append_one(i):
        st = StateStore("postgres", schema=schema, pg_pool=p, retries=1)
        base = {"review_id": f"RS-p{i}-{uuid.uuid4().hex[:6]}", "dam_id": "CRD", "subject_type": "alarm_event",
                "subject_id": f"E{i}", "review_level": "engineer", "decision": "acknowledge", "comment": "c",
                "reviewer_id": "u", "reviewer_pe_license": None, "signed_ts": "2026-11-02T00:00:00.000000+00:00",
                "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
                "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}"}
        return st.append_chained("CRD", ac.GENESIS, ac.finalizer(base))

    errs = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(append_one, i) for i in range(4)]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))
    st = StateStore("postgres", schema=schema, pg_pool=p, retries=1)
    heads = st.execute("SELECT * FROM {s}review_chain_head WHERE chain_id = %s", ("CRD",))
    rows = st.execute("SELECT chain_seq FROM {s}review_signoff WHERE chain_id = %s ORDER BY chain_seq", ("CRD",))
    with p.connection() as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    assert errs == [], errs
    assert len(heads) == 1
    assert [r["chain_seq"] for r in rows] == list(range(1, 5))
    assert heads[0]["chain_seq"] == 4
