-- 09 rpt: regulatory reports (FERC canvas rpt.*) + interpretation commentary.
USE CATALOG IDENTIFIER(:catalog);

-- FERC canvas rpt.dssmr (App K)
CREATE TABLE IF NOT EXISTS rpt.dssmr (
  dssmr_id           STRING NOT NULL,
  dam_id             STRING NOT NULL,
  period_start       DATE NOT NULL,
  period_end         DATE NOT NULL,
  dssmp_version_id   STRING NOT NULL,
  guideline_revision STRING,
  findings           STRING,
  cdse_statement     STRING,
  jco_plan_uri       STRING,
  preparer_id        STRING,
  reviewer_id        STRING,
  pe_stamped         BOOLEAN,
  status             STRING NOT NULL COMMENT 'draft | certified | submitted',
  submitted_ts       TIMESTAMP,
  artifact_uri       STRING COMMENT '/Volumes/<catalog>/rpt/artifacts/... frozen PDF',
  CONSTRAINT pk_dssmr PRIMARY KEY (dssmr_id)
);

-- DSSMR draft write path (SCHEMA_CONTRACT v1.1 §7.2): app report_draft (Lakebase) -> job
-- dam_lakebase_extract -> append-only snapshots here (one row per content change) -> rpt.dssmr row
-- with status 'draft' (dssmr_id = report_id). The app never writes UC. Certification/submission is
-- the PE sign-off path (audit.review_record), not the app.
CREATE TABLE IF NOT EXISTS rpt.dssmr_draft (
  report_id      STRING NOT NULL,
  dam_id         STRING NOT NULL,
  period_start   STRING NOT NULL,
  period_end     STRING NOT NULL,
  app_status     STRING NOT NULL,
  content_md     STRING NOT NULL,
  content_sha256 STRING NOT NULL,
  created_by     STRING NOT NULL,
  created_at     STRING NOT NULL,
  extract_ts     TIMESTAMP NOT NULL
) TBLPROPERTIES (delta.appendOnly = true);

CREATE OR REPLACE VIEW rpt.v_dssmr_draft_current AS
SELECT * EXCEPT (rn) FROM (
  SELECT *, row_number() OVER (PARTITION BY report_id ORDER BY extract_ts DESC) AS rn FROM rpt.dssmr_draft
) WHERE rn = 1;

-- FERC canvas rpt.part12d_inspection / rpt.ic_recommendation (§12.40-12.42)
CREATE TABLE IF NOT EXISTS rpt.part12d_inspection (
  inspection_id      STRING NOT NULL,
  dam_id             STRING NOT NULL,
  inspection_type    STRING COMMENT 'PI | CA',
  due_date           DATE,
  prelim_report_due  DATE,
  field_date         DATE,
  report_filed_date  DATE,
  ic_name            STRING,
  CONSTRAINT pk_p12d PRIMARY KEY (inspection_id)
);

CREATE TABLE IF NOT EXISTS rpt.ic_recommendation (
  recommendation_id     STRING NOT NULL,
  dam_id                STRING NOT NULL,
  inspection_id         STRING NOT NULL,
  recommendation        STRING NOT NULL,
  plan_due              DATE COMMENT 'report filed + 60 d',
  status                STRING,
  annual_status_reports ARRAY<STRUCT<year: INT, status: STRING, filed: DATE>>,
  CONSTRAINT pk_icrec PRIMARY KEY (recommendation_id)
);

-- Interpretation layer: LLM/deterministic commentary. ALWAYS requires engineer review.
CREATE TABLE IF NOT EXISTS rpt.engineering_commentary (
  commentary_id            STRING NOT NULL,
  dam_id                   STRING NOT NULL,
  period_start             TIMESTAMP, period_end TIMESTAMP,
  scope                    STRING COMMENT 'dam | structure | instrument',
  scope_id                 STRING,
  text                     STRING NOT NULL,
  citations                STRING NOT NULL COMMENT 'JSON list: reading_ids, finding_ids, pfm_ids, dssmp_version_id',
  confidence               DOUBLE NOT NULL,
  generator                STRING NOT NULL COMMENT 'LLM endpoint name+version | DETERMINISTIC_FALLBACK',
  prompt_hash              STRING,
  requires_engineer_review BOOLEAN NOT NULL COMMENT 'always true; CHECK enforced',
  created_ts               TIMESTAMP NOT NULL
) TBLPROPERTIES (delta.appendOnly = true);
ALTER TABLE rpt.engineering_commentary DROP CONSTRAINT IF EXISTS chk_review;
ALTER TABLE rpt.engineering_commentary ADD CONSTRAINT chk_review CHECK (requires_engineer_review = true);
