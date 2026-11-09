"""Live check of the proposed state.py patch (run from a patched or unpatched app copy). Prints one line per probe."""
import os, subprocess, sys, uuid
APP_ROOT, LB = sys.argv[1], sys.argv[2]
sys.path.insert(0, APP_ROOT)
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from backend.repos.state import StateStore
from backend import audit_chain as ac
PG = "host=/tmp port=55432 user=postgres"
tag = uuid.uuid4().hex[:6]; db, app = f"pchk_{tag}", f"pchk_app_{tag}"
psql = lambda conn, *a, env=None: subprocess.run(["psql", conn, "-X", "-q", "-v", "ON_ERROR_STOP=1", *a], capture_output=True, text=True, env=dict(os.environ, **(env or {})))
assert psql(PG + " dbname=postgres", "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN").returncode == 0
conn = f"{PG} dbname={db}"
try:
    r = psql(conn, "-f", LB, env={"PGOPTIONS": f"-c dam.app_role={app}"}); assert r.returncode == 0, r.stderr
    def store():
        pool = ConnectionPool(f"{conn} user={app}", min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)
        return StateStore("postgres", schema="dam_app", pg_pool=pool, retries=1)
    try:
        st = store(); print("app init as app role after 001: OK")
        base = {"review_id": "rs-1", "dam_id": "D1", "subject_type": "alarm_event", "subject_id": "e1", "review_level": "engineer",
                "decision": "acknowledge", "comment": "why", "reviewer_id": "e@x", "reviewer_pe_license": None,
                "signed_ts": "2026-11-09T15:00:00.000000+00:00", "signature_hash": "a" * 64, "action_type": "alarm_ack",
                "reviewer_email": "e@x", "reviewer_sub": "s", "original_value": "{}", "corrected_value": "{}"}
        st.append_chained("D1", ac.GENESIS, ac.finalizer(base))
        v = ac.verify_chain(st.chain_rows("D1"), st.chain_head("D1"))
        print("append + verify as app role:", v["ok"])
        st2 = store(); print("second init (idempotent): OK")
    except Exception as e:
        print("app init/append FAILED:", type(e).__name__, str(e).splitlines()[0][:160])
    psql(conn, "-c", "ALTER TABLE dam_app.review_signoff DISABLE TRIGGER t_notrunc_review_signoff")
    try:
        store(); print("init with a DISABLED guard: accepted (BAD)")
    except Exception as e:
        print("init with a DISABLED guard: refused:", type(e).__name__, str(e)[:120])
finally:
    psql(PG + " dbname=postgres", "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)", "-c", f"DROP ROLE IF EXISTS {app}")
