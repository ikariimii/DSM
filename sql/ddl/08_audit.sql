-- 08 audit: insert-only. Grant INSERT/SELECT only; no UPDATE/DELETE/MODIFY to any app or user
-- principal (FERC canvas REQ-23; §12.12 permanent records). Time travel is not an archive.
USE CATALOG IDENTIFIER(:catalog);

-- FERC canvas audit.review_record (App K certification; Ch.16 escalation)
CREATE TABLE IF NOT EXISTS audit.review_record (
  review_id           STRING NOT NULL,
  dam_id              STRING NOT NULL,
  subject_type        STRING NOT NULL COMMENT 'reading | exceedance | dssmr | threshold | commentary | rule_finding | incl_correction',
  subject_id          STRING NOT NULL,
  review_level        STRING NOT NULL COMMENT 'technician | engineer | CDSE | IC',
  decision            STRING NOT NULL COMMENT 'incl_correction: approve | reject (lowercase)',
  comment             STRING COMMENT 'incl_correction: the reviewer rationale (required)',
  reviewer_id         STRING NOT NULL,
  reviewer_pe_license STRING,
  signed_ts           TIMESTAMP NOT NULL,
  signature_hash      STRING NOT NULL COMMENT 'sha256 of the reviewed content/artifact; incl_correction: SCHEMA_CONTRACT §5.5 canonical record',
  source              STRING COMMENT 'APP (Lakebase extract)',
  CONSTRAINT pk_review PRIMARY KEY (review_id)
) TBLPROPERTIES (delta.appendOnly = true);

-- v1.5 (QA-039): subject_type / decision domain. 'incl_correction' rows come from the app's Lakebase
-- review_signoff (job dam_lakebase_extract) with decision 'approve' or 'reject' (lowercase) and a rationale.
-- The pre-v1.5 uppercase decisions of the other subject types stay allowed. Migration for an existing table:
-- sql/migrations/v1_5_001_contract.sql.
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)));

-- Generic event log: alarm transitions, acks, threshold/calibration changes, logins of note.
CREATE TABLE IF NOT EXISTS audit.event_log (
  event_id     STRING NOT NULL,
  dam_id       STRING,
  event_type   STRING NOT NULL COMMENT 'ALARM_TRANSITION | ALARM_ACK | THRESHOLD_CHANGE | CALIBRATION_CHANGE | RECOMPUTE | NOTE',
  subject_type STRING,
  subject_id   STRING,
  actor        STRING NOT NULL,
  event_ts     TIMESTAMP NOT NULL,
  payload      STRING COMMENT 'JSON',
  recorded_ts  TIMESTAMP NOT NULL
) TBLPROPERTIES (delta.appendOnly = true);

CREATE TABLE IF NOT EXISTS audit.recompute_log (
  recompute_batch_id STRING NOT NULL,
  requested_by       STRING NOT NULL,
  reason             STRING NOT NULL,
  sensor_id          STRING NOT NULL,
  window_start       TIMESTAMP NOT NULL,
  window_end         TIMESTAMP,
  old_calib_ids      ARRAY<STRING>,
  new_calib_ids      ARRAY<STRING>,
  rows_superseded    BIGINT,
  rows_written       BIGINT,
  status             STRING COMMENT 'PLANNED | DONE | FAILED',
  alarm_diff_count   BIGINT COMMENT 'alarm replay differences written to audit.alarm_recompute_diff',
  started_ts         TIMESTAMP, finished_ts TIMESTAMP
) TBLPROPERTIES (delta.appendOnly = true);

-- Recompute never rewrites alarm history; a replay diff is recorded for engineer review.
CREATE TABLE IF NOT EXISTS audit.alarm_recompute_diff (
  recompute_batch_id STRING NOT NULL,
  alarm_key          STRING NOT NULL,
  ts_utc             TIMESTAMP NOT NULL,
  original_event     STRING, original_to_state STRING,
  replay_event       STRING, replay_to_state   STRING,
  diff_type          STRING COMMENT 'MISSING_IN_REPLAY | NEW_IN_REPLAY | STATE_DIFFERS',
  requires_engineer_review BOOLEAN NOT NULL,
  recorded_ts        TIMESTAMP NOT NULL
) TBLPROPERTIES (delta.appendOnly = true);

-- Insert-only envelope for immutable app records extracted from Lakebase (job dam_lakebase_extract).
CREATE TABLE IF NOT EXISTS audit.app_extract (
  table_name STRING NOT NULL COMMENT 'Lakebase source table',
  pk         STRING NOT NULL,
  dam_id     STRING,
  payload    STRING NOT NULL COMMENT 'Row as JSON (sorted keys)',
  extract_ts TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.appendOnly' = 'true')
  COMMENT 'At-least-once copy of Lakebase exceedance_status/audit_log/... (review_signoff goes to audit.review_record since v1.5); dedupe on (table_name, pk)';
