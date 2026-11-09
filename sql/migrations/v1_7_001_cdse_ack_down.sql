-- DOWN for v1_7_001_cdse_ack.sql. Data-level fallback: Delta time travel / RESTORE TABLE ref.threshold (and
-- ops.alarm_transition) TO VERSION AS OF <version before v1_7_001> (DESCRIBE HISTORY); this script never touches data.
-- What it reverses: ref.threshold.cdse_ack_required loses NOT NULL and its DEFAULT false.
-- What it does NOT reverse, by design: the columns ref.threshold.cdse_ack_required and
-- ops.alarm_transition.cdse_ack_required_at_open stay with their values (the flag is part of every rule version and
-- the episode snapshot is audit evidence; the false backfill is NOT reset to NULL), and the
-- 'delta.feature.allowColumnDefaults' table feature stays (a Delta protocol upgrade cannot be undone).
-- Run the v1_7_002 / v1_7_003 downs first. Code rollback caveat: pause dam_ref_publish and the app's flag edits.
-- PRE-CHECK: none.
-- IDEMPOTENT, safe to re-run. Re-applying v1_7_001 afterwards restores everything it reverses.
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

ALTER TABLE ref.threshold ALTER COLUMN cdse_ack_required DROP NOT NULL;
ALTER TABLE ref.threshold ALTER COLUMN cdse_ack_required DROP DEFAULT;
