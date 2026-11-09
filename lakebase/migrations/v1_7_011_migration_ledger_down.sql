-- DOWN for lakebase/migrations/v1_7_011_migration_ledger.sql. Drops NOTHING: the ledger table and every row in it
-- stay (they are the record of what was deployed; append-only like the other guarded tables). There is nothing else
-- to reverse: the ledger grants nobody anything. tools/run_migration.py keeps working (it re-creates nothing that
-- exists). Data-level fallback: a Lakebase point-in-time branch / restore. IDEMPOTENT (a no-op).
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
SELECT 'v1_7_011 down: no-op (the migration ledger is kept)' AS note;
