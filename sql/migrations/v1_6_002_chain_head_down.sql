-- DOWN for v1_6_002_chain_head.sql. Data-level fallback: Delta time travel / RESTORE TABLE audit.chain_head TO
-- VERSION AS OF <version> (DESCRIBE HISTORY audit.chain_head); this script never touches data.
-- What it reverses: ck_chain_head is dropped. What it does NOT reverse, by design: audit.chain_head, its rows (the
-- recorded chain heads are audit evidence) and delta.appendOnly stay; never DROP the table. A pre-v1.6.002 extract
-- simply stops appending to it. Run the v1_6_003 .. v1_7_003 downs first.
-- PRE-CHECK: none (only a CHECK is dropped).
-- IDEMPOTENT, safe to re-run. Re-applying v1_6_002 afterwards restores everything it reverses.
-- UNVERIFIED: not run on a workspace (static + PostgreSQL-translated tests: tests/test_migration_downs.py).
-- QA-057 prod guard, layer 2 (layer 1: tools/run_migration.py, which always binds :catalog and :break_glass).
-- The FIRST statement refuses on dam_prod unless the named parameter break_glass = 'BREAK_GLASS_PROD' (bound only by
-- tools/run_migration.py --break-glass-prod "<reason>", which logs the reason). A missing parameter fails the
-- statement (unbound parameter marker) = refused. It is checked on the :catalog parameter (format characters and
-- whitespace removed, lower-cased) and again on current_catalog() right after USE CATALOG (the resolved catalog; a
-- look-alike name that does not resolve fails USE CATALOG). UNVERIFIED on Databricks: named parameter markers in a
-- sql_task file, raise_error inside SELECT CASE, the \p{Cf} regex class, current_catalog().
SELECT CASE WHEN (lower(regexp_replace(:catalog, '[\\p{Cf}\\s]', '')) = 'dam_prod' OR current_catalog() = 'dam_prod')
                 AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard;
USE CATALOG IDENTIFIER(:catalog);
SELECT CASE WHEN current_catalog() = 'dam_prod' AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard_resolved;

ALTER TABLE audit.chain_head DROP CONSTRAINT IF EXISTS ck_chain_head;
