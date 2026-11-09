-- Lakebase migration v1_7_007 (QA-056, App Dev): at most one open row per key on the owner-owned tables, enforced by
-- the database (belt and braces under the app's per-dam advisory lock + in-transaction re-checks):
--   dam_cdse_outbox: one pending CDSE event per dam; dssmp_registration_outbox: one pending row per DSSMP version;
--   ref_change_publication: one active (pending / applied) N+1 publication per rule version.
-- The block is byte-identical to lakebase/001 (tests/test_lakebase_qa056_guards.py). Run as the deployer (member of
-- dam_lakebase_owner). ORDER: after v1_7_006 (chain head log). IDEMPOTENT. Then run lakebase/validate_append_only.sql (0 rows).
-- PRE-CHECK: CREATE UNIQUE INDEX fails on rows duplicated by the pre-fix race; find them first with
--   SELECT dam_id, count(*) FROM dam_app.dam_cdse_outbox WHERE status = 'pending' GROUP BY 1 HAVING count(*) > 1;
--   SELECT dssmp_version_id, count(*) FROM dam_app.dssmp_registration_outbox WHERE status = 'pending'
--     GROUP BY 1 HAVING count(*) > 1;
-- and resolve them through the publisher (mark the loser rejected_invalid) before re-running.
-- DOWN: v1_7_007_qa056_unique_guards_down.sql (drops the three indexes only; no table, column or row).
-- UNVERIFIED: not run on a Lakebase instance.
SET search_path TO dam_app;
BEGIN;
-- BEGIN v1_7_007 block (QA-056 belt and braces on owner-owned tables; names and predicates = app/backend/repos/state.py
-- UNIQUE_GUARDS / PUB_GUARD: the app creates the same indexes on the tables it owns and skips these)
CREATE UNIQUE INDEX IF NOT EXISTS dam_cdse_outbox_one_pending ON dam_cdse_outbox (dam_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS dssmp_outbox_one_pending ON dssmp_registration_outbox (dssmp_version_id)
  WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS ref_change_publication_one_version ON ref_change_publication
  (dam_id, (payload::jsonb ->> 'threshold_id'), (payload::jsonb -> 'insert' ->> 'threshold_version'))
  WHERE kind = 'ref.threshold.new_version' AND status IN ('pending', 'applied');
-- DA (single schema source): the app-owned tables that lakebase/001 also creates carry the app's own guards, same
-- names and predicates as app UNIQUE_GUARDS (the app creates them too, IF NOT EXISTS; dssmp_registration is created by
-- the app only, so its dssmp_registration_one_open stays app-side)
CREATE UNIQUE INDEX IF NOT EXISTS dam_cdse_change_one_pending ON dam_cdse_change (dam_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS rule_reversion_one_pending ON rule_reversion_proposal (dam_id) WHERE status = 'pending';
-- END v1_7_007 block
COMMIT;
