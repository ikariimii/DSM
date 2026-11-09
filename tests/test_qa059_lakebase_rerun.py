"""QA-059 (push blocker): re-running an older Lakebase migration must never undo a later hardening. QA reproduced it:
v1_7_005 re-run after v1_7_010 handed dam_app.rule_reversion_proposal back to the app role, which could then DISABLE
its status trigger. Fixed in three layers, all tested here:
  1. the files: the v1_7_005 block (identical in lakebase/001 and the migration) never re-owns rule_reversion_proposal
     and never re-owns a table owned by dam_lakebase_owner or carrying a guard trigger; 001's app-ownership loop skips
     owner-owned / guard-triggered tables; the v1_7_005 down refuses while v1_7_009 guards the outbox;
  2. tools/run_migration.py --lakebase: ledger dam_app.schema_migration_ledger (v1_7_011, owner-owned, append-only);
     re-run / older / out-of-order-down refused unless --rerun "<reason>"; v1_7_005 refused ALWAYS once v1_7_010 is
     applied (ledger or probe), flag or not;
  3. the validator runs after every runner-applied file (exit 5 on any violation).
Plus a CI check: no Lakebase / PostgreSQL file uses \\p{...} (PG 17 ARE rejects it: "invalid escape \\ sequence")."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

from lbmig import ups

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LB = os.path.join(ROOT, "lakebase")
LB_MIG = os.path.join(LB, "migrations")
DDL = os.path.join(LB, "001_app_schema.sql")
VAL = os.path.join(LB, "validate_append_only.sql")
M005 = os.path.join(LB_MIG, "v1_7_005_cdse_reversion_tables.sql")
M009 = os.path.join(LB_MIG, "v1_7_009_status_transition_guard.sql")
M008 = os.path.join(LB_MIG, "v1_7_008_cdse_outbox_status.sql")
M010 = os.path.join(LB_MIG, "v1_7_010_rule_reversion_owner.sql")
M011 = os.path.join(LB_MIG, "v1_7_011_migration_ledger.sql")
RUNNER = os.path.join(ROOT, "tools", "run_migration.py")
GUARDED = ("exceedance_status", "review_signoff", "manual_reading", "review_chain_head_log", "ref_change_publication",
           "onboarding_publication", "dssmp_registration_outbox", "dam_cdse_outbox", "rule_reversion_proposal",
           "schema_migration_ledger")
spec = importlib.util.spec_from_file_location("run_migration_qa059", RUNNER)
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)
REASON = "INC-59: deliberate re-apply after review"


def _block(path, tag):
    s = open(path).read()
    return s[s.index(f"-- BEGIN {tag} block"):s.index(f"-- END {tag} block")]


def _code(path):
    return "\n".join(l.split("--")[0] for l in open(path).read().splitlines())


# ---------------------------------------------------------------- static: the files
def test_v1_7_005_block_is_identical_and_never_re_owns_a_guarded_table():
    blk = _block(DDL, "v1_7_005")
    assert _block(M005, "v1_7_005") == blk
    assert "dam_lakebase_owner') AND NOT EXISTS" in blk and "status_transition_guard" in blk
    for f in (DDL, M005):
        assert not re.search(r"rule_reversion_proposal OWNER TO %I", _code(f)), f


def test_v1_7_011_block_is_identical_in_001_and_the_migration():
    assert _block(DDL, "v1_7_011") == _block(M011, "v1_7_011")
    assert "OWNER TO dam_lakebase_owner" in _block(M011, "v1_7_011")


def test_audit_every_lakebase_file_that_hands_a_table_to_the_app():
    """(2) every 'OWNER TO <app role>' in lakebase/ is on this list, with the reason it cannot undo a hardening."""
    allowed = {
        "001_app_schema.sql": 2,                     # app-table loop + the v1_7_005 block (both skip owner-owned /
                                                     # guard-triggered tables)
        "v1_7_005_cdse_reversion_tables.sql": 1,     # dam_cdse_change only, skipped when owner-owned / guarded
        "v1_7_005_cdse_reversion_tables_down.sql": 1,  # refused while v1_7_009's status guard exists
        "v1_7_010_rule_reversion_owner_down.sql": 1,   # the explicit rollback of 010; runner: only when 010 is newest
    }
    found = {}
    for base, _, names in os.walk(LB):
        for n in names:
            if n.endswith(".sql"):
                c = len(re.findall(r"OWNER TO %I", _code(os.path.join(base, n))))
                if c:
                    found[n] = c
    assert found == allowed
    loop = _code(DDL)
    assert "c.relowner <> 'dam_lakebase_owner'::regrole" in loop and "status_transition_guard'))" in loop
    assert "'v1_7_005 down refused: run the v1_7_009 down" in open(os.path.join(
        LB_MIG, "v1_7_005_cdse_reversion_tables_down.sql")).read()


def test_every_lakebase_up_has_a_down_and_the_runner_knows_the_hardening():
    names = set(os.listdir(LB_MIG))
    assert all(f[:-4] + "_down.sql" in names for f in ups())
    assert rm.SUPERSEDED["v1_7_005_cdse_reversion_tables.sql"] == ("v1_7_010_rule_reversion_owner.sql",)
    assert set(rm.lakebase_files()) == set(ups())


# ---------------------------------------------------------------- static: no \p{...} in PostgreSQL code
UC_PCLASS_ALLOWED = {os.path.join("tools", "verify", "qa057_guard_probe.sql")} | {
    os.path.join("sql", "migrations", f) for f in os.listdir(os.path.join(ROOT, "sql", "migrations"))
    if f.endswith("_down.sql")}


def test_no_lakebase_or_postgres_file_uses_unicode_property_escapes():
    """\\p{...} is Java (Databricks) regex only; PostgreSQL 17 ARE fails with 'invalid escape \\ sequence'. Every
    lakebase/ file, and every runtime .py / .sql outside the UC down guard, must be free of it."""
    hits = []
    for top in ("lakebase", "src", "tools", "resources", "sql"):
        for base, _, names in os.walk(os.path.join(ROOT, top)):
            for n in names:
                if not n.endswith((".sql", ".py", ".yml", ".yaml")):
                    continue
                rel = os.path.relpath(os.path.join(base, n), ROOT)
                txt = open(os.path.join(base, n), encoding="utf-8").read()
                code = "\n".join(l.split("--")[0] for l in txt.splitlines()) if n.endswith(".sql") else txt
                if re.search(r"\\+p\{", code) and (rel.startswith("lakebase") or rel not in UC_PCLASS_ALLOWED):
                    hits.append(rel)
    assert hits == []


# ---------------------------------------------------------------- unit: the runner's decision
def _f(n):
    return os.path.join(LB_MIG, n)


def test_decide_lakebase_rules():
    d = rm.decide_lakebase
    led = [("v1_7_009_status_transition_guard.sql", "up"), ("v1_7_010_rule_reversion_owner.sql", "up")]
    for reason in (None, REASON):                                         # superseded: always, flag or not
        with pytest.raises(rm.Refused) as e:
            d(_f("v1_7_005_cdse_reversion_tables.sql"), led, (), reason)
        assert e.value.code == 3 and "no flag overrides" in str(e.value)
    with pytest.raises(rm.Refused) as e:                                  # ... also when only the probe sees 010
        d(_f("v1_7_005_cdse_reversion_tables.sql"), [], ("v1_7_010_rule_reversion_owner.sql",), REASON)
    assert e.value.code == 3
    for f in ("v1_7_010_rule_reversion_owner.sql", "v1_7_009_status_transition_guard.sql",
              "v1_7_006_chain_head_log.sql"):                             # applied / older: need --rerun
        with pytest.raises(rm.Refused) as e:
            d(_f(f), led)
        assert e.value.code == 3 and "--rerun" in str(e.value)
        with pytest.raises(rm.Refused):
            d(_f(f), led, (), "short")
        assert d(_f(f), led, (), REASON)["rerun"] is True
    ok = d(_f("v1_7_011_migration_ledger.sql"), led)
    assert ok["rerun"] is False and ok["reason"] is None
    with pytest.raises(rm.Refused):                                       # down out of order
        d(_f("v1_7_009_status_transition_guard_down.sql"), led)
    assert d(_f("v1_7_010_rule_reversion_owner_down.sql"), led)["is_down"]
    led2 = led + [("v1_7_010_rule_reversion_owner_down.sql", "down")]       # after the 010 down: 005 is allowed
    assert d(_f("v1_7_005_cdse_reversion_tables.sql"), led2, (), REASON)["rerun"] is True
    with pytest.raises(rm.Refused) as e:
        d(os.path.join(ROOT, "lakebase", "001_app_schema.sql"), led)
    assert e.value.code == 2


def test_uc_mode_unchanged_and_lakebase_needs_app_role():
    assert rm.main(["--file", _f("v1_7_011_migration_ledger.sql")]) == 2               # UC mode needs --catalog
    assert rm.main(["--lakebase", "--file", _f("v1_7_011_migration_ledger.sql")]) == 2  # no --app-role


# ---------------------------------------------------------------- live PostgreSQL
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture()
def env():
    tag = uuid.uuid4().hex[:8]
    db, app, pub = f"qa59_{tag}", f"qa59_app_{tag}", f"qa59_pub_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN",
                 "-c", f"CREATE ROLE {pub} LOGIN").returncode == 0
    e = {"su": f"{PG} dbname={db}", "app": f"{PG} dbname={db} user={app}", "app_role": app, "pub": pub,
         "opts": {"PGOPTIONS": f"-c dam.app_role={app} -c dam.publisher_login={pub}"}}
    yield e
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}", "-c", f"DROP ROLE IF EXISTS {pub}")


def _val(e):
    r = _psql(e["su"], "-f", VAL, env_extra=e["opts"])
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _owner(e, t):
    return _psql(e["su"], "-c", f"SELECT relowner::regrole::text FROM pg_class WHERE oid = 'dam_app.{t}'::regclass"
                 ).stdout.strip()


def _hardened(e):
    """Validator clean; every guarded table owned by dam_lakebase_owner and the app cannot disable / drop its triggers
    or re-own it; the app's workflow table dam_cdse_change stays the app's."""
    assert _val(e) == ""
    for t in GUARDED:
        assert _owner(e, t) == "dam_lakebase_owner", t
        for stmt in (f"ALTER TABLE dam_app.{t} DISABLE TRIGGER ALL", f"ALTER TABLE dam_app.{t} OWNER TO CURRENT_USER"):
            r = _psql(e["app"], "-c", stmt)
            assert r.returncode != 0 and "must be owner" in r.stderr, (stmt, r.stderr)
    r = _psql(e["app"], "-c", "ALTER TABLE dam_app.rule_reversion_proposal DISABLE TRIGGER t_status_rule_reversion_proposal")
    assert r.returncode != 0 and "must be owner" in r.stderr
    assert _psql(e["su"], "-c", "SELECT tgenabled FROM pg_trigger WHERE tgname = 't_status_rule_reversion_proposal'"
                 ).stdout.strip() == "A"
    assert _owner(e, "dam_cdse_change") == e["app_role"]


def _runner(e, *args):
    return subprocess.run([sys.executable, RUNNER, "--lakebase", "--dsn", e["su"], "--app-role", e["app_role"],
                           "--publisher-login", e["pub"], *args], capture_output=True, text=True)


@live
def test_pg17_rejects_unicode_property_escapes():
    r = _psql(PG, "-c", "SELECT regexp_replace('a b', '[\\p{Z}]', '')")
    assert r.returncode != 0 and "invalid escape" in r.stderr


@live
def test_010_then_direct_psql_005_then_validator(env):
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0          # current 001 = up to 010/011
    assert _psql(env["su"], "-f", M010, env_extra=env["opts"]).returncode == 0
    _hardened(env)
    for _ in range(2):                                                                 # the QA repro, direct psql
        r = _psql(env["su"], "-f", M005, env_extra=env["opts"])
        assert r.returncode == 0, r.stderr
    _hardened(env)


@live
def test_001_rerun_after_010_and_every_lakebase_up_file_rerun_keep_every_guarded_table(env):
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0
    assert _psql(env["su"], "-f", M010, env_extra=env["opts"]).returncode == 0
    for _ in range(2):
        assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0
        _hardened(env)
    for f in ups():                                     # (2) audit, live: no up file undoes a later hardening
        r = _psql(env["su"], "-f", os.path.join(LB_MIG, f), env_extra=env["opts"])
        assert r.returncode == 0, (f, r.stderr)
        _hardened(env)


@live
def test_runner_ledger_refusals_rerun_flag_and_validator(env):
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0
    r = _runner(env, "--file", M010, "--rerun", REASON)        # 010 applied by probe -> needs --rerun; recorded
    assert r.returncode == 0, r.stderr + r.stdout
    assert "validator: OK" in r.stdout
    for extra in ((), ("--rerun", REASON)):                    # 005 after 010: refused, flag or not; nothing ran
        r = _runner(env, "--file", M005, *extra)
        assert r.returncode == 3 and "no flag overrides" in r.stderr, r.stderr
    r = _runner(env, "--file", M011)                           # newer than everything applied: no flag needed
    assert r.returncode == 0 and "validator: OK" in r.stdout, r.stderr
    r = _runner(env, "--file", M011)                           # applied: refused ...
    assert r.returncode == 3 and "already applied" in r.stderr
    r = _runner(env, "--file", M009)                           # applied (probe: its trigger exists): refused
    assert r.returncode == 3 and "already applied" in r.stderr
    r = _runner(env, "--file", M008)                           # older than the newest applied: refused ...
    assert r.returncode == 3 and "older than" in r.stderr
    r = _runner(env, "--file", M008, "--rerun", REASON)        # ... unless deliberately re-run with a reason
    assert r.returncode == 0, r.stderr
    r = _runner(env, "--file", M010[:-4] + "_down.sql")        # not the newest applied (011): refused
    assert r.returncode == 3 and "reverse order" in r.stderr
    led = _psql(env["su"], "-c", "SELECT migration || '|' || direction || '|' || coalesce(rerun_reason, '') "
                                 "FROM dam_app.schema_migration_ledger ORDER BY ledger_id").stdout.strip().split("\n")
    assert led == [f"v1_7_010_rule_reversion_owner.sql|up|{REASON}", "v1_7_011_migration_ledger.sql|up|",
                   f"v1_7_008_cdse_outbox_status.sql|up|{REASON}"]
    for stmt in ("UPDATE dam_app.schema_migration_ledger SET direction = 'down'",       # append-only, even for su
                 "DELETE FROM dam_app.schema_migration_ledger", "TRUNCATE dam_app.schema_migration_ledger"):
        assert _psql(env["su"], "-c", stmt).returncode != 0, stmt
    r = _psql(env["app"], "-c", "SELECT count(*) FROM dam_app.schema_migration_ledger")
    assert r.returncode != 0 and "permission denied" in r.stderr
    r = _psql(env["su"], "-f", M005, env_extra=env["opts"])    # defense in depth: a direct psql run of 005
    assert r.returncode == 0, r.stderr
    _hardened(env)


@live
def test_runner_validator_failure_is_exit_5_and_baseline(env):
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0
    r = _runner(env, "--file", M010, "--baseline")
    assert r.returncode == 0 and "v1_7_004_dssmp_outbox_contract.sql" in r.stdout, r.stderr
    n = _psql(env["su"], "-c", "SELECT count(*) FROM dam_app.schema_migration_ledger WHERE direction = 'baseline'")
    assert n.stdout.strip() == str(len([f for f in ups() if f <= "v1_7_010_rule_reversion_owner.sql"]))
    r = _runner(env, "--file", M005, "--rerun", REASON)
    assert r.returncode == 3
    # break a guard behind the runner's back: the next runner-applied file reports it (exit 5)
    assert _psql(env["su"], "-c", f"GRANT DELETE ON dam_app.dam_cdse_outbox TO {env['app_role']}").returncode == 0
    r = _runner(env, "--file", M011)
    assert r.returncode == 5 and "app_outbox_privilege" in r.stderr, r.stderr + r.stdout


@live
def test_the_qa_repro_on_the_old_005_is_caught_by_these_checks(env, tmp_path):
    """Sensitivity: the pre-fix v1_7_005 (0e11d01) after 010 hands the table back and the app CAN disable the trigger;
    the validator reports it. The same steps on the fixed file pass _hardened (test above)."""
    from gitrev import git_show_or_skip
    old = tmp_path / "v1_7_005_0e11d01.sql"
    old.write_text(git_show_or_skip(ROOT, "0e11d01", "lakebase/migrations/v1_7_005_cdse_reversion_tables.sql"))
    assert _psql(env["su"], "-f", DDL, env_extra=env["opts"]).returncode == 0
    assert _psql(env["su"], "-f", str(old), env_extra=env["opts"]).returncode == 0
    assert _owner(env, "rule_reversion_proposal") == env["app_role"]
    assert "owner_not_dam_lakebase_owner|dam_app.rule_reversion_proposal" in _val(env)
    r = _psql(env["app"], "-c", "ALTER TABLE dam_app.rule_reversion_proposal DISABLE TRIGGER t_status_rule_reversion_proposal")
    assert r.returncode == 0, r.stderr
