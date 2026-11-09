-- DOWN for v1_8_003_lifecycle_safety_flag.sql. Data-level fallback: Delta time travel / RESTORE TABLE on a CLONE.
-- What it reverses: ck_lifecycle_event goes back to its v1_8_001 body (evidence always required; no derived_citation
-- rule). What it does NOT reverse, by design: the three columns, their values and the suspend_other_internal seed row
-- (audit.lifecycle_event is append-only, 12.12; ref rows are data). Code rollback caveat: pause dam_ref_publish first;
-- the v1_8_001 engine does not know safety_related_ops_event and treats suspend_other_internal like any citation.
-- PRE-CHECK before running it: restoring the v1_8_001 CHECK fails (and changes nothing further) if an event has an empty
-- evidence list:
--   SELECT event_id FROM audit.lifecycle_event WHERE size(evidence_uri) = 0;   -- must be empty, else stop
-- IDEMPOTENT, safe to re-run. Re-applying v1_8_003 afterwards restores it.
-- UNVERIFIED: not run on a workspace.
-- QA-057 prod guard, layer 2 (layer 1: tools/run_migration.py, which always binds :catalog and :break_glass).
-- The FIRST statement refuses on dam_prod unless the named parameter break_glass = 'BREAK_GLASS_PROD' (bound only by
-- tools/run_migration.py --break-glass-prod "<reason>", which logs the reason). A missing parameter fails the
-- statement (unbound parameter marker) = refused; a catalog bound to NULL / empty / only whitespace or format
-- characters (e.g. a blank SQL-editor widget) is refused by the first branch, whatever the session catalog; a session
-- whose default catalog is dam_prod is refused for ANY catalog value without break_glass. It is checked on the :catalog parameter (format characters and
-- whitespace removed, lower-cased) and again on current_catalog() right after USE CATALOG (the resolved catalog; a
-- look-alike name that does not resolve fails USE CATALOG). UNVERIFIED on Databricks: named parameter markers in a
-- sql_task file, raise_error inside SELECT CASE, the \p{Cf} / \p{Z} regex classes (Zs spaces such as U+00A0, U+3000,
-- U+2000-U+200A, U+202F, U+205F are removed like format characters; QA-058 residual), current_catalog().
SELECT CASE WHEN coalesce(regexp_replace(:catalog, '[\\p{Cf}\\p{Z}\\s]', ''), '') = ''
            THEN raise_error('QA-057: down script refused: the catalog parameter is missing or empty')
            WHEN (lower(regexp_replace(:catalog, '[\\p{Cf}\\p{Z}\\s]', '')) = 'dam_prod' OR current_catalog() = 'dam_prod')
                 AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard;
USE CATALOG IDENTIFIER(:catalog);
SELECT CASE WHEN current_catalog() = 'dam_prod' AND coalesce(:break_glass, '') <> 'BREAK_GLASS_PROD'
            THEN raise_error('QA-057: down script refused on dam_prod without the break_glass parameter') END AS qa057_guard_resolved;

ALTER TABLE audit.lifecycle_event DROP CONSTRAINT IF EXISTS ck_lifecycle_event;
ALTER TABLE audit.lifecycle_event ADD CONSTRAINT ck_lifecycle_event CHECK (
  from_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer', 'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
  AND to_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer', 'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
  AND size(evidence_uri) > 0 AND length(trim(citation_code)) > 0 AND length(trim(created_by)) > 0
  AND length(trim(trigger_code)) > 0 AND length(trim(authorizer_role)) > 0
  AND prev_event_hash RLIKE '^[0-9a-f]{64}$' AND event_hash RLIKE '^[0-9a-f]{64}$'
  AND (to_state <> 'suspended_monitoring_modified' OR suspension_reason IS NOT NULL)
  AND (to_state = 'suspended_monitoring_modified' OR suspension_reason IS NULL));
