-- 08 audit: insert-only. Grant INSERT/SELECT only; no UPDATE/DELETE/MODIFY to any app or user
-- principal (FERC canvas REQ-23; §12.12 permanent records). Time travel is not an archive.
USE CATALOG IDENTIFIER(:catalog);

-- FERC canvas audit.review_record (App K certification; Ch.16 escalation)
CREATE TABLE IF NOT EXISTS audit.review_record (
  review_id           STRING NOT NULL,
  dam_id              STRING NOT NULL,
  subject_type        STRING NOT NULL COMMENT 'reading | exceedance | dssmr | threshold | commentary | rule_finding',
  subject_id          STRING NOT NULL,
  review_level        STRING NOT NULL COMMENT 'technician | engineer | CDSE | IC',
  decision            STRING NOT NULL,
  comment             STRING,
  reviewer_id         STRING NOT NULL,
  reviewer_pe_license STRING,
  signed_ts           TIMESTAMP NOT NULL,
  signature_hash      STRING NOT NULL COMMENT 'sha256 of the reviewed content/artifact',
  source              STRING COMMENT 'APP (Lakebase extract)',
  CONSTRAINT pk_review PRIMARY KEY (review_id)
) TBLPROPERTIES (delta.appendOnly = true);

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
  COMMENT 'At-least-once copy of Lakebase review_signoff/exceedance_status/audit_log/...; dedupe on (table_name, pk)';
