-- DOWN for v1_6_003_alarm_rule_flag.sql. Data-level fallback: Delta time travel / RESTORE TABLE audit.review_record
-- TO VERSION AS OF <version before v1_6_003> (DESCRIBE HISTORY audit.review_record); this script never touches data.
-- What it reverses: ck_review_record_subject_decision and ck_review_record_chain go back to their v1_6_001 bodies (no
-- alarm_rule subject / actions, rationale required only on incl_correction and alarm_event).
-- Run the v1_7_001 .. v1_7_003 downs first.
-- PRE-CHECK (restoring fails, and the table is then left without that CHECK, if a newer record exists; such records
-- are never deleted - then stop and keep v1_6_003):
--   SELECT review_id FROM audit.review_record WHERE subject_type NOT IN ('reading', 'exceedance', 'dssmr',
--     'threshold', 'commentary', 'rule_finding', 'incl_correction', 'alarm_event');         -- must be empty
-- IDEMPOTENT, safe to re-run. Re-applying v1_6_003 afterwards restores everything it reverses.
-- UNVERIFIED: not run on a workspace (static + PostgreSQL-translated tests: tests/test_migration_downs.py).
USE CATALOG IDENTIFIER(:catalog);

-- v1_6_001 bodies
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction',
                   'alarm_event')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_event'
       OR (decision IN ('acknowledge', 'close') AND comment IS NOT NULL AND length(trim(comment)) > 0)));

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
