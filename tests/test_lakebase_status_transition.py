"""Residual 1: status-transition guard (lakebase/001 block v1_7_009 = lakebase/migrations/v1_7_009_*.sql).
dam_cdse_outbox / dssmp_registration_outbox: pending -> loaded | rejected only; rule_reversion_proposal: pending ->
applied | rejected only; nothing ever goes back to pending or between terminal states; a terminal row is frozen
(an UPDATE that changes nothing passes); a same-status UPDATE of a pending row is fine. Live PostgreSQL: as the
publisher (the only writer of the outboxes), as the app role (owner of rule_reversion_proposal), under
session_replication_role = replica, and every bypass the publisher could try (disable / drop the trigger, replace the
function, replica mode) is refused. The down drops only the triggers and the function."""
import os
import re
import shutil
import subprocess
import uuid

import pytest

from lbmig import later_than  # noqa: E402
from gitrev import git_show_or_skip  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
VAL = os.path.join(ROOT, "lakebase", "validate_append_only.sql")
MIG = os.path.join(ROOT, "lakebase", "migrations", "v1_7_009_status_transition_guard.sql")
DOWN = os.path.join(ROOT, "lakebase", "migrations", "v1_7_009_status_transition_guard_down.sql")
OUTBOXES = ("dam_cdse_outbox", "dssmp_registration_outbox")


def _block(path):
    s = open(path).read()
    return s[s.index("-- BEGIN v1_7_009 block"):s.index("-- END v1_7_009 block")]


def test_001_and_the_migration_carry_the_same_block_and_the_down_drops_no_data():
    blk = _block(DDL)
    assert _block(MIG) == blk
    for t, args in (("dam_cdse_outbox", "'loaded', 'rejected'"), ("dssmp_registration_outbox", "'loaded', 'rejected'"),
                    ("rule_reversion_proposal", "'applied', 'rejected'")):
        assert (f"CREATE OR REPLACE TRIGGER t_status_{t} BEFORE UPDATE ON dam_app.{t}\n  FOR EACH ROW EXECUTE "
                f"FUNCTION dam_app.status_transition_guard({args});") in blk
        assert f"ALTER TABLE dam_app.{t} ENABLE ALWAYS TRIGGER t_status_{t};" in blk
    assert "ALTER FUNCTION dam_app.status_transition_guard() OWNER TO dam_lakebase_owner;" in blk
    code = "\n".join(l for l in open(DOWN).read().splitlines() if not l.lstrip().startswith("--"))
    assert not re.search(r"\bDROP (TABLE|COLUMN|SCHEMA)\b|\bDELETE\b|\bTRUNCATE\b|\bUPDATE\b", code)


PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture()
def env():
    tag = uuid.uuid4().hex[:8]
    db, app, pub = f"lbst_{tag}", f"lbst_app_{tag}", f"lbst_pub_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN",
                 "-c", f"CREATE ROLE {pub} LOGIN").returncode == 0
    e = {"su": f"{PG} dbname={db}", "app": f"{PG} dbname={db} user={app}", "pub": f"{PG} dbname={db} user={pub}",
         "opts": {"PGOPTIONS": f"-c dam.app_role={app} -c dam.publisher_login={pub}"}}
    yield e
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}", "-c", f"DROP ROLE IF EXISTS {pub}")


ROW = {
    "dam_cdse_outbox": ("INSERT INTO dam_app.dam_cdse_outbox (outbox_id, dam_id, kind, new_cdse_id, change_reason_code, "
                        "proposed_by, approved_by, approved_at, approval_record_hash, status) VALUES "
                        "('{id}', 'D-{id}', 'assign', 'c@x.org', 'initial_appointment', 'a', 'a', 't', 'h', 'pending')"),
    "dssmp_registration_outbox": ("INSERT INTO dam_app.dssmp_registration_outbox (outbox_id, dam_id, dssmp_version_id, "
                                  "version_no, effective_from, document_sha256, proposed_by, approved_by, approved_at, "
                                  "approval_record_hash, status) VALUES ('{id}', 'D', 'DS-{id}', 1, 't', 'h', 'p', "
                                  "'c', 't', 'h', 'pending')"),
    "rule_reversion_proposal": ("INSERT INTO dam_app.rule_reversion_proposal (proposal_id, dam_id, to_dssmp_version_id, "
                                "rules, proposed_by, proposed_at, rationale, status) VALUES ('{id}', 'D-{id}', 'DS-2', "
                                "'[]', 'p', 't', 'r', 'pending')"),
}
KEY = {"dam_cdse_outbox": "outbox_id", "dssmp_registration_outbox": "outbox_id", "rule_reversion_proposal": "proposal_id"}


def _fresh(e):
    r = _psql(e["su"], "-f", DDL, env_extra=e["opts"])
    assert r.returncode == 0, r.stderr


def _new(e, t, conn="app"):
    i = f"R{uuid.uuid4().hex[:6]}"
    r = _psql(e[conn], "-c", ROW[t].format(id=i))
    assert r.returncode == 0, r.stderr
    return i


def _upd(e, who, t, i, sets):
    return _psql(e[who], "-c", f"UPDATE dam_app.{t} SET {sets} WHERE {KEY[t]} = '{i}'")


def _refused(r, what="status transition"):
    return r.returncode != 0 and what in r.stderr


@live
@pytest.mark.parametrize("t", OUTBOXES)
def test_outbox_moves_for_the_publisher(env, t):
    _fresh(env)
    for terminal, other in (("loaded", "rejected"), ("rejected", "loaded")):
        i = _new(env, t)
        assert _upd(env, "pub", t, i, "message = 'working'").returncode == 0          # same-status, pending
        assert _upd(env, "pub", t, i, "status = 'pending', message = 'still'").returncode == 0
        r = _upd(env, "pub", t, i, f"status = '{terminal}', reason_code = 'c', message = 'm', processed_at = 'now'")
        assert r.returncode == 0, r.stderr
        assert _upd(env, "pub", t, i, f"status = '{terminal}', message = 'm'").returncode == 0     # no-op passes
        assert _refused(_upd(env, "pub", t, i, "status = 'pending'"))
        assert _refused(_upd(env, "pub", t, i, "status = 'pending', reason_code = NULL, message = NULL"))
        assert _refused(_upd(env, "pub", t, i, f"status = '{other}'"))
        assert _refused(_upd(env, "pub", t, i, "message = 'rewritten'"), "is final")
        assert _refused(_upd(env, "pub", t, i, "processed_at = 'later'"), "is final")
        out = _psql(env["su"], "-c", f"SELECT status || '|' || message FROM dam_app.{t} WHERE {KEY[t]} = '{i}'").stdout
        assert out.strip() == f"{terminal}|m"


@live
def test_rule_reversion_moves_for_the_app_owner(env):
    _fresh(env)
    t = "rule_reversion_proposal"
    for terminal, other in (("applied", "rejected"), ("rejected", "applied")):
        i = _new(env, t)
        assert _upd(env, "app", t, i, "decision_rationale = 'draft'").returncode == 0
        assert _refused(_upd(env, "app", t, i, "status = 'loaded'"))            # not a target of this table
        r = _upd(env, "app", t, i, f"status = '{terminal}', decided_by = 'cdse', decided_at = 't'")
        assert r.returncode == 0, r.stderr
        assert _refused(_upd(env, "app", t, i, "status = 'pending'"))
        assert _refused(_upd(env, "app", t, i, f"status = '{other}'"))
        assert _refused(_upd(env, "app", t, i, "pub_ids = '[\"X\"]'"), "is final")
    # the app's own decide_pending SQL (WHERE status = 'pending') on a decided row: 0 rows, no error
    r = _psql(env["app"], "-c", f"UPDATE dam_app.{t} SET status = 'applied' WHERE proposal_id = '{i}' "
                                "AND status = 'pending' RETURNING proposal_id")
    assert r.returncode == 0 and r.stdout.strip() == ""


@live
@pytest.mark.parametrize("t", OUTBOXES)
def test_the_publisher_cannot_bypass_the_guard(env, t):
    _fresh(env)
    i = _new(env, t)
    assert _upd(env, "pub", t, i, "status = 'loaded'").returncode == 0
    for stmt, err in ((f"ALTER TABLE dam_app.{t} DISABLE TRIGGER t_status_{t}", "must be owner"),
                      (f"ALTER TABLE dam_app.{t} DISABLE TRIGGER ALL", "must be owner"),
                      (f"DROP TRIGGER t_status_{t} ON dam_app.{t}", "must be owner"),
                      ("CREATE OR REPLACE FUNCTION dam_app.status_transition_guard() RETURNS trigger LANGUAGE plpgsql "
                       "AS $$ BEGIN RETURN NEW; END $$", "permission denied"),
                      ("ALTER FUNCTION dam_app.status_transition_guard() OWNER TO CURRENT_USER", "must be owner"),
                      ("SET session_replication_role = replica", "permission denied"),
                      (f"CREATE TRIGGER x BEFORE UPDATE ON dam_app.{t} FOR EACH ROW EXECUTE FUNCTION "
                       "dam_app.status_transition_guard()", "permission denied")):
        r = _psql(env["pub"], "-c", stmt)
        assert r.returncode != 0 and err in r.stderr, (stmt, r.stderr)
    # replica mode (only a superuser can set it here): ENABLE ALWAYS still fires
    r = _psql(env["su"], "-c", "SET session_replication_role = replica", "-c",
              f"UPDATE dam_app.{t} SET status = 'pending' WHERE {KEY[t]} = '{i}'")
    assert _refused(r)
    # the app has no UPDATE on the owner-owned outboxes at all
    assert "permission denied" in _upd(env, "app", t, i, "status = 'pending'").stderr
    assert _psql(env["su"], "-f", VAL, env_extra=env["opts"]).stdout.strip() == ""


@live
def test_validator_reports_a_missing_or_disabled_guard_and_down_reapply(env):
    _fresh(env)
    assert _psql(env["su"], "-f", VAL, env_extra=env["opts"]).stdout.strip() == ""
    # since v1_7_010 the app no longer owns rule_reversion_proposal: it cannot disable the trigger; if the owner
    # side does, the validator reports it
    r = _psql(env["app"], "-c", "ALTER TABLE dam_app.rule_reversion_proposal DISABLE TRIGGER "
                                "t_status_rule_reversion_proposal")
    assert r.returncode != 0 and "must be owner" in r.stderr
    assert _psql(env["su"], "-c", "ALTER TABLE dam_app.rule_reversion_proposal DISABLE TRIGGER "
                                  "t_status_rule_reversion_proposal").returncode == 0
    out = _psql(env["su"], "-f", VAL, env_extra=env["opts"]).stdout
    assert "status_guard_missing|t_status_rule_reversion_proposal" in out
    i = _new(env, "dam_cdse_outbox")
    for _ in range(2):
        assert _psql(env["su"], "-f", DOWN).returncode == 0
    out = _psql(env["su"], "-f", VAL, env_extra=env["opts"]).stdout
    assert sorted(re.findall(r"status_guard_missing\|t_status_(\w+)", out)) == sorted(
        ["dam_cdse_outbox", "dssmp_registration_outbox", "rule_reversion_proposal"])
    assert _psql(env["su"], "-c", f"SELECT count(*) FROM dam_app.dam_cdse_outbox WHERE outbox_id = '{i}'"
                 ).stdout.strip() == "1"                                                       # no data dropped
    for _ in range(2):
        r = _psql(env["su"], "-f", MIG)
        assert r.returncode == 0, r.stderr
    assert _psql(env["su"], "-f", VAL, env_extra=env["opts"]).stdout.strip() == ""


@live
def test_upgrade_from_d6d3c9c_with_legacy_rows(env, tmp_path):
    """001 at d6d3c9c (no guard) + rows in every state, then the migration: rows unchanged, guard active; the earlier
    legacy-normalizing blocks (v1_7_004 / v1_7_008) still run after it."""
    old = tmp_path / "001_d6d3c9c.sql"
    old.write_text(git_show_or_skip(ROOT, "d6d3c9c", "lakebase/001_app_schema.sql"))
    assert _psql(env["su"], "-f", str(old), env_extra=env["opts"]).returncode == 0
    i = _new(env, "dam_cdse_outbox")
    assert _upd(env, "pub", "dam_cdse_outbox", i, "status = 'rejected'").returncode == 0
    assert _upd(env, "pub", "dam_cdse_outbox", i, "status = 'pending'").returncode == 0     # no guard yet
    assert _upd(env, "pub", "dam_cdse_outbox", i, "status = 'loaded'").returncode == 0
    for m in later_than("v1_7_008"):
        for _ in range(2):
            r = _psql(env["su"], "-f", m, env_extra=env["opts"])
            assert r.returncode == 0, (m, r.stderr)
    assert _refused(_upd(env, "pub", "dam_cdse_outbox", i, "status = 'pending'"))
    for m in ("v1_7_004_dssmp_outbox_contract.sql", "v1_7_008_cdse_outbox_status.sql"):
        r = _psql(env["su"], "-f", os.path.join(ROOT, "lakebase", "migrations", m), env_extra=env["opts"])
        assert r.returncode == 0, (m, r.stderr)
    assert _psql(env["su"], "-f", VAL, env_extra=env["opts"]).stdout.strip() == ""
