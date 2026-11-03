-- 05 raw (bronze). Tables here are OWNED BY THE LAKEFLOW PIPELINE (src/pipelines/bronze.py)
-- and therefore are NOT created by DDL; their contract is documented for reviewers:
--
--   raw.reading_raw        streaming table, append-only (delta.appendOnly=true)
--       dam_id, logger_id, sensor_id, ts_utc, raw_value, raw_unit, temp_raw, temp_c, baro_kpa,
--       reading_type, source ('ADAS'|'MANUAL'), reservoir_el_m, datum_id (STRING, nullable), source_file (_metadata.file_path),
--       file_mod_ts, ingest_ts, _rescued_data
--   raw.incl_survey_raw    streaming table, append-only: instrument_id, survey_ts, depth_m,
--       a0, a180, b0, b180, probe_serial, operator, source_file, ingest_ts, _rescued_data
--   raw.reading_quarantine / raw.incl_quarantine
--       rows that failed bronze parse expectations (+ quarantine_reason). Never dropped silently.
--
-- Files land in the UC volume /Volumes/<catalog>/raw/landing/<dam_id>/<instrument_type>/.
-- Manual readings entered in the app (Lakebase) are exported by job dam_lakebase_extract as
-- CSV files into /Volumes/<catalog>/raw/landing/<dam_id>/manual/ so ADAS and manual data
-- share ONE append-only ingest path (FERC canvas: ops.reading_raw = ADAS + manual as received).
USE CATALOG IDENTIFIER(:catalog);
SELECT 'raw schema tables are pipeline-owned; see src/pipelines/bronze.py' AS note;
