-- 10b Least-privilege reviewer-authority view (v1.6 ROLE-LEVEL, Genie-confirmed 2026-11-09).
-- Who may sign at review_level 'CDSE' for a dam: the dam's named Chief Dam Safety Engineer (ref.dam.cdse_id) or a
-- direct ref.project_membership grant with role CDSE for exactly that dam (FERC Ch.14 App K 14-K-4: the CDSE is a
-- named person; app roles dso / dam_admin / admin never confer CDSE). Signed-in users get SELECT on THIS VIEW ONLY,
-- never on ref.dam or ref.project_membership: ref.project_membership carries no row filter (it backs
-- ref.dam_row_filter_membership) and lists every principal and role for every dam.
-- Per-dam scope: the explicit ref.dam_row_filter(dam_id) predicate (same function as the table row filter, evaluated
-- for the querying user, dynamic-view pattern), so a dam-<id>-members user sees only that dam's CDSE rows.
-- '*' (all-dam) memberships and non-CDSE roles are excluded. Rows include revoked grants (granted_ts / revoked_ts) so
-- authority can be checked AT REVIEW TIME (gold.v_review_level_check). ref.dam.cdse_id has no history: it is the
-- CURRENT CDSE (UNVERIFIED for past reviews; VERIFICATION.md ROLE-LEVEL).
-- ⚠ UNVERIFIED on a workspace: dynamic-view evaluation of is_account_group_member() inside ref.dam_row_filter, and the
--   interaction with the ref.dam table row filter / ABAC policy when read through a view (view owner = setup SP).
USE CATALOG IDENTIFIER(:catalog);

CREATE OR REPLACE VIEW ref.v_dam_reviewer_authority
COMMENT 'CDSE reviewer authority per dam: ref.dam.cdse_id + CDSE memberships only; per-dam filtered (SCHEMA_CONTRACT 2)'
AS
SELECT d.dam_id, d.cdse_id, d.cdse_id AS principal, 'DAM_CDSE_ID' AS authority_source,
       CAST(NULL AS TIMESTAMP) AS granted_ts, CAST(NULL AS TIMESTAMP) AS revoked_ts
FROM ref.dam d
WHERE d.cdse_id IS NOT NULL AND ref.dam_row_filter(d.dam_id)
UNION ALL
SELECT m.dam_id, d.cdse_id, m.principal, 'MEMBERSHIP_CDSE' AS authority_source, m.granted_ts, m.revoked_ts
FROM ref.project_membership m
JOIN ref.dam d ON d.dam_id = m.dam_id
WHERE upper(m.role) = 'CDSE' AND m.dam_id <> '*' AND ref.dam_row_filter(m.dam_id);

-- Grants: USE SCHEMA ref + SELECT on the view, per dam group only. Bundles/SQL files cannot loop, so the block is
-- GENERATED from the dam list by tools/render_uc_reviewer_grants.py; CI runs --check. USE CATALOG for the dam groups is
-- a deployment prerequisite not granted by this repo (UNVERIFIED, VERIFICATION.md UC-GRANTS).
-- BEGIN GENERATED grants (tools/render_uc_reviewer_grants.py; do not edit by hand)
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-01-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-01-members`;
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-02-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-02-members`;
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-03-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-03-members`;
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-04-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-04-members`;
GRANT USE SCHEMA ON SCHEMA ref TO `dam-SYN-DAM-05-members`;
GRANT SELECT ON TABLE ref.v_dam_reviewer_authority TO `dam-SYN-DAM-05-members`;
-- END GENERATED grants
