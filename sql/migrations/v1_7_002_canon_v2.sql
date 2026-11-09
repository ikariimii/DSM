-- Migration v1.7.002 (canon v2; App Dev c797151, app SCHEMA_CONTRACT §7 item 14; column names are the app's):
-- audit.review_record gains actor_app_role STRING and canon_version INT (after key_id, extract 24 -> 26 columns) and
-- ck_review_record_canon. Canon-1 rows keep both NULL (never backfilled: append-only, hash-chained records).
-- ORDER: after v1_5_001, v1_6_001, v1_6_002, v1_6_003, v1_7_001; BEFORE the extract carries any canon-2 row (the
-- extract writes by the table schema, so until this has run the 26-column write fails and the watermark does not
-- move: no record is lost) and before verify_review_chain (it SELECTs both columns).
-- IDEMPOTENT, safe to re-run: guarded ADD COLUMNS (information_schema lookup in a SQL script block); DROP CONSTRAINT
-- IF EXISTS then ADD, same body as sql/ddl/08. No data change.
-- PRE-CHECK: none needed on first run (both columns are new and NULL, which the CHECK accepts). On a re-run:
--   SELECT review_id FROM audit.review_record WHERE NOT (canon_version IS NULL OR (canon_version = 2 AND actor_app_role IS NOT NULL AND length(trim(actor_app_role)) > 0));   -- must be empty
-- DOWN: sql/migrations/v1_7_002_canon_v2_down.sql (drops the CHECK only; the columns hold audit data and stay).
-- UNVERIFIED: not run on a workspace, docs/VERIFICATION.md CANON-V2.
USE CATALOG IDENTIFIER(:catalog);

BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'actor_app_role') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (actor_app_role STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'review_record' AND column_name = 'canon_version') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.review_record ADD COLUMNS (canon_version INT)';
  END IF;
END;

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_canon;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_canon CHECK (
  canon_version IS NULL OR (canon_version = 2 AND actor_app_role IS NOT NULL AND length(trim(actor_app_role)) > 0));
