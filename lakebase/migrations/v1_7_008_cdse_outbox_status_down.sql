-- DOWN for v1_7_008: puts back the earlier 4-value status CHECK on dam_app.dam_cdse_outbox
-- (pending | loaded | rejected | rejected_invalid). No table, column or row is dropped; rows mapped
-- rejected_invalid -> rejected by the up stay 'rejected' (their reason_code / message tell them apart).
-- Data-level fallback: a Lakebase point-in-time branch / restore. IDEMPOTENT. Run v1_7_009's down first if applied.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
ALTER TABLE dam_cdse_outbox DROP CONSTRAINT IF EXISTS ck_dam_cdse_outbox_status;
ALTER TABLE dam_cdse_outbox ADD CONSTRAINT ck_dam_cdse_outbox_status
  CHECK (status IN ('pending', 'loaded', 'rejected', 'rejected_invalid'));
COMMIT;
