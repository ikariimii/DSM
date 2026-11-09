-- Lakebase migration v1_7_011 (QA-059): dam_app.schema_migration_ledger, the applied-migrations ledger that
-- tools/run_migration.py --lakebase reads before and appends to after every Lakebase migration file. Owned by
-- dam_lakebase_owner, append-only (forbid_mutation row + TRUNCATE triggers, ENABLE ALWAYS), no privilege for PUBLIC
-- or the app role. The block is byte-identical to lakebase/001 (tests/test_qa059_lakebase_rerun.py); the runner also
-- runs this block itself (idempotent) before its first ledger read, so the ledger exists before any migration it
-- applies. Run as the deployer (member of dam_lakebase_owner), with SET dam.app_role.
-- ORDER: after v1_7_010. IDEMPOTENT. No data is changed. Then run lakebase/validate_append_only.sql (0 rows).
-- Existing databases migrated by hand before the ledger: record what is already applied with
--   python tools/run_migration.py --lakebase --baseline <newest applied file> ...   (docs/DEV_WORKSPACE_RUNBOOK.md 4)
-- DOWN: v1_7_011_migration_ledger_down.sql (the ledger and its rows stay: they are the deploy record).
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_7_011 block (QA-059: applied-migrations ledger for tools/run_migration.py --lakebase. One row per applied
-- Lakebase migration file (direction up | down | baseline), append-only like the other guarded tables: owned by
-- dam_lakebase_owner, forbid_mutation() row + TRUNCATE triggers ENABLE ALWAYS, no privilege for PUBLIC or the app
-- role (the deployer, a member of dam_lakebase_owner, inserts). The runner refuses a re-run of an applied migration
-- unless --rerun "<reason>" is given, refuses an older migration than the newest applied one the same way, and
-- ALWAYS refuses v1_7_005 once v1_7_010 is applied (flag or not).)
CREATE TABLE IF NOT EXISTS dam_app.schema_migration_ledger (
        ledger_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        migration TEXT NOT NULL CHECK (migration ~ '^v[0-9]+_[0-9]+_[0-9]{3}_[a-z0-9_]+[.]sql$'),
        direction TEXT NOT NULL CHECK (direction IN ('up', 'down', 'baseline')),
        sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        rerun_reason TEXT CHECK (rerun_reason IS NULL OR length(trim(rerun_reason)) >= 10),
        applied_by TEXT NOT NULL DEFAULT session_user,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
ALTER TABLE dam_app.schema_migration_ledger OWNER TO dam_lakebase_owner;
REVOKE ALL ON dam_app.schema_migration_ledger FROM PUBLIC;
CREATE OR REPLACE TRIGGER t_noupd_schema_migration_ledger BEFORE UPDATE OR DELETE ON dam_app.schema_migration_ledger
  FOR EACH ROW EXECUTE FUNCTION dam_app.forbid_mutation();
CREATE OR REPLACE TRIGGER t_notrunc_schema_migration_ledger BEFORE TRUNCATE ON dam_app.schema_migration_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION dam_app.forbid_mutation();
ALTER TABLE dam_app.schema_migration_ledger ENABLE ALWAYS TRIGGER t_noupd_schema_migration_ledger,
  ENABLE ALWAYS TRIGGER t_notrunc_schema_migration_ledger;
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE ALL ON dam_app.schema_migration_ledger FROM %I', r);
  END IF;
END $$;
-- END v1_7_011 block
COMMIT;
