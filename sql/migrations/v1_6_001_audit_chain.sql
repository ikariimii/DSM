-- Migration v1.6 (SCHEMA_CONTRACT §9 changelog, QA-039 v2 audit chain): upgrade EXISTING tables created before
-- v1.6. Fresh installs get all of this from sql/ddl. ORDER on an existing deployment: sql/migrations/
-- v1_5_001_contract.sql (if not yet applied), THEN this file, THEN the dam_uc_setup job (it re-creates the views
-- ops.alarm_event / ops.alarm_episode from sql/ddl/11_gold_views.sql, which select the new
-- ops.alarm_transition.installation_version, and its sql/ddl/08 ck_review_record_chain names the new review_record
-- columns, so both fail until this file has run). Do NOT re-run v1_5_001 after this
-- file: its ck_review_record_subject_decision is the v1.5 one, which rejects 'alarm_event' rows (if it was re-run,
-- run this file again). IDEMPOTENT, safe to re-run: guarded ADD COLUMNS (information_schema lookup inside a SQL
-- script block), CHECKs as DROP CONSTRAINT IF EXISTS then ADD (same pattern as v1_5_001 and sql/ddl).
-- PRE-CHECK (ADD CONSTRAINT fails, and the table is then left WITHOUT that CHECK, if an existing row violates it):
--   SELECT DISTINCT subject_type, decision FROM audit.review_record;    -- must fit ck_review_record_subject_decision
--   (ck_review_record_chain only constrains rows with action_type set, which exist only after this file ran.)
-- Out of scope here: the Lakebase review_signoff columns (lakebase/001_app_schema.sql mirrors the app's state.py,
-- App Developer-owned, app SCHEMA_CONTRACT §7 9a).
-- UNVERIFIED: not run on a workspace (no CLI/workspace here), docs/VERIFICATION.md MIGRATION-1.
USE CATALOG IDENTIFIER(:catalog);

BEGIN
  -- v1.6 audit.review_record chain columns (nullable, additive)
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'action_type') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (action_type STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'reviewer_email') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (reviewer_email STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'reviewer_sub') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (reviewer_sub STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'original_value') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (original_value STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'corrected_value') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (corrected_value STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'chain_id') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (chain_id STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'chain_seq') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (chain_seq BIGINT)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'prev_hash') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (prev_hash STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'record_hash') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (record_hash STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'signature') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (signature STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'sig_alg') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (sig_alg STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'key_id') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (key_id STRING)';
  END IF;
  -- v1.6 ops.alarm_transition: inclinometer installation lineage (nullable, additive)
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                 AND table_name = 'alarm_transition' AND column_name = 'installation_version') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ops.alarm_transition ADD COLUMNS (installation_version TIMESTAMP)';
  END IF;
END;

-- v1.6: review_record subject_type / decision domain adds alarm_event (acknowledge | close)
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction',
                   'alarm_event')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_event'
       OR (decision IN ('acknowledge', 'close') AND comment IS NOT NULL AND length(trim(comment)) > 0)));

-- v1.6: chained reviewer actions carry the required chain fields
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_chain;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_chain CHECK (
  (subject_type <> 'alarm_event' OR action_type IS NOT NULL)
  AND (action_type IS NULL OR (
    action_type IN ('bias_review', 'data_correction_review', 'alarm_ack', 'alarm_close')
    AND original_value IS NOT NULL AND corrected_value IS NOT NULL AND chain_id IS NOT NULL
    AND chain_seq IS NOT NULL AND chain_seq >= 1 AND prev_hash IS NOT NULL AND record_hash IS NOT NULL
    AND chain_id = dam_id
    AND ((action_type IN ('bias_review', 'data_correction_review') AND subject_type = 'incl_correction')
         OR (action_type = 'alarm_ack' AND subject_type = 'alarm_event' AND decision = 'acknowledge')
         OR (action_type = 'alarm_close' AND subject_type = 'alarm_event' AND decision = 'close')))));
