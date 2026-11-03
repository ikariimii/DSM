-- 10a (security_mode = abac): ONE catalog-level ABAC row-filter policy replaces every table-level filter.
-- Table-level filters and an ABAC policy cannot be combined on a table (UC_ABAC_MULTIPLE_ROW_FILTERS, KB 02), so this
-- file first DROPs the DDL-owned table filters, and the pipeline omits row_filter= when dam.security_mode = abac.
-- Pipeline refreshes need the executing principal in EXCEPT (KB 02 policy evaluation): the pipeline/alert run_as
-- service principal (var.runtime_sp_app_id) must be a member of dam-portfolio-all, which is EXCEPTed below.
-- UNVERIFIED (test in dev before prod, VERIFICATION.md ABAC-1/ABAC-2):
--   * IDENTIFIER(:catalog) inside CREATE POLICY ... ON CATALOG;
--   * principal `account users` in TO;
--   * that EXCEPT of a group covers its service-principal members during pipeline refresh;
--   * governed tag `dam_scope` must already exist (account admin) with allowed value 'dam_id'.
USE CATALOG IDENTIFIER(:catalog);

ALTER TABLE ref.dam                    DROP ROW FILTER;
ALTER TABLE ref.structure              DROP ROW FILTER;
ALTER TABLE ref.asset                  DROP ROW FILTER;
ALTER TABLE ref.instrument             DROP ROW FILTER;
ALTER TABLE ref.sensor                 DROP ROW FILTER;
ALTER TABLE ref.calibration            DROP ROW FILTER;
ALTER TABLE ref.threshold              DROP ROW FILTER;
ALTER TABLE ref.pfm                    DROP ROW FILTER;
ALTER TABLE silver.reading_reduced     DROP ROW FILTER;
ALTER TABLE silver.incl_reduced        DROP ROW FILTER;
ALTER TABLE ref.incl_installation      DROP ROW FILTER;
ALTER TABLE ref.incl_correction        DROP ROW FILTER;
ALTER TABLE ops.alarm_state            DROP ROW FILTER;
ALTER TABLE ops.alarm_transition       DROP ROW FILTER;
ALTER TABLE ops.exceedance             DROP ROW FILTER;
ALTER TABLE ops.safety_incident        DROP ROW FILTER;
ALTER TABLE ops.rule_finding           DROP ROW FILTER;
ALTER TABLE audit.review_record        DROP ROW FILTER;
ALTER TABLE audit.app_extract          DROP ROW FILTER;
ALTER TABLE rpt.dssmr                  DROP ROW FILTER;
ALTER TABLE rpt.dssmr_draft            DROP ROW FILTER;
ALTER TABLE rpt.engineering_commentary DROP ROW FILTER;

-- Tag every dam_id column (repeat for each in-scope table incl. pipeline-owned ones; tags are not filters):
ALTER TABLE silver.reading_reduced ALTER COLUMN dam_id SET TAGS ('dam_scope' = 'dam_id');

CREATE POLICY IF NOT EXISTS dam_membership ON CATALOG IDENTIFIER(:catalog)
  COMMENT 'Rows visible only to members of the dam project'
  ROW FILTER ref.dam_row_filter
  TO `account users` EXCEPT `dam-portfolio-all`
  FOR TABLES
  MATCH COLUMNS has_tag_value('dam_scope', 'dam_id') AS d
  USING COLUMNS (d);
