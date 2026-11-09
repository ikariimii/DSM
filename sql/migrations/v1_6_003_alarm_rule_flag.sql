-- Migration v1.6.003 (app SCHEMA_CONTRACT §7 item 13, App Dev 6f7a1d5): audit.review_record accepts the app's live
-- alarm-rule flag edits (subject_type 'alarm_rule', decision propose | apply | reject, action_type
-- alarm_rule_flag_propose | alarm_rule_flag_apply | alarm_rule_flag_reject) and requires a non-blank rationale on every
-- chained record. Authority (who may propose / apply / reject) is checked by the engine, not here.
-- ORDER: after v1_6_001_audit_chain.sql and v1_6_002_chain_head.sql, BEFORE dam_uc_setup and before the extract
-- carries any alarm_rule row (until then the old CHECK refuses the append and the extract run fails).
-- IDEMPOTENT, safe to re-run: DROP CONSTRAINT IF EXISTS then ADD, same bodies as sql/ddl/08.
-- PRE-CHECK (ADD CONSTRAINT fails, and the table is then left WITHOUT that CHECK, if an existing row violates it):
--   SELECT review_id FROM audit.review_record
--    WHERE action_type IS NOT NULL AND (comment IS NULL OR length(trim(comment)) = 0);     -- must be empty
-- UNVERIFIED: not run on a workspace, docs/VERIFICATION.md MIGRATION-2c.
USE CATALOG IDENTIFIER(:catalog);

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction',
                   'alarm_event', 'alarm_rule')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_event'
       OR (decision IN ('acknowledge', 'close') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_rule'
       OR (decision IN ('propose', 'apply', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)));

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_chain;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_chain CHECK (
  (subject_type NOT IN ('alarm_event', 'alarm_rule') OR action_type IS NOT NULL)
  AND (action_type IS NULL OR (
    action_type IN ('bias_review', 'data_correction_review', 'alarm_ack', 'alarm_close',
                    'alarm_rule_flag_propose', 'alarm_rule_flag_apply', 'alarm_rule_flag_reject')
    AND original_value IS NOT NULL AND corrected_value IS NOT NULL AND chain_id IS NOT NULL
    AND chain_seq IS NOT NULL AND chain_seq >= 1 AND prev_hash IS NOT NULL AND record_hash IS NOT NULL
    AND chain_id = dam_id AND comment IS NOT NULL AND length(trim(comment)) > 0
    AND ((action_type IN ('bias_review', 'data_correction_review') AND subject_type = 'incl_correction')
         OR (action_type = 'alarm_ack' AND subject_type = 'alarm_event' AND decision = 'acknowledge')
         OR (action_type = 'alarm_close' AND subject_type = 'alarm_event' AND decision = 'close')
         OR (action_type = 'alarm_rule_flag_propose' AND subject_type = 'alarm_rule' AND decision = 'propose')
         OR (action_type = 'alarm_rule_flag_apply' AND subject_type = 'alarm_rule' AND decision = 'apply')
         OR (action_type = 'alarm_rule_flag_reject' AND subject_type = 'alarm_rule' AND decision = 'reject')))));
