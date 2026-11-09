-- 04 DSSMP / PFM / EAP (FERC canvas entities; FERC Ch.14 App J/K, Ch.17, Ch.6). SCD2.
USE CATALOG IDENTIFIER(:catalog);

CREATE TABLE IF NOT EXISTS ref.dssmp_version (
  dssmp_version_id STRING NOT NULL,
  dam_id           STRING NOT NULL,
  version_no       INT    NOT NULL,
  effective_from   TIMESTAMP NOT NULL,
  effective_to     TIMESTAMP,
  status           STRING NOT NULL COMMENT 'draft | approved | superseded',
  change_reason    STRING,
  source_ref       STRING COMMENT 'DSSMR or IC recommendation id',
  document_uri     STRING,
  -- v1.7 (migration v1_7_003_dssmp_registration): registration from the app outbox dssmp_registration_outbox by the
  -- dam_ref_publish job (dam_safety.dssmp_registration). An approved row names its CDSE approval chain record.
  document_sha256       STRING COMMENT 'sha256 (lower-case hex) of the approved DSSMP document',
  approved_by           STRING COMMENT 'the CDSE who approved (reviewer email of the approve record)',
  approved_at           TIMESTAMP COMMENT 'signed_ts of the approve record',
  approval_record_hash  STRING COMMENT 'audit.review_record.record_hash of the dssmp_version_approve record',
  loaded_from_outbox_id STRING COMMENT 'Lakebase dssmp_registration_outbox.outbox_id it was loaded from',
  volume_uri            STRING COMMENT 'UC copy /Volumes/<catalog>/ref/dssmp_documents/<dam_id>/<dssmp_version_id>/<file> (sha256 = document_sha256, checked before and after the copy); document_uri keeps the app original',
  CONSTRAINT pk_dssmp PRIMARY KEY (dssmp_version_id)
);

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

CREATE TABLE IF NOT EXISTS ref.pfm (
  pfm_id               STRING NOT NULL,
  dam_id               STRING NOT NULL,
  pfm_code             STRING NOT NULL,
  description          STRING NOT NULL,
  loading_condition    STRING COMMENT 'NORMAL | FLOOD | SEISMIC | ...',
  ch17_disposition     STRING COMMENT 'FERC Ch.17 §17-4.7.9 disposition (e.g. Urgent / Credible / Non-credible)',
  legacy_ch14_category STRING COMMENT 'FERC Ch.14 §14.3.4 Category I-IV',
  pfma_id              STRING,
  identified_date      DATE,
  status               STRING,
  is_general_health    BOOLEAN,
  mechanism            STRING COMMENT 'INTERNAL_EROSION | SLIDING | UPLIFT | OVERTOPPING | SLOPE_INSTABILITY | ... (rule engine key)',
  effective_from       TIMESTAMP NOT NULL,
  effective_to         TIMESTAMP,
  CONSTRAINT pk_pfm PRIMARY KEY (pfm_id)
);

CREATE TABLE IF NOT EXISTS ref.pfm_instrument (
  pfm_id               STRING NOT NULL,
  instrument_id        STRING NOT NULL,
  dam_id               STRING NOT NULL,
  role                 STRING COMMENT 'PRIMARY | SUPPORTING | CONTEXT',
  expected_signature   STRING COMMENT 'rule-engine hint, e.g. RISING_HEAD_WITH_POOL, RISING_TREND, ACCELERATING_DISPLACEMENT',
  adequacy_location    STRING,
  adequacy_equipment   STRING,
  adequacy_frequency   STRING,
  adequacy_reliability STRING,
  valid_from           TIMESTAMP NOT NULL,
  valid_to             TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ref.eap_trigger (
  eap_trigger_id             STRING NOT NULL,
  dam_id                     STRING NOT NULL,
  eap_level                  STRING NOT NULL COMMENT 'HIGH_FLOW | NON_FAILURE | POTENTIAL_FAILURE | IMMINENT_FAILURE',
  trigger_condition          STRING,
  linked_threshold_id        STRING,
  notification_flowchart_ref STRING,
  eap_version                STRING,
  effective_from             TIMESTAMP NOT NULL,
  effective_to               TIMESTAMP,
  CONSTRAINT pk_eap PRIMARY KEY (eap_trigger_id)
);
