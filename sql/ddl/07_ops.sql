-- 07 ops: operational state. Job-owned (src/jobs/alarm_evaluator.py) unless noted.
USE CATALOG IDENTIFIER(:catalog);

-- Current state of the ISA-18.2-style machine per sensor (one row per key; MERGE by job).
-- Mutable by design: it is a projection; the immutable history is ops.alarm_transition.
CREATE TABLE IF NOT EXISTS ops.alarm_state (
  alarm_key                STRING NOT NULL COMMENT 'sensor_id[|metric]',
  dam_id                   STRING NOT NULL,
  sensor_id                STRING NOT NULL,
  state                    STRING NOT NULL COMMENT 'NORMAL | ALERT_UNACK | ALERT_ACK | ACTION_UNACK | ACTION_ACK | CLEARED_UNACK',
  level                    INT    NOT NULL,
  acked                    BOOLEAN NOT NULL,
  cleared_unack            BOOLEAN NOT NULL,
  pending_up               INT, pending_up_level INT, pending_down INT,
  last_value               DOUBLE,
  last_ts                  TIMESTAMP,
  active_threshold_id      STRING,
  active_threshold_version INT,
  evaluated_through_ts     TIMESTAMP COMMENT 'watermark: last reading ts processed',
  updated_ts               TIMESTAMP NOT NULL,
  CONSTRAINT pk_alarm_state PRIMARY KEY (alarm_key)
);

-- Immutable transition log (also mirrored to audit.event_log).
CREATE TABLE IF NOT EXISTS ops.alarm_transition (
  transition_id     STRING NOT NULL,
  alarm_key         STRING NOT NULL,
  dam_id            STRING NOT NULL,
  sensor_id         STRING NOT NULL,
  ts_utc            TIMESTAMP NOT NULL COMMENT 'reading ts (or ack ts) that caused the transition',
  event             STRING NOT NULL COMMENT 'RAISE | ESCALATE | DEESCALATE | CLEAR | RTN_UNACK | ACK',
  from_state        STRING NOT NULL,
  to_state          STRING NOT NULL,
  value             DOUBLE,
  metric_value      DOUBLE COMMENT 'magnitude or rate/day that was compared',
  threshold_id      STRING,
  threshold_version INT,
  actor             STRING COMMENT 'system or acknowledging user',
  comment           STRING,
  reading_id        STRING COMMENT 'silver reading that caused the transition (NULL for ACK)',
  run_id            STRING,
  recorded_ts       TIMESTAMP NOT NULL,
  -- Alarm lineage (SCHEMA_CONTRACT §4.3, app open question 6): the reduction that produced `value`.
  calib_id          STRING COMMENT 'ref.calibration row used for the reading (NULL for ACK)',
  cal_version       INT    COMMENT 'calibration version used for the reading',
  formula_id        STRING,
  formula_version   STRING,
  recompute_batch_id STRING COMMENT 'reduction batch of the reading row (silver.reading_reduced)'
) TBLPROPERTIES (delta.appendOnly = true, delta.enableChangeDataFeed = true);

-- FERC canvas ops.exceedance (§14.4.1(3); §12.3(b)(4)(viii)). One row per RAISE/ESCALATE;
-- verification workflow progresses by APPENDING status rows (exceedance_status_log) and the
-- current status is exposed by gold.v_exceedance_current.
CREATE TABLE IF NOT EXISTS ops.exceedance (
  exceedance_id             STRING NOT NULL,
  dam_id                    STRING NOT NULL,
  reading_id                STRING NOT NULL,
  transition_id             STRING NOT NULL,
  threshold_id              STRING NOT NULL,
  threshold_version         INT    NOT NULL COMMENT 'exact version active at detection (canvas SCD2 note)',
  detected_ts               TIMESTAMP NOT NULL,
  tier                      STRING NOT NULL,
  level                     INT    NOT NULL,
  value                     DOUBLE,
  metric_value              DOUBLE,
  recorded_ts               TIMESTAMP NOT NULL,
  CONSTRAINT pk_exceedance PRIMARY KEY (exceedance_id)
) TBLPROPERTIES (delta.appendOnly = true);

CREATE TABLE IF NOT EXISTS ops.exceedance_status_log (
  exceedance_id              STRING NOT NULL,
  verification_status        STRING NOT NULL COMMENT 'open | verifying | confirmed | false',
  verification_steps         STRING,
  confirmed_ts               TIMESTAMP,
  condition_affecting_safety BOOLEAN,
  cfr_category               STRING,
  incident_id                STRING,
  actor                      STRING NOT NULL,
  status_ts                  TIMESTAMP NOT NULL,
  source                     STRING COMMENT 'APP (via Lakebase extract) | SYSTEM'
) TBLPROPERTIES (delta.appendOnly = true);

-- FERC canvas ops.safety_incident / ops.regulatory_report (§12.10, §12.13)
CREATE TABLE IF NOT EXISTS ops.safety_incident (
  incident_id          STRING NOT NULL,
  dam_id               STRING NOT NULL,
  discovered_ts        TIMESTAMP NOT NULL,
  eap_level            STRING,
  cfr_12_3_items       ARRAY<STRING>,
  initial_report_ts    TIMESTAMP,
  initial_due_ts       TIMESTAMP COMMENT 'discovered_ts + 72h target (canvas)',
  written_due_date     DATE COMMENT 'set by FERC Regional Engineer',
  written_submitted_ts TIMESTAMP,
  verification_12_13   STRING,
  status               STRING,
  created_by           STRING,
  created_ts           TIMESTAMP,
  CONSTRAINT pk_incident PRIMARY KEY (incident_id)
);

CREATE TABLE IF NOT EXISTS ops.regulatory_report (
  report_id     STRING NOT NULL,
  dam_id        STRING NOT NULL,
  incident_id   STRING,
  report_type   STRING COMMENT 'INITIAL_12_10 | WRITTEN_12_10 | VERIFICATION_12_13 | OTHER',
  due_ts        TIMESTAMP,
  submitted_ts  TIMESTAMP,
  artifact_uri  STRING,
  submitted_by  STRING,
  CONSTRAINT pk_regulatory_report PRIMARY KEY (report_id)
);

-- FERC canvas ops.calibration_event / ops.surveillance_inspection (App J §2.1.2, §2.2)
CREATE TABLE IF NOT EXISTS ops.calibration_event (
  calibration_event_id STRING NOT NULL,
  dam_id               STRING NOT NULL,
  instrument_id        STRING NOT NULL,
  sensor_id            STRING,
  event_ts             TIMESTAMP NOT NULL,
  event_type           STRING COMMENT 'FACTORY | FIELD_CHECK | ZERO_RESET | REPLACEMENT',
  result               STRING,
  new_calib_id         STRING COMMENT 'ref.calibration version created by this event',
  performed_by         STRING,
  certificate_uri      STRING
);

CREATE TABLE IF NOT EXISTS ops.surveillance_inspection (
  inspection_id    STRING NOT NULL,
  dam_id           STRING NOT NULL,
  structure_id     STRING,
  inspection_ts    TIMESTAMP NOT NULL,
  inspection_type  STRING,
  checklist_result STRING COMMENT 'JSON',
  photo_uris       ARRAY<STRING>,
  inspector        STRING,
  source           STRING COMMENT 'APP (Lakebase extract)'
);

-- Engineer marks a reading erroneous (canvas ops.reading.is_erroneous); append-only.
CREATE TABLE IF NOT EXISTS ops.reading_erroneous_mark (
  reading_id       STRING NOT NULL,
  is_erroneous     BOOLEAN NOT NULL,
  erroneous_reason STRING,
  marked_by        STRING NOT NULL,
  marked_ts        TIMESTAMP NOT NULL
) TBLPROPERTIES (delta.appendOnly = true);

-- Interpretation-layer rule findings (PFM rule engine), see docs/INTERPRETATION.md
CREATE TABLE IF NOT EXISTS ops.rule_finding (
  finding_id     STRING NOT NULL,
  dam_id         STRING NOT NULL,
  pfm_id         STRING,
  rule_id        STRING NOT NULL,
  rule_version   STRING NOT NULL,
  severity       STRING COMMENT 'INFO | WATCH | CONCERN',
  window_start   TIMESTAMP, window_end TIMESTAMP,
  evidence       STRING COMMENT 'JSON: sensors, stats, reading_ids',
  classification STRING COMMENT 'SENSOR_FAULT | REAL_BEHAVIOUR | INDETERMINATE',
  confidence     DOUBLE,
  requires_engineer_review BOOLEAN NOT NULL,
  created_ts     TIMESTAMP NOT NULL
) TBLPROPERTIES (delta.appendOnly = true);

