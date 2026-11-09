"""QA-062 (extends the QA-059 CI check): no Unicode property class (backslash, 'p', '{' ... '}') in any code that can
reach PostgreSQL / Lakebase. PostgreSQL 17 ARE rejects it ("invalid escape \\ sequence"; proven live in
tests/test_qa059_lakebase_rerun.py::test_pg17_rejects_unicode_property_escapes); only Databricks (Java regex) accepts
it, and Python's re module rejects it too ("bad escape \\p").

Scope: every .sql / .py / .yml / .yaml file under app/, tests/, qa/, lakebase/, src/, tools/, resources/ and sql/ (no
folder is excluded). .sql files are scanned with '--' comments removed; .py files are scanned WHOLE, so a PG-bound SQL
string, a Python regex and a docstring all count. The only exceptions are the files in ALLOWED, each with the exact
number of occurrences it may carry (a new occurrence in an allowed file fails too) and the reason it needs them.
This file spells the class without its literal so it does not need an entry itself."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOPE = ("app", "tests", "qa", "lakebase", "src", "tools", "resources", "sql")
EXT = (".sql", ".py", ".yml", ".yaml")
PCLASS = re.compile(r"\\+p\{")

_UC_DOWN = ("UC-only (Databricks SQL warehouse, run by tools/run_migration.py in UC mode, never on PostgreSQL): the "
            "QA-057 prod guard strips format / separator characters from :catalog with the Java classes Cf and Z; "
            "the PostgreSQL emulation in tests/test_migration_downs.py replaces them before running the file")
ALLOWED = {
    # path: (exact occurrence count, justification)
    **{os.path.join("sql", "migrations", f): (4, _UC_DOWN) for f in (
        "v1_5_001_contract_down.sql", "v1_6_001_audit_chain_down.sql", "v1_6_002_chain_head_down.sql",
        "v1_6_003_alarm_rule_flag_down.sql", "v1_7_001_cdse_ack_down.sql", "v1_7_002_canon_v2_down.sql",
        "v1_7_003_dssmp_registration_down.sql", "v1_8_001_dam_lifecycle_down.sql",
        "v1_8_002_lifecycle_backfill_down.sql", "v1_8_003_lifecycle_safety_flag_down.sql")},
    os.path.join("tools", "verify", "qa057_guard_probe.sql"): (
        4, "UC-only manual probe of the same guard on a real Databricks warehouse (VERIFICATION QA-057); never PG"),
    os.path.join("tests", "test_migration_downs.py"): (
        4, "UC SQL emulation: finds the guard's Java class in the UC downs and replaces it with a PostgreSQL ARE "
           "class (PG_CF_CLASS) before the file runs on PostgreSQL; the literal is the search key"),
    os.path.join("tests", "test_qa057_prod_guard.py"): (
        5, "asserts the UC downs carry exactly the Java class (and documents that it is Databricks-only); "
           "never sent to PostgreSQL"),
    os.path.join("tests", "test_qa059_lakebase_rerun.py"): (
        2, "the live proof that PostgreSQL 17 rejects the class (sends it ON PURPOSE and asserts the error) plus the "
           "module docstring that names it"),
}


def _files():
    for top in SCOPE:
        for base, dirs, names in os.walk(os.path.join(ROOT, top)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules", ".pytest_cache")]
            for n in sorted(names):
                if n.endswith(EXT):
                    yield os.path.relpath(os.path.join(base, n), ROOT)


def _hits(rel):
    txt = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    if rel.endswith(".sql"):
        txt = "\n".join(l.split("--")[0] for l in txt.splitlines())
    return len(PCLASS.findall(txt))


def test_no_pclass_outside_the_allowlist_and_exact_counts_inside_it():
    seen = {rel: _hits(rel) for rel in _files()}
    bad = {rel: n for rel, n in seen.items() if n and rel not in ALLOWED}
    assert bad == {}, f"QA-062: PostgreSQL cannot run these (PG 17 ARE rejects the class): {bad}"
    drift = {rel: (seen.get(rel), n) for rel, (n, _) in ALLOWED.items() if seen.get(rel) != n}
    assert drift == {}, f"QA-062: allowlisted files changed their occurrence count (seen, allowed): {drift}"


def test_allowlist_is_narrow_files_only_each_justified_and_nothing_lakebase_or_app():
    for rel, (n, why) in ALLOWED.items():
        assert os.path.isfile(os.path.join(ROOT, rel)), rel                       # a file, never a folder
        assert n > 0 and len(why) >= 40, rel
        assert not rel.startswith(("lakebase", "app", "src", "qa")), rel           # PG-bound code: no exceptions
        if rel.startswith("sql"):
            assert rel.endswith("_down.sql"), rel                                  # only the UC guard
    assert set(SCOPE) >= {"app", "tests", "lakebase", "src", "sql", "tools"}


def test_the_scanner_catches_sql_strings_in_python_and_sql_files(tmp_path):
    bs = "\\"
    py = f'SQL = "SELECT regexp_replace(x, \'[{bs}{bs}p{{Z}}]\', \'\', \'g\')"\n'
    sql = f"SELECT regexp_replace(x, '[{bs}p{{Cf}}]', '');  -- and {bs}p{{Z}} in a comment\n"
    assert PCLASS.findall(py) and len(PCLASS.findall("\n".join(l.split("--")[0] for l in sql.splitlines()))) == 1
