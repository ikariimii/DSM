-- Lakebase migration v1_8_001 (v1.8 dam lifecycle): dam_app.lifecycle_transition_outbox, owned by
-- dam_lakebase_owner; the app role SELECT + INSERT only; dam_lakebase_publisher SELECT + UPDATE (status, reason_code,
-- message, processed_at); status trigger pending -> loaded | rejected (ENABLE ALWAYS); the dam-lock trigger (lock
-- order dam 4702047 -> check -> chain 4702046); one pending row per dam, one row per approval record. The block is
-- byte-identical to lakebase/001 (tests/test_lakebase_lifecycle_outbox.py). Needs dam_app.status_transition_guard()
-- (v1_7_009) and the role dam_lakebase_publisher (lakebase/001).
-- Run with tools/run_migration.py --lakebase (QA-059 ledger; it runs the validator afterwards), as the deployer
-- (member of dam_lakebase_owner) with --app-role (and --extract-role).
-- ORDER: after v1_7_011; before the app version that writes the outbox and before the dam_ref_publish task
-- apply_lifecycle_transitions. IDEMPOTENT. No data is changed.
-- DOWN: v1_8_001_lifecycle_outbox_down.sql (drops the two triggers and the lock function; the table, its rows and the
-- owner stay; the app keeps SELECT + INSERT, the publisher loses its UPDATE).
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_8_001 block (v1.8 dam lifecycle: dam_app.lifecycle_transition_outbox, one row per approved lifecycle
-- transition, inserted by the app in the transaction of its lifecycle_transition_approve chain record. Owned by
-- dam_lakebase_owner like the other outboxes: the app has SELECT + INSERT only; dam_lakebase_publisher SELECT + UPDATE
-- (status, reason_code, message, processed_at); status moves pending -> loaded | rejected only
-- (status_transition_guard, ENABLE ALWAYS); one pending row per dam. Lock order (app contract): the inserting
-- transaction must hold the dam lock pg_advisory_xact_lock(4702047, hashtext('dam_app.dam:' || dam_id)) (taken
-- FIRST, then the check, then the chain lock 4702046); t_lock_lifecycle_transition_outbox refuses the INSERT
-- otherwise. transition_reason_code = the transition's reason (audit.lifecycle_event.reason_code); reason_code /
-- message / processed_at are the load bookkeeping, as on the other outboxes. suspension_reason values are a UC-owned
-- enum (ref.lifecycle_suspension_reason), checked by the loader, not here.)
CREATE TABLE IF NOT EXISTS dam_app.lifecycle_transition_outbox (
        outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL,
        trigger_code TEXT NOT NULL, authorizer_role TEXT NOT NULL, authorizer_user_id TEXT NOT NULL,
        evidence_uri TEXT NOT NULL, ferc_docket_or_order TEXT, effective_ts TEXT NOT NULL, monitoring_rule TEXT,
        notification_checklist TEXT, retention_action TEXT, rationale TEXT, citation_code TEXT NOT NULL,
        proposed_by TEXT NOT NULL, proposed_at TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
        second_approved_by TEXT, proposal_record_hash TEXT NOT NULL, approval_record_hash TEXT NOT NULL,
        dssmp_version_id TEXT, transition_reason_code TEXT, suspension_reason TEXT, incident_report_uri TEXT,
        sworn_verification_12_13 BOOLEAN, sworn_verification_uri TEXT, cdse_basis TEXT, cdse_basis_by TEXT,
        re_directive_uri TEXT, withdrawal_filed_date TEXT, counterpart_dam_id TEXT, created_at TEXT NOT NULL,
        status TEXT NOT NULL, reason_code TEXT, message TEXT, processed_at TEXT,
        CONSTRAINT ck_lifecycle_outbox_status CHECK (status IN ('pending', 'loaded', 'rejected')),
        CONSTRAINT ck_lifecycle_outbox_states CHECK (
          from_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer',
                         'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
          AND to_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer',
                           'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')),
        CONSTRAINT ck_lifecycle_outbox_suspension CHECK (
          (to_state = 'suspended_monitoring_modified') = (suspension_reason IS NOT NULL)),
        CONSTRAINT ck_lifecycle_outbox_evidence CHECK (
          jsonb_typeof(evidence_uri::jsonb) = 'array' AND jsonb_array_length(evidence_uri::jsonb) > 0),
        CONSTRAINT ck_lifecycle_outbox_hashes CHECK (
          approval_record_hash ~ '^[0-9a-f]{64}$' AND proposal_record_hash ~ '^[0-9a-f]{64}$'));
ALTER TABLE dam_app.lifecycle_transition_outbox OWNER TO dam_lakebase_owner;
CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_outbox_one_pending ON dam_app.lifecycle_transition_outbox (dam_id)
  WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_outbox_one_approval ON dam_app.lifecycle_transition_outbox
  (approval_record_hash);
CREATE OR REPLACE FUNCTION dam_app.lifecycle_outbox_lock_guard() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_locks l WHERE l.locktype = 'advisory' AND l.pid = pg_backend_pid() AND l.granted
                   AND l.classid = 4702047::oid AND l.objsubid = 2
                   AND l.objid = ((hashtext('dam_app.dam:' || NEW.dam_id)::bigint) & 4294967295)::oid) THEN
    RAISE EXCEPTION 'lifecycle_transition_outbox: take the dam lock pg_advisory_xact_lock(4702047, hashtext(%)) first',
      quote_literal('dam_app.dam:' || NEW.dam_id) USING ERRCODE = 'lock_not_available';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION dam_app.lifecycle_outbox_lock_guard() OWNER TO dam_lakebase_owner;
CREATE OR REPLACE TRIGGER t_lock_lifecycle_transition_outbox BEFORE INSERT ON dam_app.lifecycle_transition_outbox
  FOR EACH ROW EXECUTE FUNCTION dam_app.lifecycle_outbox_lock_guard();
CREATE OR REPLACE TRIGGER t_status_lifecycle_transition_outbox BEFORE UPDATE ON dam_app.lifecycle_transition_outbox
  FOR EACH ROW EXECUTE FUNCTION dam_app.status_transition_guard('loaded', 'rejected');
ALTER TABLE dam_app.lifecycle_transition_outbox ENABLE ALWAYS TRIGGER t_lock_lifecycle_transition_outbox,
  ENABLE ALWAYS TRIGGER t_status_lifecycle_transition_outbox;
REVOKE ALL ON dam_app.lifecycle_transition_outbox FROM PUBLIC;
REVOKE ALL ON dam_app.lifecycle_transition_outbox FROM dam_lakebase_publisher;
GRANT SELECT, UPDATE (status, reason_code, message, processed_at) ON dam_app.lifecycle_transition_outbox
  TO dam_lakebase_publisher;
DO $$
DECLARE
  r TEXT := nullif(current_setting('dam.app_role', true), '');
  x TEXT := nullif(current_setting('dam.extract_role', true), '');
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE ALL ON dam_app.lifecycle_transition_outbox FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON dam_app.lifecycle_transition_outbox TO %I', r);
  END IF;
  IF x IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = x) THEN
    EXECUTE format('GRANT SELECT ON dam_app.lifecycle_transition_outbox TO %I', x);
  END IF;
END $$;
-- END v1_8_001 block
COMMIT;
