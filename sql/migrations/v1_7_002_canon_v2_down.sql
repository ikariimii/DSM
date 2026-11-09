-- DOWN for v1_7_002_canon_v2.sql. Data-level fallback: Delta time travel / RESTORE TABLE audit.review_record TO
-- VERSION AS OF <version before v1_7_002> (DESCRIBE HISTORY audit.review_record); this script never touches data.
-- What it reverses: ck_review_record_canon (dropped). What it does NOT reverse, by design: the columns actor_app_role
-- and canon_version stay, because canon-2 records hash both (dropping them would destroy audit data and make every
-- canon-2 record unverifiable; audit.review_record is delta.appendOnly). Code rollback caveat: a pre-v1.7.002 extract
-- builds rows by the table schema and fails (KeyError on the two columns) without moving its watermark; a pre-v1.7
-- verifier ignores them and reports every canon-2 row as hash_mismatch. Roll code back only together with the app.
-- PRE-CHECK: none (only a CHECK is dropped).
-- IDEMPOTENT, safe to re-run (DROP CONSTRAINT IF EXISTS). Re-applying v1_7_002 afterwards restores the CHECK.
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

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_canon;
