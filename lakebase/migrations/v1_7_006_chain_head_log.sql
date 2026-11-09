-- Lakebase migration v1_7_006 (DA residual (a), tamper-resistant chain head): append-only review_chain_head_log
-- (owner dam_lakebase_owner, guard triggers ENABLE ALWAYS, app role SELECT + INSERT only, extract role SELECT),
-- seeded once from the legacy app-owned review_chain_head. The block is byte-identical to lakebase/001
-- (tests/test_chain_head_log.py); re-running lakebase/001 has the same effect. Run as the deployer with membership in
-- dam_lakebase_owner, with SET dam.app_role and dam.extract_role.
-- ORDER: after lakebase/001 at v1_7_005; BEFORE the app carrying the app head-log change (state.py, applied by App Dev in 478b7b5) (the
-- patched app needs the log; an unpatched app keeps working and only updates the legacy table); the extract
-- (src/jobs/lakebase_extract.py) reads both tables. IDEMPOTENT. Then lakebase/validate_append_only.sql (0 rows).
-- DOWN: v1_7_006_chain_head_log_down.sql (drops nothing; revokes the app role's INSERT).
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_7_006 block (residual (a); identical in lakebase/migrations/v1_7_006_chain_head_log.sql)
-- v1.7 residual (a): tamper-resistant chain head. review_chain_head above is app-owned and UPDATEd in place, so the
-- app role could rewind it. review_chain_head_log is append-only like review_signoff (owner dam_lakebase_owner,
-- guard triggers ENABLE ALWAYS, app SELECT + INSERT only): one row per advance, the current head of a chain is its
-- highest chain_seq, PK (chain_id, chain_seq) forbids two heads at the same position. The patched app
-- (the app head-log change in state.py, applied by App Dev in 478b7b5) appends here instead of updating review_chain_head; the extract
-- takes the highest chain_seq per chain over both tables (transition), preferring this table at equal seq.
CREATE TABLE IF NOT EXISTS review_chain_head_log (
  chain_id TEXT NOT NULL, chain_seq INTEGER NOT NULL CHECK (chain_seq >= 0),
  head_hash TEXT NOT NULL CHECK (head_hash ~ '^[0-9a-f]{64}$'), updated_ts TEXT NOT NULL,
  PRIMARY KEY (chain_id, chain_seq));
-- seed from the legacy head once (idempotent): the deployer, not the app, writes this
INSERT INTO review_chain_head_log (chain_id, chain_seq, head_hash, updated_ts)
  SELECT chain_id, chain_seq, head_hash, updated_ts FROM review_chain_head WHERE head_hash ~ '^[0-9a-f]{64}$'
  ON CONFLICT (chain_id, chain_seq) DO NOTHING;
CREATE OR REPLACE TRIGGER t_noupd_review_chain_head_log BEFORE UPDATE OR DELETE ON review_chain_head_log
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE OR REPLACE TRIGGER t_notrunc_review_chain_head_log BEFORE TRUNCATE ON review_chain_head_log
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
-- END v1_7_006 block

ALTER TABLE review_chain_head_log OWNER TO dam_lakebase_owner;
ALTER TABLE review_chain_head_log ENABLE ALWAYS TRIGGER t_noupd_review_chain_head_log,
  ENABLE ALWAYS TRIGGER t_notrunc_review_chain_head_log;
REVOKE ALL ON review_chain_head_log FROM PUBLIC;
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
        x TEXT := nullif(current_setting('dam.extract_role', true), '');
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE ALL ON review_chain_head_log FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON review_chain_head_log TO %I', r);
  END IF;
  IF x IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = x) THEN
    EXECUTE format('GRANT SELECT ON review_chain_head_log TO %I', x);
  END IF;
END $$;
COMMIT;
