-- Lakebase migration v1_8_002 (v1.8.003 lifecycle safety flag; pairs with the UC migration
-- sql/migrations/v1_8_003_lifecycle_safety_flag.sql): dam_app.lifecycle_transition_outbox gains
-- safety_related_ops_event BOOLEAN DEFAULT false (NULL treated as false by the loader) and ck_lifecycle_outbox_evidence
-- allows an empty evidence list for suspension_reason 'other' only. The block is byte-identical to lakebase/001
-- (tests/test_lakebase_lifecycle_outbox.py). Run with tools/run_migration.py --lakebase (QA-059 ledger + validator), as
-- the deployer (member of dam_lakebase_owner).
-- ORDER: after v1_8_001; before the app version that sends safety_related_ops_event and before the dam_ref_publish
-- code that reads it (the loader SELECTs the column). IDEMPOTENT. No data is changed (existing rows: the column is
-- false via its default; the CHECK only widens).
-- DOWN: v1_8_002_lifecycle_safety_flag_down.sql.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_8_002 block (v1.8.003 lifecycle safety flag, Genie 2026-11-09 15:27 + 15:35 and FERC Specialist 15:27:
-- dam_app.lifecycle_transition_outbox gains safety_related_ops_event BOOLEAN DEFAULT false, set by the CDSE; NULL is
-- treated as false by the loader (dam_safety.lifecycle.safety_flag), which writes false into audit.lifecycle_event.
-- ck_lifecycle_outbox_evidence: the evidence list is a JSON array, non-empty unless suspension_reason = 'other'
-- (citation suspend_other_internal: evidence optional, the CDSE's cdse_basis is required instead; checked by the
-- loader). The app's privileges are unchanged (SELECT + INSERT on the table: the new column is insertable).)
ALTER TABLE dam_app.lifecycle_transition_outbox ADD COLUMN IF NOT EXISTS safety_related_ops_event BOOLEAN DEFAULT false;
ALTER TABLE dam_app.lifecycle_transition_outbox DROP CONSTRAINT IF EXISTS ck_lifecycle_outbox_evidence;
ALTER TABLE dam_app.lifecycle_transition_outbox ADD CONSTRAINT ck_lifecycle_outbox_evidence CHECK (
  jsonb_typeof(evidence_uri::jsonb) = 'array'
  AND (jsonb_array_length(evidence_uri::jsonb) > 0 OR coalesce(suspension_reason, '') = 'other'));
-- END v1_8_002 block
COMMIT;
