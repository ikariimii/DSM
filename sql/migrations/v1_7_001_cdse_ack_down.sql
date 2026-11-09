-- DOWN for v1_7_001_cdse_ack.sql. Data-level fallback: Delta time travel / RESTORE TABLE ref.threshold (and
-- ops.alarm_transition) TO VERSION AS OF <version before v1_7_001> (DESCRIBE HISTORY); this script never touches data.
-- What it reverses: ref.threshold.cdse_ack_required loses NOT NULL and its DEFAULT false.
-- What it does NOT reverse, by design: the columns ref.threshold.cdse_ack_required and
-- ops.alarm_transition.cdse_ack_required_at_open stay with their values (the flag is part of every rule version and
-- the episode snapshot is audit evidence; the false backfill is NOT reset to NULL), and the
-- 'delta.feature.allowColumnDefaults' table feature stays (a Delta protocol upgrade cannot be undone).
-- Run the v1_7_002 / v1_7_003 downs first. Code rollback caveat: pause dam_ref_publish and the app's flag edits.
-- PRE-CHECK: none.
-- IDEMPOTENT, safe to re-run. Re-applying v1_7_001 afterwards restores everything it reverses.
-- UNVERIFIED: not run on a workspace (static + PostgreSQL-translated tests: tests/test_migration_downs.py).
USE CATALOG IDENTIFIER(:catalog);

ALTER TABLE ref.threshold ALTER COLUMN cdse_ack_required DROP NOT NULL;
ALTER TABLE ref.threshold ALTER COLUMN cdse_ack_required DROP DEFAULT;
