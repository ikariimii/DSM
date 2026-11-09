-- DOWN for lakebase/migrations/v1_8_002_lifecycle_safety_flag.sql. Drops NOTHING that holds data: the column
-- safety_related_ops_event and its values stay (the outbox rows are audit evidence). What it reverses:
-- ck_lifecycle_outbox_evidence goes back to its v1_8_001 body (a non-empty evidence list always). PRE-CHECK: it fails
-- (and changes nothing: one transaction) while a row has an empty evidence list:
--   SELECT outbox_id FROM dam_app.lifecycle_transition_outbox WHERE jsonb_array_length(evidence_uri::jsonb) = 0;
-- Pause dam_ref_publish and roll the app back first. Run it BEFORE the v1_8_001 down (reverse order;
-- tools/run_migration.py --lakebase enforces it). IDEMPOTENT.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
DO $$
BEGIN
  IF to_regclass('dam_app.lifecycle_transition_outbox') IS NULL THEN RETURN; END IF;
  ALTER TABLE dam_app.lifecycle_transition_outbox DROP CONSTRAINT IF EXISTS ck_lifecycle_outbox_evidence;
  ALTER TABLE dam_app.lifecycle_transition_outbox ADD CONSTRAINT ck_lifecycle_outbox_evidence CHECK (
    jsonb_typeof(evidence_uri::jsonb) = 'array' AND jsonb_array_length(evidence_uri::jsonb) > 0);
END $$;
COMMIT;
