-- DOWN for v1_7_003_dssmp_registration.sql. Data-level fallback: Delta time travel / RESTORE TABLE ref.dssmp_version
-- (and audit.review_record) TO VERSION AS OF <version before v1_7_003> (DESCRIBE HISTORY); this script never touches
-- data. What it reverses: ck_dssmp_version_status and ck_dssmp_version_approval are dropped; the review_record CHECKs
-- go back to their v1_6_003 bodies (no dssmp_version subject). What it does NOT reverse, by design: the five
-- ref.dssmp_version columns stay (they hold approval evidence). PRE-CHECK before running it: restoring the v1_6_003
-- review_record CHECKs fails (and changes nothing further) if any dssmp_version record exists, because
-- audit.review_record is append-only and such records must never be deleted:
--   SELECT review_id FROM audit.review_record WHERE subject_type = 'dssmp_version';   -- must be empty, else stop
-- Code rollback caveat: pause dam_ref_publish first (a pre-v1.7.003 job does not know the new columns).
-- IDEMPOTENT, safe to re-run. Re-applying v1_7_003 afterwards restores everything.
-- UNVERIFIED: not run on a workspace.
USE CATALOG IDENTIFIER(:catalog);

ALTER TABLE ref.dssmp_version DROP CONSTRAINT IF EXISTS ck_dssmp_version_status;
ALTER TABLE ref.dssmp_version DROP CONSTRAINT IF EXISTS ck_dssmp_version_approval;

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
