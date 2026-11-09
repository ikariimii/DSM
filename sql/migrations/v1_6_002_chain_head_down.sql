-- DOWN for v1_6_002_chain_head.sql. Data-level fallback: Delta time travel / RESTORE TABLE audit.chain_head TO
-- VERSION AS OF <version> (DESCRIBE HISTORY audit.chain_head); this script never touches data.
-- What it reverses: ck_chain_head is dropped. What it does NOT reverse, by design: audit.chain_head, its rows (the
-- recorded chain heads are audit evidence) and delta.appendOnly stay; never DROP the table. A pre-v1.6.002 extract
-- simply stops appending to it. Run the v1_6_003 .. v1_7_003 downs first.
-- PRE-CHECK: none (only a CHECK is dropped).
-- IDEMPOTENT, safe to re-run. Re-applying v1_6_002 afterwards restores everything it reverses.
-- UNVERIFIED: not run on a workspace (static + PostgreSQL-translated tests: tests/test_migration_downs.py).
USE CATALOG IDENTIFIER(:catalog);

ALTER TABLE audit.chain_head DROP CONSTRAINT IF EXISTS ck_chain_head;
