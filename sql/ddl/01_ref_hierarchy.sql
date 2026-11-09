-- 01 Hierarchy: portfolio > dam (= dam project) > structure > asset > instrument > sensor (channel)
-- FERC canvas entities ref.dam, ref.instrument (App J §2.1 fields).
USE CATALOG IDENTIFIER(:catalog);

CREATE TABLE IF NOT EXISTS ref.portfolio (
  portfolio_id     STRING NOT NULL,
  name             STRING NOT NULL,
  owner_org        STRING,
  created_ts       TIMESTAMP DEFAULT current_timestamp(),
  CONSTRAINT pk_portfolio PRIMARY KEY (portfolio_id)
) TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');

-- "Dam project" level of the hierarchy. One row per FERC-regulated dam/development.
CREATE TABLE IF NOT EXISTS ref.dam (
  dam_id               STRING NOT NULL,
  portfolio_id         STRING NOT NULL,
  ferc_project_no      STRING,
  name                 STRING NOT NULL,
  hazard_potential     STRING COMMENT 'High | Significant | Low',
  height_ft            DOUBLE,
  storage_acft         DOUBLE,
  subpart_d_applicable BOOLEAN,
  regional_office      STRING,
  cdse_id              STRING COMMENT 'Chief Dam Safety Engineer principal',
  lifecycle_state      STRING COMMENT 'v1.8 mirror of the open ref.dam_lifecycle_state row (written by dam_ref_publish)',
  lifecycle_effective_from TIMESTAMP COMMENT 'v1.8 mirror: effective_from of that row',
  timezone             STRING,
  datum                STRING COMMENT 'Project vertical datum for all *_el_m columns',
  normal_pool_el_m     DOUBLE,
  min_pool_el_m        DOUBLE,
  crest_el_m           DOUBLE,
  height_m             DOUBLE,
  dam_type             STRING,
  owner                STRING,
  nid_id               STRING COMMENT 'USACE National Inventory of Dams id',
  state                STRING,
  lat                  DOUBLE,
  lon                  DOUBLE,
  status               STRING,
  CONSTRAINT pk_dam PRIMARY KEY (dam_id),
  CONSTRAINT fk_dam_portfolio FOREIGN KEY (portfolio_id) REFERENCES ref.portfolio
);

CREATE TABLE IF NOT EXISTS ref.structure (
  structure_id   STRING NOT NULL,
  dam_id         STRING NOT NULL,
  name           STRING NOT NULL,
  structure_type STRING COMMENT 'EMBANKMENT | CONCRETE_GRAVITY | ARCH | SPILLWAY | POWERHOUSE | ABUTMENT | FOUNDATION',
  CONSTRAINT pk_structure PRIMARY KEY (structure_id),
  CONSTRAINT fk_structure_dam FOREIGN KEY (dam_id) REFERENCES ref.dam
);

CREATE TABLE IF NOT EXISTS ref.asset (
  asset_id     STRING NOT NULL,
  dam_id       STRING NOT NULL,
  structure_id STRING NOT NULL,
  name         STRING NOT NULL,
  asset_type   STRING COMMENT 'SECTION | BOREHOLE | ARRAY | WEIR | GALLERY',
  station      STRING,
  CONSTRAINT pk_asset PRIMARY KEY (asset_id),
  CONSTRAINT fk_asset_structure FOREIGN KEY (structure_id) REFERENCES ref.structure
);

-- FERC App J §2.1 instrument fields
CREATE TABLE IF NOT EXISTS ref.instrument (
  instrument_id          STRING NOT NULL,
  dam_id                 STRING NOT NULL,
  asset_id               STRING NOT NULL,
  structure_id           STRING NOT NULL COMMENT 'denormalised from ref.asset for app queries',
  subtype                STRING COMMENT 'vibrating_wire | open_standpipe | probe_metric ...',
  station_m              DOUBLE,
  offset_m               DOUBLE,
  lat                    DOUBLE,
  lon                    DOUBLE,
  tip_el_m               DOUBLE,
  top_el_m               DOUBLE,
  legacy_id              STRING,
  replaces_instrument_id STRING,
  instrument_type        STRING NOT NULL COMMENT 'ref.instrument_type code (lower snake_case, e.g. piezometer, strain_gauge); see sql/seed/ref_instrument_type_seed.sql',
  brand                  STRING,
  model                  STRING,
  boring_no              STRING,
  location               STRING,
  northing               DOUBLE,
  easting                DOUBLE,
  ground_el_m            DOUBLE,
  sensing_el_m           DOUBLE,
  install_date           DATE,
  collection_method      STRING COMMENT 'ADAS | MANUAL',
  status                 STRING COMMENT 'ACTIVE | INACTIVE | ABANDONED',
  abandoned_date         DATE,
  abandoned_reason       STRING,
  last_calibration_date  DATE,
  CONSTRAINT pk_instrument PRIMARY KEY (instrument_id),
  CONSTRAINT fk_instrument_asset FOREIGN KEY (asset_id) REFERENCES ref.asset
);

-- Channel level: one logger channel / measured quantity per row.
CREATE TABLE IF NOT EXISTS ref.sensor (
  sensor_id      STRING NOT NULL,
  instrument_id  STRING NOT NULL,
  dam_id         STRING NOT NULL,
  channel        STRING COMMENT 'logger channel or axis (A/B) or quantity',
  reading_type   STRING NOT NULL COMMENT 'CONVENTIONS.md reading_type',
  native_unit    STRING,
  logger_id      STRING,
  active         BOOLEAN,
  CONSTRAINT pk_sensor PRIMARY KEY (sensor_id),
  CONSTRAINT fk_sensor_instrument FOREIGN KEY (instrument_id) REFERENCES ref.instrument
);

-- Project membership used by the row filter fallback (10_security.sql). This table must
-- NOT itself carry a row filter (UC: a filter may not reference a filtered table).
-- v1.7 (Genie 2026-11-09 (B), migration v1_7_003): SCD2 history of each dam's designated CDSE, written only by the
-- dam_ref_publish task apply_cdse_assignments from signed chain records (dam_safety.cdse_assignment):
-- dam_cdse_assign (platform admin, first CDSE, dam not live) and dam_cdse_change_propose + dam_cdse_change_approve
-- (platform admin proposes, the current CDSE approves). ref.v_dam_reviewer_authority (10b) reads it; ref.dam.cdse_id
-- is kept equal to the open row. One open row (effective_to NULL) per dam.
CREATE TABLE IF NOT EXISTS ref.dam_cdse_assignment (
  assignment_id        STRING NOT NULL COMMENT 'approval_record_hash of the applying record; legacy:<dam_id> for a pre-v1.7 ref.dam.cdse_id',
  dam_id               STRING NOT NULL,
  cdse_id              STRING NOT NULL COMMENT 'the designated CDSE principal (email)',
  effective_from       TIMESTAMP COMMENT 'signed_ts of the applying record (NULL only on the legacy row)',
  effective_to         TIMESTAMP COMMENT 'signed_ts of the record that replaced it; NULL = current',
  action_type          STRING NOT NULL COMMENT 'dam_cdse_assign | dam_cdse_change_approve | dam_cdse_legacy',
  reason_code          STRING NOT NULL,
  assigned_by          STRING COMMENT 'signer of the applying record (platform admin for assign, the previous CDSE for a change)',
  proposed_by          STRING COMMENT 'signer of the dam_cdse_change_propose record (change only)',
  proposal_record_hash STRING COMMENT 'record_hash of the dam_cdse_change_propose record (change only)',
  approval_record_hash STRING COMMENT 'audit.review_record.record_hash of the dam_cdse_assign / dam_cdse_change_approve record',
  CONSTRAINT pk_dam_cdse_assignment PRIMARY KEY (assignment_id)
);
ALTER TABLE ref.dam_cdse_assignment DROP CONSTRAINT IF EXISTS ck_dam_cdse_assignment;
ALTER TABLE ref.dam_cdse_assignment ADD CONSTRAINT ck_dam_cdse_assignment CHECK (
  length(trim(cdse_id)) > 0 AND length(trim(reason_code)) > 0
  AND action_type IN ('dam_cdse_assign', 'dam_cdse_change_approve', 'dam_cdse_legacy')
  AND (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from)
  AND (action_type = 'dam_cdse_legacy'
       OR (effective_from IS NOT NULL AND approval_record_hash IS NOT NULL AND assignment_id = approval_record_hash))
  AND (action_type <> 'dam_cdse_change_approve' OR (proposal_record_hash IS NOT NULL AND proposed_by IS NOT NULL))
  AND (action_type <> 'dam_cdse_legacy' OR (approval_record_hash IS NULL AND effective_to IS NOT NULL)));

CREATE TABLE IF NOT EXISTS ref.project_membership (
  principal   STRING NOT NULL COMMENT 'user email or account group name',
  dam_id      STRING NOT NULL COMMENT '* = all dams',
  role        STRING COMMENT 'VIEWER | ENGINEER | CDSE | ADMIN',
  granted_by  STRING,
  granted_ts  TIMESTAMP,
  revoked_ts  TIMESTAMP
);
