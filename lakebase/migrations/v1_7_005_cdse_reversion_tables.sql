-- Lakebase migration v1_7_005 (app SCHEMA_CONTRACT §7 items 20 / 21, Genie 2026-11-09): the app tables
-- dam_cdse_change and rule_reversion_proposal (app-owned workflow) and the outbox dam_cdse_outbox (owned by
-- dam_lakebase_owner; app SELECT + INSERT; dam_lakebase_publisher SELECT + UPDATE (status, reason_code, message,
-- processed_at)). The CREATE block is byte-identical to lakebase/001 (tests/test_lakebase_cdse_tables.py); re-running
-- lakebase/001 (idempotent) has the same effect as this file. Run as the deployer with membership in
-- dam_lakebase_owner and the app role, with SET dam.app_role (and dam.publisher_login if the publisher login is new).
-- ORDER: after lakebase/001 at v1_7_004; before the app version that writes these tables (App Dev 1770505) and before
-- the dam_ref_publish task apply_cdse_assignments. IDEMPOTENT. Then run lakebase/validate_append_only.sql (0 rows).
-- If the app already created any of the tables itself, CREATE IF NOT EXISTS keeps it and its rows; the outbox is
-- moved to dam_lakebase_owner (the app's guard triggers stay; the DA validator checks the grants).
-- DOWN: v1_7_005_cdse_reversion_tables_down.sql (no table, column or row is dropped).
-- QA-059: this file never hands rule_reversion_proposal to the app and never re-owns a table owned by
-- dam_lakebase_owner, so a (mistaken) re-run after v1_7_010 changes no ownership. tools/run_migration.py refuses it
-- outright once v1_7_010 is applied (SUPERSEDED rule, no flag overrides it). On a database where this file CREATES
-- rule_reversion_proposal, the table is the deployer's until v1_7_010 runs: apply v1_7_005..v1_7_010 in one deploy.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_7_005 block (app SCHEMA_CONTRACT §7 items 20 / 21; DDL copied verbatim from app/backend/repos/state.py)
CREATE TABLE IF NOT EXISTS dam_cdse_change (
        change_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('assign', 'change')),
        previous_cdse_id TEXT, new_cdse_id TEXT NOT NULL, change_reason_code TEXT NOT NULL,
        rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0), proposed_by TEXT NOT NULL, proposed_by_id TEXT,
        proposed_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_by TEXT, decided_by_id TEXT, decided_at TEXT, decision_rationale TEXT, outbox_id TEXT,
        approval_record_hash TEXT);
CREATE TABLE IF NOT EXISTS dam_cdse_outbox (
        outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, kind TEXT NOT NULL, previous_cdse_id TEXT,
        new_cdse_id TEXT NOT NULL, change_reason_code TEXT NOT NULL, proposed_by TEXT NOT NULL,
        approved_by TEXT NOT NULL, approved_at TEXT NOT NULL, approval_record_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'loaded', 'rejected')),
        reason_code TEXT, message TEXT, processed_at TEXT);
CREATE TABLE IF NOT EXISTS rule_reversion_proposal (
        proposal_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, to_dssmp_version_id TEXT NOT NULL,
        rules TEXT NOT NULL, proposed_by TEXT NOT NULL, proposed_by_id TEXT, proposed_at TEXT NOT NULL,
        rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
        status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
        decided_by TEXT, decided_by_id TEXT, decided_at TEXT, decision_rationale TEXT, pub_ids TEXT);
-- QA-059: ownership is only ever handed to the app for dam_cdse_change (the app's workflow table), and only while it
-- is NOT owned by dam_lakebase_owner; rule_reversion_proposal is never re-owned here (v1_7_010 owns it), so re-running
-- this block (or lakebase/001, or this migration) after a later hardening cannot give a guarded table back to the app.
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) AND NOT EXISTS (
       SELECT 1 FROM pg_class c JOIN pg_roles o ON o.oid = c.relowner
       WHERE c.oid = 'dam_app.dam_cdse_change'::regclass AND o.rolname = 'dam_lakebase_owner') AND NOT EXISTS (
       SELECT 1 FROM pg_trigger tr JOIN pg_proc fp ON fp.oid = tr.tgfoid
       WHERE tr.tgrelid = 'dam_app.dam_cdse_change'::regclass AND NOT tr.tgisinternal
         AND fp.proname IN ('forbid_mutation', 'app_forbid_mutation', 'status_transition_guard')) THEN
    EXECUTE format('ALTER TABLE dam_app.dam_cdse_change OWNER TO %I', r);
  END IF;
END $$;
-- END v1_7_005 block

ALTER TABLE dam_cdse_outbox OWNER TO dam_lakebase_owner;
REVOKE ALL ON dam_cdse_outbox FROM dam_lakebase_publisher;
GRANT SELECT, UPDATE (status, reason_code, message, processed_at) ON dam_cdse_outbox TO dam_lakebase_publisher;
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE ALL ON dam_cdse_outbox FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON dam_cdse_outbox TO %I', r);
  END IF;
END $$;
COMMIT;
