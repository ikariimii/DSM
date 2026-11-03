-- 00 Catalog + the seven schemas (SCHEMA_CONTRACT.md §1). Run by job `dam_uc_setup` with
-- parameter :catalog = dam_dev | dam_test | dam_prod (bundle target variable).
-- The catalog itself is created/bound by a metastore admin (or resources/uc.yml with the
-- direct engine); schemas are ALSO declared in resources/uc.yml so the bundle owns them.
-- This file is idempotent and exists so DDL can be applied/reviewed without the bundle.
USE CATALOG IDENTIFIER(:catalog);

CREATE SCHEMA IF NOT EXISTS ref    COMMENT 'Reference + configuration: hierarchy, calibrations (SCD2), thresholds (SCD2), formulas, DSSMP/PFM/EAP, schedules';
CREATE SCHEMA IF NOT EXISTS raw    COMMENT 'Bronze: append-only as-received logger/manual data + quarantine; landing volume';
CREATE SCHEMA IF NOT EXISTS silver COMMENT 'Validated, typed, deduplicated readings and engineering-unit reductions with calibration lineage';
CREATE SCHEMA IF NOT EXISTS gold   COMMENT 'Aggregates, alerting views (threshold_exceedances), interpretation outputs (HST, anomaly scores)';
CREATE SCHEMA IF NOT EXISTS ops    COMMENT 'Operational state: alarm state machine, exceedances, incidents, tasks, inspections, calibration events';
CREATE SCHEMA IF NOT EXISTS audit  COMMENT 'Insert-only audit: review/sign-off records, alarm/ack event log, recompute log';
CREATE SCHEMA IF NOT EXISTS rpt    COMMENT 'Regulatory reporting: DSSMR, Part 12D inspections, IC recommendations, engineering commentary';
CREATE SCHEMA IF NOT EXISTS ml     COMMENT 'MLflow UC registered models (@Champion alias) for anomaly scoring (KB rec 8)';

-- Landing area for datalogger drops (external volume on a dedicated S3 prefix in prod;
-- see resources/uc.yml). Managed volume fallback for dev.
CREATE VOLUME IF NOT EXISTS raw.landing   COMMENT 'Datalogger + manual-entry file drops: /<dam_id>/<instrument_type>/<file>';
CREATE VOLUME IF NOT EXISTS rpt.artifacts COMMENT 'Frozen certified DSSMR PDFs and report artifacts (hash recorded in audit.review_record)';
CREATE VOLUME IF NOT EXISTS ops.checkpoints COMMENT 'Structured Streaming checkpoints for job-owned streams (dam_reduce_readings)';
