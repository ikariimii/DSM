-- 02 Versioned calibration + threshold (SCD Type 2, half-open [effective_from, effective_to))
-- Rows are never deleted. A change = INSERT new version + close previous (effective_to,
-- is_current=false). A transcription correction = new version over the same interval and
-- the old one marked is_retired (kept for audit). See src/dam_safety/calibration.py.
USE CATALOG IDENTIFIER(:catalog);

CREATE TABLE IF NOT EXISTS ref.calibration (
  calib_id        STRING NOT NULL COMMENT 'unique per version',
  sensor_id       STRING NOT NULL,
  instrument_id   STRING NOT NULL,
  dam_id          STRING NOT NULL,
  cal_version     INT    NOT NULL,
  formula_id      STRING NOT NULL COMMENT 'FK ref.formula',
  formula_version STRING NOT NULL,
  params          MAP<STRING, STRING> NOT NULL COMMENT 'G,A,B,C,K,R0,T0,S0,method,pressure_unit,baro_correct,sensor_el_m,top_el_m,tip_el_m,interval_m,k,fixed_end,raw_axis ... G is the gauge factor ONLY (>= 0 for linear; CONVENTIONS.md)',
  sign_mult       INT    NOT NULL DEFAULT 1 COMMENT '+1|-1 applied to the gauge/change term (CONVENTIONS.md); never baked into G',
  `offset`        DOUBLE NOT NULL DEFAULT 0 COMMENT 'eng-unit offset added AFTER sign_mult (CONVENTIONS.md). Backticked: OFFSET is a SQL keyword',
  min_valid       DOUBLE COMMENT 'QC RANGE lower bound in corrected units',
  max_valid       DOUBLE,
  effective_from  TIMESTAMP NOT NULL,
  effective_to    TIMESTAMP,
  is_current      BOOLEAN NOT NULL,
  is_retired      BOOLEAN NOT NULL COMMENT 'true = superseded correction, never resolves',
  certificate_uri STRING  COMMENT 'manufacturer calibration sheet in UC volume',
  source_ref      STRING  COMMENT 'manual/section the params come from',
  notes           STRING,
  approved_by     STRING,
  approved_at     TIMESTAMP,
  created_by      STRING,
  created_ts      TIMESTAMP,
  payload_hash    STRING  COMMENT 'sha256(params, formula, sign_mult, offset) used by recompute planner',
  CONSTRAINT pk_calibration PRIMARY KEY (calib_id)
) TBLPROPERTIES (delta.enableChangeDataFeed = true,
                 'delta.feature.allowColumnDefaults' = 'supported');
-- CONVENTIONS.md 'Calibration table additionally has: sign_mult INT (+1|-1), offset'.
-- Column names follow our SCD2 contract (effective_from/effective_to = CONVENTIONS valid_from/valid_to).
ALTER TABLE ref.calibration DROP CONSTRAINT IF EXISTS ck_calibration_sign_mult;
ALTER TABLE ref.calibration ADD CONSTRAINT ck_calibration_sign_mult CHECK (sign_mult IN (1, -1));

-- FERC canvas ref.threshold (App J §2.1.1; §14.4.1(3)) + alarm-machine settings.
CREATE TABLE IF NOT EXISTS ref.threshold (
  threshold_id     STRING NOT NULL,
  threshold_version INT   NOT NULL,
  dam_id           STRING NOT NULL,
  instrument_id    STRING NOT NULL,
  sensor_id        STRING,
  pfm_id           STRING COMMENT 'nullable: general-health thresholds have no PFM',
  dssmp_version_id STRING NOT NULL,
  tier             STRING NOT NULL COMMENT 'DESIGN_BASIS | THRESHOLD | AL1..ALn',
  level            INT    NOT NULL COMMENT 'state-machine level: 1 = ALERT, 2 = ACTION (see SCHEMA_CONTRACT §4)',
  level_name       STRING COMMENT 'ALERT | ACTION | ...',
  limit_type       STRING NOT NULL COMMENT 'magnitude | rate | cyclic',
  direction        STRING NOT NULL COMMENT 'upper | lower | abs  (canvas upper/lower; abs added)',
  value            DOUBLE NOT NULL,
  unit             STRING NOT NULL,
  rate_window      STRING COMMENT 'rate limits are per day; window text kept for display',
  deadband         DOUBLE NOT NULL COMMENT 'hysteresis, same unit as value',
  persistence_on   INT    NOT NULL COMMENT 'consecutive readings to raise',
  persistence_off  INT    NOT NULL COMMENT 'consecutive readings to clear',
  condition_expr   STRING,
  basis_text       STRING,
  required_actions STRING,
  eap_level_id     STRING COMMENT 'FK ref.eap_trigger',
  set_by           STRING,
  effective_from   TIMESTAMP NOT NULL,
  effective_to     TIMESTAMP,
  is_current       BOOLEAN NOT NULL,
  CONSTRAINT pk_threshold PRIMARY KEY (threshold_id, threshold_version)
) TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- QC rule parameters per sensor (site settings; defaults UNVERIFIED, see VERIFICATION.md)
CREATE TABLE IF NOT EXISTS ref.qc_rule (
  sensor_id           STRING NOT NULL,
  spike_window        INT,
  spike_n_sigma       DOUBLE,
  spike_min_abs       DOUBLE,
  stuck_count         INT,
  stuck_eps           DOUBLE,
  expected_interval_s DOUBLE,
  gap_factor          DOUBLE,
  checksum_tol        DOUBLE,
  effective_from      TIMESTAMP NOT NULL,
  effective_to        TIMESTAMP
);

-- FERC canvas ref.reading_schedule (App J §1, §2.1.2): frequency compliance (REQ-18..21)
CREATE TABLE IF NOT EXISTS ref.reading_schedule (
  schedule_id      STRING NOT NULL,
  dam_id           STRING NOT NULL,
  instrument_id    STRING NOT NULL,
  dssmp_version_id STRING NOT NULL,
  frequency        STRING NOT NULL COMMENT 'e.g. PT1H, P1D, P7D, P1M',
  expected_interval_s DOUBLE NOT NULL,
  season           STRING,
  condition_expr   STRING COMMENT 'e.g. reservoir_el > X -> increased frequency',
  effective_from   TIMESTAMP NOT NULL,
  effective_to     TIMESTAMP
);

-- Inclinometer installation (REPORT §6.1 row 1 '1_SETUP -> installation'): explicit L and K, SI, depth from top.
CREATE TABLE IF NOT EXISTS ref.incl_installation (
  instrument_id      STRING NOT NULL,
  dam_id             STRING NOT NULL,
  system             STRING NOT NULL COMMENT 'probe | saa',
  probe_L_m          DOUBLE NOT NULL COMMENT 'reading interval, metres',
  probe_K            DOUBLE NOT NULL COMMENT 'reading = K*sin(theta) [procedure §3]',
  raw_unit           STRING NOT NULL,
  casing_depth_m     DOUBLE NOT NULL COMMENT 'bottom of deepest interval below top of casing',
  depth_reference    STRING NOT NULL COMMENT 'source label convention: top_of_casing (GTILT, DigiPro native) | from_bottom_label (workbook import profile only; top = casing - d - L, REPORT D2). Stored depth is always interval top from top of casing (procedure §4.0)',
  a0_azimuth_deg     DOUBLE,
  sign_mult_A        INT NOT NULL,
  sign_mult_B        INT NOT NULL COMMENT 'SAA = -1, applied once at ingest (procedure §4.4)',
  baseline_survey_id STRING COMMENT 'selectable baseline (REPORT D9); history in ref.incl_correction type=baseline',
  spiral_survey_id   STRING COMMENT 'spiral survey on file; spiral correction DEFERRED -> every survey gets info flag SPIRAL_NOT_APPLIED',
  stable_zone_top_m  DOUBLE COMMENT 'required for between-set bias QC (QA-020): top of stable zone below top of casing, from boring log / as-built, never auto-detected. NULL -> bottom 5 intervals, stable_zone_assumed',
  effective_from     TIMESTAMP NOT NULL,
  effective_to       TIMESTAMP
);

-- Inclinometer correction records: REPORT §6.3 audit fields, one row per decision, append-only. A change is a
-- NEW row; the old row gets superseded_by (written only by the corrections job). Raw readings are never edited.
CREATE TABLE IF NOT EXISTS ref.incl_correction (
  record_id           STRING NOT NULL,
  dam_id              STRING NOT NULL,
  instrument_id       STRING NOT NULL,
  survey_id           STRING NOT NULL,
  axis                STRING NOT NULL COMMENT 'A | B',
  correction_type     STRING NOT NULL COMMENT 'bias | rotation | depth | sensitivity | spiral | baseline',
  parameter_name      STRING NOT NULL,
  auto_value          DOUBLE,
  detection_method    STRING,
  detection_statistic STRING,
  override_value      STRING,
  applied             BOOLEAN NOT NULL,
  sign_convention     STRING COMMENT 'required for rotation: MIKKELSEN2003_EQ9 | GTILT_PLUS_SIN | A_corr=A-f*B',
  depth_range         STRING COMMENT 'top_m..bottom_m, NULL = whole survey',
  reason              STRING,
  source_citation     STRING,
  user                STRING NOT NULL,
  timestamp_utc       TIMESTAMP NOT NULL,
  before_value        DOUBLE COMMENT 'top cumulative change without this record, mm',
  after_value         DOUBLE COMMENT 'top cumulative change with it, mm',
  software_version    STRING,
  input_file_sha256   STRING,
  superseded_by       STRING,
  reviewed_by         STRING COMMENT 'independent reviewer, must differ from user; rotation applies only when reviewed, TRB-EC129 p44',
  reviewed_utc        TIMESTAMP
);
