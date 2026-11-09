-- DOWN for lakebase/migrations/v1_7_006_chain_head_log.sql. Drops NOTHING: review_chain_head_log, its rows (audit
-- evidence of every head the app reached) and its guard triggers stay. What it reverses: the app role loses INSERT on
-- the log (it keeps SELECT), so only an UNPATCHED app may run afterwards: roll back
-- tools/patches/v17_chain_head_log_state.patch in the app FIRST. The extract keeps reading both tables (the log can
-- only hold heads up to the rollback; the legacy table then moves ahead and wins on chain_seq).
-- Data-level fallback: a Lakebase point-in-time branch/restore (Postgres tables, not Delta). IDEMPOTENT. Re-applying
-- v1_7_006 (or lakebase/001) restores the grant.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF to_regclass('dam_app.review_chain_head_log') IS NULL THEN RETURN; END IF;
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE INSERT ON dam_app.review_chain_head_log FROM %I', r);
  END IF;
END $$;
COMMIT;
