-- ROLLBACK of Lakebase migration v1.7.004 (dssmp_registration_outbox back to the DA 86993d0 shape for a pre-v1.7.004
-- load job). Renames back (proposed_by -> requested_by, processed_at -> applied_at), re-adds requested_at and
-- supersedes_dssmp_version_id as NULLABLE columns (their old values were never kept; requested_at is filled from
-- approved_at so the old loader has an ordering key), maps status loaded -> applied, rejected -> rejected_invalid
-- and restores the old CHECK. reason_code is KEPT (it holds load outcomes; no data is dropped). Data-level fallback:
-- none on Lakebase beyond a branch / point-in-time restore of the Lakebase project. IDEMPOTENT. UNVERIFIED on
-- Lakebase. Run as the deployer, inside a transaction.
SET search_path TO dam_app;
DO $$ BEGIN
  IF to_regclass('dam_app.dssmp_registration_outbox') IS NULL THEN RETURN; END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'proposed_by') THEN
    ALTER TABLE dam_app.dssmp_registration_outbox RENAME COLUMN proposed_by TO requested_by;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'processed_at') THEN
    ALTER TABLE dam_app.dssmp_registration_outbox RENAME COLUMN processed_at TO applied_at;
  END IF;
  ALTER TABLE dam_app.dssmp_registration_outbox ADD COLUMN IF NOT EXISTS requested_at TEXT;
  ALTER TABLE dam_app.dssmp_registration_outbox ADD COLUMN IF NOT EXISTS supersedes_dssmp_version_id TEXT;
  UPDATE dam_app.dssmp_registration_outbox SET requested_at = approved_at WHERE requested_at IS NULL;
  ALTER TABLE dam_app.dssmp_registration_outbox DROP CONSTRAINT IF EXISTS ck_dssmp_outbox_status;
  UPDATE dam_app.dssmp_registration_outbox SET status = 'applied' WHERE status = 'loaded';
  UPDATE dam_app.dssmp_registration_outbox SET status = 'rejected_invalid' WHERE status = 'rejected';
  ALTER TABLE dam_app.dssmp_registration_outbox ADD CONSTRAINT ck_dssmp_outbox_status
    CHECK (status IN ('pending', 'applied', 'rejected_stale', 'rejected_invalid'));
END $$;
