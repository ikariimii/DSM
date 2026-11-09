-- DOWN for lakebase/migrations/v1_7_005_cdse_reversion_tables.sql. Drops NOTHING: dam_cdse_change,
-- dam_cdse_outbox and rule_reversion_proposal and every row in them stay (the outbox and the approve records it names
-- are audit evidence). What it reverses: dam_lakebase_publisher loses its privileges on dam_cdse_outbox and the
-- outbox goes back to the app role (SET dam.app_role), as before v1_7_005 when the app created and guarded it itself.
-- Pause dam_ref_publish first. Data-level fallback: a Lakebase point-in-time branch/restore (not Delta RESTORE; these
-- are Postgres tables). IDEMPOTENT. Re-applying v1_7_005 (or re-running lakebase/001) restores the grants.
-- QA-059: refused while t_status_dam_cdse_outbox (v1_7_009) exists; roll back in reverse order.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF to_regclass('dam_app.dam_cdse_outbox') IS NULL THEN RETURN; END IF;
  -- QA-059: once v1_7_009 guards the outbox's status, handing the outbox to the app would let it disable that guard
  IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'dam_app.dam_cdse_outbox'::regclass
             AND tgname = 't_status_dam_cdse_outbox') THEN
    RAISE EXCEPTION 'v1_7_005 down refused: run the v1_7_009 down (and every later down) first (QA-059)'
      USING ERRCODE = 'object_in_use';
  END IF;
  REVOKE ALL ON dam_app.dam_cdse_outbox FROM dam_lakebase_publisher;
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('ALTER TABLE dam_app.dam_cdse_outbox OWNER TO %I', r);
  END IF;
END $$;
COMMIT;
