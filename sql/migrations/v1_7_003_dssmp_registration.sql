-- Migration v1.7.003 (DSSMP registration; Genie 2026-11-09, promised to App Dev): ref.dssmp_version gains
-- document_sha256, approved_by, approved_at, approval_record_hash, loaded_from_outbox_id, volume_uri (UC copy of the
-- document in volume ref.dssmp_documents, created here and in sql/ddl/00); ck_dssmp_version_status
-- (status draft | approved | superseded, version_no >= 1, effective_to after effective_from) and
-- ck_dssmp_version_approval (an approved row carries its approval fields). audit.review_record accepts subject_type
-- 'dssmp_version' with action_type dssmp_version_propose | _approve | _reject (decision propose | approve | reject,
-- non-blank rationale): ck_review_record_subject_decision and ck_review_record_chain re-created with the same bodies as
-- sql/ddl/04 and sql/ddl/08. Loader: dam_ref_publish task load_dssmp_registrations (dam_safety.dssmp_registration).
-- CDSE bootstrap / change (Genie (B)): the same CHECKs also accept subject_type 'dam' with action_type
-- dam_cdse_assign (decision assign) | dam_cdse_change_propose (propose) | dam_cdse_change_approve (approve) |
-- dam_cdse_change_reject (reject) (names: app SCHEMA_CONTRACT §7 item 20), subject_id
-- = dam_id; new table ref.dam_cdse_assignment (same DDL as sql/ddl/01) + ck_dam_cdse_assignment, written by the
-- dam_ref_publish task apply_cdse_assignments (dam_safety.cdse_assignment). Re-run dam_uc_setup afterwards so
-- sql/ddl/10b re-creates ref.v_dam_reviewer_authority on the assignment history and ref.v_dam_go_live_readiness.
-- ORDER: after v1_5_001 .. v1_7_002; before the first dam_ref_publish run and before any app dssmp_version record.
-- IDEMPOTENT, safe to re-run: guarded ADD COLUMNS; DROP CONSTRAINT IF EXISTS then ADD. No data change.
-- PRE-CHECK (ADD CONSTRAINT validates existing rows; if either query returns rows the ADD fails and nothing else
-- changes): an approved row registered before v1.7 has no approval fields and must be (re-)registered through the
-- app outbox with a CDSE approve record, never back-filled by hand:
--   SELECT dssmp_version_id FROM ref.dssmp_version WHERE status NOT IN ('draft', 'approved', 'superseded') OR version_no < 1 OR effective_to <= effective_from;
--   SELECT dssmp_version_id FROM ref.dssmp_version WHERE status = 'approved';   -- must be empty on first run
-- DOWN: sql/migrations/v1_7_003_dssmp_registration_down.sql (drops the two ref.dssmp_version CHECKs and restores the
-- v1_6_003 review_record CHECKs; the columns stay).
-- UNVERIFIED: not run on a workspace.
USE CATALOG IDENTIFIER(:catalog);

BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'dssmp_version' AND column_name = 'document_sha256') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.dssmp_version ADD COLUMNS (document_sha256 STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'dssmp_version' AND column_name = 'approved_by') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.dssmp_version ADD COLUMNS (approved_by STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'dssmp_version' AND column_name = 'approved_at') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.dssmp_version ADD COLUMNS (approved_at TIMESTAMP)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'dssmp_version' AND column_name = 'approval_record_hash') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.dssmp_version ADD COLUMNS (approval_record_hash STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'dssmp_version' AND column_name = 'loaded_from_outbox_id') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.dssmp_version ADD COLUMNS (loaded_from_outbox_id STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'dssmp_version' AND column_name = 'volume_uri') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.dssmp_version ADD COLUMNS (volume_uri STRING)';
  END IF;
END;

ALTER TABLE ref.dssmp_version DROP CONSTRAINT IF EXISTS ck_dssmp_version_status;
ALTER TABLE ref.dssmp_version ADD CONSTRAINT ck_dssmp_version_status CHECK (
  status IN ('draft', 'approved', 'superseded') AND version_no >= 1
  AND (effective_to IS NULL OR effective_to > effective_from));
ALTER TABLE ref.dssmp_version DROP CONSTRAINT IF EXISTS ck_dssmp_version_approval;
ALTER TABLE ref.dssmp_version ADD CONSTRAINT ck_dssmp_version_approval CHECK (
  status <> 'approved'
  OR (approved_by IS NOT NULL AND length(trim(approved_by)) > 0 AND approved_at IS NOT NULL
      AND document_sha256 IS NOT NULL AND document_sha256 RLIKE '^[0-9a-f]{64}$'
      AND approval_record_hash IS NOT NULL AND approval_record_hash RLIKE '^[0-9a-f]{64}$'));

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction',
                   'alarm_event', 'alarm_rule', 'dssmp_version', 'dam')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_event'
       OR (decision IN ('acknowledge', 'close') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_rule'
       OR (decision IN ('propose', 'apply', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'dssmp_version'
       OR (decision IN ('propose', 'approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'dam'
       OR (decision IN ('assign', 'propose', 'approve', 'reject') AND comment IS NOT NULL
           AND length(trim(comment)) > 0)));
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_chain;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_chain CHECK (
  (subject_type NOT IN ('alarm_event', 'alarm_rule', 'dssmp_version', 'dam') OR action_type IS NOT NULL)
  AND (action_type IS NULL OR (
    action_type IN ('bias_review', 'data_correction_review', 'alarm_ack', 'alarm_close',
                    'alarm_rule_flag_propose', 'alarm_rule_flag_apply', 'alarm_rule_flag_reject',
                    'dssmp_version_propose', 'dssmp_version_approve', 'dssmp_version_reject',
                    'dam_cdse_assign', 'dam_cdse_change_propose', 'dam_cdse_change_approve',
                    'dam_cdse_change_reject')
    AND original_value IS NOT NULL AND corrected_value IS NOT NULL AND chain_id IS NOT NULL
    AND chain_seq IS NOT NULL AND chain_seq >= 1 AND prev_hash IS NOT NULL AND record_hash IS NOT NULL
    AND chain_id = dam_id AND comment IS NOT NULL AND length(trim(comment)) > 0
    AND ((action_type IN ('bias_review', 'data_correction_review') AND subject_type = 'incl_correction')
         OR (action_type = 'alarm_ack' AND subject_type = 'alarm_event' AND decision = 'acknowledge')
         OR (action_type = 'alarm_close' AND subject_type = 'alarm_event' AND decision = 'close')
         OR (action_type = 'alarm_rule_flag_propose' AND subject_type = 'alarm_rule' AND decision = 'propose')
         OR (action_type = 'alarm_rule_flag_apply' AND subject_type = 'alarm_rule' AND decision = 'apply')
         OR (action_type = 'alarm_rule_flag_reject' AND subject_type = 'alarm_rule' AND decision = 'reject')
         OR (action_type = 'dssmp_version_propose' AND subject_type = 'dssmp_version' AND decision = 'propose')
         OR (action_type = 'dssmp_version_approve' AND subject_type = 'dssmp_version' AND decision = 'approve')
         OR (action_type = 'dssmp_version_reject' AND subject_type = 'dssmp_version' AND decision = 'reject')
         OR (action_type = 'dam_cdse_assign' AND subject_type = 'dam' AND decision = 'assign'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_propose' AND subject_type = 'dam' AND decision = 'propose'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_approve' AND subject_type = 'dam' AND decision = 'approve'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_reject' AND subject_type = 'dam' AND decision = 'reject'
             AND subject_id = dam_id)))));

-- v1.7 (b): approved DSSMP documents are copied here by dam_ref_publish (dam_safety.dssmp_documents). Same statement as
-- sql/ddl/00. The down script leaves the volume and its files in place (no data dropped).
CREATE VOLUME IF NOT EXISTS ref.dssmp_documents COMMENT 'Approved DSSMP documents copied by dam_ref_publish: /<dam_id>/<dssmp_version_id>/<file> (sha256 checked; v1.7)';

-- v1.7 (Genie 2026-11-09 (B), migration v1_7_003): SCD2 history of each dam's designated CDSE, written only by the
-- dam_ref_publish task apply_cdse_assignments from signed chain records (dam_safety.cdse_assignment):
-- dam_cdse_assign (platform admin, first CDSE, dam not live) and dam_cdse_change_propose + dam_cdse_change_approve
-- (platform admin proposes, the current CDSE approves). ref.v_dam_reviewer_authority (10b) reads it; ref.dam.cdse_id
-- is kept equal to the open row. One open row (effective_to NULL) per dam.
CREATE TABLE IF NOT EXISTS ref.dam_cdse_assignment (
  assignment_id        STRING NOT NULL COMMENT 'approval_record_hash of the applying record; legacy:<dam_id> for a pre-v1.7 ref.dam.cdse_id',
  dam_id               STRING NOT NULL,
  cdse_id              STRING NOT NULL COMMENT 'the designated CDSE principal (email)',
  effective_from       TIMESTAMP COMMENT 'signed_ts of the applying record (NULL only on the legacy row)',
  effective_to         TIMESTAMP COMMENT 'signed_ts of the record that replaced it; NULL = current',
  action_type          STRING NOT NULL COMMENT 'dam_cdse_assign | dam_cdse_change_approve | dam_cdse_legacy',
  reason_code          STRING NOT NULL,
  assigned_by          STRING COMMENT 'signer of the applying record (platform admin for assign, the previous CDSE for a change)',
  proposed_by          STRING COMMENT 'signer of the dam_cdse_change_propose record (change only)',
  proposal_record_hash STRING COMMENT 'record_hash of the dam_cdse_change_propose record (change only)',
  approval_record_hash STRING COMMENT 'audit.review_record.record_hash of the dam_cdse_assign / dam_cdse_change_approve record',
  CONSTRAINT pk_dam_cdse_assignment PRIMARY KEY (assignment_id)
);
ALTER TABLE ref.dam_cdse_assignment DROP CONSTRAINT IF EXISTS ck_dam_cdse_assignment;
ALTER TABLE ref.dam_cdse_assignment ADD CONSTRAINT ck_dam_cdse_assignment CHECK (
  length(trim(cdse_id)) > 0 AND length(trim(reason_code)) > 0
  AND action_type IN ('dam_cdse_assign', 'dam_cdse_change_approve', 'dam_cdse_legacy')
  AND (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from)
  AND (action_type = 'dam_cdse_legacy'
       OR (effective_from IS NOT NULL AND approval_record_hash IS NOT NULL AND assignment_id = approval_record_hash))
  AND (action_type <> 'dam_cdse_change_approve' OR (proposal_record_hash IS NOT NULL AND proposed_by IS NOT NULL))
  AND (action_type <> 'dam_cdse_legacy' OR (approval_record_hash IS NULL AND effective_to IS NOT NULL)));
