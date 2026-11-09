-- DOWN for v1_7_002_canon_v2.sql. Data-level fallback: Delta time travel / RESTORE TABLE audit.review_record TO
-- VERSION AS OF <version before v1_7_002> (DESCRIBE HISTORY audit.review_record); this script never touches data.
-- What it reverses: ck_review_record_canon (dropped). What it does NOT reverse, by design: the columns actor_app_role
-- and canon_version stay, because canon-2 records hash both (dropping them would destroy audit data and make every
-- canon-2 record unverifiable; audit.review_record is delta.appendOnly). Code rollback caveat: a pre-v1.7.002 extract
-- builds rows by the table schema and fails (KeyError on the two columns) without moving its watermark; a pre-v1.7
-- verifier ignores them and reports every canon-2 row as hash_mismatch. Roll code back only together with the app.
-- PRE-CHECK: none (only a CHECK is dropped).
-- IDEMPOTENT, safe to re-run (DROP CONSTRAINT IF EXISTS). Re-applying v1_7_002 afterwards restores the CHECK.
-- UNVERIFIED: not run on a workspace.
USE CATALOG IDENTIFIER(:catalog);

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_canon;
