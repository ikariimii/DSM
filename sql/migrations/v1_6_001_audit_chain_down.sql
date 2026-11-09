-- DOWN for v1_6_001_audit_chain.sql. Data-level fallback: Delta time travel / RESTORE TABLE audit.review_record (and
-- ops.alarm_transition) TO VERSION AS OF <version before v1_6_001> (DESCRIBE HISTORY); this script never touches data.
-- What it reverses: ck_review_record_chain is dropped and ck_review_record_subject_decision goes back to its v1_5_001
-- body (no alarm_event subject). What it does NOT reverse, by design: the 12 chain columns of audit.review_record and
-- ops.alarm_transition.installation_version stay (chained records hash them; dropping them destroys audit evidence).
-- Run v1_6_002 .. v1_7_003 downs first.
-- PRE-CHECK (restoring the v1.5 CHECK fails, and the table is then left without it, if a newer record exists;
-- audit.review_record is append-only, such records are never deleted - then stop and keep v1_6_001):
--   SELECT review_id FROM audit.review_record WHERE subject_type NOT IN ('reading', 'exceedance', 'dssmr',
--     'threshold', 'commentary', 'rule_finding', 'incl_correction');                        -- must be empty
-- IDEMPOTENT, safe to re-run. Re-applying v1_6_001 afterwards restores everything it reverses.
-- UNVERIFIED: not run on a workspace (static + PostgreSQL-translated tests: tests/test_migration_downs.py).
USE CATALOG IDENTIFIER(:catalog);

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_chain;

-- v1_5_001 body
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)));
