-- 10b Least-privilege reviewer-authority view (v1.6 ROLE-LEVEL, Genie-confirmed 2026-11-09).
-- Who may sign at review_level 'CDSE' for a dam: the dam's named Chief Dam Safety Engineer (ref.dam.cdse_id) or a
-- direct ref.project_membership grant with role CDSE for exactly that dam (FERC Ch.14 App K 14-K-4: the CDSE is a
-- named person; app roles dso / dam_admin / admin never confer CDSE). Signed-in users get SELECT on THIS VIEW ONLY,
-- never on ref.dam or ref.project_membership: ref.project_membership carries no row filter (it backs
-- ref.dam_row_filter_membership) and lists every principal and role for every dam.
-- Per-dam scope: the explicit ref.dam_row_filter(dam_id) predicate (same function as the table row filter, evaluated
-- for the querying user, dynamic-view pattern), so a dam-<id>-members user sees only that dam's CDSE rows.
-- '*' (all-dam) memberships and non-CDSE roles are excluded. Rows include revoked grants (granted_ts / revoked_ts) so
-- authority can be checked AT REVIEW TIME (gold.v_review_level_check). v1.7 (B): the named CDSE comes from the SCD2
-- history ref.dam_cdse_assignment (granted_ts / revoked_ts = effective_from / effective_to, so the assignment current
-- at decision time decides); ref.dam.cdse_id (no history) is used only for a dam that has no assignment row yet
-- (pre-v1.7 dams; source DAM_CDSE_ID).
-- ⚠ UNVERIFIED on a workspace: dynamic-view evaluation of is_account_group_member() inside ref.dam_row_filter, and the
--   interaction with the ref.dam table row filter / ABAC policy when read through a view (view owner = setup SP).
USE CATALOG IDENTIFIER(:catalog);

CREATE OR REPLACE VIEW ref.v_dam_reviewer_authority
COMMENT 'CDSE reviewer authority per dam: ref.dam_cdse_assignment history (legacy ref.dam.cdse_id only for a dam with no assignment) + CDSE memberships; per-dam filtered (SCHEMA_CONTRACT 2)'
AS
SELECT a.dam_id, a.cdse_id, a.cdse_id AS principal, 'DAM_CDSE_ASSIGNMENT' AS authority_source,
       a.effective_from AS granted_ts, a.effective_to AS revoked_ts
FROM ref.dam_cdse_assignment a
WHERE ref.dam_row_filter(a.dam_id)
UNION ALL
SELECT d.dam_id, d.cdse_id, d.cdse_id AS principal, 'DAM_CDSE_ID' AS authority_source,
       CAST(NULL AS TIMESTAMP) AS granted_ts, CAST(NULL AS TIMESTAMP) AS revoked_ts
FROM ref.dam d
WHERE d.cdse_id IS NOT NULL AND ref.dam_row_filter(d.dam_id)
  AND NOT EXISTS (SELECT 1 FROM ref.dam_cdse_assignment x WHERE x.dam_id = d.dam_id)
UNION ALL
SELECT m.dam_id, d.cdse_id, m.principal, 'MEMBERSHIP_CDSE' AS authority_source, m.granted_ts, m.revoked_ts
FROM ref.project_membership m
JOIN ref.dam d ON d.dam_id = m.dam_id
WHERE upper(m.role) = 'CDSE' AND m.dam_id <> '*' AND ref.dam_row_filter(m.dam_id);

-- v1.7 (c) go-live readiness (Genie-approved as designed): a dam is ready only when ref.v_dam_reviewer_authority has
-- a CDSE row for it that is in force now. Read through the authority view, so a caller who cannot see the dam gets
-- ready = false (fail closed). has_current_dssmp is informational (the app's go-live already requires a DSSMP version).
-- Rows: every dam in ref.dam plus every dam that only has an assignment (a not-live dam has no ref.dam row yet, app
-- §7 item 20). assert_dam_go_live_ready works for any dam_id, listed or not (unknown -> raises).
-- Use: SELECT ref.assert_dam_go_live_ready('<dam_id>') raises dam_go_live_not_ready (UNVERIFIED on a workspace).
CREATE OR REPLACE VIEW ref.v_dam_go_live_readiness
COMMENT 'Go-live readiness per dam: ready = a CDSE in force in ref.v_dam_reviewer_authority (v1.7)'
AS
SELECT d.dam_id,
       EXISTS (SELECT 1 FROM ref.v_dam_reviewer_authority v WHERE v.dam_id = d.dam_id
               AND (v.granted_ts IS NULL OR v.granted_ts <= current_timestamp())
               AND (v.revoked_ts IS NULL OR v.revoked_ts > current_timestamp())) AS ready,
       EXISTS (SELECT 1 FROM ref.dssmp_version s WHERE s.dam_id = d.dam_id AND s.status = 'approved'
               AND s.effective_from <= current_timestamp()
               AND (s.effective_to IS NULL OR s.effective_to > current_timestamp())) AS has_current_dssmp,
       CASE WHEN EXISTS (SELECT 1 FROM ref.v_dam_reviewer_authority v WHERE v.dam_id = d.dam_id
                         AND (v.granted_ts IS NULL OR v.granted_ts <= current_timestamp())
                         AND (v.revoked_ts IS NULL OR v.revoked_ts > current_timestamp()))
            THEN NULL ELSE 'no_cdse_in_reviewer_authority' END AS blocking_reason
FROM (SELECT dam_id FROM ref.dam UNION SELECT dam_id FROM ref.dam_cdse_assignment) d;

CREATE OR REPLACE FUNCTION ref.assert_dam_go_live_ready(p_dam_id STRING)
RETURNS BOOLEAN
COMMENT 'true when the dam is go-live ready; otherwise raises dam_go_live_not_ready (v1.7 (c))'
RETURN CASE WHEN EXISTS (SELECT 1 FROM ref.v_dam_reviewer_authority v WHERE v.dam_id = p_dam_id
                         AND (v.granted_ts IS NULL OR v.granted_ts <= current_timestamp())
                         AND (v.revoked_ts IS NULL OR v.revoked_ts > current_timestamp()))
            THEN true
            ELSE raise_error(concat('dam_go_live_not_ready: ', coalesce(p_dam_id, '<null>'),
                                    ' has no CDSE in force in ref.v_dam_reviewer_authority')) END;

-- v1.7 (C) DSSMP re-versioning (app SCHEMA_CONTRACT §7 item 21). Thresholds stay on the DSSMP version they were set
-- under; a newer approved version does NOT move them. ref.v_dssmp_affected_rules lists every current rule whose
-- dssmp_version_id is not the dam's DSSMP version in force now (reason: dssmp_missing = no version on the rule,
-- dssmp_superseded = the rule's version is not the one in force; current_dssmp_version_id NULL = none in force).
-- ref.dssmp_affected_rules(dam, to) = the rules a re-version to `to` must cover (the app's affected_rules: the dam's
-- current rules whose dssmp_version_id is not `to`). The re-version itself is the app's rule_reversion workflow
-- (one CDSE rule_reversion_approve record, per-rule N+1 publications with cause 'dssmp_reversion', published by
-- dam_ref_publish); flag edits on rules of a superseded version keep failing with dssmp_superseded.
CREATE OR REPLACE VIEW ref.v_dssmp_affected_rules
COMMENT 'v1.7 (C): current rules not on the dam''s DSSMP version in force (re-version candidates)'
AS
SELECT t.dam_id, t.threshold_id, t.threshold_version, t.dssmp_version_id, cur.dssmp_version_id AS current_dssmp_version_id,
       t.cdse_ack_required,
       CASE WHEN t.dssmp_version_id IS NULL THEN 'dssmp_missing' ELSE 'dssmp_superseded' END AS reason
FROM ref.threshold t
LEFT JOIN (SELECT v.dam_id, max(v.dssmp_version_id) AS dssmp_version_id FROM ref.dssmp_version v
           WHERE v.status = 'approved' AND v.effective_from <= current_timestamp()
             AND (v.effective_to IS NULL OR v.effective_to > current_timestamp())
           GROUP BY v.dam_id) cur ON cur.dam_id = t.dam_id
WHERE t.is_current = true
  AND (t.dssmp_version_id IS NULL OR cur.dssmp_version_id IS NULL OR t.dssmp_version_id <> cur.dssmp_version_id);

CREATE OR REPLACE FUNCTION ref.dssmp_affected_rules(p_dam_id STRING, p_to_dssmp_version_id STRING)
RETURNS TABLE (dam_id STRING, threshold_id STRING, threshold_version INT, dssmp_version_id STRING,
               cdse_ack_required BOOLEAN)
COMMENT 'v1.7 (C): the current rules of p_dam_id a re-version to p_to_dssmp_version_id must cover'
RETURN SELECT t.dam_id, t.threshold_id, t.threshold_version, t.dssmp_version_id, t.cdse_ack_required
       FROM ref.threshold t
       WHERE t.is_current = true AND t.dam_id = p_dam_id
         AND (t.dssmp_version_id IS NULL OR t.dssmp_version_id <> p_to_dssmp_version_id);

-- Grants: USE SCHEMA ref + SELECT on the view + (v1.7) SELECT on ref.dssmp_version (row-filtered per dam, 10a_*), per dam
-- group only. Bundles/SQL files cannot loop, so the block is
-- GENERATED from the dam list by tools/render_uc_reviewer_grants.py; CI runs --check. USE CATALOG for the dam groups is
-- a deployment prerequisite not granted by this repo (UNVERIFIED, VERIFICATION.md UC-GRANTS).
-- BEGIN GENERATED grants (tools/render_uc_reviewer_grants.py; do not edit by hand)
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-01-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-01-members`;
GRANT SELECT ON TABLE ref.dssmp_version TO `dam-SYN-DAM-01-members`;
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-02-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-02-members`;
GRANT SELECT ON TABLE ref.dssmp_version TO `dam-SYN-DAM-02-members`;
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-03-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-03-members`;
GRANT SELECT ON TABLE ref.dssmp_version TO `dam-SYN-DAM-03-members`;
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-04-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-04-members`;
GRANT SELECT ON TABLE ref.dssmp_version TO `dam-SYN-DAM-04-members`;
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-05-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-05-members`;
GRANT SELECT ON TABLE ref.dssmp_version TO `dam-SYN-DAM-05-members`;
-- END GENERATED grants
