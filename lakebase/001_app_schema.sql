-- Lakebase (Postgres 17, Autoscaling project dam-<target>) app OLTP state.
-- Schema dam_app. OWNERSHIP: the App Developer's app/backend/repos/state.py creates
-- app_user, dam_role, alarm_ack, alarm_comment, onboarding_draft, onboarding_publication,
-- report_draft, pfm_review, audit_log (CREATE IF NOT EXISTS at startup). This file is the
-- data-layer copy of those definitions (kept column-compatible) PLUS the tables the data
-- layer needs: inspection_note, user_preference, exceedance_status, review_signoff,
-- manual_reading, and the extract watermark. Lakebase is NOT the system of record for any
-- regulatory record: job dam_lakebase_extract copies append-style rows to UC Delta
-- (audit.event_log, audit.review_record, ops.exceedance_status_log, ops.surveillance_inspection,
-- raw landing volume for manual readings). Timestamps: app uses ISO-8601 TEXT (UTC, 'Z').
CREATE SCHEMA IF NOT EXISTS dam_app;
SET search_path TO dam_app;

-- ---- owned by app/backend/repos/state.py (mirrored; do not diverge) ----
CREATE TABLE IF NOT EXISTS app_user (email TEXT PRIMARY KEY, display_name TEXT,
  is_admin BOOLEAN NOT NULL DEFAULT FALSE, active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TEXT NOT NULL, created_by TEXT);
CREATE TABLE IF NOT EXISTS dam_role (email TEXT NOT NULL, dam_id TEXT NOT NULL, role TEXT NOT NULL,
  granted_by TEXT, granted_at TEXT NOT NULL, PRIMARY KEY (email, dam_id));
CREATE TABLE IF NOT EXISTS alarm_ack (event_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, state TEXT NOT NULL,
  acknowledged_by TEXT, acknowledged_at TEXT, closed_by TEXT, closed_at TEXT, close_reason TEXT);
CREATE TABLE IF NOT EXISTS alarm_comment (id BIGSERIAL PRIMARY KEY, event_id TEXT NOT NULL, dam_id TEXT NOT NULL,
  author TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS onboarding_draft (draft_id TEXT PRIMARY KEY, created_by TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL, proposed_dam_id TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS onboarding_publication (pub_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL,
  dam_id TEXT NOT NULL, payload TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
  status TEXT NOT NULL, applied_at TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS report_draft (report_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL,
  period_start TEXT NOT NULL, period_end TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  status TEXT NOT NULL, content_md TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pfm_review (id BIGSERIAL PRIMARY KEY, pfm_id TEXT NOT NULL, dam_id TEXT NOT NULL,
  reviewed_by TEXT NOT NULL, reviewed_at TEXT NOT NULL, status TEXT NOT NULL, notes TEXT);
CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, dam_id TEXT, target TEXT, detail TEXT);

-- ---- data-layer additions (proposed in SCHEMA_CONTRACT.md §7; need App Dev agreement) ----
CREATE TABLE IF NOT EXISTS inspection_note (
  id BIGSERIAL PRIMARY KEY, dam_id TEXT NOT NULL, structure_id TEXT, instrument_id TEXT,
  inspection_ts TEXT NOT NULL, inspection_type TEXT, checklist_result JSONB, body TEXT NOT NULL,
  photo_uris TEXT[], author TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_preference (
  email TEXT NOT NULL, pref_key TEXT NOT NULL, pref_value JSONB NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (email, pref_key));
CREATE TABLE IF NOT EXISTS exceedance_status (
  id BIGSERIAL PRIMARY KEY, exceedance_id TEXT NOT NULL, dam_id TEXT NOT NULL,
  verification_status TEXT NOT NULL CHECK (verification_status IN ('open','verifying','confirmed','false')),
  verification_steps TEXT, condition_affecting_safety BOOLEAN, cfr_category TEXT, incident_id TEXT,
  actor TEXT NOT NULL, status_ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_signoff (
  review_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
  review_level TEXT NOT NULL, decision TEXT NOT NULL, comment TEXT, reviewer_id TEXT NOT NULL,
  reviewer_pe_license TEXT, signed_ts TEXT NOT NULL, signature_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manual_reading (
  id BIGSERIAL PRIMARY KEY, dam_id TEXT NOT NULL, sensor_id TEXT NOT NULL, ts_utc TEXT NOT NULL,
  raw_value DOUBLE PRECISION NOT NULL, raw_unit TEXT NOT NULL, temp_c DOUBLE PRECISION,
  reservoir_el_m DOUBLE PRECISION, entered_by TEXT NOT NULL, entered_at TEXT NOT NULL, note TEXT,
  datum_id TEXT);  -- survey datum (CONVENTIONS.md 'Schema additions'); exported as-is to the landing CSV
ALTER TABLE manual_reading ADD COLUMN IF NOT EXISTS datum_id TEXT;
-- Append-only guard on the tables the extract job copies (no UPDATE/DELETE after insert).
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$;
DROP TRIGGER IF EXISTS t_noupd_exceedance_status ON exceedance_status;
CREATE TRIGGER t_noupd_exceedance_status BEFORE UPDATE OR DELETE ON exceedance_status
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
DROP TRIGGER IF EXISTS t_noupd_review_signoff ON review_signoff;
CREATE TRIGGER t_noupd_review_signoff BEFORE UPDATE OR DELETE ON review_signoff
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
DROP TRIGGER IF EXISTS t_noupd_manual_reading ON manual_reading;
CREATE TRIGGER t_noupd_manual_reading BEFORE UPDATE OR DELETE ON manual_reading
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE IF NOT EXISTS extract_watermark (
  table_name TEXT PRIMARY KEY, last_id BIGINT, last_ts TEXT, updated_at TEXT NOT NULL);
