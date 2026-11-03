-- 10a (security_mode = table, default): table-level row filters on DDL-owned tables only.
-- Pipeline-owned tables get theirs from LDP row_filter= in src/pipelines/*.py; never ALTER them here.
USE CATALOG IDENTIFIER(:catalog);

-- Idempotent: SET replaces an existing filter.
ALTER TABLE ref.dam                    SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.structure              SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.asset                  SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.instrument             SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.sensor                 SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.calibration            SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.threshold              SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.pfm                    SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE silver.reading_reduced     SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE silver.incl_reduced        SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.incl_installation      SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ref.incl_correction        SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ops.alarm_state            SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ops.alarm_transition       SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ops.exceedance             SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ops.safety_incident        SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE ops.rule_finding           SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE audit.review_record        SET ROW FILTER ref.dam_row_filter ON (dam_id);
-- audit.app_extract is created in 08; rows with NULL dam_id (global audit_log entries) are visible only to dam-portfolio-all (concat with NULL is NULL).
ALTER TABLE audit.app_extract          SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE rpt.dssmr                  SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE rpt.dssmr_draft            SET ROW FILTER ref.dam_row_filter ON (dam_id);
ALTER TABLE rpt.engineering_commentary SET ROW FILTER ref.dam_row_filter ON (dam_id);

