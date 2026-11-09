-- DOWN for v1_5_001_contract.sql. Data-level fallback: Delta time travel / RESTORE TABLE <table> TO VERSION AS OF
-- <version before v1_5_001> (DESCRIBE HISTORY <table>) for ops.alarm_transition, ref.incl_correction,
-- audit.review_record and silver.incl_reduced; this script never touches data.
-- What it reverses: ck_alarm_transition_value, ck_incl_correction_applied_reviewed and
-- ck_review_record_subject_decision are dropped (no CHECK existed before v1.5).
-- What it does NOT reverse, by design: the six added columns stay (they hold lineage / review links of rows written
-- since), and delta.appendOnly stays on ref.incl_correction and audit.review_record (removing it would make audit
-- data mutable). Run the later downs first (v1_6_001_audit_chain_down.sql restores the v1.5 review_record CHECK).
-- PRE-CHECK: none (only CHECKs are dropped).
-- IDEMPOTENT, safe to re-run. Re-applying v1_5_001 afterwards restores everything it reverses.
-- UNVERIFIED: not run on a workspace (static + PostgreSQL-translated tests: tests/test_migration_downs.py).
USE CATALOG IDENTIFIER(:catalog);

ALTER TABLE ops.alarm_transition DROP CONSTRAINT IF EXISTS ck_alarm_transition_value;
ALTER TABLE ref.incl_correction DROP CONSTRAINT IF EXISTS ck_incl_correction_applied_reviewed;
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
