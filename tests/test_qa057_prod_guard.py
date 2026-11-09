"""QA-057: down scripts refuse to run against dam_prod. Layer 1: tools/run_migration.py (catalog normalized with
dam_safety.catalog.normalize_catalog; *_down.sql on dam_prod needs --break-glass-prod "<reason>", logged).
Layer 2: the first statement of every sql/migrations/*_down.sql raises via raise_error on dam_prod unless the named
parameter break_glass = 'BREAK_GLASS_PROD'; checked again on current_catalog() right after USE CATALOG. Layer 2 is
executed here on PostgreSQL (raise_error emulated, current_catalog() bound per case); the Databricks mechanism
(named parameter markers in a sql_task file, raise_error, \\p{Cf}) is UNVERIFIED (runbook step / VERIFICATION QA-057)."""
import importlib.util
import json
import os
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIG = os.path.join(ROOT, "sql", "migrations")
DOWNS = sorted(f for f in os.listdir(MIG) if f.endswith("_down.sql"))
UPS = sorted(f for f in os.listdir(MIG) if f.endswith(".sql") and not f.endswith("_down.sql"))
DOWN = os.path.join("sql", "migrations", DOWNS[-1])
UP = os.path.join("sql", "migrations", UPS[-1])
REASON = "INC-0001 rollback approved by on-call lead"

spec = importlib.util.spec_from_file_location("run_migration", os.path.join(ROOT, "tools", "run_migration.py"))
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_migration_downs as tmd  # noqa: E402

PROD_LOOKALIKES = ["dam_prod", "DAM_PROD", " dam_prod ", "dam\u200b_prod", "\u200ddam_prod\ufeff", "dam_pro\u00add",
                   "\uff44\uff41\uff4d\uff3f\uff50\uff52\uff4f\uff44",          # fullwidth (NFKC)
                   "dam_\u2060prod"]


def _main(*args, log=None):
    return rm.main(["--dry-run", "--log-file", str(log or os.devnull), *args])


# ---------------------------------------------------------------- layer 1: tools/run_migration.py
@pytest.mark.parametrize("cat", PROD_LOOKALIKES)
def test_runner_refuses_a_down_on_prod_and_its_lookalikes_without_the_flag(cat, capsys):
    with pytest.raises(rm.Refused) as e:
        rm.decide(cat, os.path.join(ROOT, DOWN))
    assert e.value.code == 3 and "QA-057" in str(e.value)
    assert _main("--catalog", cat, "--file", DOWN) == 3
    assert "REFUSED" in capsys.readouterr().err


@pytest.mark.parametrize("f", DOWNS)
def test_runner_refuses_every_down_on_prod(f):
    assert _main("--catalog", "dam_prod", "--file", os.path.join("sql", "migrations", f)) == 3


@pytest.mark.parametrize("reason", ["", "   ", "short"])
def test_runner_refuses_prod_with_an_empty_or_short_reason(reason):
    assert _main("--catalog", "dam_prod", "--file", DOWN, "--break-glass-prod", reason) == 3


@pytest.mark.parametrize("cat", ["dam_pr0d", "dam_prоd", "dam-prod", "dam_production", "", "prod", "dam_dev2"])
def test_runner_fails_closed_on_unknown_catalogs(cat):          # incl. Cyrillic o (not NFKC-equivalent)
    assert _main("--catalog", cat, "--file", DOWN) == 2
    assert _main("--catalog", cat, "--file", DOWN, "--break-glass-prod", REASON) == 2


@pytest.mark.parametrize("cat", ["dam_dev", "dam_test", "DAM_TEST", "dam\u200b_dev"])
def test_runner_allows_a_down_on_dev_and_test(cat, capsys):
    assert _main("--catalog", cat, "--file", DOWN) == 0
    spec_ = json.loads(capsys.readouterr().out.splitlines()[0])
    assert spec_["tasks"][0]["sql_task"]["parameters"] == {"catalog": rm.normalize_catalog(cat), "break_glass": ""}


def test_runner_allows_an_up_on_prod_without_the_flag(capsys):
    assert _main("--catalog", "dam_prod", "--file", UP) == 0
    assert json.loads(capsys.readouterr().out)["tasks"][0]["sql_task"]["parameters"]["break_glass"] == ""


@pytest.mark.parametrize("cat", PROD_LOOKALIKES)
def test_runner_allows_prod_with_the_flag_and_logs_the_reason(cat, tmp_path, capsys):
    log = tmp_path / "bg.log"
    assert _main("--catalog", cat, "--file", DOWN, "--break-glass-prod", REASON, log=log) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["tasks"][0]["sql_task"]["parameters"] == {"catalog": "dam_prod",
                                                                      "break_glass": rm.BREAK_GLASS_TOKEN}
    entry = json.loads(log.read_text().strip())
    assert entry["reason"] == REASON and entry["catalog"] == "dam_prod" and entry["file"] == DOWN
    assert len(entry["sha256"]) == 64 and "QA-057_BREAK_GLASS_PROD" in err


def test_runner_refuses_files_outside_sql_migrations(tmp_path):
    p = tmp_path / "x_down.sql"
    p.write_text("SELECT 1;")
    assert _main("--catalog", "dam_dev", "--file", str(p)) == 2
    assert _main("--catalog", "dam_dev", "--file", "sql/migrations/does_not_exist_down.sql") == 2


def test_runner_cli_refuses_without_dry_run_and_never_reaches_submit(monkeypatch):
    monkeypatch.setattr(rm, "submit", lambda spec: pytest.fail("submitted"))
    assert rm.main(["--catalog", "dam_prod", "--file", DOWN, "--warehouse-id", "w", "--workspace-files", "/W"]) == 3
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "run_migration.py"), "--catalog",
                        "dam\u200b_prod", "--file", DOWN, "--dry-run"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 3 and "QA-057" in r.stderr


# ---------------------------------------------------------------- layer 2: first statement of every down
def _statements(sql):
    return [s.strip() for s in tmd._strip_comments(sql).split(";") if s.strip()]


@pytest.mark.parametrize("f", DOWNS)
def test_every_down_starts_with_the_guard_and_rechecks_after_use_catalog(f):
    st = _statements(open(os.path.join(MIG, f)).read())
    assert st[0].startswith("SELECT CASE WHEN") and "raise_error(" in st[0] and ":break_glass" in st[0]
    assert "current_catalog() = 'dam_prod'" in st[0] and "AS qa057_guard" in st[0]
    assert st[1] == "USE CATALOG IDENTIFIER(:catalog)"
    assert "current_catalog() = 'dam_prod'" in st[2] and "AS qa057_guard_resolved" in st[2]
    first = _statements(open(os.path.join(MIG, DOWNS[0])).read())
    assert st[:3] == first[:3]                                  # identical guard in every down
    assert "UNVERIFIED" in open(os.path.join(MIG, f)).read().split("QA-057", 1)[1][:1200]


@pytest.mark.parametrize("f", UPS)
def test_ups_carry_no_guard(f):
    assert "qa057_guard" not in open(os.path.join(MIG, f)).read()


PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG, reason="DAM_TEST_PG not set")

# (catalog parameter, current_catalog() before USE, after USE (None = USE fails), break_glass, expect)
CASES = [
    ("dam_prod", "dam_dev", "dam_prod", "", "QA-057"),
    ("dam_prod", "dam_dev", "dam_prod", None, "QA-057"),                    # parameter bound to NULL
    ("dam_prod", "dam_dev", "dam_prod", "yes", "QA-057"),                   # wrong token
    ("DAM_PROD", "dam_dev", "dam_prod", "", "QA-057"),
    ("dam\u200b_prod", "dam_dev", None, "", "QA-057"),                      # zero-width: caught on the parameter
    ("\ufeffdam_prod\u2060", "dam_dev", None, "", "QA-057"),
    ("\uff44\uff41\uff4d\uff3f\uff50\uff52\uff4f\uff44", "dam_dev", None, "", "CATALOG_NOT_FOUND"),  # fullwidth
    ("dam_dev", "dam_prod", "dam_dev", "", "QA-057"),                       # session default prod: fail closed
    ("dam_dev", "dam_dev", "dam_prod", "", "QA-057"),                       # resolves to prod after USE
    ("dam_dev", "dam_dev", "dam_dev", "", None),
    ("dam_test", "dam_test", "dam_test", "", None),
    ("dam_prod", "dam_dev", "dam_prod", "BREAK_GLASS_PROD", None),
    ("dam_prod", "dam_prod", "dam_prod", "BREAK_GLASS_PROD", None),
]


@live
@pytest.mark.parametrize("case", CASES, ids=[f"{i}" for i in range(len(CASES))])
def test_guard_statements_executed_on_postgres(case, tmp_path):
    cat, session, resolved, bg, expect = case
    name = f"qa057_{uuid.uuid4().hex[:8]}"
    assert tmd._psql(PG, f"CREATE DATABASE {name}").returncode == 0
    try:
        conn = f"{PG} dbname={name}"
        sql = open(os.path.join(MIG, DOWNS[0])).read()
        guard = sql[:sql.index("AS qa057_guard_resolved;") + len("AS qa057_guard_resolved;")]
        p = tmp_path / "g.sql"
        p.write_text(tmd.PG_RAISE_ERROR + tmd._bind_guard(tmd._strip_comments(guard), cat, session, resolved, bg)
                     + "\nSELECT 'reached-the-body';")
        r = tmd._psql(conn, f=str(p))
        if expect is None:
            assert r.returncode == 0 and "reached-the-body" in r.stdout, r.stderr
        else:
            assert r.returncode != 0 and f"ERROR:  {expect}" in r.stderr and "reached-the-body" not in r.stdout, r.stderr
    finally:
        tmd._psql(PG, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@live
def test_a_whole_down_on_prod_changes_nothing(tmp_path):
    """Run the real v1_7_003 down translated for dam_prod without break_glass: it fails on the first statement."""
    name = f"qa057_{uuid.uuid4().hex[:8]}"
    assert tmd._psql(PG, f"CREATE DATABASE {name}").returncode == 0
    try:
        conn = f"{PG} dbname={name}"
        p = tmp_path / "d.sql"
        p.write_text(tmd._pg(open(os.path.join(MIG, DOWNS[-1])).read(), catalog="dam_prod", session="dam_dev",
                             resolved="dam_prod", break_glass=""))
        r = tmd._psql(conn, f=str(p))
        assert r.returncode != 0 and "ERROR:  QA-057" in r.stderr
    finally:
        tmd._psql(PG, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


PROBE = os.path.join(ROOT, "tools", "verify", "qa057_guard_probe.sql")


def test_runbook_probe_is_exactly_the_downs_first_statement_and_touches_nothing():
    st = _statements(open(PROBE).read())
    assert st[0] == _statements(open(os.path.join(MIG, DOWNS[0])).read())[0]
    assert st[1:] == ["SELECT 'reached-the-body' AS probe"]


@live
@pytest.mark.parametrize("case", [("dam_prod", "", False), ("dam\u200b_prod", "", False),
                                  ("dam_prod", "BREAK_GLASS_PROD", True), ("dam_dev", "", True),
                                  ("", "", False), ("", "BREAK_GLASS_PROD", False)])
def test_runbook_probe_on_postgres(case, tmp_path):
    cat, bg, ok = case
    p = tmp_path / "p.sql"
    p.write_text(tmd.PG_RAISE_ERROR + tmd._bind_guard(tmd._strip_comments(open(PROBE).read()), cat, "dam_dev",
                                                      "dam_dev", bg))
    r = tmd._psql(PG, f=str(p))
    if ok:
        assert r.returncode == 0 and "reached-the-body" in r.stdout, r.stderr
    else:
        assert r.returncode != 0 and "ERROR:  QA-057" in r.stderr and "reached-the-body" not in r.stdout, r.stderr


# ---------------------------------------------------------------- residual 3: missing parameter / session default prod
UNBOUND = "<unbound>"          # the parameter is not submitted at all


def _bind_with_unbound(code, catalog, session, resolved, bg):
    """Databricks fails a statement that references an unbound named parameter marker (UNBOUND_SQL_PARAMETER) before
    evaluating anything (UNVERIFIED on a warehouse); emulated by replacing such a statement with that error."""
    out = []
    for st in [s for s in code.split(";") if s.strip()]:
        names = [n for n, v in (("catalog", catalog), ("break_glass", bg)) if v is UNBOUND and f":{n}" in st]
        out.append(f"\nSELECT raise_error('UNBOUND_SQL_PARAMETER: {names[0]}')" if names else st)
    code = ";".join(out) + ";"
    return tmd._bind_guard(code, None if catalog is UNBOUND else catalog, session, resolved,
                           None if bg is UNBOUND else bg)


# (catalog, session default catalog, after USE (None = USE fails), break_glass, expected error or None)
MISSING_CASES = [
    (UNBOUND, "dam_prod", None, "", "UNBOUND_SQL_PARAMETER"),
    (UNBOUND, "dam_dev", None, "", "UNBOUND_SQL_PARAMETER"),
    ("dam_prod", "dam_dev", "dam_prod", UNBOUND, "UNBOUND_SQL_PARAMETER"),
    ("dam_dev", "dam_prod", "dam_dev", UNBOUND, "UNBOUND_SQL_PARAMETER"),
    (None, "dam_prod", None, "", "QA-057: down script refused: the catalog parameter is missing"),
    (None, "dam_dev", None, "", "QA-057: down script refused: the catalog parameter is missing"),
    (None, "dam_dev", None, "BREAK_GLASS_PROD", "QA-057: down script refused: the catalog parameter is missing"),
    ("", "dam_prod", None, "", "QA-057: down script refused: the catalog parameter is missing"),
    ("", "dam_dev", None, "", "QA-057: down script refused: the catalog parameter is missing"),
    (" \u200b\ufeff", "dam_dev", None, "", "QA-057: down script refused: the catalog parameter is missing"),
    ("dam_dev", "dam_prod", "dam_dev", "", "QA-057: down script refused on dam_prod"),
    ("dam_dev", "dam_prod", "dam_dev", None, "QA-057: down script refused on dam_prod"),
    ("dam_test", "dam_prod", "dam_test", "nope", "QA-057: down script refused on dam_prod"),
    ("dam_prod", "dam_prod", "dam_prod", "", "QA-057: down script refused on dam_prod"),
    ("dam_dev", "dam_dev", "dam_prod", "", "QA-057: down script refused on dam_prod"),   # resolves to prod
    ("dam_prod", "dam_prod", "dam_prod", "BREAK_GLASS_PROD", None),
    ("dam_dev", "dam_dev", "dam_dev", "", None),
]


@live
@pytest.mark.parametrize("f", DOWNS)
def test_every_downs_own_guard_refuses_a_missing_parameter_and_a_prod_session_default(f, tmp_path):
    """Each down's OWN first three statements (not a copy) for every case, before any statement of its body."""
    sql = open(os.path.join(MIG, f)).read()
    guard = tmd._strip_comments(sql[:sql.index("AS qa057_guard_resolved;") + len("AS qa057_guard_resolved;")])
    assert _statements(guard) == _statements(sql)[:3]
    for i, (cat, session, resolved, bg, expect) in enumerate(MISSING_CASES):
        p = tmp_path / f"g{i}.sql"
        p.write_text(tmd.PG_RAISE_ERROR + _bind_with_unbound(guard, cat, session, resolved, bg)
                     + "\nSELECT 'reached-the-body';")
        r = tmd._psql(PG, f=str(p))
        if expect is None:
            assert r.returncode == 0 and "reached-the-body" in r.stdout, (f, i, r.stderr)
        else:
            assert r.returncode != 0 and f"ERROR:  {expect}" in r.stderr and "reached-the-body" not in r.stdout, \
                (f, i, r.stderr)


@pytest.mark.parametrize("cat", ["", "  ", "\u200b", None])
def test_runner_refuses_a_missing_or_blank_catalog(cat):
    with pytest.raises(rm.Refused):
        rm.decide(cat, DOWN)


def test_runner_always_binds_both_parameters():
    for cat in ("dam_dev", "dam_test"):
        assert set(rm.decide(cat, DOWN)["parameters"]) == {"catalog", "break_glass"}
    assert set(rm.decide("dam_prod", DOWN, REASON)["parameters"]) == {"catalog", "break_glass"}
