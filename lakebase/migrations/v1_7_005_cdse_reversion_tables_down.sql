-- DOWN for lakebase/migrations/v1_7_005_cdse_reversion_tables.sql. Drops NOTHING: dam_cdse_change,
-- dam_cdse_outbox and rule_reversion_proposal and every row in them stay (the outbox and the approve records it names
-- are audit evidence). What it reverses: dam_lakebase_publisher loses its privileges on dam_cdse_outbox and the
-- outbox goes back to the app role (SET dam.app_role), as before v1_7_005 when the app created and guarded it itself.
-- Pause dam_ref_publish first. Data-level fallback: a Lakebase point-in-time branch/restore (not Delta RESTORE; these
-- are Postgres tables). IDEMPOTENT. Re-applying v1_7_005 (or re-running lakebase/001) restores the grants.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF to_regclass('dam_app.dam_cdse_outbox') IS NULL THEN RETURN; END IF;
  REVOKE ALL ON dam_app.dam_cdse_outbox FROM dam_lakebase_publisher;
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('ALTER TABLE dam_app.dam_cdse_outbox OWNER TO %I', r);
  END IF;
END $$;
COMMIT;
