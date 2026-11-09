-- Migration v1.8.002 (lifecycle backfill, rule 6): one draft_commissioning -> live audit.lifecycle_event per dam
-- from its onboarding_golive audit_log record(s) in audit.app_extract (at-least-once copies deduplicated on pk; a dam
-- with several go-live records gets ONE event, the earliest ts then lowest pk, because a second draft -> live is not
-- an edge). ts is TEXT ISO-8601 with Z, parsed strictly (YYYY-MM-DDTHH:MM:SSZ): ANY malformed ts, a missing payload
-- dam_id, an envelope dam_id that differs from the payload's, or a payload that mentions onboarding_golive but cannot
-- be read FAILS the whole migration (raise_error; nothing is written). Chained: prev_event_hash = 64 x 0 (it is the
-- dam's first event), event_hash = dam_safety.lifecycle.hash_sql (tests recompute it in Python). Then the SCD-2 row
-- and the ref.dam mirror (only where the mirror is still NULL). evidence_uri = uc:audit.app_extract/audit_log/<pk>,
-- citation go_live_dssmp_12_12.
-- ORDER: after v1_8_001. IDEMPOTENT: a dam that already has any lifecycle event is skipped; state rows / mirror only
-- where missing.
-- PRE-CHECK (informational; the guard below refuses the same rows):
--   SELECT pk, payload FROM audit.app_extract WHERE table_name = 'audit_log' AND payload LIKE '%onboarding_golive%';
-- DOWN: sql/migrations/v1_8_002_lifecycle_backfill_down.sql (no-op: back-filled events are permanent records).
-- UNVERIFIED: not run on a workspace.
USE CATALOG IDENTIFIER(:catalog);

SELECT CASE WHEN count(*) > 0 THEN raise_error(concat('v1_8_002: ', CAST(count(*) AS STRING),
       ' onboarding_golive record(s) with a malformed ts or dam_id; nothing back-filled')) END AS v1_8_002_guard
FROM (SELECT get_json_object(payload, '$.action') AS action, get_json_object(payload, '$.ts') AS ts,
             get_json_object(payload, '$.dam_id') AS p_dam, dam_id AS env_dam
      FROM audit.app_extract WHERE table_name = 'audit_log' AND payload LIKE '%onboarding_golive%') g
WHERE action IS NULL
   OR (action = 'onboarding_golive' AND (ts IS NULL OR NOT (ts RLIKE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$') OR p_dam IS NULL
       OR (env_dam IS NOT NULL AND env_dam <> p_dam)));

SELECT CASE WHEN count(*) > 0 THEN raise_error('v1_8_002: an onboarding_golive ts is not a valid UTC time')
       END AS v1_8_002_ts_guard
FROM (SELECT DISTINCT pk, get_json_object(payload, '$.dam_id') AS dam_id, get_json_object(payload, '$.ts') AS ts,
         get_json_object(payload, '$.actor') AS actor
  FROM audit.app_extract WHERE table_name = 'audit_log' AND get_json_object(payload, '$.action') = 'onboarding_golive') g WHERE CAST(ts AS TIMESTAMP) IS NULL;

INSERT INTO audit.lifecycle_event (event_id, dam_id, from_state, to_state, trigger_code, authorizer_role,
  authorizer_user_id, evidence_uri, effective_ts, created_by, created_ts, rationale, citation_code, approved_by,
  reason_code, prev_event_hash, event_hash)
SELECT b.event_id, b.dam_id, 'draft_commissioning', 'live', 'backfill_onboarding_golive', 'legacy_onboarding_admin',
  b.actor, split(concat('uc:audit.app_extract/audit_log/', b.pk), '~~~~'), CAST(b.ts AS TIMESTAMP),
  'migration:v1_8_002', current_timestamp(), 'backfilled from the onboarding_golive audit_log record',
  'go_live_dssmp_12_12', b.actor, 'backfill_onboarding_golive', '0000000000000000000000000000000000000000000000000000000000000000',
  sha2(concat(
    concat(coalesce(CAST(length('0000000000000000000000000000000000000000000000000000000000000000') AS STRING), '-1'), ':', coalesce('0000000000000000000000000000000000000000000000000000000000000000', ''), '|'),
    concat(coalesce(CAST(length(b.event_id) AS STRING), '-1'), ':', coalesce(b.event_id, ''), '|'),
    concat(coalesce(CAST(length(b.dam_id) AS STRING), '-1'), ':', coalesce(b.dam_id, ''), '|'),
    concat(coalesce(CAST(length('draft_commissioning') AS STRING), '-1'), ':', coalesce('draft_commissioning', ''), '|'),
    concat(coalesce(CAST(length('live') AS STRING), '-1'), ':', coalesce('live', ''), '|'),
    concat(coalesce(CAST(length('backfill_onboarding_golive') AS STRING), '-1'), ':', coalesce('backfill_onboarding_golive', ''), '|'),
    concat(coalesce(CAST(length('legacy_onboarding_admin') AS STRING), '-1'), ':', coalesce('legacy_onboarding_admin', ''), '|'),
    concat(coalesce(CAST(length(b.actor) AS STRING), '-1'), ':', coalesce(b.actor, ''), '|'),
    concat(coalesce(CAST(length(concat('uc:audit.app_extract/audit_log/', b.pk)) AS STRING), '-1'), ':', coalesce(concat('uc:audit.app_extract/audit_log/', b.pk), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(b.ts) AS STRING), '-1'), ':', coalesce(b.ts, ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length('migration:v1_8_002') AS STRING), '-1'), ':', coalesce('migration:v1_8_002', ''), '|'),
    concat(coalesce(CAST(length('backfilled from the onboarding_golive audit_log record') AS STRING), '-1'), ':', coalesce('backfilled from the onboarding_golive audit_log record', ''), '|'),
    concat(coalesce(CAST(length('go_live_dssmp_12_12') AS STRING), '-1'), ':', coalesce('go_live_dssmp_12_12', ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(b.actor) AS STRING), '-1'), ':', coalesce(b.actor, ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length('backfill_onboarding_golive') AS STRING), '-1'), ':', coalesce('backfill_onboarding_golive', ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|'),
    concat(coalesce(CAST(length(CAST(NULL AS STRING)) AS STRING), '-1'), ':', coalesce(CAST(NULL AS STRING), ''), '|')), 256)
FROM (SELECT concat('backfill:onboarding_golive:', d.pk) AS event_id, d.pk, d.dam_id, d.ts, d.actor,
             row_number() OVER (PARTITION BY d.dam_id ORDER BY d.ts, d.pk) AS rn
      FROM (SELECT DISTINCT pk, get_json_object(payload, '$.dam_id') AS dam_id, get_json_object(payload, '$.ts') AS ts,
         get_json_object(payload, '$.actor') AS actor
  FROM audit.app_extract WHERE table_name = 'audit_log' AND get_json_object(payload, '$.action') = 'onboarding_golive') d) b
WHERE b.rn = 1 AND NOT EXISTS (SELECT 1 FROM audit.lifecycle_event e WHERE e.dam_id = b.dam_id);

INSERT INTO ref.dam_lifecycle_state (state_id, dam_id, lifecycle_state, effective_from, effective_to, event_id)
SELECT e.event_id, e.dam_id, e.to_state, e.effective_ts, CAST(NULL AS TIMESTAMP), e.event_id
FROM audit.lifecycle_event e
WHERE e.event_id LIKE 'backfill:onboarding_golive:%'
  AND NOT EXISTS (SELECT 1 FROM ref.dam_lifecycle_state s WHERE s.dam_id = e.dam_id);

MERGE INTO ref.dam AS d
USING (SELECT dam_id, lifecycle_state, effective_from FROM ref.dam_lifecycle_state WHERE effective_to IS NULL) AS s
ON d.dam_id = s.dam_id
WHEN MATCHED AND d.lifecycle_state IS NULL THEN UPDATE SET lifecycle_state = s.lifecycle_state,
  lifecycle_effective_from = s.effective_from;
