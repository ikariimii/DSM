"""v1.7 residual (a): tamper-resistant Lakebase chain head. review_chain_head_log is append-only under
dam_lakebase_owner (guard triggers ENABLE ALWAYS; app SELECT + INSERT only); the extract takes the highest chain_seq per
chain over the log and the legacy head; the app change is tools/patches/v17_chain_head_log_state.patch (NOT applied
here: the live test applies it to a throw-away export of HEAD). Live parts need DAM_TEST_PG (PostgreSQL >= 15)."""
import os
import shutil
import subprocess
import sys
import tarfile
import io
import uuid

import re

import pytest

from gitrev import git_show_or_skip, require_git  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
import lakebase_extract as lx  # noqa: E402

DDL_PATH = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
VAL_PATH = os.path.join(ROOT, "lakebase", "validate_append_only.sql")
PATCH = os.path.join(ROOT, "tools", "patches", "v17_chain_head_log_state.patch")
MIG = os.path.join(ROOT, "lakebase", "migrations", "v1_7_006_chain_head_log.sql")
DOWN = os.path.join(ROOT, "lakebase", "migrations", "v1_7_006_chain_head_log_down.sql")
BLOCK = re.compile(r"-- BEGIN v1_7_006 block.*?-- END v1_7_006 block\n", re.S)
H = lambda c: c * 64  # noqa: E731


def test_extract_merges_log_and_legacy_heads():
    legacy = [{"chain_id": "A", "chain_seq": 5, "head_hash": H("a")},    # rewound legacy: log wins
              {"chain_id": "B", "chain_seq": 7, "head_hash": H("b")},    # unpatched app ahead: legacy wins
              {"chain_id": "C", "chain_seq": 2, "head_hash": H("e")}]    # same seq, different hash: log wins
    log = [{"chain_id": "A", "chain_seq": 9, "head_hash": H("9")}, {"chain_id": "B", "chain_seq": 6, "head_hash": H("6")},
           {"chain_id": "C", "chain_seq": 2, "head_hash": H("c")}, {"chain_id": "D", "chain_seq": 0, "head_hash": H("0")}]
    got = {h["chain_id"]: (h["chain_seq"], h["head_hash"]) for h in lx.current_heads(legacy, log)}
    assert got == {"A": (9, H("9")), "B": (7, H("b")), "C": (2, H("c")), "D": (0, H("0"))}
    assert lx.current_heads(legacy, []) == sorted(legacy, key=lambda h: h["chain_id"])
    assert "DISTINCT ON (chain_id)" in lx.HEAD_LOG_SQL and "chain_seq DESC" in lx.HEAD_LOG_SQL


def test_001_and_validator_guard_the_head_log():
    ddl = open(DDL_PATH).read()
    for s in ("CREATE TABLE IF NOT EXISTS review_chain_head_log", "PRIMARY KEY (chain_id, chain_seq)",
              "ALTER TABLE review_chain_head_log OWNER TO dam_lakebase_owner",
              "CREATE OR REPLACE TRIGGER t_noupd_review_chain_head_log BEFORE UPDATE OR DELETE",
              "CREATE OR REPLACE TRIGGER t_notrunc_review_chain_head_log BEFORE TRUNCATE",
              "ENABLE ALWAYS TRIGGER t_noupd_review_chain_head_log"):
        assert s in ddl, s
    assert "('review_chain_head_log')" in open(VAL_PATH).read()


def test_patch_applies_to_the_app_at_head_and_drops_update_of_the_head():
    r = subprocess.run(["git", "-C", ROOT, "apply", "--check", PATCH], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    txt = open(PATCH).read()
    state = txt[txt.index("+++ b/app/backend/repos/state.py"):txt.index("diff ", txt.index("+++ b/app/backend/repos/state.py"))]
    added = [l[1:] for l in state.splitlines() if l.startswith("+") and not l.startswith("+++")
             and not l[1:].lstrip().startswith("#")]
    code = "\n".join(added)
    assert "review_chain_head_log" in code and "pg_advisory_xact_lock" in code
    assert 'q("UPDATE {s}review_chain_head' not in code and '" FOR UPDATE"' not in code


# ---------------------------------------------------------------- live
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tag = uuid.uuid4().hex[:8]
    db, app = f"lbhead_{tag}", f"lbhead_app_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN").returncode == 0
    conn = f"{PG} dbname={db}"
    # an older deployment: legacy head already has a chain at seq 3 before 001 v1.7 runs -> seeded into the log
    assert _psql(conn, "-c", "CREATE SCHEMA dam_app", "-c", "CREATE TABLE dam_app.review_chain_head (chain_id TEXT "
                 "PRIMARY KEY, chain_seq INTEGER NOT NULL, head_hash TEXT NOT NULL, updated_ts TEXT NOT NULL)",
                 "-c", f"INSERT INTO dam_app.review_chain_head VALUES ('OLD', 3, '{H('3')}', 't')").returncode == 0
    opts = {"PGOPTIONS": f"-c dam.app_role={app}"}
    for _ in range(2):
        r = _psql(conn, "-f", DDL_PATH, env_extra=opts)
        assert r.returncode == 0, r.stderr
    yield {"conn": conn, "app": app, "as_app": f"{conn} user={app}", "opts": opts, "tmp": tmp_path_factory.mktemp("x")}
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}")


@live
def test_legacy_head_is_seeded_and_the_validator_passes(env):
    r = _psql(env["conn"], "-c", "SELECT chain_id || ':' || chain_seq FROM dam_app.review_chain_head_log")
    assert r.stdout.split() == ["OLD:3"]
    v = _psql(env["conn"], "-F", "|", "-f", VAL_PATH, env_extra=env["opts"])
    assert v.returncode == 0 and v.stdout.strip() == "", v.stdout + v.stderr


@live
@pytest.mark.parametrize("stmt", [
    "UPDATE dam_app.review_chain_head_log SET chain_seq = 1",                       # rewind
    "UPDATE dam_app.review_chain_head_log SET head_hash = '" + H("f") + "'",        # forge in place
    "DELETE FROM dam_app.review_chain_head_log",                                    # drop the head
    "TRUNCATE dam_app.review_chain_head_log",
    "ALTER TABLE dam_app.review_chain_head_log DISABLE TRIGGER ALL",
    "DROP TABLE dam_app.review_chain_head_log",
    "SET session_replication_role = replica; DELETE FROM dam_app.review_chain_head_log",
])
def test_app_role_cannot_rewind_forge_or_remove_a_head(env, stmt):
    r = _psql(env["as_app"], "-c", stmt)
    assert r.returncode != 0 and ("permission denied" in r.stderr or "must be owner" in r.stderr), r.stderr


@live
def test_even_the_owner_cannot_update_without_dropping_the_guard(env):
    r = _psql(env["conn"], "-c", "SET ROLE dam_lakebase_owner",
              "-c", "DELETE FROM dam_app.review_chain_head_log WHERE chain_id = 'OLD'")
    assert r.returncode != 0 and "append-only" in r.stderr


@live
def test_patched_app_appends_heads_and_reads_the_latest(env):
    """Apply the patch to an export of HEAD's app/ and drive the real StateStore as the app role."""
    dst = env["tmp"] / "app_export"
    require_git(ROOT, "`git archive HEAD app` (the patch is applied to HEAD's committed app/)")
    tar = subprocess.run(["git", "-C", ROOT, "archive", "HEAD", "app"], capture_output=True, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(tar)).extractall(dst, filter="data")
    r = subprocess.run(["git", "apply", PATCH], cwd=dst, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    code = f"""
import sys; sys.path.insert(0, {str(dst / 'app')!r})
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from backend.repos.state import StateStore
from backend import audit_chain as ac
pool = ConnectionPool({env['as_app']!r}, kwargs={{"row_factory": dict_row}}, open=True)
st = StateStore("postgres", schema="dam_app", pg_pool=pool)
base = dict(dam_id="D1", subject_type="incl_correction", subject_id="C-1", review_level="engineer",
            decision="approve", comment="ok", reviewer_id="e@x.org", reviewer_email="e@x.org",
            signed_ts="2026-11-09T15:00:00Z", signature_hash="x", action_type="bias_review",
            original_value="{{}}", corrected_value="{{}}")
import uuid
for i in range(3):
    st.append_chained("D1", ac.GENESIS, ac.finalizer({{**base, "review_id": "RS-" + uuid.uuid4().hex}}))
head = st.chain_head("D1")
v = ac.verify_chain(st.chain_rows("D1"), head)
old = st.chain_head("OLD")
print(head["chain_seq"], v["ok"], old["chain_seq"])
pool.close()
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if "No module named" in r.stderr and ("psycopg" in r.stderr or "fastapi" in r.stderr):
        pytest.skip(r.stderr.strip().splitlines()[-1])
    assert r.returncode == 0, r.stderr[-3000:]
    assert r.stdout.split() == ["3", "True", "3"], r.stdout
    out = _psql(env["conn"], "-c", "SELECT string_agg(chain_seq::text, ',' ORDER BY chain_seq) FROM "
                                   "dam_app.review_chain_head_log WHERE chain_id = 'D1'").stdout.strip()
    assert out == "0,1,2,3"                                           # genesis + one row per append, none updated


def test_migration_block_is_identical_to_001_and_down_drops_nothing():
    mig, ddl, down = open(MIG).read(), open(DDL_PATH).read(), open(DOWN).read()
    assert BLOCK.search(mig).group(0) == BLOCK.search(ddl).group(0)
    code = "\n".join(l.split("--")[0] for l in down.splitlines()).upper()
    for bad in ("DROP ", "DELETE ", "TRUNCATE", "UPDATE ", "DISABLE TRIGGER"):
        assert bad not in code, bad


@live
def test_upgrade_by_migration_rollback_keeps_rows_and_reapply(tmp_path):
    old_sql = git_show_or_skip(ROOT, "ea66581", "lakebase/001_app_schema.sql")
    tag = uuid.uuid4().hex[:8]
    db, app = f"lbh6_{tag}", f"lbh6_app_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN").returncode == 0
    conn, opts = f"{PG} dbname={db}", {"PGOPTIONS": f"-c dam.app_role={app}"}
    try:
        old = tmp_path / "001_ea66581.sql"                       # the last 001 without the head log
        old.write_text(old_sql)
        assert _psql(conn, "-f", str(old), env_extra=opts).returncode == 0
        assert _psql(conn, "-c", f"INSERT INTO dam_app.review_chain_head VALUES ('OLD', 4, '{H('4')}', 't')"
                     ).returncode == 0
        for _ in range(2):
            r = _psql(conn, "-f", MIG, env_extra=opts)
            assert r.returncode == 0, r.stderr
        v = _psql(conn, "-f", VAL_PATH, env_extra=opts)
        assert v.returncode == 0 and v.stdout.strip() == "", v.stdout + v.stderr
        ins = f"INSERT INTO dam_app.review_chain_head_log VALUES ('OLD', 5, '{H('5')}', 't')"
        assert _psql(f"{conn} user={app}", "-c", ins).returncode == 0
        assert _psql(f"{conn} user={app}", "-c", "UPDATE dam_app.review_chain_head_log SET chain_seq = 9"
                     ).returncode != 0
        for _ in range(2):
            r = _psql(conn, "-f", DOWN, env_extra=opts)
            assert r.returncode == 0, r.stderr
        rows = _psql(conn, "-c", "SELECT string_agg(chain_seq::text, ',' ORDER BY chain_seq) FROM "
                                 "dam_app.review_chain_head_log").stdout.strip()
        assert rows == "4,5"                                          # seeded + appended rows kept
        assert _psql(f"{conn} user={app}", "-c", ins.replace("5,", "6,").replace(H("5"), H("6"))).returncode != 0
        assert _psql(f"{conn} user={app}", "-c", "SELECT count(*) FROM dam_app.review_chain_head_log").returncode == 0
        assert _psql(conn, "-f", MIG, env_extra=opts).returncode == 0
        assert _psql(f"{conn} user={app}", "-c", ins.replace("5,", "6,").replace(H("5"), H("6"))).returncode == 0
        assert _psql(conn, "-f", DDL_PATH, env_extra=opts).returncode == 0
        v = _psql(conn, "-f", VAL_PATH, env_extra=opts)
        assert v.returncode == 0 and v.stdout.strip() == "", v.stdout + v.stderr
    finally:
        _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}")
