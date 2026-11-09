-- Lakebase (Postgres 17, Autoscaling project dam-<target>) app OLTP state.
-- Schema dam_app. OBJECT OWNER (v1.7): role dam_lakebase_owner (NOLOGIN), see 'ownership hardening' below.
-- DEFINITIONS: the App Developer's app/backend/repos/state.py creates
-- app_user, dam_role, alarm_ack, alarm_comment, onboarding_draft, onboarding_publication,
-- report_draft, pfm_review, audit_log (CREATE IF NOT EXISTS at startup). This file is the
-- data-layer copy of those definitions (kept column-compatible) PLUS the tables the data
-- layer needs: inspection_note, user_preference, exceedance_status, review_signoff,
-- manual_reading, and the extract watermark. Lakebase is NOT the system of record for any
-- regulatory record: job dam_lakebase_extract copies append-style rows to UC Delta
-- (audit.event_log, audit.review_record, ops.exceedance_status_log, ops.surveillance_inspection,
-- raw landing volume for manual readings). Timestamps: app uses ISO-8601 TEXT (UTC, 'Z').
-- QA-045c: the whole init runs in ONE transaction under a transaction-scoped advisory lock, so concurrent inits
-- (two psql sessions, or this script racing the app's StateStore.init_schema, which takes the same key) serialize,
-- and no other session ever sees a table without its guard. Key 4702045 = the shared 'dam_app schema init' lock
-- (same literal in app/backend/repos/state.py, see tools/patches). Released automatically at COMMIT/ROLLBACK.
BEGIN;
DO $$ BEGIN PERFORM pg_advisory_xact_lock(4702045); END $$;  -- held until COMMIT (no result row printed)
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
-- v1.6 §7 item 13 publisher outbox (same DDL as state.py). The app only INSERTs; dam_ref_publish writes the outcome.
CREATE TABLE IF NOT EXISTS ref_change_publication (pub_id TEXT PRIMARY KEY, kind TEXT NOT NULL, dam_id TEXT NOT NULL,
  proposal_id TEXT, payload TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
  status TEXT NOT NULL, applied_at TEXT, message TEXT);

-- ---- v1.7 data-layer addition: DSSMP registration outbox (Genie 2026-11-09; working name
-- app.dssmp_registration_outbox; NOT yet in state.py - App Dev adds the INSERT, see SCHEMA_CONTRACT.md). One row per
-- CDSE-approved DSSMP version; approval_record_hash = record_hash of the chained dssmp_version_approve review_signoff
-- row. The app only INSERTs; task load_dssmp_registrations (dam_safety.dssmp_registration) writes status / message /
-- applied_at. ----
CREATE TABLE IF NOT EXISTS dssmp_registration_outbox (outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL,
  dssmp_version_id TEXT NOT NULL, version_no INTEGER NOT NULL, effective_from TEXT NOT NULL,
  supersedes_dssmp_version_id TEXT, change_reason TEXT, source_ref TEXT, document_uri TEXT,
  document_sha256 TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
  approval_record_hash TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', applied_at TEXT, message TEXT,
  CONSTRAINT ck_dssmp_outbox_status CHECK (status IN ('pending', 'applied', 'rejected_stale', 'rejected_invalid')));

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
-- Append-only guard on the tables the extract job copies (no UPDATE/DELETE/TRUNCATE after insert).
-- Row-level BEFORE UPDATE OR DELETE triggers do not fire on TRUNCATE (PostgreSQL 17 docs, CREATE TRIGGER:
-- "triggers may be defined to fire for TRUNCATE, though only FOR EACH STATEMENT"), so each table also gets a
-- statement-level BEFORE TRUNCATE trigger (QA-045b). Same function; TG_OP names the refused operation.
-- SQLSTATE 'insufficient_privilege' (42501). Idempotent and race-free: CREATE OR REPLACE FUNCTION and
-- CREATE OR REPLACE TRIGGER (QA-045c; replaces the old DROP TRIGGER IF EXISTS + CREATE TRIGGER pair, which left a
-- window with no guard when run outside a transaction). CREATE OR REPLACE TRIGGER needs PostgreSQL >= 14
-- (OR REPLACE is in the PG 14+ CREATE TRIGGER docs, absent from PG 13's); Lakebase's Postgres major version is
-- UNVERIFIED (docs/VERIFICATION.md LB-PG14). OR REPLACE is not supported for constraint triggers (none here).
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION '% is append-only (% refused)', TG_TABLE_NAME, TG_OP
     USING ERRCODE = 'insufficient_privilege'; END $$;
CREATE OR REPLACE TRIGGER t_noupd_exceedance_status BEFORE UPDATE OR DELETE ON exceedance_status
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE OR REPLACE TRIGGER t_noupd_review_signoff BEFORE UPDATE OR DELETE ON review_signoff
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE OR REPLACE TRIGGER t_noupd_manual_reading BEFORE UPDATE OR DELETE ON manual_reading
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE OR REPLACE TRIGGER t_notrunc_exceedance_status BEFORE TRUNCATE ON exceedance_status
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE OR REPLACE TRIGGER t_notrunc_review_signoff BEFORE TRUNCATE ON review_signoff
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE OR REPLACE TRIGGER t_notrunc_manual_reading BEFORE TRUNCATE ON manual_reading
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TABLE IF NOT EXISTS extract_watermark (
  table_name TEXT PRIMARY KEY, last_id BIGINT, last_ts TEXT, updated_at TEXT NOT NULL);

-- ---- v1.7 mirror of the rest of state.py's init (QA-039 v2 chain), so the app never needs DDL ----
-- Same names/types as state.py _init_schema() on an existing v1 table (nullable ADD COLUMN, chain_seq BIGINT).
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS action_type TEXT, ADD COLUMN IF NOT EXISTS reviewer_email TEXT,
  ADD COLUMN IF NOT EXISTS reviewer_sub TEXT, ADD COLUMN IF NOT EXISTS original_value TEXT,
  ADD COLUMN IF NOT EXISTS corrected_value TEXT, ADD COLUMN IF NOT EXISTS chain_id TEXT,
  ADD COLUMN IF NOT EXISTS chain_seq BIGINT, ADD COLUMN IF NOT EXISTS prev_hash TEXT,
  ADD COLUMN IF NOT EXISTS record_hash TEXT, ADD COLUMN IF NOT EXISTS signature TEXT,
  ADD COLUMN IF NOT EXISTS sig_alg TEXT, ADD COLUMN IF NOT EXISTS key_id TEXT;
-- canon v2 (App Dev c797151): same names/types/CHECK as state.py (actor_app_role TEXT, canon_version INTEGER,
-- ck_review_signoff_canon), added here because the app role cannot ALTER the owner-owned table.
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS actor_app_role TEXT,
  ADD COLUMN IF NOT EXISTS canon_version INTEGER;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_review_signoff_canon'
                 AND conrelid = 'review_signoff'::regclass) THEN
    ALTER TABLE review_signoff ADD CONSTRAINT ck_review_signoff_canon CHECK (
      canon_version IS NULL OR (canon_version = 2 AND actor_app_role IS NOT NULL AND length(trim(actor_app_role)) > 0));
  END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS review_signoff_chain_seq ON review_signoff (chain_id, chain_seq);
CREATE TABLE IF NOT EXISTS review_chain_head (
  chain_id TEXT PRIMARY KEY, chain_seq INTEGER NOT NULL, head_hash TEXT NOT NULL, updated_ts TEXT NOT NULL);
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS user_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS app_user_user_id ON app_user (user_id);

-- ---- v1.7 ownership hardening (QA-045b residual risks) ----
-- Risk 1: a table OWNER can ALTER TABLE ... DISABLE TRIGGER (then TRUNCATE/UPDATE/DELETE), drop the trigger, replace
-- the trigger function, or GRANT itself TRUNCATE back; the SCHEMA owner can DROP any table in the schema.
-- Risk 2: a superuser, or a role granted SET on session_replication_role, can SET session_replication_role = replica,
-- under which ordinary (ORIGIN-enabled) triggers do not fire (PG 17 docs: ALTER TABLE ... ENABLE/DISABLE TRIGGER;
-- runtime-config-client session_replication_role).
-- Model (split ownership):
--   * NOLOGIN role dam_lakebase_owner owns the schema dam_app, the append-only tables exceedance_status,
--     review_signoff, manual_reading (their id sequences move with them) and the trigger functions forbid_mutation()
--     and app_forbid_mutation(). Nobody logs in as it; the deployer runs this file with membership in it.
--   * The app service principal's role (SET dam.app_role) gets USAGE + CREATE on the schema, SELECT + INSERT (no
--     UPDATE / DELETE / TRUNCATE / TRIGGER) on the append-only tables and USAGE on their sequences, and OWNS the
--     mutable app tables (app_user, alarm_ack, ..., review_chain_head, and anything state.py creates later), so
--     state.py keeps managing those. It must NOT be superuser, REPLICATION or BYPASSRLS, nor a member of
--     dam_lakebase_owner; this file refuses to finish if it is. state.py must skip DDL on the owner-owned objects:
--     tools/patches/v17_lakebase_owner_state_init.patch (App Dev applies it; not applied here).
--   * Guard triggers are ENABLE ALWAYS, so they fire even under session_replication_role = replica.
--   * The extract job's role (SET dam.extract_role) gets SELECT on all tables + INSERT/UPDATE on extract_watermark.
-- lakebase/validate_append_only.sql (tools/validate_lakebase.py) re-checks all of it after every deploy.
-- Unset role GUCs => that role's grants are skipped (guarded ownership is still moved). Needs CREATEROLE (first
-- run) and the deployer able to act as the old and new owners (PG ALTER ... OWNER rules). UNVERIFIED on Lakebase:
-- the deployer role and its attributes, the app SP role's attributes (docs/VERIFICATION.md LB-OWNER).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dam_lakebase_owner') THEN
    CREATE ROLE dam_lakebase_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dam_lakebase_owner'
             AND (rolcanlogin OR rolsuper OR rolreplication OR rolbypassrls)) THEN
    RAISE EXCEPTION 'dam_lakebase_owner must be NOLOGIN NOSUPERUSER NOREPLICATION NOBYPASSRLS'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
END $$;
ALTER SCHEMA dam_app OWNER TO dam_lakebase_owner;
ALTER TABLE exceedance_status OWNER TO dam_lakebase_owner;
ALTER TABLE review_signoff OWNER TO dam_lakebase_owner;
ALTER TABLE manual_reading OWNER TO dam_lakebase_owner;
ALTER TABLE extract_watermark OWNER TO dam_lakebase_owner;
DO $$
DECLARE o RECORD;
BEGIN
  FOR o IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
           WHERE p.pronamespace = 'dam_app'::regnamespace AND p.proname IN ('forbid_mutation', 'app_forbid_mutation')
             AND p.proowner <> 'dam_lakebase_owner'::regrole LOOP
    EXECUTE format('ALTER ROUTINE %s OWNER TO dam_lakebase_owner', o.fn);
  END LOOP;
END $$;
REVOKE ALL ON SCHEMA dam_app FROM PUBLIC;
-- Guard triggers fire ALWAYS (also under session_replication_role = replica). These tables are never
-- logical-replication targets (SCHEMA_CONTRACT 7.3: no synced tables), so ALWAYS cannot block a replication apply.
ALTER TABLE exceedance_status ENABLE ALWAYS TRIGGER t_noupd_exceedance_status,
  ENABLE ALWAYS TRIGGER t_notrunc_exceedance_status;
ALTER TABLE review_signoff ENABLE ALWAYS TRIGGER t_noupd_review_signoff,
  ENABLE ALWAYS TRIGGER t_notrunc_review_signoff;
ALTER TABLE manual_reading ENABLE ALWAYS TRIGGER t_noupd_manual_reading,
  ENABLE ALWAYS TRIGGER t_notrunc_manual_reading;
DO $$
DECLARE
  r TEXT := nullif(current_setting('dam.app_role', true), '');
  x TEXT := nullif(current_setting('dam.extract_role', true), '');
  bad TEXT;
  o RECORD;
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    SELECT string_agg(a, ', ') INTO bad FROM (
      SELECT 'SUPERUSER' a FROM pg_roles WHERE rolname = r AND rolsuper
      UNION ALL SELECT 'REPLICATION' FROM pg_roles WHERE rolname = r AND rolreplication
      UNION ALL SELECT 'BYPASSRLS' FROM pg_roles WHERE rolname = r AND rolbypassrls) z;
    IF bad IS NOT NULL THEN
      RAISE EXCEPTION 'app role % must not be %', r, bad USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF pg_has_role(r, 'dam_lakebase_owner', 'MEMBER') THEN
      EXECUTE format('REVOKE dam_lakebase_owner FROM %I', r);   -- direct membership only
      IF pg_has_role(r, 'dam_lakebase_owner', 'MEMBER') THEN
        RAISE EXCEPTION 'app role % is an indirect member of dam_lakebase_owner', r
          USING ERRCODE = 'insufficient_privilege';
      END IF;
    END IF;
    -- mutable app tables (everything except the guarded three and extract_watermark) belong to the app
    FOR o IN SELECT c.oid::regclass AS rel FROM pg_class c
             WHERE c.relnamespace = 'dam_app'::regnamespace AND c.relkind IN ('r', 'p')
               AND c.relname NOT IN ('exceedance_status', 'review_signoff', 'manual_reading', 'extract_watermark')
               AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = r) LOOP
      EXECUTE format('ALTER TABLE %s OWNER TO %I', o.rel, r);
    END LOOP;
    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA dam_app TO %I', r);
    EXECUTE format('REVOKE ALL ON exceedance_status, review_signoff, manual_reading FROM %I', r);
    EXECUTE format('REVOKE TRUNCATE ON exceedance_status, review_signoff, manual_reading FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON exceedance_status, review_signoff, manual_reading TO %I', r);
    EXECUTE format('GRANT USAGE ON SEQUENCE exceedance_status_id_seq, manual_reading_id_seq TO %I', r);
  END IF;
  IF x IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = x) THEN
    EXECUTE format('GRANT USAGE ON SCHEMA dam_app TO %I', x);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA dam_app TO %I', x);
    EXECUTE format('GRANT INSERT, UPDATE ON extract_watermark TO %I', x);
  END IF;
END $$;

COMMIT;
