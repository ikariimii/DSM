-- DOWN for v1_7_007 (QA-056): drops the three unique guard indexes on the owner-owned tables; no table, column or row
-- is touched. dam_cdse_change_one_pending and rule_reversion_one_pending stay: the app owns those tables and creates
-- the same indexes itself at start-up (app UNIQUE_GUARDS), so dropping them would only be undone by the next start.
-- Data-level fallback: a Lakebase point-in-time branch / restore. IDEMPOTENT.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
DROP INDEX IF EXISTS dam_cdse_outbox_one_pending;
DROP INDEX IF EXISTS dssmp_outbox_one_pending;
DROP INDEX IF EXISTS ref_change_publication_one_version;
COMMIT;
