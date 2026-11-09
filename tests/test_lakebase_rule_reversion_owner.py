"""App Dev follow-up 1 (v1_7_010): dam_app.rule_reversion_proposal owned by dam_lakebase_owner. The app role keeps
SELECT, INSERT and UPDATE of exactly the columns its code updates (app/backend/routers/rule_reversion.py approve /
reject -> StateStore.decide_pending):
    status, decided_by, decided_by_id, decided_at, decision_rationale, pub_ids
status moves stay guarded by t_status_rule_reversion_proposal (v1_7_009, ENABLE ALWAYS). Live PostgreSQL, as the app
role: the allowed columns update, every other column / DELETE / TRUNCATE / disabling or dropping the trigger / DDL is
refused, and the app's real init_schema + insert_row + decide_pending still work. Upgrade from the app-owned layout
(001@c7bca54), rollback (ownership back to the app, nothing dropped) and the validator checks."""
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

from gitrev import git_show_or_skip  # noqa: E402
from lbmig import later_than  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
VAL = os.path.join(ROOT, "lakebase", "validate_append_only.sql")
MIG = os.path.join(ROOT, "lakebase", "migrations", "v1_7_010_rule_reversion_owner.sql")
DOWN = os.path.join(ROOT, "lakebase", "migrations", "v1_7_010_rule_reversion_owner_down.sql")
T = "dam_app.rule_reversion_proposal"
APP_UPDATE_COLS = ("status", "decided_by", "decided_by_id", "decided_at", "decision_rationale", "pub_ids")
OTHER_COLS = ("proposal_id", "dam_id", "to_dssmp_version_id", "rules", "proposed_by", "proposed_by_id", "proposed_at",
              "rationale")


def _block(path):
    s = open(path).read()
    return s[s.index("-- BEGIN v1_7_010 block"):s.index("-- END v1_7_010 block")]


def test_001_and_the_migration_carry_the_same_block_and_the_down_drops_nothing():
    blk = _block(DDL)
    assert _block(MIG) == blk
    assert ("GRANT UPDATE (" + ", ".join(APP_UPDATE_COLS) + ") '\n                   'ON dam_app.rule_reversion_proposal") in blk
    assert "ALTER TABLE dam_app.rule_reversion_proposal OWNER TO dam_lakebase_owner;" in blk
    assert "ENABLE ALWAYS TRIGGER t_status_rule_reversion_proposal" in blk
    assert "'rule_reversion_proposal')" in open(DDL).read().split("-- BEGIN v1_7_010 block")[0]   # not re-owned by app
    code = "\n".join(l for l in open(DOWN).read().splitlines() if not l.lstrip().startswith("--"))
    assert not re.search(r"\bDROP\b|\bDELETE\b|\bTRUNCATE\b|\bUPDATE\b|\bDISABLE\b", code)


def test_the_grant_list_is_exactly_what_the_app_code_updates():
    """Derive the columns from the app source: every decide_pending(... 'rule_reversion_proposal' ...) field dict."""
    src = open(os.path.join(ROOT, "app", "backend", "routers", "rule_reversion.py")).read()
    found = set()
    for m in re.finditer(r'decide_pending\(q, "rule_reversion_proposal", "proposal_id", proposal_id, \{(.*?)\}\)',
                         src, re.S):
        found |= set(re.findall(r'"(\w+)":', m.group(1)))
    assert found == set(APP_UPDATE_COLS), found
    st = open(os.path.join(ROOT, "app", "backend", "repos", "state.py")).read()
    assert not re.search(r"UPDATE \{s\}rule_reversion_proposal", st)           # no other UPDATE path in the app
    for base, _, names in os.walk(os.path.join(ROOT, "app", "backend")):
        for n in names:
            if n.endswith(".py"):
                text = open(os.path.join(base, n)).read()
                assert not re.search(r"(?i)(UPDATE|DELETE FROM)\s+\{s\}rule_reversion_proposal", text), n


PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture()
def env():
    tag = uuid.uuid4().hex[:8]
    db, app, pub = f"lbrr_{tag}", f"lbrr_app_{tag}", f"lbrr_pub_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN",
                 "-c", f"CREATE ROLE {pub} LOGIN").returncode == 0
    e = {"su": f"{PG} dbname={db}", "app": f"{PG} dbname={db} user={app}", "app_role": app,
         "opts": {"PGOPTIONS": f"-c dam.app_role={app} -c dam.publisher_login={pub}"}}
    yield e
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}", "-c", f"DROP ROLE IF EXISTS {pub}")


def _owner(e):
    return _psql(e["su"], "-c", f"SELECT relowner::regrole::text FROM pg_class WHERE oid = '{T}'::regclass").stdout.strip()


def _val(e):
    r = _psql(e["su"], "-f", VAL, env_extra=e["opts"])
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _ins(e, pid, conn="app"):
    return _psql(e[conn], "-c", f"INSERT INTO {T} (proposal_id, dam_id, to_dssmp_version_id, rules, proposed_by, "
                                f"proposed_at, rationale, status) VALUES ('{pid}', 'D-{pid}', 'DS-2', '[]', 'p', 't', "
                                "'r', 'pending')")


@live
def test_fresh_001_app_role_privileges(env):
    r = _psql(env["su"], "-f", DDL, env_extra=env["opts"])
    assert r.returncode == 0, r.stderr
    assert _owner(env) == "dam_lakebase_owner" and _val(env) == ""
    assert _ins(env, "P1").returncode == 0
    for c in APP_UPDATE_COLS:                                          # each allowed column, on a pending row
        v = "'pending'" if c == "status" else "'x'"
        r = _psql(env["app"], "-c", f"UPDATE {T} SET {c} = {v} WHERE proposal_id = 'P1' AND status = 'pending'")
        assert r.returncode == 0, (c, r.stderr)
    for c in OTHER_COLS:
        r = _psql(env["app"], "-c", f"UPDATE {T} SET {c} = {c} WHERE proposal_id = 'P1'")
        assert r.returncode != 0 and "permission denied" in r.stderr, c
    for stmt, err in ((f"DELETE FROM {T}", "permission denied"), (f"TRUNCATE {T}", "permission denied"),
                      (f"ALTER TABLE {T} DISABLE TRIGGER t_status_rule_reversion_proposal", "must be owner"),
                      (f"ALTER TABLE {T} DISABLE TRIGGER ALL", "must be owner"),
                      (f"DROP TRIGGER t_status_rule_reversion_proposal ON {T}", "must be owner"),
                      (f"ALTER TABLE {T} ADD COLUMN x TEXT", "must be owner"),
                      (f"ALTER TABLE {T} OWNER TO CURRENT_USER", "must be owner"),
                      (f"DROP TABLE {T}", "must be owner"),
                      (f"CREATE TRIGGER x BEFORE UPDATE ON {T} FOR EACH ROW EXECUTE FUNCTION "
                       "dam_app.status_transition_guard()", "permission denied"),
                      ("SET session_replication_role = replica", "permission denied")):
        r = _psql(env["app"], "-c", stmt)
        assert r.returncode != 0 and err in r.stderr, (stmt, r.stderr)
    r = _psql(env["app"], "-c", f"GRANT UPDATE ON {T} TO PUBLIC")       # no grant option: a no-op
    assert "no privileges were granted" in r.stderr
    assert _psql(env["su"], "-c", f"SELECT has_table_privilege('public', '{T}', 'UPDATE')").stdout.strip() == "f"
    # status stays guarded by the transition trigger
    assert _psql(env["app"], "-c", f"UPDATE {T} SET status = 'applied' WHERE proposal_id = 'P1'").returncode == 0
    r = _psql(env["app"], "-c", f"UPDATE {T} SET status = 'pending' WHERE proposal_id = 'P1'")
    assert r.returncode != 0 and "status transition" in r.stderr
    r = _psql(env["app"], "-c", f"UPDATE {T} SET decision_rationale = 'rewritten' WHERE proposal_id = 'P1'")
    assert r.returncode != 0 and "is final" in r.stderr
    assert _psql(env["su"], "-c", f"SELECT count(*) FROM {T}").stdout.strip() == "1"


def _app_real_statements(e):
    code = f"""
import sys, json; sys.path.insert(0, {os.path.join(ROOT, 'app')!r})
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from backend.repos.state import StateStore
pool = ConnectionPool({e['app']!r}, kwargs={{"row_factory": dict_row}}, open=True)
st = StateStore("postgres", schema="dam_app", pg_pool=pool)
st.init_schema()
row = lambda pid, dam: {{"proposal_id": pid, "dam_id": dam, "to_dssmp_version_id": "DS-2", "rules": "[]",
    "proposed_by": "a@x.org", "proposed_by_id": None, "proposed_at": "2026-11-09T19:00:00Z", "rationale": "r",
    "status": "pending", "decided_by": None, "decided_by_id": None, "decided_at": None, "decision_rationale": None,
    "pub_ids": None}}
with st.transaction() as q:
    st.insert_row(q, "rule_reversion_proposal", row("RRV-A", "DA"))
    st.insert_row(q, "rule_reversion_proposal", row("RRV-R", "DR"))
with st.transaction() as q:        # approve (routers/rule_reversion.py approve -> also_row)
    st.decide_pending(q, "rule_reversion_proposal", "proposal_id", "RRV-A", {{
        "status": "applied", "decided_by": "cdse@x.org", "decided_by_id": "u1",
        "decided_at": "2026-11-09T19:01:00Z", "decision_rationale": "ok", "pub_ids": json.dumps(["PUB-1"])}})
with st.transaction() as q:        # reject
    st.decide_pending(q, "rule_reversion_proposal", "proposal_id", "RRV-R", {{
        "status": "rejected", "decided_by": "cdse@x.org", "decided_by_id": "u1",
        "decided_at": "2026-11-09T19:02:00Z", "decision_rationale": "no"}})
try:
    with st.transaction() as q:    # deciding again: 0 rows -> RuntimeError (no trigger error, no privilege error)
        st.decide_pending(q, "rule_reversion_proposal", "proposal_id", "RRV-A", {{"status": "rejected"}})
    print("SECOND-DECISION-PASSED")
except RuntimeError as e:
    print("second:", e)
print(json.dumps([dict(r) for r in st.rows("rule_reversion_proposal", "proposal_id")], default=str))
pool.close()
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if "No module named" in r.stderr:
        pytest.skip(r.stderr.strip().splitlines()[-1])
    assert r.returncode == 0, r.stderr[-3000:]
    return r.stdout


@live
def test_the_apps_real_init_insert_and_decide_pending_still_work(env):
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0
    out = _app_real_statements(env)
    assert "second: RRV-A is no longer pending" in out and "SECOND-DECISION-PASSED" not in out
    assert '"proposal_id": "RRV-A"' in out and '"status": "applied"' in out and '"pub_ids": "[\\"PUB-1\\"]"' in out
    assert '"status": "rejected"' in out
    assert _owner(env) == "dam_lakebase_owner" and _val(env) == ""       # init_schema changed nothing


@live
def test_upgrade_from_the_app_owned_layout_rollback_and_validator(env, tmp_path):
    old = tmp_path / "001_c7bca54.sql"
    old.write_text(git_show_or_skip(ROOT, "c7bca54", "lakebase/001_app_schema.sql"))
    assert _psql(env["su"], "-f", str(old), env_extra=env["opts"]).returncode == 0
    assert _owner(env) == env["app_role"] and _ins(env, "OLD").returncode == 0
    assert f"owner_not_dam_lakebase_owner|dam_app.rule_reversion_proposal owned by {env['app_role']}" in _val(env)                                 # the new validator flags the old layout
    r = _psql(env["su"], "-f", MIG)                                      # no dam.app_role: refused, nothing changed
    assert r.returncode != 0 and "SET dam.app_role" in r.stderr and _owner(env) == env["app_role"]
    for _ in range(2):
        r = _psql(env["su"], "-f", MIG, env_extra=env["opts"])
        assert r.returncode == 0, r.stderr
    for m in later_than("v1_7_010"):                                     # then up to date (v1_7_011, v1_8_001, ...)
        assert _psql(env["su"], "-f", m, env_extra=env["opts"]).returncode == 0, m
    assert _owner(env) == "dam_lakebase_owner" and _val(env) == ""
    assert _psql(env["su"], "-c", f"SELECT count(*) FROM {T}").stdout.strip() == "1"
    assert _psql(env["app"], "-c", f"DELETE FROM {T}").returncode != 0
    # validator: an extra column grant or ownership back to the app is reported
    assert _psql(env["su"], "-c", f"GRANT UPDATE (rules) ON {T} TO {env['app_role']}").returncode == 0
    assert "app_workflow_privilege|" + env["app_role"] + " has column UPDATE (rules)" in _val(env)
    assert _psql(env["su"], "-c", f"REVOKE UPDATE (rules) ON {T} FROM {env['app_role']}").returncode == 0
    assert _psql(env["su"], "-c", f"REVOKE UPDATE (pub_ids) ON {T} FROM {env['app_role']}").returncode == 0
    assert "lacks UPDATE on dam_app.rule_reversion_proposal" in _val(env)
    # rollback twice: ownership back to the app, rows and trigger kept
    assert _psql(env["su"], "-f", DOWN).returncode != 0                  # needs dam.app_role
    for _ in range(2):
        r = _psql(env["su"], "-f", DOWN, env_extra=env["opts"])
        assert r.returncode == 0, r.stderr
    assert _owner(env) == env["app_role"]
    assert _psql(env["su"], "-c", f"SELECT count(*) FROM {T}").stdout.strip() == "1"
    assert _psql(env["su"], "-c", "SELECT tgenabled FROM pg_trigger WHERE tgname = 't_status_rule_reversion_proposal'"
                 ).stdout.strip() == "A"
    for _ in range(2):
        assert _psql(env["su"], "-f", MIG, env_extra=env["opts"]).returncode == 0
    assert _owner(env) == "dam_lakebase_owner" and _val(env) == ""
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0 and _val(env) == ""


GUARDED = ("exceedance_status", "review_signoff", "manual_reading", "review_chain_head_log", "ref_change_publication",
           "onboarding_publication", "dssmp_registration_outbox", "dam_cdse_outbox", "rule_reversion_proposal")


@live
@pytest.mark.parametrize("table", GUARDED)
def test_qa058_an_owner_change_alone_is_reported_while_every_trigger_stays_enabled(env, table):
    """QA-058: change ONLY pg_class.relowner (to the app role, then to a bystander role); every trigger stays
    ENABLE ALWAYS, no privilege changes - the validator must still fail with owner_not_dam_lakebase_owner."""
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0 and _val(env) == ""
    trig = lambda: _psql(env["su"], "-c", f"SELECT string_agg(tgname || '=' || tgenabled, ',' ORDER BY tgname) FROM "
                                          f"pg_trigger WHERE tgrelid = 'dam_app.{table}'::regclass "
                                          "AND NOT tgisinternal").stdout.strip()
    before = trig()
    other = "qa058_" + uuid.uuid4().hex[:6]
    assert _psql(env["su"], "-c", f"CREATE ROLE {other} NOLOGIN").returncode == 0
    try:
        for new_owner in (env["app_role"], other):
            assert _psql(env["su"], "-c", f"ALTER TABLE dam_app.{table} OWNER TO {new_owner}").returncode == 0
            assert trig() == before and all(x.endswith("=A") for x in before.split(",") if x)
            out = _val(env)
            assert f"owner_not_dam_lakebase_owner|dam_app.{table} owned by {new_owner}" in out, out
            assert "status_guard_missing" not in out
        assert _psql(env["su"], "-c", f"ALTER TABLE dam_app.{table} OWNER TO dam_lakebase_owner").returncode == 0
    finally:
        _psql(env["su"], "-c", f"REASSIGN OWNED BY {other} TO dam_lakebase_owner", "-c", f"DROP OWNED BY {other}")
        _psql(PG, "-c", f"DROP ROLE IF EXISTS {other}")


@live
def test_qa058_a_new_trigger_protected_table_must_be_owner_owned(env):
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0
    assert _psql(env["su"], "-c", f"CREATE TABLE dam_app.qa058_new (id int, status text)", "-c",
                 f"ALTER TABLE dam_app.qa058_new OWNER TO {env['app_role']}", "-c",
                 "CREATE TRIGGER t_status_qa058_new BEFORE UPDATE ON dam_app.qa058_new FOR EACH ROW EXECUTE FUNCTION "
                 "dam_app.status_transition_guard('done')").returncode == 0
    assert f"owner_not_dam_lakebase_owner|dam_app.qa058_new owned by {env['app_role']}" in _val(env)
