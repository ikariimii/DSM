"""v1.5: sql/migrations upgrade existing tables to the sql/ddl shape, idempotently (static checks; the migration
itself is UNVERIFIED on a workspace, docs/VERIFICATION.md MIGRATION-1)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIG = open(os.path.join(ROOT, "sql", "migrations", "v1_5_001_contract.sql")).read()
DDL = {f: open(os.path.join(ROOT, "sql", "ddl", f)).read() for f in os.listdir(os.path.join(ROOT, "sql", "ddl"))}


def _code(sql):
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def _ddl_type(table, col):
    schema, name = table.split(".")
    for txt in DDL.values():
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {schema}\.{name} \((.*?)\n\)", txt, re.S)
        if m:
            t = re.search(rf"^\s*{col}\s+(\w+)", m.group(1), re.M)
            return t and t.group(1)
    return None


EXPECTED = [("silver.incl_reduced", "qc_rule_version"), ("silver.incl_reduced", "installation_version"),
            ("silver.incl_reduced", "probe_constants"), ("ops.alarm_transition", "qa_flag"),
            ("ref.incl_correction", "supersedes"), ("ref.incl_correction", "review_id")]


def test_every_column_add_is_guarded_and_matches_the_ddl_type():
    code = _code(MIG)
    adds = re.findall(r"IF NOT EXISTS \(SELECT 1 FROM information_schema\.columns WHERE table_schema = '(\w+)'\s+"
                      r"AND table_name = '(\w+)' AND column_name = '(\w+)'\) THEN\s+EXECUTE IMMEDIATE "
                      r"'ALTER TABLE (\w+)\.(\w+) ADD COLUMNS \((\w+) (\w+)\)';\s+END IF;", code)
    assert [(f"{s}.{t}", c) for s, t, c, *_ in adds] == EXPECTED
    for s, t, c, s2, t2, c2, typ in adds:
        assert (s, t, c) == (s2, t2, c2) and _ddl_type(f"{s}.{t}", c) == typ
    assert len(re.findall(r"ADD COLUMNS", code)) == len(EXPECTED)                          # none unguarded


def test_every_constraint_is_drop_if_exists_then_add_and_identical_to_ddl():
    code = _code(MIG)
    adds = re.findall(r"ALTER TABLE ([\w.]+) ADD CONSTRAINT (\w+)\s+CHECK", code)
    assert {n for _, n in adds} == {"ck_alarm_transition_value", "ck_incl_correction_applied_reviewed",
                                    "ck_review_record_subject_decision"}
    alldd = _code("\n".join(DDL.values()))
    norm = lambda s: re.sub(r"\s+", " ", s)
    for table, name in adds:
        assert f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name};" in code
        body = re.search(rf"ADD CONSTRAINT {name}\s+CHECK \((.*?)\);", code, re.S).group(1)
        dbody = re.search(rf"ADD CONSTRAINT {name}\s+CHECK \((.*?)\);", alldd, re.S).group(1)
        assert norm(body) == norm(dbody), name
    assert "SET TBLPROPERTIES ('delta.appendOnly' = 'true')" in code
    assert not re.search(r"\b(DROP TABLE|DELETE FROM|UPDATE \w+\.\w+ SET|TRUNCATE)\b", code)
