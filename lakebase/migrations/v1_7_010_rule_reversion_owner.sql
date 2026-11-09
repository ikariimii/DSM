-- Lakebase migration v1_7_010: dam_app.rule_reversion_proposal owned by dam_lakebase_owner (App Dev follow-up 1).
-- The app role keeps SELECT, INSERT and UPDATE (status, decided_by, decided_by_id, decided_at, decision_rationale,
-- pub_ids) only; status moves stay guarded by t_status_rule_reversion_proposal (v1_7_009, ENABLE ALWAYS).
-- Run as a superuser / the owner's admin with the app role named:
--   PGOPTIONS='-c dam.app_role=<app role>' psql "$DSN" -X -v ON_ERROR_STOP=1 -f lakebase/migrations/v1_7_010_rule_reversion_owner.sql
-- ORDER: after v1_7_009. IDEMPOTENT. No data is touched. Then run lakebase/validate_append_only.sql (0 rows).
-- Rollback: v1_7_010_rule_reversion_owner_down.sql (ownership back to the app role; no data dropped).
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_7_010 block (App Dev follow-up 1: rule_reversion_proposal owned by dam_lakebase_owner, like the
-- outboxes. The app keeps SELECT, INSERT and column UPDATE of exactly the columns its decide_pending writes
-- (app/backend/routers/rule_reversion.py approve / reject -> state.decide_pending): status, decided_by,
-- decided_by_id, decided_at, decision_rationale, pub_ids. status moves stay guarded by t_status_rule_reversion_proposal
-- (v1_7_009, ENABLE ALWAYS); no table-level UPDATE, DELETE, TRUNCATE, REFERENCES or TRIGGER, and as a non-owner the
-- app can no longer ALTER, drop or disable the table's triggers. Refuses to run when the table is owned by an
-- unprivileged role (the app) and dam.app_role is not set, because the app would lose all access.)
DO $$
DECLARE
  r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF (r IS NULL OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)) AND EXISTS (
       SELECT 1 FROM pg_class c JOIN pg_roles o ON o.oid = c.relowner
       WHERE c.oid = 'dam_app.rule_reversion_proposal'::regclass
         AND o.rolname <> 'dam_lakebase_owner' AND NOT o.rolsuper) THEN
    RAISE EXCEPTION 'v1_7_010: SET dam.app_role to the app role first (it owns dam_app.rule_reversion_proposal and would lose access)'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  ALTER TABLE dam_app.rule_reversion_proposal OWNER TO dam_lakebase_owner;
  IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'dam_app.rule_reversion_proposal'::regclass
             AND tgname = 't_status_rule_reversion_proposal') THEN
    ALTER TABLE dam_app.rule_reversion_proposal ENABLE ALWAYS TRIGGER t_status_rule_reversion_proposal;
  END IF;
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE ALL ON dam_app.rule_reversion_proposal FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON dam_app.rule_reversion_proposal TO %I', r);
    EXECUTE format('GRANT UPDATE (status, decided_by, decided_by_id, decided_at, decision_rationale, pub_ids) '
                   'ON dam_app.rule_reversion_proposal TO %I', r);
  END IF;
END $$;
-- END v1_7_010 block
COMMIT;
