-- Migration v1.7 (SCHEMA_CONTRACT §4.4, cdse_ack_required): upgrade EXISTING tables created before v1.7. Fresh
-- installs get this from sql/ddl (02, 07). ORDER on an existing deployment: v1_5_001, v1_6_001 (if not yet applied),
-- THEN this file, THEN the dam_uc_setup job (it re-creates ops.alarm_event / ops.alarm_episode from sql/ddl/11, which
-- select the new columns, so the views fail until this file has run).
-- IDEMPOTENT, safe to re-run: guarded ADD COLUMNS (information_schema lookup inside a SQL script block); the
-- table-feature property, SET DEFAULT, the NULL backfill and SET NOT NULL are all no-ops on a second run.
-- Delta rules (Databricks ALTER TABLE ... COLUMN docs, read 2026-11-09): ADD COLUMN on an existing Delta table cannot
-- carry a DEFAULT and existing rows read NULL; SET DEFAULT needs 'delta.feature.allowColumnDefaults' and applies to
-- new rows only. Hence: ADD COLUMNS -> enable feature -> SET DEFAULT false -> backfill NULL -> false (the ONLY data
-- change in any migration: a brand-new column on the SCD2 config table, every existing rule = not flagged, the
-- DSSMP default) -> SET NOT NULL. Enabling allowColumnDefaults is an irreversible Delta protocol upgrade.
-- ops.alarm_transition is append-only: RAISE rows written before v1.7 keep cdse_ack_required_at_open NULL (unknown);
-- they are never backfilled.
-- PRE-CHECK: SELECT count(*) FROM ref.threshold;   -- rows that will be backfilled to false
-- UNVERIFIED: not run on a workspace (no CLI/workspace here), docs/VERIFICATION.md MIGRATION-3.
USE CATALOG IDENTIFIER(:catalog);

BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'threshold' AND column_name = 'cdse_ack_required') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.threshold ADD COLUMNS (cdse_ack_required BOOLEAN)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                 AND table_name = 'alarm_transition' AND column_name = 'cdse_ack_required_at_open') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ops.alarm_transition ADD COLUMNS (cdse_ack_required_at_open BOOLEAN)';
  END IF;
END;

ALTER TABLE ref.threshold SET TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');
ALTER TABLE ref.threshold ALTER COLUMN cdse_ack_required SET DEFAULT false;
-- backfill-new-column: ref.threshold.cdse_ack_required
UPDATE ref.threshold SET cdse_ack_required = false WHERE cdse_ack_required IS NULL;
ALTER TABLE ref.threshold ALTER COLUMN cdse_ack_required SET NOT NULL;
