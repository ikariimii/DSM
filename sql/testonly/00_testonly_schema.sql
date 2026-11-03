-- TEST-ONLY restricted-dataset schema. dam_dev / dam_test ONLY - never dam_prod.
-- Owner decision 2026-11-02 (relayed by the Geotech Instrumentation Expert): datasets with
-- usage_restriction = 'internal-test-only' (ref.dataset_catalog; source of truth
-- /workspace/dam-refs/datasets/_shared/manifest.csv) are internal testing only: never
-- redistributed or bundled into the app, shipped tables, repo, sample data or published outputs.
-- This file is executed ONLY by job dam_testonly_setup, which is declared under
-- targets.dev/test.resources in databricks.yml (absent from the prod target).
-- Data is uploaded manually by an engineer into the volume below from the internal
-- dam-refs mirror; nothing in this repo contains restricted data (tests/test_license_leak.py).
USE CATALOG IDENTIFIER(:catalog);

-- hard guard: abort if someone points this at prod
SELECT CASE WHEN :catalog NOT IN ('dam_dev', 'dam_test')
            THEN raise_error(concat('testonly_restricted is forbidden in catalog ', :catalog))
            ELSE 'ok' END AS guard;

CREATE SCHEMA IF NOT EXISTS testonly_restricted
  COMMENT 'INTERNAL TEST ONLY - restricted-licence datasets (Enguri, ICOLD BW2022 A/C, USBR RISE, ...). Never in prod, never shipped.';
ALTER SCHEMA testonly_restricted SET TAGS ('usage_restriction' = 'internal_test_only');

CREATE VOLUME IF NOT EXISTS testonly_restricted.files
  COMMENT 'Manual upload of internal-test-only files; mirrors dam-refs/datasets layout';
ALTER VOLUME testonly_restricted.files SET TAGS ('usage_restriction' = 'internal_test_only');

CREATE TABLE IF NOT EXISTS testonly_restricted.series_long (
  dataset_id   STRING    NOT NULL COMMENT 'FK ref.dataset_catalog (must be internal-test-only)',
  series_id    STRING    NOT NULL,
  ts_utc       TIMESTAMP NOT NULL,
  value        DOUBLE,
  unit         STRING,
  variable     STRING,
  source_file  STRING,
  loaded_ts    TIMESTAMP
) COMMENT 'Restricted series in long format for interpretation-layer validation only';
ALTER TABLE testonly_restricted.series_long SET TAGS ('usage_restriction' = 'internal_test_only');

-- Results computed FROM restricted data are also restricted (derived outputs = published outputs risk).
CREATE TABLE IF NOT EXISTS testonly_restricted.validation_result (
  run_id STRING NOT NULL, dataset_id STRING NOT NULL, model STRING, metric STRING, value DOUBLE,
  run_ts TIMESTAMP, notes STRING
) COMMENT 'Validation metrics on restricted data; internal only, never copied to gold/rpt';
ALTER TABLE testonly_restricted.validation_result SET TAGS ('usage_restriction' = 'internal_test_only');

-- Tag the catalog rows' governance on the catalog table itself.
ALTER TABLE ref.dataset_catalog ALTER COLUMN usage_restriction SET TAGS ('governance' = 'licence');
