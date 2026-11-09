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

ALTER TABLE ops.alarm_transition DROP CONSTRAINT IF EXISTS ck_alarm_transition_value;
ALTER TABLE ref.incl_correction DROP CONSTRAINT IF EXISTS ck_incl_correction_applied_reviewed;
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
