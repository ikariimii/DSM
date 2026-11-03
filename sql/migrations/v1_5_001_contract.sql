-- Migration v1.4/v1.5 contract (SCHEMA_CONTRACT §9 changelog): upgrade EXISTING tables created before v1.4.
-- Fresh installs get all of this from sql/ddl (CREATE TABLE IF NOT EXISTS does not alter an existing table, hence
-- this file). ORDER: on a deployment created before v1.5 run this file FIRST, then the dam_uc_setup job (sql/ddl/02
-- adds a CHECK naming review_id, which fails until the column exists). It assumes the tables exist, so it is not
-- for a fresh install. IDEMPOTENT, safe to re-run:
--   * columns: Delta has no ADD COLUMN IF NOT EXISTS, so each ADD is guarded by an information_schema.columns
--     lookup inside a SQL script block (BEGIN ... END, IF ... THEN EXECUTE IMMEDIATE ...).
--   * CHECK constraints: DROP CONSTRAINT IF EXISTS, then ADD (same pattern as sql/ddl).
--   * table properties: SET TBLPROPERTIES is idempotent.
-- PRE-CHECK before running (ADD CONSTRAINT fails if any existing row violates it, nothing is changed then):
--   SELECT count(*) FROM ops.alarm_transition WHERE NOT (event = 'ACK' OR (value IS NOT NULL AND
--     reading_id IS NOT NULL AND metric_value IS NOT NULL));                                    -- must be 0
--   SELECT count(*) FROM ref.incl_correction WHERE applied;   -- must be 0 (no applied row has a review_id yet;
--     an applied legacy row needs a review and a new applied version row first, SCHEMA_CONTRACT §5.5.1)
--   SELECT DISTINCT subject_type, decision FROM audit.review_record;     -- must fit ck_review_record_subject_decision
-- UNVERIFIED: not run on a workspace (no CLI/workspace here). SQL scripting needs a SQL warehouse / DBR version
-- that supports it (docs/VERIFICATION.md MIGRATION-1).
USE CATALOG IDENTIFIER(:catalog);

BEGIN
  -- v1.4 silver.incl_reduced lineage (nullable, additive)
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'silver'
                 AND table_name = 'incl_reduced' AND column_name = 'qc_rule_version') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE silver.incl_reduced ADD COLUMNS (qc_rule_version STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'silver'
                 AND table_name = 'incl_reduced' AND column_name = 'installation_version') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE silver.incl_reduced ADD COLUMNS (installation_version TIMESTAMP)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'silver'
                 AND table_name = 'incl_reduced' AND column_name = 'probe_constants') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE silver.incl_reduced ADD COLUMNS (probe_constants STRING)';
  END IF;
  -- v1.4 ops.alarm_transition
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                 AND table_name = 'alarm_transition' AND column_name = 'qa_flag') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ops.alarm_transition ADD COLUMNS (qa_flag STRING)';
  END IF;
  -- v1.5 ref.incl_correction (QA-039)
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'incl_correction' AND column_name = 'supersedes') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.incl_correction ADD COLUMNS (supersedes STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'incl_correction' AND column_name = 'review_id') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.incl_correction ADD COLUMNS (review_id STRING)';
  END IF;
END;

-- v1.4 (QA-035 decision v1.5): metric_value NOT NULL on every non-ACK transition
ALTER TABLE ops.alarm_transition DROP CONSTRAINT IF EXISTS ck_alarm_transition_value;
ALTER TABLE ops.alarm_transition ADD CONSTRAINT ck_alarm_transition_value
  CHECK (event = 'ACK' OR (value IS NOT NULL AND reading_id IS NOT NULL AND metric_value IS NOT NULL));

-- v1.5 (QA-039): append-only corrections, applied => independent review named by review_id
ALTER TABLE ref.incl_correction SET TBLPROPERTIES ('delta.appendOnly' = 'true');
ALTER TABLE ref.incl_correction DROP CONSTRAINT IF EXISTS ck_incl_correction_applied_reviewed;
ALTER TABLE ref.incl_correction ADD CONSTRAINT ck_incl_correction_applied_reviewed
  CHECK (NOT applied OR (reviewed_by IS NOT NULL AND reviewed_by <> `user` AND reviewed_utc IS NOT NULL AND review_id IS NOT NULL));

-- v1.5 (QA-039): review_record subject_type / decision domain
ALTER TABLE audit.review_record SET TBLPROPERTIES ('delta.appendOnly' = 'true');
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)));
