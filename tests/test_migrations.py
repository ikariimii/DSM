"""v1.5 / v1.6: sql/migrations upgrade existing tables to the sql/ddl shape, idempotently (static checks; the
migrations themselves are UNVERIFIED on a workspace, docs/VERIFICATION.md MIGRATION-1 / MIGRATION-2).
Migration files are historical: a constraint redefined by a later file must equal the DDL only in the LAST file
that defines it (v1_5_001's ck_review_record_subject_decision is the v1.5 body, superseded by v1_6_001)."""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIG_DIR = os.path.join(ROOT, "sql", "migrations")
FILES = sorted(f for f in os.listdir(MIG_DIR) if f.endswith(".sql"))
MIGS = {f: open(os.path.join(MIG_DIR, f)).read() for f in FILES}
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


EXPECTED_COLS = {
    "v1_5_001_contract.sql": [("silver.incl_reduced", "qc_rule_version"), ("silver.incl_reduced", "installation_version"),
                              ("silver.incl_reduced", "probe_constants"), ("ops.alarm_transition", "qa_flag"),
                              ("ref.incl_correction", "supersedes"), ("ref.incl_correction", "review_id")],
    "v1_6_001_audit_chain.sql": [("audit.review_record", c) for c in (
        "action_type", "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id", "chain_seq",
        "prev_hash", "record_hash", "signature", "sig_alg", "key_id")] + [("ops.alarm_transition", "installation_version")],
    "v1_6_002_chain_head.sql": [],
    "v1_6_003_alarm_rule_flag.sql": [],
    "v1_7_001_cdse_ack.sql": [("ref.threshold", "cdse_ack_required"), ("ops.alarm_transition", "cdse_ack_required_at_open")],
}
EXPECTED_CKS = {
    "v1_5_001_contract.sql": {"ck_alarm_transition_value", "ck_incl_correction_applied_reviewed",
                              "ck_review_record_subject_decision"},
    "v1_6_001_audit_chain.sql": {"ck_review_record_subject_decision", "ck_review_record_chain"},
    "v1_6_002_chain_head.sql": {"ck_chain_head"},
    "v1_6_003_alarm_rule_flag.sql": {"ck_review_record_subject_decision", "ck_review_record_chain"},
    "v1_7_001_cdse_ack.sql": set(),
}
# The only data change any migration may make: backfilling a column the same file just added, marked by a
# '-- backfill-new-column: <table>.<col>' line right above an UPDATE ... SET <col> = <literal> WHERE <col> IS NULL.
_BACKFILL = r"UPDATE (\w+\.\w+) SET (\w+) = (?:false|true|0|'[^']*') WHERE \2 IS NULL;"
_ADD_CK = r"ALTER TABLE ([\w.]+) ADD CONSTRAINT (\w+)\s+CHECK"
norm = lambda s: re.sub(r"\s+", " ", s)


def test_known_migration_files():
    assert FILES == sorted(EXPECTED_COLS)


@pytest.mark.parametrize("f", FILES)
def test_every_column_add_is_guarded_and_matches_the_ddl_type(f):
    code = _code(MIGS[f])
    adds = re.findall(r"IF NOT EXISTS \(SELECT 1 FROM information_schema\.columns WHERE table_schema = '(\w+)'\s+"
                      r"AND table_name = '(\w+)' AND column_name = '(\w+)'\) THEN\s+EXECUTE IMMEDIATE "
                      r"'ALTER TABLE (\w+)\.(\w+) ADD COLUMNS \((\w+) (\w+)\)';\s+END IF;", code)
    assert [(f"{s}.{t}", c) for s, t, c, *_ in adds] == EXPECTED_COLS[f]
    for s, t, c, s2, t2, c2, typ in adds:
        assert (s, t, c) == (s2, t2, c2) and _ddl_type(f"{s}.{t}", c) == typ, (f, c)
    assert len(re.findall(r"ADD COLUMNS", code)) == len(EXPECTED_COLS[f])                  # none unguarded


@pytest.mark.parametrize("f", FILES)
def test_every_constraint_is_drop_if_exists_then_add(f):
    code = _code(MIGS[f])
    adds = re.findall(_ADD_CK, code)
    assert {n for _, n in adds} == EXPECTED_CKS[f]
    for table, name in adds:
        assert f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name};" in code
    marked = re.findall(r"-- backfill-new-column: (\w+\.\w+)\n" + _BACKFILL, MIGS[f])
    for table, t2, col in marked:
        assert table == f"{t2}.{col}".rsplit(".", 1)[0] + "." + col and (t2, col) in {(t, c) for t, c in EXPECTED_COLS[f]}
    code = re.sub(_BACKFILL, "", code, count=len(marked))
    assert not re.search(r"\b(DROP TABLE|DELETE FROM|UPDATE \w+\.\w+ SET|TRUNCATE)\b", code)
    assert "USE CATALOG IDENTIFIER(:catalog);" in code


def test_latest_definition_of_each_constraint_is_identical_to_ddl():
    alldd = _code("\n".join(DDL.values()))
    latest = {}
    for f in FILES:                                   # sorted: v1_5 < v1_6, later files win
        code = _code(MIGS[f])
        for _, name in re.findall(_ADD_CK, code):
            latest[name] = re.search(rf"ADD CONSTRAINT {name}\s+CHECK \((.*?)\);", code, re.S).group(1)
    assert latest
    for name, body in latest.items():
        dbody = re.search(rf"ADD CONSTRAINT {name}\s+CHECK \((.*?)\);", alldd, re.S).group(1)
        assert norm(body) == norm(dbody), name


def test_v1_5_keeps_append_only_property():
    assert "SET TBLPROPERTIES ('delta.appendOnly' = 'true')" in _code(MIGS["v1_5_001_contract.sql"])


def test_v1_6_header_documents_order_and_unverified():
    h = MIGS["v1_6_001_audit_chain.sql"]
    for s in ("v1_5_001_contract.sql", "dam_uc_setup", "Do NOT re-run v1_5_001", "IDEMPOTENT", "UNVERIFIED", "PRE-CHECK"):
        assert s in h, s


def test_v1_6_002_creates_chain_head_exactly_as_the_ddl_and_append_only():
    """QA-045d: the migration's CREATE TABLE is byte-for-byte the sql/ddl/08 one (modulo whitespace), keeps
    delta.appendOnly (no UPDATE/DELETE), and the header documents order, idempotence and UNVERIFIED."""
    pat = r"CREATE TABLE IF NOT EXISTS audit\.chain_head \(.*?\n\) TBLPROPERTIES \(delta\.appendOnly = true\)"
    m = re.search(pat, MIGS["v1_6_002_chain_head.sql"], re.S)
    d = re.search(pat, DDL["08_audit.sql"], re.S)
    assert m and d and norm(m.group(0)) == norm(d.group(0))
    assert "SET TBLPROPERTIES ('delta.appendOnly' = 'true')" in _code(MIGS["v1_6_002_chain_head.sql"])
    for s in ("v1_6_001_audit_chain.sql", "dam_uc_setup", "IDEMPOTENT", "UNVERIFIED", "PRE-CHECK"):
        assert s in MIGS["v1_6_002_chain_head.sql"], s
def test_v1_7_threshold_flag_is_backfilled_then_not_null_with_default():
    code = _code(MIGS["v1_7_001_cdse_ack.sql"])
    order = ["ADD COLUMNS (cdse_ack_required BOOLEAN)", "'delta.feature.allowColumnDefaults' = 'supported'",
             "ALTER COLUMN cdse_ack_required SET DEFAULT false",
             "UPDATE ref.threshold SET cdse_ack_required = false WHERE cdse_ack_required IS NULL",
             "ALTER COLUMN cdse_ack_required SET NOT NULL"]
    pos = [code.index(o) for o in order]
    assert pos == sorted(pos)
    assert "ops.alarm_transition SET" not in code                    # append-only: legacy RAISE rows stay NULL
    h = MIGS["v1_7_001_cdse_ack.sql"]
    for s in ("v1_6_001", "dam_uc_setup", "IDEMPOTENT", "UNVERIFIED", "PRE-CHECK", "irreversible"):
        assert s in h, s
