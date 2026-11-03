-- 10 Row-level security keyed on dam (= project) membership.
-- KB rec (summary.md, 06 governance): prefer ABAC governed-tag policies at catalog scope +
-- simple group-based filter functions; avoid mapping-table subqueries in filters (perf,
-- and a filter may not reference a table that itself has a filter). Row filters cannot be
-- attached to views; pipeline-owned tables get the filter via the LDP `row_filter` clause.
-- ⚠ UNVERIFIED SYNTAX: CREATE POLICY / governed tags are written from the KB + docs memory;
--   confirm against current Databricks docs before prod (VERIFICATION.md row SEC-1/SEC-2).
USE CATALOG IDENTIFIER(:catalog);

-- Primary: group-based. Groups are account groups named dam-<dam_id>-members, plus
-- dam-portfolio-all (CDSE / admins / service principals of jobs).
CREATE OR REPLACE FUNCTION ref.dam_row_filter(dam_id STRING)
RETURNS BOOLEAN
COMMENT 'TRUE if caller is in dam-portfolio-all or dam-<dam_id>-members'
RETURN is_account_group_member('dam-portfolio-all')
    OR is_account_group_member(concat('dam-', dam_id, '-members'));

-- Fallback (use only if group-per-dam is not acceptable): membership table lookup.
-- ref.project_membership must never get a row filter itself.
CREATE OR REPLACE FUNCTION ref.dam_row_filter_membership(dam_id STRING)
RETURNS BOOLEAN
RETURN is_account_group_member('dam-portfolio-all')
    OR EXISTS (SELECT 1 FROM ref.project_membership m
               WHERE (m.dam_id = dam_id OR m.dam_id = '*') AND m.revoked_ts IS NULL
                 AND (m.principal = current_user() OR is_account_group_member(m.principal)));

-- Row filters are applied by ONE of two mutually exclusive files, chosen by bundle variable security_mode
-- (uc_setup task ddl_10a_row_filters): 10a_row_filters_table.sql (default) or 10a_row_filters_abac.sql.
-- A table cannot carry a table-level filter (ALTER ... SET ROW FILTER or LDP row_filter=) AND an ABAC row-filter
-- policy: access fails with UC_ABAC_MULTIPLE_ROW_FILTERS (KB 02, Databricks SME 2026-11-02).

-- Append-only grants (REQ-23): no MODIFY on audit/ops logs for humans or the app SP.
-- GRANT USE CATALOG ON CATALOG IDENTIFIER(:catalog) TO `dam-app-sp`;
-- GRANT SELECT ON SCHEMA gold TO `dam-app-sp`;  GRANT SELECT ON SCHEMA silver TO `dam-app-sp`;
-- (ingest jobs run_as the pipeline SP, which alone holds MODIFY on raw/silver/ops/audit)

-- Pipeline-owned datasets (raw.reading_raw/reading_quarantine, silver.reading_clean/reading_quarantine/
-- incl_survey, gold.instrument_latest/instrument_daily/data_freshness) get the same filter through the
-- LDP row_filter argument (src/pipelines/*.py DAM_ROW_FILTER), managed ONLY in pipeline code: never ALTER TABLE a
-- pipeline-owned table (KB 03: manage filters through the pipeline definition). In abac mode the pipeline omits it. raw.incl_survey_raw / raw.incl_quarantine
-- carry no dam_id: SELECT on schema raw is granted ONLY to dam-portfolio-all (never to dam-<id>-members).
