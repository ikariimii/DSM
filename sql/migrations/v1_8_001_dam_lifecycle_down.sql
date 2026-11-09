-- DOWN for v1_8_001_dam_lifecycle.sql. Data-level fallback: Delta time travel / RESTORE TABLE (DESCRIBE HISTORY) on
-- the touched tables; this script never touches data. What it reverses: ck_lifecycle_citation, ck_dam_lifecycle_state
-- and ck_lifecycle_event are dropped; the review_record CHECKs go back to their v1_7_003 bodies (no dam_lifecycle
-- subject). What it does NOT reverse, by design: audit.lifecycle_event (append-only permanent record, 12.12) and the
-- other new tables with their rows and seeds, and the ref.dam columns lifecycle_state / lifecycle_effective_from stay.
-- PRE-CHECK before running it: restoring the v1_7_003 review_record CHECKs fails (and changes nothing further) if any
-- dam_lifecycle record exists (audit.review_record is append-only; such records are never deleted):
--   SELECT review_id FROM audit.review_record WHERE subject_type = 'dam_lifecycle';   -- must be empty, else stop
-- Code rollback caveat: pause dam_ref_publish first (remove the apply_lifecycle_transitions task).
-- IDEMPOTENT, safe to re-run. Re-applying v1_8_001 afterwards RESTOREs everything.
-- UNVERIFIED: not run on a workspace.
-- QA-057 prod guard, layer 2 (layer 1: tools/run_migration.py, which always binds :catalog and :break_glass).
-- The FIRST statement refuses on dam_prod unless the named parameter break_glass = 'BREAK_GLASS_PROD' (bound only by
-- tools/run_migration.py --break-glass-prod "<reason>", which logs the reason). A missing parameter fails the
-- statement (unbound parameter marker) = refused; a catalog bound to NULL / empty / only whitespace or format
-- characters (e.g. a blank SQL-editor widget) is refused by the first branch, whatever the session catalog; a session
-- whose default catalog is dam_prod is refused for ANY catalog value without break_glass. It is checked on the :catalog parameter (format characters and
-- whitespace removed, lower-cased) and again on current_catalog() right after USE CATALOG (the resolved catalog; a
-- look-alike name that does not resolve fails USE CATALOG). UNVERIFIED on Databricks: named parameter markers in a
-- sql_task file, raise_error inside SELECT CASE, the \p{Cf} / \p{Z} regex classes (Zs spaces such as U+00A0, U+3000,
-- U+2000-U+200A, U+202F, U+205F are removed like format characters; QA-058 residual), current_catalog().
SELECT CASE WHEN coalesce(regexp_replace(:catalog, '[\\p{Cf}\\p{Z}\\s]', ''), '') = ''
            THEN raise_error('QA-057: down script refused: the catalog parameter is missing or empty')
            WHEN (lower(regexp_replace(:catalog, '[\\p{Cf}\\p{Z}\\s]', '')) = 'dam_prod' OR current_catalog() = 'dam_prod')
                 AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard;
USE CATALOG IDENTIFIER(:catalog);
SELECT CASE WHEN current_catalog() = 'dam_prod' AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard_resolved;

ALTER TABLE ref.lifecycle_citation DROP CONSTRAINT IF EXISTS ck_lifecycle_citation;
ALTER TABLE ref.dam_lifecycle_state DROP CONSTRAINT IF EXISTS ck_dam_lifecycle_state;
ALTER TABLE audit.lifecycle_event DROP CONSTRAINT IF EXISTS ck_lifecycle_event;

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction',
                   'alarm_event', 'alarm_rule', 'dssmp_version', 'dam', 'rule_reversion')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_event'
       OR (decision IN ('acknowledge', 'close') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_rule'
       OR (decision IN ('propose', 'apply', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'dssmp_version'
       OR (decision IN ('propose', 'approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'dam'
       OR (decision IN ('assign', 'propose', 'approve', 'reject') AND comment IS NOT NULL
           AND length(trim(comment)) > 0))
  AND (subject_type <> 'rule_reversion'
       OR (decision IN ('propose', 'approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)));
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_chain;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_chain CHECK (
  (subject_type NOT IN ('alarm_event', 'alarm_rule', 'dssmp_version', 'dam', 'rule_reversion')
   OR action_type IS NOT NULL)
  AND (action_type IS NULL OR (
    action_type IN ('bias_review', 'data_correction_review', 'alarm_ack', 'alarm_close',
                    'alarm_rule_flag_propose', 'alarm_rule_flag_apply', 'alarm_rule_flag_reject',
                    'dssmp_version_propose', 'dssmp_version_approve', 'dssmp_version_reject',
                    'dam_cdse_assign', 'dam_cdse_change_propose', 'dam_cdse_change_approve',
                    'dam_cdse_change_reject',
                    'rule_reversion_propose', 'rule_reversion_approve', 'rule_reversion_reject')
    AND original_value IS NOT NULL AND corrected_value IS NOT NULL AND chain_id IS NOT NULL
    AND chain_seq IS NOT NULL AND chain_seq >= 1 AND prev_hash IS NOT NULL AND record_hash IS NOT NULL
    AND chain_id = dam_id AND comment IS NOT NULL AND length(trim(comment)) > 0
    AND ((action_type IN ('bias_review', 'data_correction_review') AND subject_type = 'incl_correction')
         OR (action_type = 'alarm_ack' AND subject_type = 'alarm_event' AND decision = 'acknowledge')
         OR (action_type = 'alarm_close' AND subject_type = 'alarm_event' AND decision = 'close')
         OR (action_type = 'alarm_rule_flag_propose' AND subject_type = 'alarm_rule' AND decision = 'propose')
         OR (action_type = 'alarm_rule_flag_apply' AND subject_type = 'alarm_rule' AND decision = 'apply')
         OR (action_type = 'alarm_rule_flag_reject' AND subject_type = 'alarm_rule' AND decision = 'reject')
         OR (action_type = 'dssmp_version_propose' AND subject_type = 'dssmp_version' AND decision = 'propose')
         OR (action_type = 'dssmp_version_approve' AND subject_type = 'dssmp_version' AND decision = 'approve')
         OR (action_type = 'dssmp_version_reject' AND subject_type = 'dssmp_version' AND decision = 'reject')
         OR (action_type = 'dam_cdse_assign' AND subject_type = 'dam' AND decision = 'assign'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_propose' AND subject_type = 'dam' AND decision = 'propose'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_approve' AND subject_type = 'dam' AND decision = 'approve'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_reject' AND subject_type = 'dam' AND decision = 'reject'
             AND subject_id = dam_id)
         OR (action_type = 'rule_reversion_propose' AND subject_type = 'rule_reversion' AND decision = 'propose')
         OR (action_type = 'rule_reversion_approve' AND subject_type = 'rule_reversion' AND decision = 'approve')
         OR (action_type = 'rule_reversion_reject' AND subject_type = 'rule_reversion' AND decision = 'reject')))));
