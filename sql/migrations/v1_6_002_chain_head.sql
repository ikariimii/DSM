-- Migration v1.6.002 (QA-045d): add the append-only audit.chain_head table to an EXISTING v1.6 deployment. Fresh
-- installs get it from sql/ddl/08_audit.sql. ORDER: after sql/migrations/v1_6_001_audit_chain.sql, BEFORE the
-- dam_uc_setup job (its sql/ddl/10a_row_filters_table.sql sets a row filter on audit.chain_head) and before the next
-- dam_lakebase_extract run (it appends to the table).
-- IDEMPOTENT, safe to re-run: CREATE TABLE IF NOT EXISTS with the exact sql/ddl/08 definition, appendOnly re-asserted,
-- CHECK as DROP CONSTRAINT IF EXISTS then ADD. Nothing is updated or deleted.
-- PRE-CHECK: none (new table). If the table already exists with other columns, CREATE IF NOT EXISTS keeps it:
--   DESCRIBE TABLE audit.chain_head;   -- must list dam_id, chain_id, chain_seq, head_hash, computed_utc, job_run_id, source
-- UNVERIFIED: not run on a workspace (no CLI/workspace here), docs/VERIFICATION.md MIGRATION-2b.
USE CATALOG IDENTIFIER(:catalog);

CREATE TABLE IF NOT EXISTS audit.chain_head (
  dam_id       STRING    NOT NULL,
  chain_id     STRING    NOT NULL COMMENT 'per-dam reviewer chain (= dam_id)',
  chain_seq    BIGINT    NOT NULL COMMENT 'seq of the last record when observed; 0 = empty chain',
  head_hash    STRING    NOT NULL COMMENT 'record_hash of that record (GENESIS for seq 0)',
  computed_utc TIMESTAMP NOT NULL COMMENT 'when the job read the head (same REPEATABLE READ snapshot as the records)',
  job_run_id   STRING    NOT NULL COMMENT 'Databricks job run id (or manual-<utc>) that wrote the row',
  source       STRING    COMMENT 'APP:lakebase:review_chain_head'
) TBLPROPERTIES (delta.appendOnly = true)
  COMMENT 'QA-045d: append-only history of the app chain head per dam/chain. Verifiers read the latest row (never a caller-supplied head); every row is also a checkpoint.';
ALTER TABLE audit.chain_head SET TBLPROPERTIES ('delta.appendOnly' = 'true');

ALTER TABLE audit.chain_head DROP CONSTRAINT IF EXISTS ck_chain_head;
ALTER TABLE audit.chain_head ADD CONSTRAINT ck_chain_head CHECK (
  chain_id = dam_id AND chain_seq >= 0 AND head_hash RLIKE '^[0-9a-f]{64}$');
