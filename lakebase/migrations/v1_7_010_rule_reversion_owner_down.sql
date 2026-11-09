-- Rollback of lakebase/migrations/v1_7_010_rule_reversion_owner.sql: dam_app.rule_reversion_proposal goes back to the
-- app role (as before v1_7_010). Drops nothing: no table, column, row, trigger or function; the v1_7_009 status trigger
-- stays (ENABLE ALWAYS). Needs dam.app_role:
--   PGOPTIONS='-c dam.app_role=<app role>' psql "$DSN" -X -v ON_ERROR_STOP=1 -f lakebase/migrations/v1_7_010_rule_reversion_owner_down.sql
-- IDEMPOTENT. After it the validator reports nothing for this table again only if v1_7_010's checks are also rolled
-- back (lakebase/validate_append_only.sql from before v1_7_010): app ownership is what the newer validator flags.
SET search_path TO dam_app;
BEGIN;
DO $$
DECLARE
  r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF r IS NULL OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    RAISE EXCEPTION 'v1_7_010 down: SET dam.app_role to the app role' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  EXECUTE format('ALTER TABLE dam_app.rule_reversion_proposal OWNER TO %I', r);
END $$;
COMMIT;
