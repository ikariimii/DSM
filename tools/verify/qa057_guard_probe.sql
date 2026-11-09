-- QA-057 runbook probe (docs/DEV_WORKSPACE_RUNBOOK.md step P8). Exactly the FIRST statement of every
-- sql/migrations/*_down.sql (tests/test_qa057_prod_guard.py keeps it identical), then a marker. It contains no
-- USE CATALOG and touches no table, so it is safe to submit with catalog=dam_prod to check the parameter binding,
-- raise_error and the \p{Cf} class on a real warehouse. Not a migration; never referenced by a job.
SELECT CASE WHEN (lower(regexp_replace(:catalog, '[\\p{Cf}\\s]', '')) = 'dam_prod' OR current_catalog() = 'dam_prod')
                 AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard;
SELECT 'reached-the-body' AS probe;
