"""QA-056 (App Dev patch for the DA): the unique guard indexes on the owner-owned Lakebase tables. lakebase/001 and
migration v1_7_007 carry the same block; names and predicates match the app's state.py; live: a fresh 001 has them,
a second pending row is refused for the app role, and the down migration drops only the indexes."""
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
MIG = os.path.join(ROOT, "lakebase", "migrations", "v1_7_007_qa056_unique_guards.sql")
DOWN = os.path.join(ROOT, "lakebase", "migrations", "v1_7_007_qa056_unique_guards_down.sql")
STATE = os.path.join(ROOT, "app", "backend", "repos", "state.py")
NAMES = ("dam_cdse_outbox_one_pending", "dssmp_outbox_one_pending", "ref_change_publication_one_version")
APP_OWNED = ("dam_cdse_change_one_pending", "rule_reversion_one_pending")


def _block(path):
    s = open(path).read()
    return s[s.index("-- BEGIN v1_7_007 block"):s.index("-- END v1_7_007 block")]


def test_001_and_the_migration_carry_the_same_block_matching_the_app():
    blk = _block(DDL)
    assert _block(MIG) == blk
    st = open(STATE).read()
    for name, table in (("dam_cdse_outbox_one_pending", "dam_cdse_outbox"),
                        ("dssmp_outbox_one_pending", "dssmp_registration_outbox"),
                        ("dam_cdse_change_one_pending", "dam_cdse_change"),          # DA: app-owned, also in 001
                        ("rule_reversion_one_pending", "rule_reversion_proposal")):
        cols = re.search(r'\("' + name + r'", "' + table + r'",\s*"([^"]+)"\)', st, re.S).group(1)
        assert re.sub(r"\s+", " ", f"ON {table} {cols};") in re.sub(r"\s+", " ", blk), name
    assert "payload::jsonb ->> 'threshold_id'" in st and "payload::jsonb ->> 'threshold_id'" in blk
    assert "kind = '{PUB_KIND_THRESHOLD}' AND status IN ('pending', 'applied')" in st
    assert "kind = 'ref.threshold.new_version' AND status IN ('pending', 'applied')" in blk
    code = "\n".join(l for l in open(DOWN).read().splitlines() if not l.lstrip().startswith("--"))
    assert not re.search(r"\bDROP (TABLE|COLUMN)\b|\bDELETE\b|\bTRUNCATE\b", code)
    assert all(f"DROP INDEX IF EXISTS {n};" in code for n in NAMES)


PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=dict(os.environ, **(env_extra or {})))


@live
def test_fresh_001_has_the_guards_and_the_app_role_cannot_insert_a_second_pending_row():
    tag = uuid.uuid4().hex[:8]
    db, app = f"lbq56_{tag}", f"lbq56_app_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN").returncode == 0
    su, ap = f"{PG} dbname={db}", f"{PG} dbname={db} user={app}"
    try:
        r = _psql(su, "-f", DDL, env_extra={"PGOPTIONS": f"-c dam.app_role={app}"})
        assert r.returncode == 0, r.stderr
        have = set(_psql(su, "-c", "SELECT indexname FROM pg_indexes WHERE schemaname = 'dam_app'").stdout.split())
        assert set(NAMES) <= have
        row = ("INSERT INTO dam_app.dam_cdse_outbox (outbox_id, dam_id, kind, new_cdse_id, change_reason_code, "
               "proposed_by, approved_by, approved_at, approval_record_hash, status) VALUES "
               "('{0}', 'D1', 'assign', 'c@x', 'initial_appointment', 'a@x', 'a@x', 't', 'h', 'pending')")
        assert _psql(ap, "-c", row.format("O1")).returncode == 0
        dup = _psql(ap, "-c", row.format("O2"))
        assert dup.returncode != 0 and "dam_cdse_outbox_one_pending" in dup.stderr
        assert _psql(su, "-f", DOWN).returncode == 0
        have = set(_psql(su, "-c", "SELECT indexname FROM pg_indexes WHERE schemaname = 'dam_app'").stdout.split())
        assert not set(NAMES) & have
        assert set(APP_OWNED) <= have                                   # app-owned guards are left to the app
        chg = ("INSERT INTO dam_app.dam_cdse_change (change_id, dam_id, kind, new_cdse_id, change_reason_code, "
               "rationale, proposed_by, proposed_at, status) VALUES ('{0}', 'D1', 'change', 'c@x', 'retirement', "
               "'why', 'a@x', 't', 'pending')")
        assert _psql(ap, "-c", chg.format("C1")).returncode == 0
        dup = _psql(ap, "-c", chg.format("C2"))
        assert dup.returncode != 0 and "dam_cdse_change_one_pending" in dup.stderr
        assert _psql(su, "-f", MIG).returncode == 0                       # re-apply restores (idempotent)
        assert _psql(su, "-f", MIG).returncode == 0
    finally:
        _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)", "-c", f"DROP ROLE IF EXISTS {app}")


@live
def test_upgrade_from_the_previous_001_by_migration_passes_the_validator(tmp_path):
    old_sql = git_show_or_skip(ROOT, "d150288", "lakebase/001_app_schema.sql")
    tag = uuid.uuid4().hex[:8]
    db, app = f"lbq56u_{tag}", f"lbq56u_app_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN").returncode == 0
    su, opts = f"{PG} dbname={db}", {"PGOPTIONS": f"-c dam.app_role={app}"}
    try:
        old = tmp_path / "001_d150288.sql"                        # the last 001 without the v1_7_007 block
        old.write_text(old_sql)
        assert _psql(su, "-f", str(old), env_extra=opts).returncode == 0
        for _ in range(2):
            r = _psql(su, "-f", MIG, env_extra=opts)
            assert r.returncode == 0, r.stderr
        for m in later_than("v1_7_007"):                       # the validator checks the CURRENT schema
            r = _psql(su, "-f", m, env_extra=opts)
            assert r.returncode == 0, (m, r.stderr)
        have = set(_psql(su, "-c", "SELECT indexname FROM pg_indexes WHERE schemaname = 'dam_app'").stdout.split())
        assert set(NAMES) | set(APP_OWNED) <= have
        v = _psql(su, "-f", os.path.join(ROOT, "lakebase", "validate_append_only.sql"), env_extra=opts)
        assert v.returncode == 0 and v.stdout.strip() == "", v.stdout + v.stderr
    finally:
        _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)", "-c", f"DROP ROLE IF EXISTS {app}")

