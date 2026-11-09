-- Lakebase migration v1_7_009 (residual 1): refuse any status change out of a terminal state on
-- dam_cdse_outbox, dssmp_registration_outbox (terminal: loaded, rejected) and rule_reversion_proposal (terminal:
-- applied, rejected); allowed: pending -> one terminal state, and same-status updates of a pending row. The block is
-- byte-identical to lakebase/001 (tests/test_lakebase_status_transition.py). Run as the deployer (member of
-- dam_lakebase_owner and of the app role, which owns rule_reversion_proposal). ORDER: after v1_7_008. IDEMPOTENT. Then run lakebase/validate_append_only.sql (0 rows).
-- No data is changed. DOWN: v1_7_009_status_transition_guard_down.sql (drops the three triggers and the function only).
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_7_009 block (residual 1: status transitions. On the two publication outboxes and the app's
-- rule_reversion_proposal a row leaves 'pending' once, to one of its terminal states, and never comes back: allowed
-- moves are pending -> loaded | rejected (outboxes) and pending -> applied | rejected (rule_reversion_proposal);
-- a same-status UPDATE is fine on a pending row; a terminal row is frozen (only an UPDATE that changes nothing passes);
-- a legacy value of an older contract may only be normalized to a terminal state (so v1_7_004 / v1_7_008 still run).
-- Function owned by dam_lakebase_owner, triggers ENABLE ALWAYS (fire under session_replication_role = replica too).
-- The outboxes are owner-owned, so neither the app nor the publisher can drop / disable the trigger or replace the
-- function. rule_reversion_proposal: owned by dam_lakebase_owner since v1_7_010 (before that the app role, as owner,
-- could drop or disable its trigger); lakebase/validate_append_only.sql reports a missing / disabled one.)
CREATE OR REPLACE FUNCTION dam_app.status_transition_guard() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    IF OLD.status = 'pending' OR to_jsonb(NEW) IS NOT DISTINCT FROM to_jsonb(OLD) THEN RETURN NEW; END IF;
    RAISE EXCEPTION '%: row is final (status %); it cannot be changed', TG_TABLE_NAME, OLD.status
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.status = 'pending' AND NEW.status = ANY (TG_ARGV) THEN RETURN NEW; END IF;
  -- a LEGACY value (not pending and not a current terminal state, e.g. 'applied' / 'rejected_invalid' left by an
  -- older contract) may only be normalized to a terminal state (the v1_7_004 / v1_7_008 mappings); never to pending
  IF OLD.status <> 'pending' AND NOT (OLD.status = ANY (TG_ARGV)) AND NEW.status = ANY (TG_ARGV) THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION '%: status transition % -> % refused (only pending -> %)', TG_TABLE_NAME, OLD.status,
    NEW.status, array_to_string(TG_ARGV, ' | ') USING ERRCODE = 'check_violation';
END $$;
ALTER FUNCTION dam_app.status_transition_guard() OWNER TO dam_lakebase_owner;
CREATE OR REPLACE TRIGGER t_status_dam_cdse_outbox BEFORE UPDATE ON dam_app.dam_cdse_outbox
  FOR EACH ROW EXECUTE FUNCTION dam_app.status_transition_guard('loaded', 'rejected');
CREATE OR REPLACE TRIGGER t_status_dssmp_registration_outbox BEFORE UPDATE ON dam_app.dssmp_registration_outbox
  FOR EACH ROW EXECUTE FUNCTION dam_app.status_transition_guard('loaded', 'rejected');
CREATE OR REPLACE TRIGGER t_status_rule_reversion_proposal BEFORE UPDATE ON dam_app.rule_reversion_proposal
  FOR EACH ROW EXECUTE FUNCTION dam_app.status_transition_guard('applied', 'rejected');
ALTER TABLE dam_app.dam_cdse_outbox ENABLE ALWAYS TRIGGER t_status_dam_cdse_outbox;
ALTER TABLE dam_app.dssmp_registration_outbox ENABLE ALWAYS TRIGGER t_status_dssmp_registration_outbox;
ALTER TABLE dam_app.rule_reversion_proposal ENABLE ALWAYS TRIGGER t_status_rule_reversion_proposal;
-- END v1_7_009 block
COMMIT;
