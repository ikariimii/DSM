-- Lakebase migration v1.7.004: dam_app.dssmp_registration_outbox to the agreed app contract (app SCHEMA_CONTRACT §7
-- item 19, App Dev e988454). Replaces the DA 86993d0 shape IN PLACE (rename, never drop and recreate):
--   requested_by -> proposed_by, applied_at -> processed_at, + reason_code; requested_at and
--   supersedes_dssmp_version_id are removed ONLY if they hold no value (else the script raises and changes nothing:
--   rows written in the old shape need a human decision); status CHECK = pending | loaded | rejected
--   (§7 item 19, v1.7 steering 2026-11-09: 'rejected_invalid' dropped, neither the app nor the DA load job writes
--   it), old values mapped applied -> loaded, rejected_stale / rejected_invalid -> rejected (reason_code / message
--   kept). Re-running this file on a database that ran an earlier version converges to the 3-value CHECK.
-- The same block is inlined in lakebase/001_app_schema.sql (tests/test_dssmp_outbox_contract.py checks they are
-- identical), so a normal 001 re-run applies it; this file is for running it on its own. Run as the deployer (member
-- of dam_lakebase_owner), inside a transaction. IDEMPOTENT: a table already in the contract shape is left as is.
-- ROLLBACK: lakebase/migrations/v1_7_004_dssmp_outbox_contract_down.sql (renames back; keeps reason_code; no data
-- dropped). UNVERIFIED on Lakebase.
SET search_path TO dam_app;
-- BEGIN v1_7_004 block
DO $$
DECLARE has_col BOOLEAN;
BEGIN
  IF to_regclass('dam_app.dssmp_registration_outbox') IS NULL THEN RETURN; END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'requested_by')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'proposed_by') THEN
    ALTER TABLE dam_app.dssmp_registration_outbox RENAME COLUMN requested_by TO proposed_by;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'applied_at')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'processed_at') THEN
    ALTER TABLE dam_app.dssmp_registration_outbox RENAME COLUMN applied_at TO processed_at;
  END IF;
  ALTER TABLE dam_app.dssmp_registration_outbox ADD COLUMN IF NOT EXISTS reason_code TEXT;
  FOR has_col IN SELECT true FROM information_schema.columns WHERE table_schema = 'dam_app'
                 AND table_name = 'dssmp_registration_outbox' AND column_name = 'requested_at' LOOP
    IF EXISTS (SELECT 1 FROM dam_app.dssmp_registration_outbox WHERE requested_at IS NOT NULL) THEN
      RAISE EXCEPTION 'dssmp_registration_outbox.requested_at holds values; not dropped (decide by hand)'
        USING ERRCODE = 'object_in_use';
    END IF;
    ALTER TABLE dam_app.dssmp_registration_outbox DROP COLUMN requested_at;
  END LOOP;
  FOR has_col IN SELECT true FROM information_schema.columns WHERE table_schema = 'dam_app'
                 AND table_name = 'dssmp_registration_outbox' AND column_name = 'supersedes_dssmp_version_id' LOOP
    IF EXISTS (SELECT 1 FROM dam_app.dssmp_registration_outbox WHERE supersedes_dssmp_version_id IS NOT NULL) THEN
      RAISE EXCEPTION 'dssmp_registration_outbox.supersedes_dssmp_version_id holds values; not dropped (decide by hand)'
        USING ERRCODE = 'object_in_use';
    END IF;
    ALTER TABLE dam_app.dssmp_registration_outbox DROP COLUMN supersedes_dssmp_version_id;
  END LOOP;
  ALTER TABLE dam_app.dssmp_registration_outbox ALTER COLUMN status DROP DEFAULT;
  ALTER TABLE dam_app.dssmp_registration_outbox DROP CONSTRAINT IF EXISTS ck_dssmp_outbox_status;
  UPDATE dam_app.dssmp_registration_outbox SET status = 'loaded' WHERE status = 'applied';
  UPDATE dam_app.dssmp_registration_outbox SET status = 'rejected' WHERE status IN ('rejected_stale', 'rejected_invalid');
  ALTER TABLE dam_app.dssmp_registration_outbox ADD CONSTRAINT ck_dssmp_outbox_status
    CHECK (status IN ('pending', 'loaded', 'rejected'));
END $$;
-- END v1_7_004 block
