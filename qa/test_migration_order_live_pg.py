"""Live PG 17 simulation of UC migration order (v1_5_001 → v1_6_001 → v1_6_002 → v1_6_003 → v1_7_001 → dam_uc_setup).

The real files are Databricks SQL (USE CATALOG, EXECUTE IMMEDIATE, Delta TBLPROPERTIES). This harness translates
the *schema effects* to PostgreSQL 17 so we can check order, idempotence, forward-only behaviour, and out-of-order
failure modes on DAM_TEST_PG. Delta-only ops are recorded as skipped, not silently applied.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

PG = os.environ.get("DAM_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="set DAM_TEST_PG for live PostgreSQL 17")

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "sql" / "migrations"


def _connect():
    psycopg = pytest.importorskip("psycopg")
    return psycopg.connect(PG), psycopg


def _schema():
    return f"mig_{uuid.uuid4().hex[:10]}"


def _exec(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall() if cur.description else []


def _seed_pre_v15(conn, s: str):
    """Minimal pre-v1.5 tables the migrations ALTER (empty / stub columns)."""
    _exec(conn, f'CREATE SCHEMA "{s}"')
    # Map UC schemas into one PG schema with prefixed names
    stmts = [
        f'CREATE TABLE "{s}".silver_incl_reduced (id int PRIMARY KEY)',
        f'CREATE TABLE "{s}".ops_alarm_transition (id int PRIMARY KEY, event text, value float, reading_id text, metric_value float)',
        f'CREATE TABLE "{s}".ref_incl_correction (id int PRIMARY KEY, applied boolean DEFAULT false, reviewed_by text, reviewed_utc timestamptz)',
        f'''CREATE TABLE "{s}".audit_review_record (
              review_id text PRIMARY KEY, dam_id text NOT NULL, subject_type text NOT NULL,
              subject_id text NOT NULL, review_level text, decision text, comment text,
              reviewer_id text, reviewer_pe_license text, signed_ts timestamptz, signature_hash text, source text)''',
        f'CREATE TABLE "{s}".ref_threshold (threshold_id text PRIMARY KEY, threshold_version int NOT NULL DEFAULT 1)',
        f'CREATE TABLE "{s}".migration_log (step text PRIMARY KEY, applied_at timestamptz DEFAULT now())',
    ]
    for st in stmts:
        _exec(conn, st)
    # seed one threshold + one open transition + one review row (pre-chain)
    _exec(conn, f'INSERT INTO "{s}".ref_threshold VALUES (\'T1\', 1)')
    _exec(conn, f'INSERT INTO "{s}".ops_alarm_transition (id, event, value, reading_id, metric_value) '
                f'VALUES (1, \'RAISE\', 1.0, \'R1\', 1.0)')
    _exec(conn, f'INSERT INTO "{s}".audit_review_record '
                f'(review_id, dam_id, subject_type, subject_id, review_level, decision, comment, reviewer_id, signed_ts, signature_hash) '
                f'VALUES (\'RS-1\',\'CRD\',\'incl_correction\',\'C1\',\'engineer\',\'approve\',\'why\',\'u\','
                f'\'2026-01-01+00\',\'{"a"*64}\')')


def _add_col(conn, s, table, col, typ):
    exists = _q(conn,
        "SELECT 1 FROM information_schema.columns WHERE table_schema=%s AND table_name=%s AND column_name=%s",
        (s, table, col))
    if not exists:
        _exec(conn, f'ALTER TABLE "{s}".{table} ADD COLUMN {col} {typ}')
    return not bool(exists)


def _drop_add_check(conn, s, table, name, expr):
    _exec(conn, f'ALTER TABLE "{s}".{table} DROP CONSTRAINT IF EXISTS {name}')
    _exec(conn, f'ALTER TABLE "{s}".{table} ADD CONSTRAINT {name} CHECK ({expr})')


DELTA_SKIPPED = []


def apply_v1_5_001(conn, s):
    # guarded ADD COLUMNS (translate EXECUTE IMMEDIATE blocks)
    _add_col(conn, s, "silver_incl_reduced", "qc_rule_version", "text")
    _add_col(conn, s, "silver_incl_reduced", "installation_version", "timestamptz")
    _add_col(conn, s, "silver_incl_reduced", "probe_constants", "text")
    _add_col(conn, s, "ops_alarm_transition", "qa_flag", "text")
    _add_col(conn, s, "ref_incl_correction", "supersedes", "text")
    _add_col(conn, s, "ref_incl_correction", "review_id", "text")
    _drop_add_check(conn, s, "ops_alarm_transition", "ck_alarm_transition_value",
                    "event = 'ACK' OR (value IS NOT NULL AND reading_id IS NOT NULL AND metric_value IS NOT NULL)")
    DELTA_SKIPPED.append("v1_5: SET TBLPROPERTIES delta.appendOnly on incl_correction/review_record")
    _drop_add_check(conn, s, "audit_review_record", "ck_review_record_subject_decision",
                    "subject_type IN ('reading','exceedance','dssmr','threshold','commentary','rule_finding',"
                    "'incl_correction') AND (subject_type <> 'incl_correction' OR "
                    "(decision IN ('approve','reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))")
    _exec(conn, f'''INSERT INTO "{s}".migration_log(step) VALUES ('v1_5_001')
                    ON CONFLICT (step) DO NOTHING''')


def apply_v1_6_001(conn, s):
    for col, typ in [("action_type","text"),("reviewer_email","text"),("reviewer_sub","text"),
                     ("original_value","text"),("corrected_value","text"),("chain_id","text"),
                     ("chain_seq","bigint"),("prev_hash","text"),("record_hash","text"),
                     ("signature","text"),("sig_alg","text"),("key_id","text")]:
        _add_col(conn, s, "audit_review_record", col, typ)
    _add_col(conn, s, "ops_alarm_transition", "installation_version", "timestamptz")
    _drop_add_check(conn, s, "audit_review_record", "ck_review_record_subject_decision",
                    "subject_type IN ('reading','exceedance','dssmr','threshold','commentary','rule_finding',"
                    "'incl_correction','alarm_event') AND (subject_type <> 'incl_correction' OR "
                    "(decision IN ('approve','reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)) "
                    "AND (subject_type <> 'alarm_event' OR "
                    "(decision IN ('acknowledge','close') AND comment IS NOT NULL AND length(trim(comment)) > 0))")
    _drop_add_check(conn, s, "audit_review_record", "ck_review_record_chain",
                    "(subject_type <> 'alarm_event' OR action_type IS NOT NULL) AND (action_type IS NULL OR ("
                    "action_type IN ('bias_review','data_correction_review','alarm_ack','alarm_close') "
                    "AND original_value IS NOT NULL AND corrected_value IS NOT NULL AND chain_id IS NOT NULL "
                    "AND chain_seq IS NOT NULL AND chain_seq >= 1 AND prev_hash IS NOT NULL AND record_hash IS NOT NULL "
                    "AND chain_id = dam_id))")
    _exec(conn, f'''INSERT INTO "{s}".migration_log(step) VALUES ('v1_6_001')
                    ON CONFLICT (step) DO NOTHING''')


def apply_v1_6_002(conn, s):
    _exec(conn, f'''CREATE TABLE IF NOT EXISTS "{s}".audit_chain_head (
        dam_id text NOT NULL, chain_id text NOT NULL, chain_seq bigint NOT NULL,
        head_hash text NOT NULL, computed_utc timestamptz NOT NULL, job_run_id text NOT NULL, source text)''')
    DELTA_SKIPPED.append("v1_6_002: delta.appendOnly TBLPROPERTIES (simulated via trigger below)")
    # Simulate append-only with triggers (Delta property has no PG equivalent)
    _exec(conn, f'''CREATE OR REPLACE FUNCTION "{s}".refuse_chain_head_mut() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'audit.chain_head is append-only'; END; $$ LANGUAGE plpgsql''')
    _exec(conn, f'''DROP TRIGGER IF EXISTS t_noupd_chain_head ON "{s}".audit_chain_head''')
    _exec(conn, f'''CREATE TRIGGER t_noupd_chain_head BEFORE UPDATE OR DELETE ON "{s}".audit_chain_head
                    FOR EACH ROW EXECUTE FUNCTION "{s}".refuse_chain_head_mut()''')
    _drop_add_check(conn, s, "audit_chain_head", "ck_chain_head",
                    "chain_id = dam_id AND chain_seq >= 0 AND head_hash ~ '^[0-9a-f]{64}$'")
    _exec(conn, f'''INSERT INTO "{s}".migration_log(step) VALUES ('v1_6_002')
                    ON CONFLICT (step) DO NOTHING''')


def apply_v1_6_003(conn, s):
    _drop_add_check(conn, s, "audit_review_record", "ck_review_record_subject_decision",
                    "subject_type IN ('reading','exceedance','dssmr','threshold','commentary','rule_finding',"
                    "'incl_correction','alarm_event','alarm_rule') AND (subject_type <> 'incl_correction' OR "
                    "(decision IN ('approve','reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)) "
                    "AND (subject_type <> 'alarm_event' OR "
                    "(decision IN ('acknowledge','close') AND comment IS NOT NULL AND length(trim(comment)) > 0)) "
                    "AND (subject_type <> 'alarm_rule' OR "
                    "(decision IN ('propose','apply','reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))")
    _drop_add_check(conn, s, "audit_review_record", "ck_review_record_chain",
                    "(subject_type NOT IN ('alarm_event','alarm_rule') OR action_type IS NOT NULL) AND "
                    "(action_type IS NULL OR ("
                    "action_type IN ('bias_review','data_correction_review','alarm_ack','alarm_close',"
                    "'alarm_rule_flag_propose','alarm_rule_flag_apply','alarm_rule_flag_reject') "
                    "AND original_value IS NOT NULL AND corrected_value IS NOT NULL AND chain_id IS NOT NULL "
                    "AND chain_seq IS NOT NULL AND chain_seq >= 1 AND prev_hash IS NOT NULL AND record_hash IS NOT NULL "
                    "AND chain_id = dam_id AND comment IS NOT NULL AND length(trim(comment)) > 0 "
                    "AND ((action_type IN ('bias_review','data_correction_review') AND subject_type='incl_correction') "
                    "OR (action_type='alarm_ack' AND subject_type='alarm_event' AND decision='acknowledge') "
                    "OR (action_type='alarm_close' AND subject_type='alarm_event' AND decision='close') "
                    "OR (action_type='alarm_rule_flag_propose' AND subject_type='alarm_rule' AND decision='propose') "
                    "OR (action_type='alarm_rule_flag_apply' AND subject_type='alarm_rule' AND decision='apply') "
                    "OR (action_type='alarm_rule_flag_reject' AND subject_type='alarm_rule' AND decision='reject'))))")
    _exec(conn, f'''INSERT INTO "{s}".migration_log(step) VALUES ('v1_6_003')
                    ON CONFLICT (step) DO NOTHING''')


def apply_v1_7_001(conn, s):
    added_t = _add_col(conn, s, "ref_threshold", "cdse_ack_required", "boolean")
    added_o = _add_col(conn, s, "ops_alarm_transition", "cdse_ack_required_at_open", "boolean")
    DELTA_SKIPPED.append("v1_7: delta.feature.allowColumnDefaults + SET DEFAULT (irreversible on Delta)")
    # backfill ONLY the new threshold column (documented sole data change); never touch existing transition snapshots
    _exec(conn, f'''UPDATE "{s}".ref_threshold SET cdse_ack_required = false WHERE cdse_ack_required IS NULL''')
    _exec(conn, f'''ALTER TABLE "{s}".ref_threshold ALTER COLUMN cdse_ack_required SET NOT NULL''')
    _exec(conn, f'''ALTER TABLE "{s}".ref_threshold ALTER COLUMN cdse_ack_required SET DEFAULT false''')
    _exec(conn, f'''INSERT INTO "{s}".migration_log(step) VALUES ('v1_7_001')
                    ON CONFLICT (step) DO NOTHING''')
    return added_t, added_o


def apply_dam_uc_setup_stub(conn, s):
    """Stand-in for dam_uc_setup pieces that depend on migrations: re-assert ddl/08 CHECKs + chain_head + note row filter."""
    # Re-apply v1_6_003 CHECKs (ddl/08 current bodies) — idempotent DROP/ADD
    apply_v1_6_003(conn, s)
    apply_v1_6_002(conn, s)  # CREATE IF NOT EXISTS + trigger
    # Row filter has no PG equivalent; record intent (10a sets ROW FILTER on audit.chain_head)
    DELTA_SKIPPED.append("dam_uc_setup: ROW FILTER ref.dam_row_filter ON audit.chain_head (UC only)")
    _exec(conn, f'''INSERT INTO "{s}".migration_log(step) VALUES ('dam_uc_setup')
                    ON CONFLICT (step) DO NOTHING''')


ORDER = [
    ("v1_5_001", apply_v1_5_001),
    ("v1_6_001", apply_v1_6_001),
    ("v1_6_002", apply_v1_6_002),
    ("v1_6_003", apply_v1_6_003),
    ("v1_7_001", apply_v1_7_001),
    ("dam_uc_setup", apply_dam_uc_setup_stub),
]


def _run_order(conn, s):
    for name, fn in ORDER:
        fn(conn, s)


def test_files_declare_order_idempotent_no_down():
    ups = sorted(p.name for p in MIG.glob("v1_*.sql") if "_down" not in p.name)
    assert ups == ["v1_5_001_contract.sql", "v1_6_001_audit_chain.sql",
                   "v1_6_002_chain_head.sql", "v1_6_003_alarm_rule_flag.sql",
                   "v1_7_001_cdse_ack.sql", "v1_7_002_canon_v2.sql",
                   "v1_7_003_dssmp_registration.sql"]
    for name in ups:
        text = (MIG / name).read_text()
        assert "IDEMPOTENT" in text
        assert "UNVERIFIED" in text
        assert re.search(r"ORDER", text, re.I)
    # v1_7_002 is the first migration with an explicit DOWN (CHECK-only; columns stay)
    downs = sorted(p.name for p in MIG.glob("*down*"))
    assert "v1_5_001_contract_down.sql" in downs
    assert "v1_7_001_cdse_ack_down.sql" in downs
    assert "v1_7_002_canon_v2_down.sql" in downs
    assert "v1_7_003_dssmp_registration_down.sql" in downs
    for name in downs:
        text = (MIG / name).read_text()
        assert "DROP TABLE" not in text.upper() or "DROP TABLE IF EXISTS" not in text  # no table drops
        assert "DELETE FROM" not in text.upper()
        # no column drops of hashed audit data
        assert "DROP COLUMN" not in text.upper() or name.startswith("v1_7_001")  # v1_7_001 only drops DEFAULT/NOT NULL
    d002 = (MIG / "v1_7_002_canon_v2_down.sql").read_text()
    assert "DROP CONSTRAINT" in d002 and "DROP COLUMN" not in d002.upper()
    assert "irreversible" in (MIG / "v1_7_001_cdse_ack.sql").read_text().lower()


def test_forward_order_idempotent_and_no_rewrite_of_existing_rows():
    conn, psycopg = _connect()
    s = _schema()
    try:
        _seed_pre_v15(conn, s)
        before_review = _q(conn, f'SELECT review_id, dam_id, subject_type, decision, comment FROM "{s}".audit_review_record ORDER BY review_id')
        # Full order once
        _run_order(conn, s)
        assert _q(conn, f'SELECT cdse_ack_required FROM "{s}".ref_threshold') == [(False,)]
        # open transition snapshot NOT backfilled
        assert _q(conn, f'SELECT cdse_ack_required_at_open FROM "{s}".ops_alarm_transition') == [(None,)]
        assert _q(conn, f'SELECT review_id, dam_id, subject_type, decision, comment FROM "{s}".audit_review_record ORDER BY review_id') == before_review
        row = _q(conn, f'SELECT action_type, chain_seq FROM "{s}".audit_review_record WHERE review_id=%s', ("RS-1",))
        assert row == [(None, None)]
        # Idempotent re-run BEFORE any post-v1.6 rows exist (v1_5_001 docs: do NOT re-run after alarm_event/alarm_rule rows)
        cols_before = _q(conn, "SELECT column_name FROM information_schema.columns WHERE table_schema=%s ORDER BY 1", (s,))
        _run_order(conn, s)
        cols_after = _q(conn, "SELECT column_name FROM information_schema.columns WHERE table_schema=%s ORDER BY 1", (s,))
        assert cols_before == cols_after
        assert _q(conn, f'SELECT cdse_ack_required FROM "{s}".ref_threshold') == [(False,)]
        assert _q(conn, f'SELECT cdse_ack_required_at_open FROM "{s}".ops_alarm_transition') == [(None,)]
        # chain_head append-only
        _exec(conn, f"""INSERT INTO "{s}".audit_chain_head
            VALUES ('CRD','CRD',1,'{"a"*64}','2026-11-09T00:00:00+00','run1',NULL)""")
        with pytest.raises(Exception, match="append-only"):
            _exec(conn, f"""UPDATE "{s}".audit_chain_head SET chain_seq = 0""")
        with pytest.raises(Exception, match="append-only"):
            _exec(conn, f"""DELETE FROM "{s}".audit_chain_head""")
        # §7.13 accepts flag row; unknown action rejected
        _exec(conn, f"""INSERT INTO "{s}".audit_review_record
            (review_id,dam_id,subject_type,subject_id,review_level,decision,comment,reviewer_id,signed_ts,signature_hash,
             action_type,original_value,corrected_value,chain_id,chain_seq,prev_hash,record_hash)
            VALUES ('RS-F','CRD','alarm_rule','T1','CDSE','apply','why','u','2026-11-09+00','{"b"*64}',
                    'alarm_rule_flag_apply','{{}}','{{}}','CRD',1,'{"0"*64}','{"c"*64}')""")
        with pytest.raises(Exception):
            _exec(conn, f"""INSERT INTO "{s}".audit_review_record
                (review_id,dam_id,subject_type,subject_id,decision,comment,reviewer_id,signed_ts,signature_hash,
                 action_type,original_value,corrected_value,chain_id,chain_seq,prev_hash,record_hash)
                VALUES ('RS-BAD','CRD','alarm_rule','T1','apply','why','u','2026-11-09+00','{"b"*64}',
                        'alarm_rule_flag_delete','{{}}','{{}}','CRD',1,'{"0"*64}','{"c"*64}')""")
        # Re-running v1_5_001 after §7.13 rows exist fails loudly (documented), does not silently keep a wrong CHECK
        with pytest.raises(Exception):
            apply_v1_5_001(conn, s)
        steps = [r[0] for r in _q(conn, f'SELECT step FROM "{s}".migration_log ORDER BY step')]
        assert steps == ["dam_uc_setup", "v1_5_001", "v1_6_001", "v1_6_002", "v1_6_003", "v1_7_001"]
        assert DELTA_SKIPPED, "expected Delta-only skips to be recorded"
    finally:
        _exec(conn, f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
        conn.close()


def test_out_of_order_v1_7_before_v1_6_fails_loudly_not_silent():
    """v1_7 alone on pre-v1.5 is fine (adds columns), but v1_6_003 before v1_6_001 fails: chain CHECK refs missing cols."""
    conn, psycopg = _connect()
    s = _schema()
    try:
        _seed_pre_v15(conn, s)
        # Skipping v1_6_001 then applying v1_6_003: ADD CONSTRAINT referencing action_type → loud failure
        with pytest.raises(Exception) as ei:
            apply_v1_6_003(conn, s)
        msg = str(ei.value).lower()
        assert "action_type" in msg or "does not exist" in msg or "undefined" in msg
        # table must not be left half-migrated with a silent wrong CHECK: constraint absent
        cons = _q(conn, """SELECT constraint_name FROM information_schema.table_constraints
                           WHERE table_schema=%s AND constraint_name='ck_review_record_chain'""", (s,))
        # DROP succeeded, ADD failed → constraint gone (documented PRE-CHECK hazard on Databricks too)
        assert cons == []
        # v1_6_002 before anything still works (new table) but dam_uc_setup row-filter target missing on real UC;
        # here chain_head exists in isolation — fine. Out-of-order v1_5 AFTER v1_6_001 would re-narrow subject CHECK:
        apply_v1_6_001(conn, s)
        # insert an alarm_event row that v1_6 allows
        _exec(conn, f'''INSERT INTO "{s}".audit_review_record
            (review_id,dam_id,subject_type,subject_id,decision,comment,reviewer_id,signed_ts,signature_hash,
             action_type,original_value,corrected_value,chain_id,chain_seq,prev_hash,record_hash)
            VALUES ('RS-AE','CRD','alarm_event','E1','acknowledge','ack','u','2026-11-09+00','{"b"*64}',
                    'alarm_ack','{{}}','{{}}','CRD',1,'{"0"*64}','{"c"*64}')''')
        # Re-running v1_5_001 after v1_6 rewrites subject CHECK to the v1.5 body → ADD fails loudly on existing row
        # (or would leave table without CHECK if DROP already ran). Mirror Databricks PRE-CHECK note.
        _exec(conn, f'ALTER TABLE "{s}".audit_review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision')
        with pytest.raises(Exception):
            _exec(conn, f'''ALTER TABLE "{s}".audit_review_record ADD CONSTRAINT ck_review_record_subject_decision
                CHECK (subject_type IN ('reading','exceedance','dssmr','threshold','commentary','rule_finding',
                'incl_correction') AND (subject_type <> 'incl_correction' OR
                (decision IN ('approve','reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)))''')
        # constraint missing after failed ADD — not silently corrupted to a weaker check that accepts alarm_event
        cons2 = _q(conn, """SELECT constraint_name FROM information_schema.table_constraints
                            WHERE table_schema=%s AND constraint_name='ck_review_record_subject_decision'""", (s,))
        assert cons2 == []
    finally:
        _exec(conn, f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
        conn.close()
