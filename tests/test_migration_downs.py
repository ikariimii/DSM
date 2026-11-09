"""Residual (b): a DOWN script for every UC migration (sql/migrations/*_down.sql). Static: each down names Delta
RESTORE as the data fallback, is idempotent, drops / deletes / updates no data and no column, and takes the CHECK
state of the migration back to exactly the state before it (simulated over the whole up/down sequence). Live
(DAM_TEST_PG): the Delta SQL is translated mechanically to PostgreSQL (guarded ADD COLUMNS -> ADD COLUMN IF NOT EXISTS,
TBLPROPERTIES / VOLUME / USE CATALOG dropped, RLIKE -> ~, the QA-057 guard bound to dam_dev) and the full chain is run: every up twice, every down twice
in reverse order (constraint set checked after each step, rows and columns kept), then every up again.
Delta itself stays UNVERIFIED (docs/VERIFICATION.md MIGRATION-DOWN)."""
import os
import re
import shutil
import subprocess
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIG_DIR = os.path.join(ROOT, "sql", "migrations")
UPS = sorted(f for f in os.listdir(MIG_DIR) if f.endswith(".sql") and not f.endswith("_down.sql"))
DOWN = {f: f[:-4] + "_down.sql" for f in UPS}
TXT = {f: open(os.path.join(MIG_DIR, f)).read() for f in os.listdir(MIG_DIR) if f.endswith(".sql")}
norm = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731


def _code(sql):
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def _apply_checks(state, sql):
    """CHECK constraints after running `sql` on `state` {(table, name): body}."""
    state = dict(state)
    for m in re.finditer(r"ALTER TABLE ([\w.]+) (DROP CONSTRAINT IF EXISTS (\w+);|ADD CONSTRAINT (\w+)\s+CHECK "
                         r"\((.*?)\);)", _code(sql), re.S):
        if m.group(3):
            state.pop((m.group(1), m.group(3)), None)
        else:
            state[(m.group(1), m.group(4))] = norm(m.group(5))
    return state


def _states():
    st, out = {}, [{}]
    for f in UPS:
        st = _apply_checks(st, TXT[f])
        out.append(st)
    return out


def test_every_migration_has_a_down():
    assert [f for f in UPS if DOWN[f] not in TXT] == []


@pytest.mark.parametrize("f", UPS)
def test_down_header_and_no_data_or_column_loss(f):
    d = TXT[DOWN[f]]
    for s in ("RESTORE", "IDEMPOTENT", "UNVERIFIED", "PRE-CHECK", f"DOWN for {f}"):
        assert s in d, (f, s)
    code = _code(d).upper()
    assert "USE CATALOG IDENTIFIER(:CATALOG);" in code
    for bad in ("DROP TABLE", "DROP COLUMN", "DELETE FROM", "TRUNCATE", "UNSET TBLPROPERTIES", "DROP VOLUME",
                "DROP SCHEMA", "REPLACE COLUMNS", "RENAME"):
        assert bad not in code, (f, bad)
    assert not re.search(r"\bUPDATE\s+\w+\.\w+\s+SET\b", code), f


@pytest.mark.parametrize("i", range(len(UPS)))
def test_down_restores_the_check_state_before_its_migration(i):
    states = _states()
    assert _apply_checks(states[i + 1], TXT[DOWN[UPS[i]]]) == states[i], UPS[i]


# ---------------------------------------------------------------- PostgreSQL translation + full chain
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")
TYPES = {"STRING": "TEXT", "TIMESTAMP": "TIMESTAMPTZ", "DOUBLE": "DOUBLE PRECISION", "INT": "INT", "BIGINT": "BIGINT",
         "BOOLEAN": "BOOLEAN", "DATE": "DATE", "FLOAT": "REAL"}
DDL = "\n".join(open(os.path.join(ROOT, "sql", "ddl", f)).read() for f in sorted(os.listdir(os.path.join(ROOT, "sql", "ddl"))))


def _pgtype(t):
    t = t.upper()
    return TYPES.get(t, "NUMERIC" if t.startswith("DECIMAL") else "TEXT")


def _added_cols():
    out = set()
    for f in UPS:
        out |= {(f"{s}.{t}", c) for s, t, c in re.findall(
            r"EXECUTE IMMEDIATE 'ALTER TABLE (\w+)\.(\w+) ADD COLUMNS \((\w+) \w+\)'", TXT[f])}
    return out


def _base_table(table):
    """The table as before v1.5: current DDL columns minus every column a migration adds; nullable, no CHECKs."""
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \((.*?)\n\)", DDL, re.S).group(1)
    added = _added_cols()
    cols = []
    for line in _code(body).splitlines():
        m = re.match(r"\s+(\w+)\s+(\w+(?:\([\d, ]+\))?|ARRAY<[^>]+>|MAP<[^>]+>)", line)
        if m and m.group(1).upper() not in ("CONSTRAINT", "PRIMARY", "FOREIGN") and (table, m.group(1)) not in added:
            cols.append(f'"{m.group(1)}" {_pgtype(m.group(2))}')
    return f"CREATE TABLE {table} ({', '.join(cols)});"


def _strip_comments(sql):
    """Remove -- comments outside string literals (a COMMENT '...' may contain '--')."""
    out, q, i = [], False, 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            q = not q
        elif not q and sql.startswith("--", i):
            while i < len(sql) and sql[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


PG_RAISE_ERROR = """CREATE OR REPLACE FUNCTION pg_temp.raise_error(msg text) RETURNS text LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION '%', msg; END $$;
CREATE OR REPLACE FUNCTION pg_temp.sha2(t text, n int) RETURNS text LANGUAGE sql AS
$$ SELECT encode(sha256(convert_to(t, 'UTF8')), 'hex') $$;
CREATE OR REPLACE FUNCTION pg_temp.get_json_object(t text, path text) RETURNS text LANGUAGE plpgsql AS
$$ BEGIN RETURN (t::jsonb) ->> substr(path, 3); EXCEPTION WHEN others THEN RETURN NULL; END $$;
"""
# v1.8: Spark SQL -> PostgreSQL for the lifecycle migrations (Databricks semantics kept: get_json_object returns NULL
# on unreadable JSON; split with a literal delimiter; sha2(x, 256) hex; inline VALUES tables)
PG_FUNCS = ((r"(?<!\.)\bsha2\(", "pg_temp.sha2("), (r"(?<!\.)\bget_json_object\(", "pg_temp.get_json_object("),
            (r"\bsplit\(", "string_to_array("), (r"\bsize\(", "cardinality("), (r"ARRAY<STRING>", "TEXT[]"),
            (r"AS STRING\)", "AS TEXT)"), (r"AS TIMESTAMP\)", "AS TIMESTAMPTZ)"),
            (r"current_timestamp\(\)", "current_timestamp"))
# Databricks regex [\p{Cf}\p{Z}\s] emulated in PostgreSQL ARE: whitespace + the common format (Cf) code points + every
# separator (Z: Zs spaces U+00A0, U+1680, U+2000-U+200A, U+202F, U+205F, U+3000; Zl U+2028; Zp U+2029).
PG_CF_CLASS = r"'[[:space:]\u00ad\u0600-\u0605\u061c\u06dd\u070f\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]'"
USE = "USE CATALOG IDENTIFIER(:catalog);"


def _lit(v):
    return "NULL" if v is None else "'" + v.replace("'", "''") + "'"


def _bind_guard(code, catalog, session, resolved, break_glass):
    """Bind the QA-057 parameters. current_catalog() is `session` before USE CATALOG and `resolved` after it;
    resolved=None simulates a catalog name that does not exist (USE CATALOG fails)."""
    code = code.replace("'[\\\\p{Cf}\\\\p{Z}\\\\s]', ''", PG_CF_CLASS + ", '', 'g'")
    code = code.replace("(:catalog,", f"({_lit(catalog)}::text,").replace(":break_glass", f"{_lit(break_glass)}::text")
    if USE in code:
        before, after = code.split(USE, 1)
        use = "" if resolved is not None else "SELECT pg_temp.raise_error('CATALOG_NOT_FOUND');"
        code = before.replace("current_catalog()", _lit(session)) + use + after.replace("current_catalog()",
                                                                                       _lit(resolved))
    else:
        code = code.replace("current_catalog()", _lit(session))
    return code.replace("raise_error(", "pg_temp.raise_error(").replace("pg_temp.pg_temp.", "pg_temp.")


def _pg(sql, catalog="dam_dev", session="dam_dev", resolved="dam_dev", break_glass=""):
    code = _strip_comments(sql)
    code = PG_RAISE_ERROR + _bind_guard(code, catalog, session, resolved, break_glass)
    code = re.sub(r"(?s)\nBEGIN\n(.*?)\nEND;", lambda m: "\n" + "\n".join(
        f"ALTER TABLE {s}.{t} ADD COLUMN IF NOT EXISTS {c} {_pgtype(ty)};" for s, t, c, ty in re.findall(
            r"EXECUTE IMMEDIATE 'ALTER TABLE (\w+)\.(\w+) ADD COLUMNS \((\w+) (\w+)\)'", m.group(1))), code)
    code = re.sub(r"ALTER TABLE [\w.]+ SET TBLPROPERTIES \([^;]*\);", "", code)
    code = re.sub(r"COMMENT '(?:[^']|'')*'", "", code)
    code = re.sub(r"CREATE VOLUME[^;]*;", "", code)
    code = re.sub(r"\)\s*TBLPROPERTIES \([^)]*\)\s*;", ");", code)
    code = re.sub(r"(?i)\bRLIKE\b", "~", code)
    code = re.sub(r"`(\w+)`", r'"\1"', code)
    for a, b in PG_FUNCS:
        code = re.sub(a, b, code)
    code = re.sub(r"(?s)FROM VALUES(.*?) AS (\w+)\(", r"FROM (VALUES\1) AS \2(", code)

    def ctab(m):
        lines = []
        for l in m.group(2).splitlines():
            t = re.match(r"(\s+\w+\s+)(\w+)(.*)", l)
            if t and t.group(1).strip().upper() not in ("CONSTRAINT", "PRIMARY"):
                l = t.group(1) + _pgtype(t.group(2)) + re.sub(r"\bGENERATED ALWAYS AS \(.*?\)", "", t.group(3))
            lines.append(l)
        return f"CREATE TABLE IF NOT EXISTS {m.group(1)} (" + "\n".join(lines) + "\n);"
    code = re.sub(r"(?s)CREATE TABLE IF NOT EXISTS ([\w.]+) \((.*?)\n\);", ctab, code)
    return code


def _psql(conn, sql=None, f=None):
    args = ["psql", conn, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"] + (["-c", sql] if sql else ["-f", f])
    return subprocess.run(args, capture_output=True, text=True)


@pytest.fixture()
def db(tmp_path):
    name = f"migdn_{uuid.uuid4().hex[:8]}"
    assert _psql(PG, f"CREATE DATABASE {name}").returncode == 0
    conn = f"{PG} dbname={name}"
    yield conn, tmp_path
    _psql(PG, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _run(conn, tmp, sql, label):
    p = tmp / f"{label}.sql"
    p.write_text(_pg(sql))
    r = _psql(conn, f=str(p))
    assert r.returncode == 0, f"{label}: {r.stderr}\n{p.read_text()[:3000]}"


def _cks(conn):
    out = _psql(conn, "SELECT n.nspname || '.' || c.relname || '|' || k.conname FROM pg_constraint k "
                      "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                      "WHERE k.contype = 'c' AND n.nspname IN ('ops', 'ref', 'audit', 'silver')").stdout.split()
    return {tuple(x.split("|")) for x in out}


TABLES = ("silver.incl_reduced", "ops.alarm_transition", "ref.incl_correction", "audit.review_record",
          "ref.threshold", "ref.dssmp_version", "ref.dam", "audit.app_extract")
COUNT_SQL = " UNION ALL ".join(f"SELECT '{t}:' || count(*) FROM {t}" for t in
                               ("ops.alarm_transition", "ref.incl_correction", "audit.review_record", "ref.threshold"))


@live
def test_full_chain_up_down_up_on_postgres(db):
    conn, tmp = db
    base = "CREATE SCHEMA silver; CREATE SCHEMA ops; CREATE SCHEMA ref; CREATE SCHEMA audit;\n" + \
        "\n".join(_base_table(t) for t in TABLES) + """
INSERT INTO ops.alarm_transition (event) VALUES ('ACK');
INSERT INTO ref.incl_correction (applied) VALUES (false);
INSERT INTO audit.review_record (review_id, dam_id, subject_type, decision, comment)
  VALUES ('R1', 'D', 'incl_correction', 'approve', 'ok');
INSERT INTO ref.threshold (threshold_id, threshold_version, dam_id) VALUES ('T-1', 1, 'D');"""
    r = _psql(conn, base)
    assert r.returncode == 0, r.stderr
    counts = sorted(_psql(conn, COUNT_SQL).stdout.split())
    states = _states()
    for f in UPS:
        for k in range(2):
            _run(conn, tmp, TXT[f], f"{f}-{k}")
    assert _cks(conn) == set(states[-1])
    assert _psql(conn, "SELECT cdse_ack_required FROM ref.threshold").stdout.strip() == "f"     # backfilled
    for i in reversed(range(len(UPS))):
        for k in range(2):
            _run(conn, tmp, TXT[DOWN[UPS[i]]], f"{DOWN[UPS[i]]}-{k}")
        assert _cks(conn) == set(states[i]), UPS[i]
    assert sorted(_psql(conn, COUNT_SQL).stdout.split()) == counts                              # no row lost
    for table, col in _added_cols():                                                            # no column lost
        s, t = table.split(".")
        assert _psql(conn, f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{s}' AND "
                           f"table_name = '{t}' AND column_name = '{col}'").stdout.strip() == "1", (table, col)
    assert _psql(conn, "SELECT cdse_ack_required FROM ref.threshold").stdout.strip() == "f"     # value kept
    assert _psql(conn, "SELECT count(*) FROM audit.chain_head").returncode == 0                 # table kept
    assert _psql(conn, "SELECT count(*) FROM ref.dam_cdse_assignment").returncode == 0
    for f in UPS:
        _run(conn, tmp, TXT[f], f"{f}-again")
    assert _cks(conn) == set(states[-1])
    assert sorted(_psql(conn, COUNT_SQL).stdout.split()) == counts
