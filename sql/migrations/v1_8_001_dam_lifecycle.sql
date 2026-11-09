-- Migration v1.8.001 (dam lifecycle; contract agreed 2026-11-09 14:20, citation list 14:22). New tables (same DDL
-- as sql/ddl/14): ref.lifecycle_suspension_reason and ref.lifecycle_citation (UC-owned enums stored as data, seeded
-- here only where a code is missing: a later data change, e.g. a citation moving to VERIFIED, is never overwritten),
-- ref.dam_lifecycle_state (SCD-2), audit.lifecycle_event (append-only, chained per dam); CHECKs ck_lifecycle_citation,
-- ck_dam_lifecycle_state, ck_lifecycle_event; ref.dam gains lifecycle_state, lifecycle_effective_from (mirror);
-- audit.review_record accepts subject_type 'dam_lifecycle' with action_type lifecycle_transition_propose | _approve |
-- _reject (decision propose | approve | reject, subject_id = dam_id, non-blank rationale): ck_review_record_subject_decision
-- and ck_review_record_chain re-created with the same bodies as sql/ddl/08. Views (ref.v_dam_lifecycle_current,
-- ref.v_dam_go_live) live in sql/ddl/14: re-run dam_uc_setup afterwards.
-- ORDER: after v1_5_001 .. v1_7_003; BEFORE v1_8_002_lifecycle_backfill.sql and before the first dam_ref_publish run
-- with apply_lifecycle_transitions.
-- IDEMPOTENT, safe to re-run: CREATE TABLE IF NOT EXISTS, guarded ADD COLUMNS, DROP CONSTRAINT IF EXISTS then ADD,
-- seeds INSERT ... WHERE NOT EXISTS.
-- PRE-CHECK: none of the new CHECKs can fail on a first run (new tables; review_record CHECKs only widen).
-- DOWN: sql/migrations/v1_8_001_dam_lifecycle_down.sql (drops the three new CHECKs and restores the v1_7_003
-- review_record CHECKs; tables, columns, seeds and rows stay).
-- UNVERIFIED: not run on a workspace.
USE CATALOG IDENTIFIER(:catalog);

CREATE TABLE IF NOT EXISTS ref.lifecycle_suspension_reason (
  suspension_reason STRING NOT NULL COMMENT 'v1.8 UC-owned enum (data, not code)',
  description       STRING,
  active            BOOLEAN NOT NULL,
  CONSTRAINT pk_lifecycle_suspension_reason PRIMARY KEY (suspension_reason)
);
CREATE TABLE IF NOT EXISTS ref.lifecycle_citation (
  citation_code       STRING NOT NULL COMMENT 'v1.8 UC-owned enum (data, not code); dam_safety.lifecycle reads it',
  from_states         ARRAY<STRING> NOT NULL COMMENT 'the edges this code is valid for: from_states -> to_state',
  to_state            STRING NOT NULL,
  suspension_reasons  ARRAY<STRING> COMMENT 'into suspended_monitoring_modified: the reasons this code covers',
  citation            STRING NOT NULL COMMENT 'legal / internal basis (18 CFR unless stated)',
  verification_status STRING NOT NULL COMMENT 'VERIFIED | UNVERIFIED-pending (per row)',
  withdrawal_notice   BOOLEAN NOT NULL COMMENT 'true: requires withdrawal_filed_date; 15-day warning (385.216)',
  active              BOOLEAN NOT NULL,
  CONSTRAINT pk_lifecycle_citation PRIMARY KEY (citation_code)
);
CREATE TABLE IF NOT EXISTS ref.dam_lifecycle_state (
  state_id         STRING NOT NULL COMMENT '= event_id of the audit.lifecycle_event that opened the row',
  dam_id           STRING NOT NULL,
  lifecycle_state  STRING NOT NULL,
  effective_from   TIMESTAMP NOT NULL,
  effective_to     TIMESTAMP COMMENT 'effective_ts of the next event; NULL = current',
  event_id         STRING NOT NULL,
  CONSTRAINT pk_dam_lifecycle_state PRIMARY KEY (state_id)
);
CREATE TABLE IF NOT EXISTS audit.lifecycle_event (
  event_id                 STRING NOT NULL COMMENT 'lc:<approval_record_hash> (loader) | backfill:onboarding_golive:<pk>; transfer counterpart: <id>:counterpart',
  dam_id                   STRING NOT NULL,
  from_state               STRING NOT NULL,
  to_state                 STRING NOT NULL,
  trigger_code             STRING NOT NULL,
  authorizer_role          STRING NOT NULL,
  authorizer_user_id       STRING,
  evidence_uri             ARRAY<STRING> NOT NULL,
  ferc_docket_or_order     STRING,
  effective_ts             TIMESTAMP NOT NULL COMMENT 'second precision, UTC',
  monitoring_rule          STRING,
  notification_checklist   STRING COMMENT 'JSON',
  retention_action         STRING,
  created_by               STRING NOT NULL,
  created_ts               TIMESTAMP NOT NULL,
  rationale                STRING,
  citation_code            STRING NOT NULL COMMENT 'ref.lifecycle_citation',
  proposed_by              STRING,
  approved_by              STRING,
  second_approved_by       STRING,
  approval_record_hash     STRING COMMENT 'audit.review_record.record_hash of the lifecycle_transition_approve record',
  dssmp_version_id         STRING,
  reason_code              STRING COMMENT 'the transition reason (outbox transition_reason_code)',
  suspension_reason        STRING COMMENT 'ref.lifecycle_suspension_reason; required into suspended_monitoring_modified',
  incident_report_uri      STRING,
  sworn_verification_12_13 BOOLEAN,
  sworn_verification_uri   STRING,
  cdse_basis               STRING,
  cdse_basis_by            STRING COMMENT 'the CDSE user id',
  re_directive_uri         STRING,
  withdrawal_filed_date    DATE,
  counterpart_dam_id       STRING COMMENT 'transfer: the other record (new licensee <-> old)',
  prev_event_hash          STRING NOT NULL COMMENT 'event_hash of the previous event of the dam; first = 64 x 0',
  event_hash               STRING NOT NULL COMMENT 'sha256 of dam_safety.lifecycle.hash_input (length-prefixed fields)',
  CONSTRAINT pk_lifecycle_event PRIMARY KEY (event_id)
) TBLPROPERTIES (delta.appendOnly = true);

ALTER TABLE ref.lifecycle_citation DROP CONSTRAINT IF EXISTS ck_lifecycle_citation;
ALTER TABLE ref.lifecycle_citation ADD CONSTRAINT ck_lifecycle_citation CHECK (
  length(trim(citation_code)) > 0 AND length(trim(citation)) > 0 AND size(from_states) > 0
  AND to_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer', 'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
  AND verification_status IN ('VERIFIED', 'UNVERIFIED-pending'));
ALTER TABLE ref.dam_lifecycle_state DROP CONSTRAINT IF EXISTS ck_dam_lifecycle_state;
ALTER TABLE ref.dam_lifecycle_state ADD CONSTRAINT ck_dam_lifecycle_state CHECK (
  lifecycle_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer', 'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
  AND state_id = event_id AND (effective_to IS NULL OR effective_to >= effective_from));
ALTER TABLE audit.lifecycle_event DROP CONSTRAINT IF EXISTS ck_lifecycle_event;
ALTER TABLE audit.lifecycle_event ADD CONSTRAINT ck_lifecycle_event CHECK (
  from_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer', 'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
  AND to_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer', 'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
  AND size(evidence_uri) > 0 AND length(trim(citation_code)) > 0 AND length(trim(created_by)) > 0
  AND length(trim(trigger_code)) > 0 AND length(trim(authorizer_role)) > 0
  AND prev_event_hash RLIKE '^[0-9a-f]{64}$' AND event_hash RLIKE '^[0-9a-f]{64}$'
  AND (to_state <> 'suspended_monitoring_modified' OR suspension_reason IS NOT NULL)
  AND (to_state = 'suspended_monitoring_modified' OR suspension_reason IS NULL));

BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'dam' AND column_name = 'lifecycle_state') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.dam ADD COLUMNS (lifecycle_state STRING)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ref'
                 AND table_name = 'dam' AND column_name = 'lifecycle_effective_from') THEN
    EXECUTE IMMEDIATE 'ALTER TABLE ref.dam ADD COLUMNS (lifecycle_effective_from TIMESTAMP)';
  END IF;
END;

INSERT INTO ref.lifecycle_suspension_reason (suspension_reason, description, active)
SELECT s.suspension_reason, s.description, s.active FROM VALUES
  ('construction_modification', 'Construction / modification of project works (18 CFR 12.11; 60-day warning 12.11(b)(2); sworn 12.13)', true),
  ('emergency_modification', 'Emergency modification (incident report + sworn 12.13; no 60-day rule)', true),
  ('drawdown', 'Reservoir drawdown (per license or RE direction; CDSE basis required)', true),
  ('monitoring_change', 'Monitoring change (per license or RE direction; CDSE basis required)', true),
  ('other', 'Other (rationale required; NO citation code seeded yet)', true),
  ('re_directed', 'Directed by the FERC Regional Engineer (12.4(b)(2); RE directive evidence required)', true) AS s(suspension_reason, description, active)
WHERE NOT EXISTS (SELECT 1 FROM ref.lifecycle_suspension_reason t WHERE t.suspension_reason = s.suspension_reason);
INSERT INTO ref.lifecycle_citation (citation_code, from_states, to_state, suspension_reasons, citation,
                                    verification_status, withdrawal_notice, active)
SELECT s.citation_code, s.from_states, s.to_state, s.suspension_reasons, s.citation, s.verification_status,
       s.withdrawal_notice, s.active FROM VALUES
  ('go_live_dssmp_12_12', split('draft_commissioning', ','), 'live', CAST(NULL AS ARRAY<STRING>), 'INTERNAL: DSSMP (Ch14 App J) + permanent records 12.12(a)(1)(ii)', 'UNVERIFIED-pending', false, true),
  ('modify_project_works_12_11', split('live', ','), 'suspended_monitoring_modified', split('construction_modification,emergency_modification', ','), '12.11, sworn 12.13; emergency also 12.10(a)(2); 12.24(b)-(c) if EAP changes', 'UNVERIFIED-pending', false, true),
  ('suspend_re_directed_12_4', split('live', ','), 'suspended_monitoring_modified', split('re_directed', ','), '12.4(b)(2)', 'UNVERIFIED-pending', false, true),
  ('suspend_ops_drawdown_internal', split('live', ','), 'suspended_monitoring_modified', split('drawdown,monitoring_change', ','), 'INTERNAL: license articles; record under 12.12(a)(1)(iii)(A)', 'UNVERIFIED-pending', false, true),
  ('resume_dssmp_internal', split('suspended_monitoring_modified', ','), 'live', CAST(NULL AS ARRAY<STRING>), 'INTERNAL', 'UNVERIFIED-pending', false, true),
  ('transfer_filed_fpa8_9_1', split('live', ','), 'pending_transfer', CAST(NULL AS ARRAY<STRING>), 'FPA 8; 9.1-9.2', 'UNVERIFIED-pending', false, true),
  ('transfer_withdrawn_385_216', split('pending_transfer', ','), 'live', CAST(NULL AS ARRAY<STRING>), '385.216', 'UNVERIFIED-pending', true, true),
  ('transfer_denied_order', split('pending_transfer', ','), 'live', CAST(NULL AS ARRAY<STRING>), 'Commission order', 'UNVERIFIED-pending', false, true),
  ('transfer_final_9_3_12_12c', split('pending_transfer', ','), 'transferred', CAST(NULL AS ARRAY<STRING>), '9.3; 12.12(c)', 'VERIFIED', false, true),
  ('surrender_filed_6_1', split('live,suspended_monitoring_modified', ','), 'pending_surrender', CAST(NULL AS ARRAY<STRING>), '6.1; 12.1', 'UNVERIFIED-pending', false, true),
  ('surrender_withdrawn_385_216', split('pending_surrender', ','), 'live', CAST(NULL AS ARRAY<STRING>), '385.216', 'UNVERIFIED-pending', true, true),
  ('surrender_denied_order', split('pending_surrender', ','), 'live', CAST(NULL AS ARRAY<STRING>), 'Commission order', 'UNVERIFIED-pending', false, true),
  ('surrender_effective_6_2_6_5', split('pending_surrender', ','), 'decommissioned_archived', CAST(NULL AS ARRAY<STRING>), '6.2; 6.5; 12.1', 'VERIFIED', false, true),
  ('void_from_draft_internal', split('draft_commissioning', ','), 'voided_in_error', CAST(NULL AS ARRAY<STRING>), 'INTERNAL', 'UNVERIFIED-pending', false, true),
  ('void_from_live_internal', split('live', ','), 'voided_in_error', CAST(NULL AS ARRAY<STRING>), 'INTERNAL', 'UNVERIFIED-pending', false, true) AS s(citation_code, from_states, to_state, suspension_reasons, citation, verification_status, withdrawal_notice, active)
WHERE NOT EXISTS (SELECT 1 FROM ref.lifecycle_citation t WHERE t.citation_code = s.citation_code);

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction',
                   'alarm_event', 'alarm_rule', 'dssmp_version', 'dam', 'rule_reversion',
                   'dam_lifecycle')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_event'
       OR (decision IN ('acknowledge', 'close') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_rule'
       OR (decision IN ('propose', 'apply', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'dssmp_version'
       OR (decision IN ('propose', 'approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'dam'
       OR (decision IN ('assign', 'propose', 'approve', 'reject') AND comment IS NOT NULL
           AND length(trim(comment)) > 0))
  AND (subject_type <> 'rule_reversion'
       OR (decision IN ('propose', 'approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'dam_lifecycle'
       OR (decision IN ('propose', 'approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0)));

ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_chain;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_chain CHECK (
  (subject_type NOT IN ('alarm_event', 'alarm_rule', 'dssmp_version', 'dam', 'rule_reversion',
                        'dam_lifecycle')
   OR action_type IS NOT NULL)
  AND (action_type IS NULL OR (
    action_type IN ('bias_review', 'data_correction_review', 'alarm_ack', 'alarm_close',
                    'alarm_rule_flag_propose', 'alarm_rule_flag_apply', 'alarm_rule_flag_reject',
                    'dssmp_version_propose', 'dssmp_version_approve', 'dssmp_version_reject',
                    'dam_cdse_assign', 'dam_cdse_change_propose', 'dam_cdse_change_approve',
                    'dam_cdse_change_reject',
                    'rule_reversion_propose', 'rule_reversion_approve', 'rule_reversion_reject',
                    'lifecycle_transition_propose', 'lifecycle_transition_approve', 'lifecycle_transition_reject')
    AND original_value IS NOT NULL AND corrected_value IS NOT NULL AND chain_id IS NOT NULL
    AND chain_seq IS NOT NULL AND chain_seq >= 1 AND prev_hash IS NOT NULL AND record_hash IS NOT NULL
    AND chain_id = dam_id AND comment IS NOT NULL AND length(trim(comment)) > 0
    AND ((action_type IN ('bias_review', 'data_correction_review') AND subject_type = 'incl_correction')
         OR (action_type = 'alarm_ack' AND subject_type = 'alarm_event' AND decision = 'acknowledge')
         OR (action_type = 'alarm_close' AND subject_type = 'alarm_event' AND decision = 'close')
         OR (action_type = 'alarm_rule_flag_propose' AND subject_type = 'alarm_rule' AND decision = 'propose')
         OR (action_type = 'alarm_rule_flag_apply' AND subject_type = 'alarm_rule' AND decision = 'apply')
         OR (action_type = 'alarm_rule_flag_reject' AND subject_type = 'alarm_rule' AND decision = 'reject')
         OR (action_type = 'dssmp_version_propose' AND subject_type = 'dssmp_version' AND decision = 'propose')
         OR (action_type = 'dssmp_version_approve' AND subject_type = 'dssmp_version' AND decision = 'approve')
         OR (action_type = 'dssmp_version_reject' AND subject_type = 'dssmp_version' AND decision = 'reject')
         OR (action_type = 'dam_cdse_assign' AND subject_type = 'dam' AND decision = 'assign'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_propose' AND subject_type = 'dam' AND decision = 'propose'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_approve' AND subject_type = 'dam' AND decision = 'approve'
             AND subject_id = dam_id)
         OR (action_type = 'dam_cdse_change_reject' AND subject_type = 'dam' AND decision = 'reject'
             AND subject_id = dam_id)
         OR (action_type = 'rule_reversion_propose' AND subject_type = 'rule_reversion' AND decision = 'propose')
         OR (action_type = 'rule_reversion_approve' AND subject_type = 'rule_reversion' AND decision = 'approve')
         OR (action_type = 'rule_reversion_reject' AND subject_type = 'rule_reversion' AND decision = 'reject')
         OR (action_type = 'lifecycle_transition_propose' AND subject_type = 'dam_lifecycle' AND decision = 'propose'
             AND subject_id = dam_id)
         OR (action_type = 'lifecycle_transition_approve' AND subject_type = 'dam_lifecycle' AND decision = 'approve'
             AND subject_id = dam_id)
         OR (action_type = 'lifecycle_transition_reject' AND subject_type = 'dam_lifecycle' AND decision = 'reject'
             AND subject_id = dam_id)))));
