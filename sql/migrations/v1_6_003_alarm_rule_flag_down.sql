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
-- QA-057 prod guard, layer 2 (layer 1: tools/run_migration.py, which always binds :catalog and :break_glass).
-- The FIRST statement refuses on dam_prod unless the named parameter break_glass = 'BREAK_GLASS_PROD' (bound only by
-- tools/run_migration.py --break-glass-prod "<reason>", which logs the reason). A missing parameter fails the
-- statement (unbound parameter marker) = refused; a catalog bound to NULL / empty / only whitespace or format
-- characters (e.g. a blank SQL-editor widget) is refused by the first branch, whatever the session catalog; a session
-- whose default catalog is dam_prod is refused for ANY catalog value without break_glass. It is checked on the :catalog parameter (format characters and
-- whitespace removed, lower-cased) and again on current_catalog() right after USE CATALOG (the resolved catalog; a
-- look-alike name that does not resolve fails USE CATALOG). UNVERIFIED on Databricks: named parameter markers in a
-- sql_task file, raise_error inside SELECT CASE, the \p{Cf} regex class, current_catalog().
SELECT CASE WHEN coalesce(regexp_replace(:catalog, '[\\p{Cf}\\s]', ''), '') = ''
            THEN raise_error('QA-057: down script refused: the catalog parameter is missing or empty')
            WHEN (lower(regexp_replace(:catalog, '[\\p{Cf}\\s]', '')) = 'dam_prod' OR current_catalog() = 'dam_prod')
                 AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard;
USE CATALOG IDENTIFIER(:catalog);
SELECT CASE WHEN current_catalog() = 'dam_prod' AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard_resolved;

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
