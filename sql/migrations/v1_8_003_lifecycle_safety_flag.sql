-- Migration v1.8.003 (lifecycle safety flag; Genie decisions 2026-11-09 15:27 + 15:35, FERC Specialist review
-- 15:27). Adds, guarded: ref.lifecycle_citation.safety_derived_citation, audit.lifecycle_event.safety_related_ops_event
-- (BOOLEAN, set by the CDSE; default FALSE: NULL is treated as false and the loader writes false) and
-- audit.lifecycle_event.derived_citation (the citation's safety_derived_citation, written ONLY when the flag is true).
-- Re-creates ck_lifecycle_event (same body as sql/ddl/14): an empty evidence list is allowed for suspension_reason
-- 'other' only; derived_citation only with safety_related_ops_event = true. Seeds (INSERT ... WHERE NOT EXISTS, same
-- rows as sql/ddl/14): the new citation suspend_other_internal (live -> suspended_monitoring_modified, reason 'other',
-- 'Internal: no specific Part 12 basis; CDSE rationale required', UNVERIFIED-pending until the FERC Specialist reviews
-- the label). View audit.v_lifecycle_permanent_ops_history lives in sql/ddl/14: re-run dam_uc_setup afterwards.
-- ORDER: after v1_8_001 (independent of the v1_8_002 backfill: before or after it), BEFORE dam_uc_setup (ddl_14's view
-- audit.v_lifecycle_permanent_ops_history reads the new columns) and before the dam_ref_publish code that writes them.
-- IDEMPOTENT, safe to re-run.
-- DATA NOTE (a later data change is never overwritten, and migrations make no UPDATEs): on a catalog where v1_8_001
-- already seeded ref.lifecycle_citation, the existing suspend_ops_drawdown_internal row keeps its old text and a NULL
-- safety_derived_citation; until it is corrected, safety_related_ops_event = true on that code is REFUSED
-- (lifecycle_citation_invalid; fail closed). The reviewed one-time correction is docs/DEV_WORKSPACE_RUNBOOK.md step 7c.
-- PRE-CHECK: none (the CHECK only widens; new columns are NULL on existing rows).
-- DOWN: sql/migrations/v1_8_003_lifecycle_safety_flag_down.sql.
-- UNVERIFIED: not run on a workspace.
USE CATALOG IDENTIFIER(:catalog);

BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'lifecycle_citation' AND column_name = 'safety_derived_citation') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.lifecycle_citation ADD COLUMNS (safety_derived_citation STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'lifecycle_event' AND column_name = 'safety_related_ops_event') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.lifecycle_event ADD COLUMNS (safety_related_ops_event BOOLEAN)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'audit'
                 AND table_name = 'lifecycle_event' AND column_name = 'derived_citation') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE audit.lifecycle_event ADD COLUMNS (derived_citation STRING)';
  END IF;
END;

ALTER TABLE audit.lifecycle_event DROP CONSTRAINT IF EXISTS ck_lifecycle_event;
ALTER TABLE audit.lifecycle_event ADD CONSTRAINT ck_lifecycle_event CHECK (
  from_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer', 'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
  AND to_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer', 'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
  AND (size(evidence_uri) > 0 OR coalesce(suspension_reason, '') = 'other') AND length(trim(citation_code)) > 0 AND length(trim(created_by)) > 0
  AND length(trim(trigger_code)) > 0 AND length(trim(authorizer_role)) > 0
  AND prev_event_hash RLIKE '^[0-9a-f]{64}$' AND event_hash RLIKE '^[0-9a-f]{64}$'
  AND (to_state <> 'suspended_monitoring_modified' OR suspension_reason IS NOT NULL)
  AND (to_state = 'suspended_monitoring_modified' OR suspension_reason IS NULL)
  AND (derived_citation IS NULL OR coalesce(safety_related_ops_event, false)));

INSERT INTO ref.lifecycle_suspension_reason (suspension_reason, description, active)
SELECT s.suspension_reason, s.description, s.active FROM VALUES
  ('construction_modification', 'Construction / modification of project works (18 CFR 12.11; 60-day warning 12.11(b)(2); sworn 12.13)', true),
  ('emergency_modification', 'Emergency modification (incident report + sworn 12.13; no 60-day rule)', true),
  ('drawdown', 'Reservoir drawdown (per license or RE direction; CDSE basis required)', true),
  ('monitoring_change', 'Monitoring change (per license or RE direction; CDSE basis required)', true),
  ('other', 'Other (no specific Part 12 basis: citation suspend_other_internal; the CDSE records cdse_basis of at least 20 characters + cdse_basis_by; evidence optional)', true),
  ('re_directed', 'Directed by the FERC Regional Engineer (12.4(b)(2); RE directive evidence required)', true) AS s(suspension_reason, description, active)
WHERE NOT EXISTS (SELECT 1 FROM ref.lifecycle_suspension_reason t WHERE t.suspension_reason = s.suspension_reason);
INSERT INTO ref.lifecycle_citation (citation_code, from_states, to_state, suspension_reasons, citation,
                                    verification_status, withdrawal_notice, active, safety_derived_citation)
SELECT s.citation_code, s.from_states, s.to_state, s.suspension_reasons, s.citation, s.verification_status,
       s.withdrawal_notice, s.active, s.safety_derived_citation FROM VALUES
  ('go_live_dssmp_12_12', split('draft_commissioning', ','), 'live', CAST(NULL AS ARRAY<STRING>), 'INTERNAL: DSSMP (Ch14 App J) + permanent records 12.12(a)(1)(ii)', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('modify_project_works_12_11', split('live', ','), 'suspended_monitoring_modified', split('construction_modification,emergency_modification', ','), '12.11, sworn 12.13; emergency also 12.10(a)(2); 12.24(b)-(c) if EAP changes', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('suspend_re_directed_12_4', split('live', ','), 'suspended_monitoring_modified', split('re_directed', ','), '12.4(b)(2)', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('suspend_ops_drawdown_internal', split('live', ','), 'suspended_monitoring_modified', split('drawdown,monitoring_change', ','), 'INTERNAL: license articles; no unconditional Part 12 cite (12.12(a)(1)(iii)(A) only when the CDSE sets safety_related_ops_event = true)', 'UNVERIFIED-pending', false, true, '12.12(a)(1)(iii)(A)'),
  ('suspend_other_internal', split('live', ','), 'suspended_monitoring_modified', split('other', ','), 'Internal: no specific Part 12 basis; CDSE rationale required', 'UNVERIFIED-pending', false, true, '12.12(a)(1)(iii)(A)'),
  ('resume_dssmp_internal', split('suspended_monitoring_modified', ','), 'live', CAST(NULL AS ARRAY<STRING>), 'INTERNAL', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('transfer_filed_fpa8_9_1', split('live', ','), 'pending_transfer', CAST(NULL AS ARRAY<STRING>), 'FPA 8; 9.1-9.2', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('transfer_withdrawn_385_216', split('pending_transfer', ','), 'live', CAST(NULL AS ARRAY<STRING>), '385.216', 'UNVERIFIED-pending', true, true, CAST(NULL AS STRING)),
  ('transfer_denied_order', split('pending_transfer', ','), 'live', CAST(NULL AS ARRAY<STRING>), 'Commission order', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('transfer_final_9_3_12_12c', split('pending_transfer', ','), 'transferred', CAST(NULL AS ARRAY<STRING>), '9.3; 12.12(c)', 'VERIFIED', false, true, CAST(NULL AS STRING)),
  ('surrender_filed_6_1', split('live,suspended_monitoring_modified', ','), 'pending_surrender', CAST(NULL AS ARRAY<STRING>), '6.1; 12.1', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('surrender_withdrawn_385_216', split('pending_surrender', ','), 'live', CAST(NULL AS ARRAY<STRING>), '385.216', 'UNVERIFIED-pending', true, true, CAST(NULL AS STRING)),
  ('surrender_denied_order', split('pending_surrender', ','), 'live', CAST(NULL AS ARRAY<STRING>), 'Commission order', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('surrender_effective_6_2_6_5', split('pending_surrender', ','), 'decommissioned_archived', CAST(NULL AS ARRAY<STRING>), '6.2; 6.5; 12.1', 'VERIFIED', false, true, CAST(NULL AS STRING)),
  ('void_from_draft_internal', split('draft_commissioning', ','), 'voided_in_error', CAST(NULL AS ARRAY<STRING>), 'INTERNAL', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)),
  ('void_from_live_internal', split('live', ','), 'voided_in_error', CAST(NULL AS ARRAY<STRING>), 'INTERNAL', 'UNVERIFIED-pending', false, true, CAST(NULL AS STRING)) AS s(citation_code, from_states, to_state, suspension_reasons, citation, verification_status, withdrawal_notice, active, safety_derived_citation)
WHERE NOT EXISTS (SELECT 1 FROM ref.lifecycle_citation t WHERE t.citation_code = s.citation_code);
