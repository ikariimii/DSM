-- Lakebase migration v1_7_008: dam_app.dam_cdse_outbox status CHECK = pending | loaded | rejected (app SCHEMA_CONTRACT
-- §7 item 20; same 3 values as dssmp_registration_outbox since 6709371). Earlier 001 / v1_7_005 created the table with
-- an inline CHECK that also allowed 'rejected_invalid' (no writer ever used it: the CDSE load job writes 'loaded' /
-- 'rejected'). Any legacy 'rejected_invalid' row becomes 'rejected' with reason_code / message kept (the only data
-- change). The block is byte-identical to lakebase/001 (tests/test_lakebase_cdse_outbox_status.py). Run as the deployer
-- (member of dam_lakebase_owner). ORDER: after v1_7_007, BEFORE v1_7_009 (the status-transition guard would refuse
-- the legacy mapping). IDEMPOTENT. Then run lakebase/validate_append_only.sql (0 rows).
-- PRE-CHECK (informational): SELECT status, count(*) FROM dam_app.dam_cdse_outbox GROUP BY 1;
-- DOWN: v1_7_008_cdse_outbox_status_down.sql (restores the 4-value CHECK; no row, column or table dropped).
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_7_008 block (app SCHEMA_CONTRACT §7 item 20: dam_cdse_outbox status pending | loaded | rejected; the same
-- 3 values as dssmp_registration_outbox. Replaces every earlier status CHECK on the table (the inline
-- dam_cdse_outbox_status_check of the v1_7_005 CREATE allowed 'rejected_invalid'), maps a legacy 'rejected_invalid' row
-- to 'rejected' (reason_code / message kept), then one named CHECK. Re-run safe.)
DO $$
DECLARE c RECORD;
BEGIN
  IF to_regclass('dam_app.dam_cdse_outbox') IS NULL THEN RETURN; END IF;
  FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'dam_app.dam_cdse_outbox'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) ~ '\mstatus\M' LOOP
    EXECUTE format('ALTER TABLE dam_app.dam_cdse_outbox DROP CONSTRAINT %I', c.conname);
  END LOOP;
  UPDATE dam_app.dam_cdse_outbox SET status = 'rejected' WHERE status = 'rejected_invalid';
  ALTER TABLE dam_app.dam_cdse_outbox ADD CONSTRAINT ck_dam_cdse_outbox_status
    CHECK (status IN ('pending', 'loaded', 'rejected'));
END $$;
-- END v1_7_008 block
COMMIT;
