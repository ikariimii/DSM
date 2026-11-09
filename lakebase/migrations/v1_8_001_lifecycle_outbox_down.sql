-- DOWN for lakebase/migrations/v1_8_001_lifecycle_outbox.sql. Drops NOTHING that holds data: the outbox, its rows and
-- its owner (dam_lakebase_owner) stay (the rows name approve records; they are audit evidence). What it reverses: the
-- status and lock triggers and the lock function are dropped, and dam_lakebase_publisher loses its privileges on the
-- outbox (pause dam_ref_publish first). The app keeps SELECT + INSERT (never more). Run it BEFORE the v1_7_009 down
-- (reverse order; tools/run_migration.py --lakebase enforces it). IDEMPOTENT.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
DO $$
BEGIN
  IF to_regclass('dam_app.lifecycle_transition_outbox') IS NULL THEN RETURN; END IF;
  DROP TRIGGER IF EXISTS t_status_lifecycle_transition_outbox ON dam_app.lifecycle_transition_outbox;
  DROP TRIGGER IF EXISTS t_lock_lifecycle_transition_outbox ON dam_app.lifecycle_transition_outbox;
  REVOKE ALL ON dam_app.lifecycle_transition_outbox FROM dam_lakebase_publisher;
END $$;
DROP FUNCTION IF EXISTS dam_app.lifecycle_outbox_lock_guard();
COMMIT;
