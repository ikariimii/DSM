-- DOWN for v1_8_002_lifecycle_backfill.sql: intentionally a NO-OP apart from the guard. The back-filled
-- audit.lifecycle_event rows are permanent records (append-only, 12.12) and are never deleted; their SCD-2 rows and the
-- ref.dam mirror are derived from them and stay too (re-running v1_8_002 changes nothing: it is IDEMPOTENT). To RESTORE
-- the pre-backfill table state for a test catalog use Delta time travel on a CLONE, never on the source tables.
-- PRE-CHECK: none (nothing is changed).
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
