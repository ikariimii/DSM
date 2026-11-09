-- DOWN for v1_6_001_audit_chain.sql. Data-level fallback: Delta time travel / RESTORE TABLE audit.review_record (and
-- ops.alarm_transition) TO VERSION AS OF <version before v1_6_001> (DESCRIBE HISTORY); this script never touches data.
-- What it reverses: ck_review_record_chain is dropped and ck_review_record_subject_decision goes back to its v1_5_001
-- body (no alarm_event subject). What it does NOT reverse, by design: the 12 chain columns of audit.review_record and
-- ops.alarm_transition.installation_version stay (chained records hash them; dropping them destroys audit evidence).
-- Run v1_6_002 .. v1_7_003 downs first.
-- PRE-CHECK (restoring the v1.5 CHECK fails, and the table is then left without it, if a newer record exists;
-- audit.review_record is append-only, such records are never deleted - then stop and keep v1_6_001):
--   SELECT review_id FROM audit.review_record WHERE subject_type NOT IN ('reading', 'exceedance', 'dssmr',
--     'threshold', 'commentary', 'rule_finding', 'incl_correction');                        -- must be empty
-- IDEMPOTENT, safe to re-run. Re-applying v1_6_001 afterwards restores everything it reverses.
-- UNVERIFIED: not run on a workspace (static + PostgreSQL-translated tests: tests/test_migration_downs.py).
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

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_chain;

-- v1_5_001 body
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)));
