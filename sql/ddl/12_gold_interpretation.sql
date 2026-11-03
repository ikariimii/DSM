-- 12 Interpretation-layer outputs (docs/INTERPRETATION.md). Written by job dam_interpretation.
USE CATALOG IDENTIFIER(:catalog);

CREATE TABLE IF NOT EXISTS gold.hst_fit (
  model_id        STRING NOT NULL,
  dam_id          STRING NOT NULL,
  sensor_id       STRING NOT NULL,
  variant         STRING NOT NULL COMMENT 'SALAZAR2017 | ANCOLD_FRANCE | HSTT',
  coefficients    MAP<STRING, DOUBLE> NOT NULL,
  t0_days         DOUBLE,
  ref_date        DATE NOT NULL,
  h_ref           STRUCT<h_min: DOUBLE, h_max: DOUBLE> COMMENT 'normalisation of reservoir level',
  n_obs           BIGINT, train_start TIMESTAMP, train_end TIMESTAMP,
  r2              DOUBLE, rmse DOUBLE, resid_sigma DOUBLE, resid_mad_sigma DOUBLE,
  validation      STRING COMMENT 'JSON: holdout window, rmse, coverage of ±2σ band',
  fitted_ts       TIMESTAMP NOT NULL,
  is_champion     BOOLEAN
) TBLPROPERTIES (delta.appendOnly = true);

CREATE TABLE IF NOT EXISTS gold.anomaly_scores (
  dam_id          STRING NOT NULL,
  sensor_id       STRING NOT NULL,
  reading_id      STRING NOT NULL,
  ts_utc          TIMESTAMP NOT NULL,
  observed        DOUBLE, predicted DOUBLE, residual DOUBLE,
  z_score         DOUBLE,
  band            STRING COMMENT 'WITHIN | OUTSIDE_2S | OUTSIDE_3S',
  model_id        STRING NOT NULL,
  classification  STRING COMMENT 'SENSOR_FAULT | REAL_BEHAVIOUR | INDETERMINATE | NORMAL',
  class_reasons   ARRAY<STRING>,
  confidence      DOUBLE,
  scored_ts       TIMESTAMP NOT NULL
) TBLPROPERTIES (delta.appendOnly = true);
