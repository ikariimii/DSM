-- 06 silver. Pipeline-owned (src/pipelines/silver.py): silver.reading_clean,
-- silver.reading_quarantine, silver.incl_survey. JOB-owned (created here): reductions,
-- because history recompute appends replacement rows with lineage.
USE CATALOG IDENTIFIER(:catalog);

CREATE TABLE IF NOT EXISTS silver.reading_reduced (
  reading_id         STRING    NOT NULL COMMENT 'sha2(sensor_id|ts_utc|source_file) from silver.reading_clean',
  dam_id             STRING    NOT NULL,
  sensor_id          STRING    NOT NULL,
  instrument_id      STRING    NOT NULL,
  ts_utc             TIMESTAMP NOT NULL,
  datum_id           STRING  COMMENT 'survey/reference datum the raw value is expressed in; a change => qa_reason DATUM_CHANGE + re-baseline (CONVENTIONS.md)',
  raw_value          DOUBLE  COMMENT 'as received, NEVER re-signed (e.g. up-positive elevation/dU)',
  raw_unit           STRING,
  eng_value          DOUBLE  COMMENT 'pressure kPa (VW) / depth m (standpipe) / settlement mm positive-DOWN (SETTLEMENT_DELTA)',
  eng_unit           STRING,
  corrected_value    DOUBLE  COMMENT 'piezometric / water elevation, m above project datum',
  corrected_unit     STRING,
  head_m             DOUBLE,
  temp_corr          DOUBLE,
  baro_corr          DOUBLE,
  is_dry             BOOLEAN,
  qa_flag            STRING  NOT NULL COMMENT 'OK | SUSPECT | BAD | MISSING',
  qa_reason          STRING  COMMENT 'comma-joined: RANGE_LOW|RANGE_HIGH|NULL_VALUE|STUCK|SPIKE|AFTER_GAP|CHECKSUM|DRY|DATUM_CHANGE|CALIB_ERROR:<msg>',
  calib_id           STRING  COMMENT 'lineage: calibration version used',
  cal_version        INT,
  formula_id         STRING  COMMENT 'lineage: formula used',
  formula_version    STRING,
  recompute_batch_id STRING  NOT NULL COMMENT 'inc-<run> for incremental, rc-<uuid> for recompute',
  is_recompute       BOOLEAN NOT NULL,
  reduced_ts         TIMESTAMP NOT NULL
) CLUSTER BY (dam_id, sensor_id, ts_utc)
  TBLPROPERTIES (delta.appendOnly = true, delta.enableChangeDataFeed = true,
                 delta.enableRowTracking = true);

-- Current reduction per reading + who superseded it. Gold reads ONLY this view.
CREATE OR REPLACE VIEW silver.v_reading_current AS
SELECT * EXCEPT (rn)
FROM (
  SELECT r.*, row_number() OVER (PARTITION BY reading_id ORDER BY reduced_ts DESC) AS rn
  FROM silver.reading_reduced r
) WHERE rn = 1;

-- Full lineage history: every reduction of every reading, with superseded_by.
CREATE OR REPLACE VIEW silver.v_reading_lineage AS
SELECT r.*,
       lead(recompute_batch_id) OVER (PARTITION BY reading_id ORDER BY reduced_ts) AS superseded_by,
       lead(reduced_ts)         OVER (PARTITION BY reading_id ORDER BY reduced_ts) AS superseded_ts
FROM silver.reading_reduced r;

CREATE TABLE IF NOT EXISTS silver.incl_reduced (
  dam_id             STRING NOT NULL,
  instrument_id      STRING NOT NULL,
  survey_id          STRING NOT NULL,
  survey_ts          TIMESTAMP NOT NULL,
  initial_survey_ts  TIMESTAMP NOT NULL,
  depth_m            DOUBLE NOT NULL,
  diff_a             DOUBLE COMMENT 'UN-HALVED A0 - A180 (QA.md gotcha; deviation = L*diff/(2K))',
  diff_b             DOUBLE,
  checksum_a         DOUBLE,
  checksum_b         DOUBLE,
  dev_a              DOUBLE, dev_b DOUBLE,
  inc_a              DOUBLE COMMENT 'mm, vs initial survey',
  inc_b              DOUBLE,
  cum_a              DOUBLE COMMENT 'mm, cumulative from fixed end; +A = downstream',
  cum_b              DOUBLE,
  resultant          DOUBLE,
  fixed_end          STRING COMMENT 'bottom | top',
  qa_flag            STRING, qa_reason STRING,
  calib_id           STRING, formula_id STRING, formula_version STRING,
  recompute_batch_id STRING NOT NULL,
  reduced_ts         TIMESTAMP NOT NULL,
  -- SCHEMA_CONTRACT v1.1 §5.5 (additive, nullable). Populated once the reduce job is switched to
  -- dam_safety.inclinometer.engine (PENDING; today's reducer leaves them NULL). depth_m stays the
  -- interval-top depth from top of casing (procedure / REPORT §6 rule).
  depth_reference       STRING COMMENT 'source label convention: top_of_casing | from_bottom_label (same literals as ref.incl_installation)',
  baseline_survey_id    STRING COMMENT 'baseline the inc/cum change is relative to (ref.incl_installation)',
  correction_record_ids ARRAY<STRING> COMMENT 'ref.incl_correction.record_id applied, in apply order',
  raw_depth_label       STRING COMMENT 'depth label exactly as in the source file (workbook: ft from bottom)',
  -- SCHEMA_CONTRACT v1.4 (additive, nullable): inclinometer lineage. calib_id is NULL for inclinometers; approval is
  -- formula_id/formula_version -> ref.formula.verified plus the ref.incl_installation row named here (§5.5).
  qc_rule_version       STRING COMMENT 'between-set bias QC rule (dam_safety.inclinometer.qc.BIAS_RULE, e.g. QA021-v2)',
  installation_version  TIMESTAMP COMMENT 'ref.incl_installation.effective_from of the SCD2 row used (probe L/K, sign_mult, baseline)',
  probe_constants       STRING COMMENT 'L_m;K;raw_unit;system;sign_mult_A;sign_mult_B as used, e.g. L_m=0.5;K=25000.0;...'
) TBLPROPERTIES (delta.appendOnly = true);
