-- DOWN for v1_7_009: removes the status-transition triggers and their function. No table, column or row is
-- touched; the outboxes keep their other guards (grants, CHECKs, the app's bookkeeping-column triggers).
-- Data-level fallback: a Lakebase point-in-time branch / restore. IDEMPOTENT.
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
DROP TRIGGER IF EXISTS t_status_dam_cdse_outbox ON dam_app.dam_cdse_outbox;
DROP TRIGGER IF EXISTS t_status_dssmp_registration_outbox ON dam_app.dssmp_registration_outbox;
DROP TRIGGER IF EXISTS t_status_rule_reversion_proposal ON dam_app.rule_reversion_proposal;
DROP FUNCTION IF EXISTS dam_app.status_transition_guard();
COMMIT;
