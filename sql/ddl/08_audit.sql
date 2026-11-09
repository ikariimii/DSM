-- 08 audit: insert-only. Grant INSERT/SELECT only; no UPDATE/DELETE/MODIFY to any app or user
-- principal (FERC canvas REQ-23; §12.12 permanent records). Time travel is not an archive.
USE CATALOG IDENTIFIER(:catalog);

-- FERC canvas audit.review_record (App K certification; Ch.16 escalation)
CREATE TABLE IF NOT EXISTS audit.review_record (
  review_id           STRING NOT NULL,
  dam_id              STRING NOT NULL,
  subject_type        STRING NOT NULL COMMENT 'reading | exceedance | dssmr | threshold | commentary | rule_finding | incl_correction | alarm_event',
  subject_id          STRING NOT NULL,
  review_level        STRING NOT NULL COMMENT 'technician | engineer | CDSE | IC',
  decision            STRING NOT NULL COMMENT 'incl_correction: approve | reject; alarm_event: acknowledge | close (lowercase)',
  comment             STRING COMMENT 'incl_correction / alarm_event: the reviewer rationale (required, non-blank)',
  reviewer_id         STRING NOT NULL,
  reviewer_pe_license STRING,
  signed_ts           TIMESTAMP NOT NULL,
  signature_hash      STRING NOT NULL COMMENT 'sha256 of the reviewed content/artifact; incl_correction: SCHEMA_CONTRACT §5.5 canonical record',
  source              STRING COMMENT 'APP (Lakebase extract)',
  -- v1.6 (QA-039 v2, App Dev §7 9b): per-dam hash chain of reviewer actions (nullable: rows that are not app
  -- reviewer actions carry none, and ck_review_record_chain requires them whenever action_type is set).
  action_type         STRING COMMENT 'bias_review | data_correction_review | alarm_ack | alarm_close',
  reviewer_email      STRING COMMENT 'X-Forwarded-Email at signing',
  reviewer_sub        STRING COMMENT 'unverified sub claim of the user token (identity label only)',
  original_value      STRING COMMENT 'canonical JSON; the value before the action, never overwritten',
  corrected_value     STRING COMMENT 'canonical JSON; the proposed / resulting value',
  chain_id            STRING COMMENT 'per-dam chain (= dam_id)',
  chain_seq           BIGINT COMMENT '1-based position in chain_id',
  prev_hash           STRING COMMENT 'record_hash of chain_seq-1; GENESIS = 64 x 0',
  record_hash         STRING COMMENT 'sha256 canonical JSON of all columns except record_hash, signature, sig_alg, key_id (SCHEMA_CONTRACT §5.5.2)',
  signature           STRING COMMENT 'reserved (future key-based signature), NULL',
  sig_alg             STRING,
  key_id              STRING,
  -- v1.7 canon v2 (App Dev c797151, app SCHEMA_CONTRACT §7 item 14; names are the app's): hashed on canon-2 records.
  actor_app_role      STRING COMMENT 'canon 2: raw app roles of the actor on the dam at signed_ts, sorted comma-joined from viewer|engineer|dso|dam_admin|admin|cdse (server-derived); NULL on canon-1 rows',
  canon_version       INT COMMENT 'canonical record form: NULL = canon 1 (CHAIN_FIELDS), 2 = + actor_app_role, canon_version',
  CONSTRAINT pk_review PRIMARY KEY (review_id)
) TBLPROPERTIES (delta.appendOnly = true);

-- v1.5 (QA-039) / v1.6 (QA-039 v2): subject_type / decision domain. 'incl_correction' rows (decision 'approve' or
-- 'reject') and 'alarm_event' rows (decision 'acknowledge' or 'close') come from the app's Lakebase review_signoff
-- (job dam_lakebase_extract), lowercase, with a non-blank rationale in comment. The pre-v1.5 uppercase decisions of
-- the other subject types stay allowed. Migrations for an existing table: sql/migrations/v1_5_001_contract.sql,
-- then sql/migrations/v1_6_001_audit_chain.sql.
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

-- v1.6 (QA-039 v2): a chained reviewer action carries every required chain field (signature, sig_alg, key_id,
-- reviewer_email and reviewer_sub stay nullable), its action matches its subject and decision, the chain is the
-- dam's, and every alarm_event record is a chained action. v1.6 §7.13 (app 6f7a1d5): subject_type 'alarm_rule'
-- (live cdse_ack_required edits: propose | apply | reject, action_type alarm_rule_flag_*) and a non-blank rationale on
-- EVERY chained record. WHO may propose/apply/reject is NOT a row CHECK (it needs ref.v_dam_reviewer_authority at
-- signed_ts): dam_safety.rule_flag_authority, run by the verify_review_chain task. Rows with action_type NULL (pre-v1.6 incl_correction
-- extracts, other subject types) are not affected.
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

-- v1.7 canon v2 (migration v1_7_002_canon_v2): a canon-2 record names its actor's app role. Shape only; the token
-- vocabulary and ordering are checked by the verifier (dam_safety.audit_chain.actor_role_problem: actor_role_invalid).
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_canon;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_canon CHECK (
  canon_version IS NULL OR (canon_version = 2 AND actor_app_role IS NOT NULL AND length(trim(actor_app_role)) > 0));

-- QA-045d: append-only chain heads. dam_lakebase_extract appends the app's Lakebase review_chain_head per chain
-- (read in the same REPEATABLE READ snapshot as the review_signoff rows it copies) whenever it changed; the verifier
-- (dam_safety.audit_chain.verify_with_chain_heads) reads the LATEST row per (dam_id, chain_id) and checks every earlier
-- row as a checkpoint. delta.appendOnly refuses UPDATE and DELETE (Delta docs, "append-only tables"); like the other
-- audit tables, grant INSERT/SELECT only. Existing deployments: sql/migrations/v1_6_002_chain_head.sql.
CREATE TABLE IF NOT EXISTS audit.chain_head (
  dam_id       STRING    NOT NULL,
  chain_id     STRING    NOT NULL COMMENT 'per-dam reviewer chain (= dam_id)',
  chain_seq    BIGINT    NOT NULL COMMENT 'seq of the last record when observed; 0 = empty chain',
  head_hash    STRING    NOT NULL COMMENT 'record_hash of that record (GENESIS for seq 0)',
  computed_utc TIMESTAMP NOT NULL COMMENT 'when the job read the head (same REPEATABLE READ snapshot as the records)',
  job_run_id   STRING    NOT NULL COMMENT 'Databricks job run id (or manual-<utc>) that wrote the row',
  source       STRING    COMMENT 'APP:lakebase:review_chain_head'
) TBLPROPERTIES (delta.appendOnly = true)
  COMMENT 'QA-045d: append-only history of the app chain head per dam/chain. Verifiers read the latest row (never a caller-supplied head); every row is also a checkpoint.';
ALTER TABLE audit.chain_head DROP CONSTRAINT IF EXISTS ck_chain_head;
ALTER TABLE audit.chain_head ADD CONSTRAINT ck_chain_head CHECK (
  chain_id = dam_id AND chain_seq >= 0 AND head_hash RLIKE '^[0-9a-f]{64}$');

-- Generic event log: alarm transitions, acks, threshold/calibration changes, logins of note.
CREATE TABLE IF NOT EXISTS audit.event_log (
  event_id     STRING NOT NULL,
  dam_id       STRING,
  event_type   STRING NOT NULL COMMENT 'ALARM_TRANSITION | ALARM_ACK | THRESHOLD_CHANGE | CALIBRATION_CHANGE | RECOMPUTE | NOTE | CDSE_ASSIGNMENT_LOAD (job apply_cdse_assignments, one row per outbox outcome, event_id cdse_load:<outbox_id>:<status>)',
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
