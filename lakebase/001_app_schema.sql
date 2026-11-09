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
-- (same literal INIT_LOCK_KEY in app/backend/repos/state.py). Released automatically at COMMIT/ROLLBACK.
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

-- v1.7 app DSSMP document store (same DDL as state.py; app-owned). The DSSMP load job reads it to copy the approved
-- document to the UC volume ref.dssmp_documents: dam_lakebase_publisher gets SELECT on it (and nothing else).
CREATE TABLE IF NOT EXISTS dssmp_document (
        document_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, filename TEXT NOT NULL, content_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, content_b64 TEXT NOT NULL, uploaded_by TEXT NOT NULL,
        uploaded_at TEXT NOT NULL);

-- v1.7 §7 item 20 (dam CDSE bootstrap / change) and item 21 (bulk re-version): dam_cdse_change and
-- rule_reversion_proposal are app-owned workflow tables; dam_cdse_outbox is a publication outbox (owned by
-- dam_lakebase_owner like the others: app SELECT + INSERT, publisher SELECT + UPDATE of the 4 bookkeeping columns).
-- Migration for an existing database: lakebase/migrations/v1_7_005_cdse_reversion_tables.sql (+ _down.sql).
-- BEGIN v1_7_005 block (app SCHEMA_CONTRACT §7 items 20 / 21; DDL copied verbatim from app/backend/repos/state.py)
CREATE TABLE IF NOT EXISTS dam_cdse_change (
        change_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('assign', 'change')),
        previous_cdse_id TEXT, new_cdse_id TEXT NOT NULL, change_reason_code TEXT NOT NULL,
        rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0), proposed_by TEXT NOT NULL, proposed_by_id TEXT,
        proposed_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_by TEXT, decided_by_id TEXT, decided_at TEXT, decision_rationale TEXT, outbox_id TEXT,
        approval_record_hash TEXT);
CREATE TABLE IF NOT EXISTS dam_cdse_outbox (
        outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, kind TEXT NOT NULL, previous_cdse_id TEXT,
        new_cdse_id TEXT NOT NULL, change_reason_code TEXT NOT NULL, proposed_by TEXT NOT NULL,
        approved_by TEXT NOT NULL, approved_at TEXT NOT NULL, approval_record_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'loaded', 'rejected')),
        reason_code TEXT, message TEXT, processed_at TEXT);
CREATE TABLE IF NOT EXISTS rule_reversion_proposal (
        proposal_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, to_dssmp_version_id TEXT NOT NULL,
        rules TEXT NOT NULL, proposed_by TEXT NOT NULL, proposed_by_id TEXT, proposed_at TEXT NOT NULL,
        rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
        status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
        decided_by TEXT, decided_by_id TEXT, decided_at TEXT, decision_rationale TEXT, pub_ids TEXT);
-- QA-059: ownership is only ever handed to the app for dam_cdse_change (the app's workflow table), and only while it
-- is NOT owned by dam_lakebase_owner; rule_reversion_proposal is never re-owned here (v1_7_010 owns it), so re-running
-- this block (or lakebase/001, or this migration) after a later hardening cannot give a guarded table back to the app.
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) AND NOT EXISTS (
       SELECT 1 FROM pg_class c JOIN pg_roles o ON o.oid = c.relowner
       WHERE c.oid = 'dam_app.dam_cdse_change'::regclass AND o.rolname = 'dam_lakebase_owner') AND NOT EXISTS (
       SELECT 1 FROM pg_trigger tr JOIN pg_proc fp ON fp.oid = tr.tgfoid
       WHERE tr.tgrelid = 'dam_app.dam_cdse_change'::regclass AND NOT tr.tgisinternal
         AND fp.proname IN ('forbid_mutation', 'app_forbid_mutation', 'status_transition_guard')) THEN
    EXECUTE format('ALTER TABLE dam_app.dam_cdse_change OWNER TO %I', r);
  END IF;
END $$;
-- END v1_7_005 block
-- BEGIN v1_7_008 block (app SCHEMA_CONTRACT §7 item 20: dam_cdse_outbox status pending | loaded | rejected; the same
-- 3 values as dssmp_registration_outbox. Replaces every earlier status CHECK on the table (the inline
-- dam_cdse_outbox_status_check of the v1_7_005 CREATE allowed 'rejected_invalid'), maps a legacy 'rejected_invalid' row
-- to 'rejected' (reason_code / message kept), then one named CHECK. Re-run safe.)
DO $$
DECLARE c RECORD;
BEGIN
  IF to_regclass('dam_app.dam_cdse_outbox') IS NULL THEN RETURN; END IF;
  FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'dam_app.dam_cdse_outbox'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) ~ '\mstatus\M' LOOP
    EXECUTE format('ALTER TABLE dam_app.dam_cdse_outbox DROP CONSTRAINT %I', c.conname);
  END LOOP;
  UPDATE dam_app.dam_cdse_outbox SET status = 'rejected' WHERE status = 'rejected_invalid';
  ALTER TABLE dam_app.dam_cdse_outbox ADD CONSTRAINT ck_dam_cdse_outbox_status
    CHECK (status IN ('pending', 'loaded', 'rejected'));
END $$;
-- END v1_7_008 block

-- ---- v1.7 DSSMP registration outbox: the agreed app contract (app SCHEMA_CONTRACT §7 item 19; same columns as
-- app/backend/repos/state.py). One row per CDSE-approved DSSMP version, inserted by the app at 'pending' in the
-- approve transaction; approval_record_hash = record_hash of the chained dssmp_version_approve record. Owned by
-- dam_lakebase_owner below: the app may only INSERT / SELECT; the DSSMP load job (role dam_lakebase_publisher) sets
-- status / reason_code / message / processed_at only. A table in the earlier DA 86993d0 shape is renamed in place by
-- the v1_7_004 block (lakebase/migrations/v1_7_004_dssmp_outbox_contract.sql + _down.sql). ----
CREATE TABLE IF NOT EXISTS dssmp_registration_outbox (outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL,
  dssmp_version_id TEXT NOT NULL, version_no INTEGER NOT NULL, effective_from TEXT NOT NULL, change_reason TEXT,
  source_ref TEXT, document_uri TEXT, document_sha256 TEXT NOT NULL, proposed_by TEXT NOT NULL,
  approved_by TEXT NOT NULL, approved_at TEXT NOT NULL, approval_record_hash TEXT NOT NULL, status TEXT NOT NULL,
  reason_code TEXT, message TEXT, processed_at TEXT,
  CONSTRAINT ck_dssmp_outbox_status CHECK (status IN ('pending', 'loaded', 'rejected')));
-- BEGIN v1_7_004 block
DO $$
DECLARE has_col BOOLEAN;
BEGIN
  IF to_regclass('dam_app.dssmp_registration_outbox') IS NULL THEN RETURN; END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'requested_by')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'proposed_by') THEN
    ALTER TABLE dam_app.dssmp_registration_outbox RENAME COLUMN requested_by TO proposed_by;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'applied_at')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dam_app'
             AND table_name = 'dssmp_registration_outbox' AND column_name = 'processed_at') THEN
    ALTER TABLE dam_app.dssmp_registration_outbox RENAME COLUMN applied_at TO processed_at;
  END IF;
  ALTER TABLE dam_app.dssmp_registration_outbox ADD COLUMN IF NOT EXISTS reason_code TEXT;
  FOR has_col IN SELECT true FROM information_schema.columns WHERE table_schema = 'dam_app'
                 AND table_name = 'dssmp_registration_outbox' AND column_name = 'requested_at' LOOP
    IF EXISTS (SELECT 1 FROM dam_app.dssmp_registration_outbox WHERE requested_at IS NOT NULL) THEN
      RAISE EXCEPTION 'dssmp_registration_outbox.requested_at holds values; not dropped (decide by hand)'
        USING ERRCODE = 'object_in_use';
    END IF;
    ALTER TABLE dam_app.dssmp_registration_outbox DROP COLUMN requested_at;
  END LOOP;
  FOR has_col IN SELECT true FROM information_schema.columns WHERE table_schema = 'dam_app'
                 AND table_name = 'dssmp_registration_outbox' AND column_name = 'supersedes_dssmp_version_id' LOOP
    IF EXISTS (SELECT 1 FROM dam_app.dssmp_registration_outbox WHERE supersedes_dssmp_version_id IS NOT NULL) THEN
      RAISE EXCEPTION 'dssmp_registration_outbox.supersedes_dssmp_version_id holds values; not dropped (decide by hand)'
        USING ERRCODE = 'object_in_use';
    END IF;
    ALTER TABLE dam_app.dssmp_registration_outbox DROP COLUMN supersedes_dssmp_version_id;
  END LOOP;
  ALTER TABLE dam_app.dssmp_registration_outbox ALTER COLUMN status DROP DEFAULT;
  ALTER TABLE dam_app.dssmp_registration_outbox DROP CONSTRAINT IF EXISTS ck_dssmp_outbox_status;
  UPDATE dam_app.dssmp_registration_outbox SET status = 'loaded' WHERE status = 'applied';
  UPDATE dam_app.dssmp_registration_outbox SET status = 'rejected' WHERE status IN ('rejected_stale', 'rejected_invalid');
  ALTER TABLE dam_app.dssmp_registration_outbox ADD CONSTRAINT ck_dssmp_outbox_status
    CHECK (status IN ('pending', 'loaded', 'rejected'));
END $$;
-- END v1_7_004 block

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
-- BEGIN v1_7_006 block (residual (a); identical in lakebase/migrations/v1_7_006_chain_head_log.sql)
-- v1.7 residual (a): tamper-resistant chain head. review_chain_head above is app-owned and UPDATEd in place, so the
-- app role could rewind it. review_chain_head_log is append-only like review_signoff (owner dam_lakebase_owner,
-- guard triggers ENABLE ALWAYS, app SELECT + INSERT only): one row per advance, the current head of a chain is its
-- highest chain_seq, PK (chain_id, chain_seq) forbids two heads at the same position. The patched app
-- (the app head-log change in state.py, applied by App Dev in 478b7b5) appends here instead of updating review_chain_head; the extract
-- takes the highest chain_seq per chain over both tables (transition), preferring this table at equal seq.
CREATE TABLE IF NOT EXISTS review_chain_head_log (
  chain_id TEXT NOT NULL, chain_seq INTEGER NOT NULL CHECK (chain_seq >= 0),
  head_hash TEXT NOT NULL CHECK (head_hash ~ '^[0-9a-f]{64}$'), updated_ts TEXT NOT NULL,
  PRIMARY KEY (chain_id, chain_seq));
-- seed from the legacy head once (idempotent): the deployer, not the app, writes this
INSERT INTO review_chain_head_log (chain_id, chain_seq, head_hash, updated_ts)
  SELECT chain_id, chain_seq, head_hash, updated_ts FROM review_chain_head WHERE head_hash ~ '^[0-9a-f]{64}$'
  ON CONFLICT (chain_id, chain_seq) DO NOTHING;
CREATE OR REPLACE TRIGGER t_noupd_review_chain_head_log BEFORE UPDATE OR DELETE ON review_chain_head_log
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE OR REPLACE TRIGGER t_notrunc_review_chain_head_log BEFORE TRUNCATE ON review_chain_head_log
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
-- END v1_7_006 block
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
--     the app state.py owner-aware init (applied by App Dev in febbe7c / ac14bdf; patch file removed in e413293).
--   * Guard triggers are ENABLE ALWAYS, so they fire even under session_replication_role = replica.
--   * The extract job's role (SET dam.extract_role) gets SELECT on all tables + INSERT/UPDATE on extract_watermark,
--     and SELECT on tables created later by dam_lakebase_owner or the app role (ALTER DEFAULT PRIVILEGES, app
--     SCHEMA_CONTRACT §7 item 17 (c)). It is never widened beyond that.
--   * v1.7 publication outboxes (ref_change_publication, onboarding_publication, dssmp_registration_outbox,
--     dam_cdse_outbox) are
--     owned by dam_lakebase_owner: the app has SELECT + INSERT only. A separate NOLOGIN role dam_lakebase_publisher
--     has USAGE, SELECT and column-level UPDATE (status, message, applied_at; dssmp_registration_outbox and
--     dam_cdse_outbox: status,
--     reason_code, message, processed_at) on exactly those tables, SELECT on dssmp_document, and nothing
--     else (no DELETE / INSERT / TRUNCATE, no default privileges). The dam_ref_publish job's login is made a member
--     via SET dam.publisher_login (it must not be the app role, nor a member of dam_lakebase_owner).
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
ALTER TABLE review_chain_head_log OWNER TO dam_lakebase_owner;
ALTER TABLE extract_watermark OWNER TO dam_lakebase_owner;
ALTER TABLE ref_change_publication OWNER TO dam_lakebase_owner;
ALTER TABLE onboarding_publication OWNER TO dam_lakebase_owner;
ALTER TABLE dssmp_registration_outbox OWNER TO dam_lakebase_owner;
ALTER TABLE dam_cdse_outbox OWNER TO dam_lakebase_owner;
-- BEGIN v1_7_007 block (QA-056 belt and braces on owner-owned tables; names and predicates = app/backend/repos/state.py
-- UNIQUE_GUARDS / PUB_GUARD: the app creates the same indexes on the tables it owns and skips these)
CREATE UNIQUE INDEX IF NOT EXISTS dam_cdse_outbox_one_pending ON dam_cdse_outbox (dam_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS dssmp_outbox_one_pending ON dssmp_registration_outbox (dssmp_version_id)
  WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS ref_change_publication_one_version ON ref_change_publication
  (dam_id, (payload::jsonb ->> 'threshold_id'), (payload::jsonb -> 'insert' ->> 'threshold_version'))
  WHERE kind = 'ref.threshold.new_version' AND status IN ('pending', 'applied');
-- DA (single schema source): the app-owned tables that lakebase/001 also creates carry the app's own guards, same
-- names and predicates as app UNIQUE_GUARDS (the app creates them too, IF NOT EXISTS; dssmp_registration is created by
-- the app only, so its dssmp_registration_one_open stays app-side)
CREATE UNIQUE INDEX IF NOT EXISTS dam_cdse_change_one_pending ON dam_cdse_change (dam_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS rule_reversion_one_pending ON rule_reversion_proposal (dam_id) WHERE status = 'pending';
-- END v1_7_007 block
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dam_lakebase_publisher') THEN
    CREATE ROLE dam_lakebase_publisher NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dam_lakebase_publisher'
             AND (rolcanlogin OR rolsuper OR rolreplication OR rolbypassrls OR rolcreaterole OR rolcreatedb)) THEN
    RAISE EXCEPTION 'dam_lakebase_publisher must be NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA dam_app FROM dam_lakebase_publisher;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA dam_app FROM dam_lakebase_publisher;
GRANT USAGE ON SCHEMA dam_app TO dam_lakebase_publisher;
GRANT SELECT, UPDATE (status, message, applied_at) ON ref_change_publication, onboarding_publication
  TO dam_lakebase_publisher;
GRANT SELECT, UPDATE (status, reason_code, message, processed_at) ON dssmp_registration_outbox, dam_cdse_outbox
  TO dam_lakebase_publisher;
GRANT SELECT ON dssmp_document TO dam_lakebase_publisher;     -- v1.7 (b): document copy to ref.dssmp_documents
DO $$
DECLARE o RECORD;
BEGIN
  FOR o IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
           WHERE p.pronamespace = 'dam_app'::regnamespace AND p.proname IN ('forbid_mutation', 'app_forbid_mutation', 'status_transition_guard')
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
ALTER TABLE review_chain_head_log ENABLE ALWAYS TRIGGER t_noupd_review_chain_head_log,
  ENABLE ALWAYS TRIGGER t_notrunc_review_chain_head_log;
-- BEGIN v1_7_009 block (residual 1: status transitions. On the two publication outboxes and the app's
-- rule_reversion_proposal a row leaves 'pending' once, to one of its terminal states, and never comes back: allowed
-- moves are pending -> loaded | rejected (outboxes) and pending -> applied | rejected (rule_reversion_proposal);
-- a same-status UPDATE is fine on a pending row; a terminal row is frozen (only an UPDATE that changes nothing passes);
-- a legacy value of an older contract may only be normalized to a terminal state (so v1_7_004 / v1_7_008 still run).
-- Function owned by dam_lakebase_owner, triggers ENABLE ALWAYS (fire under session_replication_role = replica too).
-- The outboxes are owner-owned, so neither the app nor the publisher can drop / disable the trigger or replace the
-- function. rule_reversion_proposal: owned by dam_lakebase_owner since v1_7_010 (before that the app role, as owner,
-- could drop or disable its trigger); lakebase/validate_append_only.sql reports a missing / disabled one.)
CREATE OR REPLACE FUNCTION dam_app.status_transition_guard() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    IF OLD.status = 'pending' OR to_jsonb(NEW) IS NOT DISTINCT FROM to_jsonb(OLD) THEN RETURN NEW; END IF;
    RAISE EXCEPTION '%: row is final (status %); it cannot be changed', TG_TABLE_NAME, OLD.status
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.status = 'pending' AND NEW.status = ANY (TG_ARGV) THEN RETURN NEW; END IF;
  -- a LEGACY value (not pending and not a current terminal state, e.g. 'applied' / 'rejected_invalid' left by an
  -- older contract) may only be normalized to a terminal state (the v1_7_004 / v1_7_008 mappings); never to pending
  IF OLD.status <> 'pending' AND NOT (OLD.status = ANY (TG_ARGV)) AND NEW.status = ANY (TG_ARGV) THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION '%: status transition % -> % refused (only pending -> %)', TG_TABLE_NAME, OLD.status,
    NEW.status, array_to_string(TG_ARGV, ' | ') USING ERRCODE = 'check_violation';
END $$;
ALTER FUNCTION dam_app.status_transition_guard() OWNER TO dam_lakebase_owner;
CREATE OR REPLACE TRIGGER t_status_dam_cdse_outbox BEFORE UPDATE ON dam_app.dam_cdse_outbox
  FOR EACH ROW EXECUTE FUNCTION dam_app.status_transition_guard('loaded', 'rejected');
CREATE OR REPLACE TRIGGER t_status_dssmp_registration_outbox BEFORE UPDATE ON dam_app.dssmp_registration_outbox
  FOR EACH ROW EXECUTE FUNCTION dam_app.status_transition_guard('loaded', 'rejected');
CREATE OR REPLACE TRIGGER t_status_rule_reversion_proposal BEFORE UPDATE ON dam_app.rule_reversion_proposal
  FOR EACH ROW EXECUTE FUNCTION dam_app.status_transition_guard('applied', 'rejected');
ALTER TABLE dam_app.dam_cdse_outbox ENABLE ALWAYS TRIGGER t_status_dam_cdse_outbox;
ALTER TABLE dam_app.dssmp_registration_outbox ENABLE ALWAYS TRIGGER t_status_dssmp_registration_outbox;
ALTER TABLE dam_app.rule_reversion_proposal ENABLE ALWAYS TRIGGER t_status_rule_reversion_proposal;
-- END v1_7_009 block
DO $$
DECLARE
  r TEXT := nullif(current_setting('dam.app_role', true), '');
  x TEXT := nullif(current_setting('dam.extract_role', true), '');
  pl TEXT := nullif(current_setting('dam.publisher_login', true), '');
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
               AND c.relname NOT IN ('exceedance_status', 'review_signoff', 'manual_reading', 'extract_watermark',
                                     'review_chain_head_log',
                                     'ref_change_publication', 'onboarding_publication', 'dssmp_registration_outbox',
                                     'dam_cdse_outbox', 'rule_reversion_proposal')
               -- QA-059: never re-own a table that a hardening gave to dam_lakebase_owner or that carries a guard
               -- trigger, even when it is missing from the list above (a later guarded table)
               AND c.relowner <> 'dam_lakebase_owner'::regrole
               AND NOT EXISTS (SELECT 1 FROM pg_trigger tr JOIN pg_proc fp ON fp.oid = tr.tgfoid
                               WHERE tr.tgrelid = c.oid AND NOT tr.tgisinternal
                                 AND fp.proname IN ('forbid_mutation', 'app_forbid_mutation', 'status_transition_guard'))
               AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = r) LOOP
      EXECUTE format('ALTER TABLE %s OWNER TO %I', o.rel, r);
    END LOOP;
    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA dam_app TO %I', r);
    EXECUTE format('REVOKE ALL ON exceedance_status, review_signoff, manual_reading, review_chain_head_log FROM %I', r);
    EXECUTE format('REVOKE TRUNCATE ON exceedance_status, review_signoff, manual_reading, review_chain_head_log FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON exceedance_status, review_signoff, manual_reading, review_chain_head_log TO %I', r);
    EXECUTE format('GRANT USAGE ON SEQUENCE exceedance_status_id_seq, manual_reading_id_seq TO %I', r);
    EXECUTE format('REVOKE ALL ON ref_change_publication, onboarding_publication, dssmp_registration_outbox, '
                   'dam_cdse_outbox FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON ref_change_publication, onboarding_publication, '
                   'dssmp_registration_outbox, dam_cdse_outbox TO %I', r);
    IF pg_has_role(r, 'dam_lakebase_publisher', 'MEMBER') THEN
      EXECUTE format('REVOKE dam_lakebase_publisher FROM %I', r);
    END IF;
  END IF;
  IF x IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = x) THEN
    EXECUTE format('GRANT USAGE ON SCHEMA dam_app TO %I', x);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA dam_app TO %I', x);
    EXECUTE format('GRANT INSERT, UPDATE ON extract_watermark TO %I', x);
    -- tables created later (by the owner, or by the app at startup) stay readable by the extract: SELECT only
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE dam_lakebase_owner IN SCHEMA dam_app GRANT SELECT ON TABLES '
                   'TO %I', x);
    IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA dam_app GRANT SELECT ON TABLES TO %I', r, x);
    END IF;
  END IF;
  IF pl IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = pl) THEN
    IF pl = r OR pg_has_role(pl, 'dam_lakebase_owner', 'MEMBER') THEN
      RAISE EXCEPTION 'publisher login % must be neither the app role nor a member of dam_lakebase_owner', pl
        USING ERRCODE = 'insufficient_privilege';
    END IF;
    EXECUTE format('GRANT dam_lakebase_publisher TO %I', pl);
  END IF;
END $$;

-- BEGIN v1_7_010 block (App Dev follow-up 1: rule_reversion_proposal owned by dam_lakebase_owner, like the
-- outboxes. The app keeps SELECT, INSERT and column UPDATE of exactly the columns its decide_pending writes
-- (app/backend/routers/rule_reversion.py approve / reject -> state.decide_pending): status, decided_by,
-- decided_by_id, decided_at, decision_rationale, pub_ids. status moves stay guarded by t_status_rule_reversion_proposal
-- (v1_7_009, ENABLE ALWAYS); no table-level UPDATE, DELETE, TRUNCATE, REFERENCES or TRIGGER, and as a non-owner the
-- app can no longer ALTER, drop or disable the table's triggers. Refuses to run when the table is owned by an
-- unprivileged role (the app) and dam.app_role is not set, because the app would lose all access.)
DO $$
DECLARE
  r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF (r IS NULL OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)) AND EXISTS (
       SELECT 1 FROM pg_class c JOIN pg_roles o ON o.oid = c.relowner
       WHERE c.oid = 'dam_app.rule_reversion_proposal'::regclass
         AND o.rolname <> 'dam_lakebase_owner' AND NOT o.rolsuper) THEN
    RAISE EXCEPTION 'v1_7_010: SET dam.app_role to the app role first (it owns dam_app.rule_reversion_proposal and would lose access)'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  ALTER TABLE dam_app.rule_reversion_proposal OWNER TO dam_lakebase_owner;
  IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'dam_app.rule_reversion_proposal'::regclass
             AND tgname = 't_status_rule_reversion_proposal') THEN
    ALTER TABLE dam_app.rule_reversion_proposal ENABLE ALWAYS TRIGGER t_status_rule_reversion_proposal;
  END IF;
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE ALL ON dam_app.rule_reversion_proposal FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON dam_app.rule_reversion_proposal TO %I', r);
    EXECUTE format('GRANT UPDATE (status, decided_by, decided_by_id, decided_at, decision_rationale, pub_ids) '
                   'ON dam_app.rule_reversion_proposal TO %I', r);
  END IF;
END $$;
-- END v1_7_010 block
-- BEGIN v1_7_011 block (QA-059: applied-migrations ledger for tools/run_migration.py --lakebase. One row per applied
-- Lakebase migration file (direction up | down | baseline), append-only like the other guarded tables: owned by
-- dam_lakebase_owner, forbid_mutation() row + TRUNCATE triggers ENABLE ALWAYS, no privilege for PUBLIC or the app
-- role (the deployer, a member of dam_lakebase_owner, inserts). The runner refuses a re-run of an applied migration
-- unless --rerun "<reason>" is given, refuses an older migration than the newest applied one the same way, and
-- ALWAYS refuses v1_7_005 once v1_7_010 is applied (flag or not).)
CREATE TABLE IF NOT EXISTS dam_app.schema_migration_ledger (
        ledger_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        migration TEXT NOT NULL CHECK (migration ~ '^v[0-9]+_[0-9]+_[0-9]{3}_[a-z0-9_]+[.]sql$'),
        direction TEXT NOT NULL CHECK (direction IN ('up', 'down', 'baseline')),
        sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        rerun_reason TEXT CHECK (rerun_reason IS NULL OR length(trim(rerun_reason)) >= 10),
        applied_by TEXT NOT NULL DEFAULT session_user,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
ALTER TABLE dam_app.schema_migration_ledger OWNER TO dam_lakebase_owner;
REVOKE ALL ON dam_app.schema_migration_ledger FROM PUBLIC;
CREATE OR REPLACE TRIGGER t_noupd_schema_migration_ledger BEFORE UPDATE OR DELETE ON dam_app.schema_migration_ledger
  FOR EACH ROW EXECUTE FUNCTION dam_app.forbid_mutation();
CREATE OR REPLACE TRIGGER t_notrunc_schema_migration_ledger BEFORE TRUNCATE ON dam_app.schema_migration_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION dam_app.forbid_mutation();
ALTER TABLE dam_app.schema_migration_ledger ENABLE ALWAYS TRIGGER t_noupd_schema_migration_ledger,
  ENABLE ALWAYS TRIGGER t_notrunc_schema_migration_ledger;
DO $$
DECLARE r TEXT := nullif(current_setting('dam.app_role', true), '');
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE ALL ON dam_app.schema_migration_ledger FROM %I', r);
  END IF;
END $$;
-- END v1_7_011 block
-- BEGIN v1_8_001 block (v1.8 dam lifecycle: dam_app.lifecycle_transition_outbox, one row per approved lifecycle
-- transition, inserted by the app in the transaction of its lifecycle_transition_approve chain record. Owned by
-- dam_lakebase_owner like the other outboxes: the app has SELECT + INSERT only; dam_lakebase_publisher SELECT + UPDATE
-- (status, reason_code, message, processed_at); status moves pending -> loaded | rejected only
-- (status_transition_guard, ENABLE ALWAYS); one pending row per dam. Lock order (app contract): the inserting
-- transaction must hold the dam lock pg_advisory_xact_lock(4702047, hashtext('dam_app.dam:' || dam_id)) (taken
-- FIRST, then the check, then the chain lock 4702046); t_lock_lifecycle_transition_outbox refuses the INSERT
-- otherwise. transition_reason_code = the transition's reason (audit.lifecycle_event.reason_code); reason_code /
-- message / processed_at are the load bookkeeping, as on the other outboxes. suspension_reason values are a UC-owned
-- enum (ref.lifecycle_suspension_reason), checked by the loader, not here.)
CREATE TABLE IF NOT EXISTS dam_app.lifecycle_transition_outbox (
        outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL,
        trigger_code TEXT NOT NULL, authorizer_role TEXT NOT NULL, authorizer_user_id TEXT NOT NULL,
        evidence_uri TEXT NOT NULL, ferc_docket_or_order TEXT, effective_ts TEXT NOT NULL, monitoring_rule TEXT,
        notification_checklist TEXT, retention_action TEXT, rationale TEXT, citation_code TEXT NOT NULL,
        proposed_by TEXT NOT NULL, proposed_at TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
        second_approved_by TEXT, proposal_record_hash TEXT NOT NULL, approval_record_hash TEXT NOT NULL,
        dssmp_version_id TEXT, transition_reason_code TEXT, suspension_reason TEXT, incident_report_uri TEXT,
        sworn_verification_12_13 BOOLEAN, sworn_verification_uri TEXT, cdse_basis TEXT, cdse_basis_by TEXT,
        re_directive_uri TEXT, withdrawal_filed_date TEXT, counterpart_dam_id TEXT, created_at TEXT NOT NULL,
        status TEXT NOT NULL, reason_code TEXT, message TEXT, processed_at TEXT,
        CONSTRAINT ck_lifecycle_outbox_status CHECK (status IN ('pending', 'loaded', 'rejected')),
        CONSTRAINT ck_lifecycle_outbox_states CHECK (
          from_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer',
                         'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')
          AND to_state IN ('draft_commissioning', 'live', 'suspended_monitoring_modified', 'pending_transfer',
                           'transferred', 'pending_surrender', 'decommissioned_archived', 'voided_in_error')),
        CONSTRAINT ck_lifecycle_outbox_suspension CHECK (
          (to_state = 'suspended_monitoring_modified') = (suspension_reason IS NOT NULL)),
        CONSTRAINT ck_lifecycle_outbox_evidence CHECK (
          jsonb_typeof(evidence_uri::jsonb) = 'array' AND jsonb_array_length(evidence_uri::jsonb) > 0),
        CONSTRAINT ck_lifecycle_outbox_hashes CHECK (
          approval_record_hash ~ '^[0-9a-f]{64}$' AND proposal_record_hash ~ '^[0-9a-f]{64}$'));
ALTER TABLE dam_app.lifecycle_transition_outbox OWNER TO dam_lakebase_owner;
CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_outbox_one_pending ON dam_app.lifecycle_transition_outbox (dam_id)
  WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_outbox_one_approval ON dam_app.lifecycle_transition_outbox
  (approval_record_hash);
CREATE OR REPLACE FUNCTION dam_app.lifecycle_outbox_lock_guard() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_locks l WHERE l.locktype = 'advisory' AND l.pid = pg_backend_pid() AND l.granted
                   AND l.classid = 4702047::oid AND l.objsubid = 2
                   AND l.objid = ((hashtext('dam_app.dam:' || NEW.dam_id)::bigint) & 4294967295)::oid) THEN
    RAISE EXCEPTION 'lifecycle_transition_outbox: take the dam lock pg_advisory_xact_lock(4702047, hashtext(%)) first',
      quote_literal('dam_app.dam:' || NEW.dam_id) USING ERRCODE = 'lock_not_available';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION dam_app.lifecycle_outbox_lock_guard() OWNER TO dam_lakebase_owner;
CREATE OR REPLACE TRIGGER t_lock_lifecycle_transition_outbox BEFORE INSERT ON dam_app.lifecycle_transition_outbox
  FOR EACH ROW EXECUTE FUNCTION dam_app.lifecycle_outbox_lock_guard();
CREATE OR REPLACE TRIGGER t_status_lifecycle_transition_outbox BEFORE UPDATE ON dam_app.lifecycle_transition_outbox
  FOR EACH ROW EXECUTE FUNCTION dam_app.status_transition_guard('loaded', 'rejected');
ALTER TABLE dam_app.lifecycle_transition_outbox ENABLE ALWAYS TRIGGER t_lock_lifecycle_transition_outbox,
  ENABLE ALWAYS TRIGGER t_status_lifecycle_transition_outbox;
REVOKE ALL ON dam_app.lifecycle_transition_outbox FROM PUBLIC;
REVOKE ALL ON dam_app.lifecycle_transition_outbox FROM dam_lakebase_publisher;
GRANT SELECT, UPDATE (status, reason_code, message, processed_at) ON dam_app.lifecycle_transition_outbox
  TO dam_lakebase_publisher;
DO $$
DECLARE
  r TEXT := nullif(current_setting('dam.app_role', true), '');
  x TEXT := nullif(current_setting('dam.extract_role', true), '');
BEGIN
  IF r IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('REVOKE ALL ON dam_app.lifecycle_transition_outbox FROM %I', r);
    EXECUTE format('GRANT SELECT, INSERT ON dam_app.lifecycle_transition_outbox TO %I', r);
  END IF;
  IF x IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = x) THEN
    EXECUTE format('GRANT SELECT ON dam_app.lifecycle_transition_outbox TO %I', x);
  END IF;
END $$;
-- END v1_8_001 block
COMMIT;
