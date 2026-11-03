-- 11 Contract views. RUN AFTER THE FIRST PIPELINE UPDATE (they reference pipeline-owned
-- silver.reading_clean / silver.incl_survey / raw.reading_raw). Job dam_uc_setup task 2.
-- gold views for the app + SQL alerts. (Heavier aggregates are pipeline MVs in
-- src/pipelines/gold.py: gold.instrument_daily, gold.data_freshness.)
USE CATALOG IDENTIFIER(:catalog);

-- SQL alerts read THIS view (KB rec 7). One row per currently-active exceedance.
-- Source of truth = alarm state machine (ops.alarm_state) so hysteresis/persistence apply,
-- joined to the threshold version that raised it.
CREATE OR REPLACE VIEW gold.threshold_exceedances AS
SELECT s.dam_id, d.name AS dam_name, se.instrument_id, s.sensor_id, s.alarm_key,
       s.state, s.level, t.level_name, t.tier, t.limit_type, t.direction,
       t.value AS threshold_value, t.unit, s.last_value, s.last_ts,
       s.active_threshold_id AS threshold_id, s.active_threshold_version AS threshold_version,
       t.pfm_id, t.eap_level_id, t.required_actions,
       NOT s.acked AS unacknowledged,
       (unix_timestamp(current_timestamp()) - unix_timestamp(s.last_ts)) / 3600.0 AS hours_since_reading,
       s.updated_ts
FROM ops.alarm_state s
JOIN ref.sensor se ON se.sensor_id = s.sensor_id
JOIN ref.dam d ON d.dam_id = s.dam_id
LEFT JOIN ref.threshold t
  ON t.threshold_id = s.active_threshold_id AND t.threshold_version = s.active_threshold_version
WHERE s.level > 0;

-- Exceedance verification status (latest status row)
CREATE OR REPLACE VIEW gold.v_exceedance_current AS
SELECT e.*, l.verification_status, l.verification_steps, l.confirmed_ts,
       l.condition_affecting_safety, l.cfr_category, l.incident_id, l.actor AS status_actor, l.status_ts
FROM ops.exceedance e
LEFT JOIN (SELECT * EXCEPT (rn) FROM (
  SELECT *, row_number() OVER (PARTITION BY exceedance_id ORDER BY status_ts DESC) rn
  FROM ops.exceedance_status_log) WHERE rn = 1) l USING (exceedance_id);

-- Canvas names gold.alarm_state / gold.alarm_events: app-facing views
CREATE OR REPLACE VIEW gold.alarm_state  AS SELECT * FROM ops.alarm_state;
CREATE OR REPLACE VIEW gold.alarm_events AS SELECT * FROM ops.alarm_transition;
-- Canvas name silver.readings_clean / silver.reduced_readings / silver.quarantine: aliases
CREATE OR REPLACE VIEW silver.reduced_readings AS SELECT * FROM silver.v_reading_current;

-- Canvas name ops.reading: contract VIEW over silver (validated reading + engineer marks).
CREATE OR REPLACE VIEW ops.reading AS
SELECT c.reading_id, c.dam_id, c.instrument_id, c.sensor_id,
       c.ts_utc AS reading_ts, c.corrected_value AS value, c.corrected_unit AS unit,
       c.eng_value, c.eng_unit, c.raw_value, c.raw_unit,
       rc.source, rc.reservoir_el_m AS reservoir_el, rc.ingest_ts,
       c.qa_flag AS qc_flag, c.qa_reason AS qc_reason,
       coalesce(m.is_erroneous, false) AS is_erroneous, m.erroneous_reason,
       c.calib_id, c.cal_version, c.formula_id, c.formula_version, c.recompute_batch_id,
       CAST(NULL AS STRING) AS schedule_slot_id
FROM silver.v_reading_current c
LEFT JOIN silver.reading_clean rc USING (reading_id)
LEFT JOIN (
  SELECT * EXCEPT (rn) FROM (
    SELECT *, row_number() OVER (PARTITION BY reading_id ORDER BY marked_ts DESC) rn
    FROM ops.reading_erroneous_mark) WHERE rn = 1
) m USING (reading_id);

-- Canvas name ops.reading_raw: contract VIEW over the bronze streaming table.
CREATE OR REPLACE VIEW ops.reading_raw AS SELECT * FROM raw.reading_raw;

-- ---- App-facing compatibility views (SCHEMA_CONTRACT.md §5) ----
CREATE OR REPLACE VIEW ref.site AS
SELECT dam_id, name, dam_type, hazard_potential AS hazard_class, owner, nid_id, state, lat, lon,
       height_m, crest_el_m AS crest_elev_m, normal_pool_el_m AS normal_pool_m, datum, status,
       ferc_project_no, portfolio_id, timezone
FROM ref.dam;

-- Alarm episodes (RAISE ... CLEAR) for the alarm console; event_id = transition_id of RAISE.
CREATE OR REPLACE VIEW ops.alarm_event AS
WITH t AS (
  SELECT *, sum(CASE WHEN event = 'RAISE' THEN 1 ELSE 0 END)
              OVER (PARTITION BY alarm_key ORDER BY ts_utc, recorded_ts
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS episode_no
  FROM ops.alarm_transition)
SELECT first_value(CASE WHEN event = 'RAISE' THEN transition_id END, true)
         OVER w AS event_id,
       dam_id, sensor_id, alarm_key, episode_no, ts_utc AS event_ts, event, from_state, to_state,
       value, metric_value, threshold_id, threshold_version, actor, comment,
       reading_id, calib_id, cal_version, formula_id, formula_version, recompute_batch_id, qa_flag,
       installation_version
FROM t WHERE episode_no > 0
WINDOW w AS (PARTITION BY alarm_key, episode_no ORDER BY ts_utc, recorded_ts
             ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING);

-- One row per alarm episode (what the app's list expects).
-- v1.5 (App Developer mismatch 2): the engineer's erroneous mark on the episode's `reading_id` (the reading behind
-- `value`) is exposed here as erroneous_mark / erroneous_reason / erroneous_marked_by / erroneous_marked_ts, from
-- the latest ops.reading_erroneous_mark row (the same row ops.reading uses for is_erroneous), so the app no longer
-- reads ops.reading for alarms. erroneous_mark is false when the reading was never marked or the mark was withdrawn.
CREATE OR REPLACE VIEW ops.alarm_episode AS
WITH g AS (
SELECT event_id, any_value(dam_id) AS dam_id, any_value(se.instrument_id) AS instrument_id,
       any_value(e.sensor_id) AS sensor_id,
       min(event_ts) AS event_ts,
       max(CASE WHEN event = 'CLEAR' OR event = 'RTN_UNACK' THEN event_ts END) AS cleared_ts,
       max_by(to_state, event_ts) AS machine_state,
       max(CASE WHEN event IN ('RAISE','ESCALATE') THEN threshold_id END) AS threshold_id,
       max(CASE WHEN event IN ('RAISE','ESCALATE') THEN threshold_version END) AS threshold_version,
       -- v1.4: value/lineage come from the latest READING-driven transition - ACK rows (value NULL) are skipped,
       -- so an acknowledged episode never shows a NULL value (max_by ignores NULL ordering keys).
       max_by(value, CASE WHEN reading_id IS NOT NULL THEN event_ts END) AS value,
       -- lineage of `value` (latest reading-driven transition) and of the RAISE that opened the episode
       max_by(reading_id, CASE WHEN reading_id IS NOT NULL THEN event_ts END)  AS reading_id,
       max_by(cal_version, CASE WHEN reading_id IS NOT NULL THEN event_ts END) AS cal_version,
       max_by(cal_version, CASE WHEN reading_id IS NOT NULL THEN event_ts END) AS calibration_version,
       max_by(calib_id, CASE WHEN reading_id IS NOT NULL THEN event_ts END)    AS calib_id,
       max_by(formula_id, CASE WHEN reading_id IS NOT NULL THEN event_ts END)  AS formula_id,
       max_by(formula_version, CASE WHEN reading_id IS NOT NULL THEN event_ts END) AS formula_version,
       max_by(qa_flag, CASE WHEN reading_id IS NOT NULL THEN event_ts END)     AS qa_flag,
       -- v1.6 (App Dev §7 10d): inclinometer episodes have calib_id NULL, so installation_version (the
       -- ref.incl_installation row valid at the reading's ts, written by alarm_evaluator) is their lineage.
       max_by(installation_version, CASE WHEN reading_id IS NOT NULL THEN event_ts END) AS installation_version,
       max(CASE WHEN event = 'RAISE' THEN reading_id END)  AS raise_reading_id,
       max(CASE WHEN event = 'RAISE' THEN cal_version END) AS raise_cal_version,
       size(collect_set(cal_version)) > 1                   AS mixed_calibration
FROM ops.alarm_event e JOIN ref.sensor se USING (sensor_id)
GROUP BY event_id),
mk AS (
  SELECT * EXCEPT (rn) FROM (
    SELECT *, row_number() OVER (PARTITION BY reading_id ORDER BY marked_ts DESC) rn
    FROM ops.reading_erroneous_mark) WHERE rn = 1)
SELECT g.*,
       coalesce(mk.is_erroneous, false) AS erroneous_mark,
       mk.erroneous_reason              AS erroneous_reason,
       mk.marked_by                     AS erroneous_marked_by,
       mk.marked_ts                     AS erroneous_marked_ts
FROM g LEFT JOIN mk ON mk.reading_id = g.reading_id;

CREATE OR REPLACE VIEW silver.incl_survey_header AS
SELECT instrument_id, survey_id, survey_ts, is_baseline, any_value(probe_serial) AS probe_serial,
       any_value(operator) AS operator, count(*) AS n_depths
FROM silver.incl_survey GROUP BY instrument_id, survey_id, survey_ts, is_baseline;

-- v1.5 (QA-039, QA-036): app-facing status of every inclinometer correction version. ref.incl_correction is
-- append-only, so supersession is DERIVED (a later row's `supersedes`) and approval comes from the insert-only
-- audit.review_record row the version names in review_id (independent reviewer, decision 'approve').
-- approval_status: proposed | approved | superseded | unreviewed_applied. The CHECK
-- ck_incl_correction_applied_reviewed makes an applied row without reviewer columns impossible, and
-- 'unreviewed_applied' is the defensive value for an applied row whose review row is missing or does not match.
-- The review signature is verified by the reduction job (corrections.approval_problem), not by this view.
CREATE OR REPLACE VIEW gold.incl_correction_status AS
SELECT c.* EXCEPT (superseded_by),
       coalesce(c.superseded_by, nxt.record_id) AS superseded_by,
       r.reviewer_id    AS approved_by,
       r.signed_ts      AS approved_utc,
       r.comment        AS review_rationale,
       r.signature_hash AS review_signature_hash,
       CASE WHEN c.superseded_by IS NOT NULL OR nxt.record_id IS NOT NULL THEN 'superseded'
            WHEN c.applied AND r.review_id IS NOT NULL THEN 'approved'
            WHEN c.applied THEN 'unreviewed_applied'
            ELSE 'proposed' END AS approval_status
FROM ref.incl_correction c
LEFT JOIN (SELECT supersedes, max_by(record_id, timestamp_utc) AS record_id
           FROM ref.incl_correction WHERE supersedes IS NOT NULL GROUP BY supersedes) nxt
  ON nxt.supersedes = c.record_id
LEFT JOIN audit.review_record r
  ON r.review_id = c.review_id AND r.subject_type = 'incl_correction' AND r.decision = 'approve'
 AND r.reviewer_id = c.reviewed_by AND lower(r.reviewer_id) <> lower(c.`user`)
 AND r.subject_id IN (c.record_id, c.supersedes);
