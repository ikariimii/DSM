"""dam_cdse_outbox status = pending | loaded | rejected (app SCHEMA_CONTRACT §7 item 20, same 3 values as
dssmp_registration_outbox). lakebase/001 (CREATE + v1_7_008 block) and migration v1_7_008 carry the same block; live
PG: fresh 001 has exactly one status CHECK and refuses 'rejected_invalid'; a database at 001@6709371 (4-value inline
CHECK) holding a 'rejected_invalid' row is converged by the migration (row -> rejected, reason kept); the down puts the
4-value CHECK back and drops nothing. Also: every status CHECK in 001 is consistent with the contract lists."""
import os
import re
import shutil
import subprocess
import uuid

import pytest

from gitrev import git_show_or_skip  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
MIG = os.path.join(ROOT, "lakebase", "migrations", "v1_7_008_cdse_outbox_status.sql")
DOWN = os.path.join(ROOT, "lakebase", "migrations", "v1_7_008_cdse_outbox_status_down.sql")
# status sets per table in lakebase/001 (contract: app SCHEMA_CONTRACT §7 items 19 / 20 / 21 and state.py)
EXPECTED = {"dam_cdse_change": {"pending", "approved", "rejected"},
            "dam_cdse_outbox": {"pending", "loaded", "rejected"},
            "rule_reversion_proposal": {"pending", "applied", "rejected"},
            "dssmp_registration_outbox": {"pending", "loaded", "rejected"}}


def _block(path):
    s = open(path).read()
    return s[s.index("-- BEGIN v1_7_008 block"):s.index("-- END v1_7_008 block")]


def test_001_and_the_migration_carry_the_same_block():
    assert _block(MIG) == _block(DDL)
    code = "\n".join(l for l in open(DOWN).read().splitlines() if not l.lstrip().startswith("--"))
    assert not re.search(r"\bDROP (TABLE|COLUMN)\b|\bDELETE\b|\bTRUNCATE\b|\bUPDATE\b", code)


def test_every_status_check_in_001_matches_the_contract_list():
    s = "\n".join(l.split("--")[0] for l in open(DDL).read().splitlines())
    found = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\);", s, re.S):
        for vals in re.findall(r"CHECK \(status IN \(([^)]*)\)\)", m.group(2)):
            found.setdefault(m.group(1), []).append(set(re.findall(r"'(\w+)'", vals)))
    for m in re.finditer(r"ALTER TABLE dam_app\.(\w+) ADD CONSTRAINT \w+\s+CHECK \(status IN \(([^)]*)\)\)", s):
        found.setdefault(m.group(1), []).append(set(re.findall(r"'(\w+)'", m.group(2))))
    assert set(found) == set(EXPECTED), found
    for t, sets in found.items():
        assert all(v == EXPECTED[t] for v in sets), (t, sets)
    assert "rejected_invalid" not in s.split("CREATE TABLE IF NOT EXISTS dam_cdse_outbox", 1)[1].split(";", 1)[0]


PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture()
def db():
    tag = uuid.uuid4().hex[:8]
    name, app = f"lbcos_{tag}", f"lbcos_app_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {name}", "-c", f"CREATE ROLE {app} LOGIN").returncode == 0
    yield f"{PG} dbname={name}", {"PGOPTIONS": f"-c dam.app_role={app}"}
    _psql(PG, "-c", f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}")


INS = ("INSERT INTO dam_app.dam_cdse_outbox (outbox_id, dam_id, kind, new_cdse_id, change_reason_code, proposed_by, "
       "approved_by, approved_at, approval_record_hash, status, reason_code, message) VALUES "
       "('{}', 'D{}', 'assign', 'c@x.org', 'initial_appointment', 'a', 'a', 't', 'h', '{}', '{}', 'm')")


def _checks(conn):
    return _psql(conn, "-c", "SELECT conname || '=' || pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                             "'dam_app.dam_cdse_outbox'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) "
                             "~ 'status' ORDER BY 1").stdout.strip().splitlines()


@live
def test_fresh_001_has_one_three_value_check(db):
    conn, opts = db
    assert _psql(conn, "-f", DDL, env_extra=opts).returncode == 0
    assert _psql(conn, "-f", DDL, env_extra=opts).returncode == 0                       # idempotent
    cks = _checks(conn)
    assert len(cks) == 1 and cks[0].startswith("ck_dam_cdse_outbox_status=") and "rejected_invalid" not in cks[0]
    r = _psql(conn, "-c", INS.format("O-RI", 1, "rejected_invalid", "x"))
    assert r.returncode != 0 and "ck_dam_cdse_outbox_status" in r.stderr
    for i, s in enumerate(("pending", "loaded", "rejected")):
        assert _psql(conn, "-c", INS.format(f"O-{i}", i, s, "x")).returncode == 0, s


@live
def test_upgrade_from_6709371_maps_rejected_invalid_and_down_restores_without_data_loss(db, tmp_path):
    conn, opts = db
    old = tmp_path / "001_6709371.sql"
    old.write_text(git_show_or_skip(ROOT, "6709371", "lakebase/001_app_schema.sql"))
    assert _psql(conn, "-f", str(old), env_extra=opts).returncode == 0
    for i, s in enumerate(("pending", "loaded", "rejected", "rejected_invalid")):
        assert _psql(conn, "-c", INS.format(f"O-{i}", i, s, f"code{i}")).returncode == 0, s
    for _ in range(2):
        r = _psql(conn, "-f", MIG)
        assert r.returncode == 0, r.stderr
    rows = _psql(conn, "-c", "SELECT outbox_id || ':' || status || ':' || reason_code FROM dam_app.dam_cdse_outbox "
                             "ORDER BY 1").stdout.split()
    assert rows == ["O-0:pending:code0", "O-1:loaded:code1", "O-2:rejected:code2", "O-3:rejected:code3"]
    cks = _checks(conn)
    assert len(cks) == 1 and "rejected_invalid" not in cks[0]
    for _ in range(2):
        assert _psql(conn, "-f", DOWN).returncode == 0
    assert "rejected_invalid" in _checks(conn)[0] and len(_checks(conn)) == 1
    assert len(_psql(conn, "-c", "SELECT outbox_id FROM dam_app.dam_cdse_outbox").stdout.split()) == 4
    assert _psql(conn, "-f", MIG).returncode == 0 and len(_checks(conn)) == 1
    assert _psql(conn, "-f", DDL, env_extra=opts).returncode == 0 and len(_checks(conn)) == 1
