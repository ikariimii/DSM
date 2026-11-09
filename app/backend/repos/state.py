"""Transactional app state: Lakebase (Postgres) in Databricks, SQLite in demo mode.

Same SQL for both backends: statements are written with %s placeholders and
converted to ? for SQLite. JSON columns are stored as text and decoded here."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

log = logging.getLogger(__name__)

ROLES = ("viewer", "engineer", "dso", "dam_admin")  # ascending privilege
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}
ALARM_STATES = ("open", "acknowledged", "closed")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


DDL = [
    """CREATE TABLE IF NOT EXISTS {s}app_user (
        email TEXT PRIMARY KEY, display_name TEXT, is_admin BOOLEAN NOT NULL DEFAULT FALSE,
        active BOOLEAN NOT NULL DEFAULT TRUE, created_at TEXT NOT NULL, created_by TEXT)""",
    """CREATE TABLE IF NOT EXISTS {s}dam_role (
        email TEXT NOT NULL, dam_id TEXT NOT NULL, role TEXT NOT NULL, granted_by TEXT, granted_at TEXT NOT NULL,
        PRIMARY KEY (email, dam_id))""",
    """CREATE TABLE IF NOT EXISTS {s}alarm_ack (
        event_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, state TEXT NOT NULL,
        acknowledged_by TEXT, acknowledged_at TEXT, closed_by TEXT, closed_at TEXT, close_reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS {s}alarm_comment (
        id {serial}, event_id TEXT NOT NULL, dam_id TEXT NOT NULL, author TEXT NOT NULL,
        created_at TEXT NOT NULL, body TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS {s}onboarding_draft (
        draft_id TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        status TEXT NOT NULL, proposed_dam_id TEXT, data TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS {s}onboarding_publication (
        pub_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, dam_id TEXT NOT NULL, payload TEXT NOT NULL,
        requested_by TEXT NOT NULL, requested_at TEXT NOT NULL, status TEXT NOT NULL, applied_at TEXT, message TEXT)""",
    """CREATE TABLE IF NOT EXISTS {s}report_draft (
        report_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, content_md TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS {s}pfm_review (
        id {serial}, pfm_id TEXT NOT NULL, dam_id TEXT NOT NULL, reviewed_by TEXT NOT NULL,
        reviewed_at TEXT NOT NULL, status TEXT NOT NULL, notes TEXT)""",
    """CREATE TABLE IF NOT EXISTS {s}audit_log (
        id {serial}, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, dam_id TEXT,
        target TEXT, detail TEXT)""",
    # QA-039 v2: hash-chained, append-only record of EVERY reviewer action (bias / data-correction reviews, alarm
    # acknowledge / close). Data-layer table ../lakebase/001_app_schema.sql (column-compatible with UC
    # audit.review_record + chain columns); created here too so the app never depends on DDL order. Insert-only
    # (triggers below). comment = the reviewer's rationale (required, non-empty); original_value / corrected_value =
    # canonical JSON, the original is never overwritten (Ch.14 §14.4.1).
    """CREATE TABLE IF NOT EXISTS {s}review_signoff (
        review_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
        review_level TEXT NOT NULL, decision TEXT NOT NULL, comment TEXT NOT NULL CHECK (length(trim(comment)) > 0),
        reviewer_id TEXT NOT NULL, reviewer_pe_license TEXT, signed_ts TEXT NOT NULL, signature_hash TEXT NOT NULL,
        action_type TEXT NOT NULL, reviewer_email TEXT, reviewer_sub TEXT,
        original_value TEXT NOT NULL, corrected_value TEXT NOT NULL,
        chain_id TEXT NOT NULL, chain_seq INTEGER NOT NULL, prev_hash TEXT NOT NULL, record_hash TEXT NOT NULL,
        signature TEXT, sig_alg TEXT, key_id TEXT, actor_app_role TEXT, canon_version INTEGER,
        CONSTRAINT ck_review_signoff_canon CHECK (canon_version IS NULL OR (canon_version = 2
            AND actor_app_role IS NOT NULL AND length(trim(actor_app_role)) > 0)))""",
    # Live-dam alarm-rule flag edits (cdse_ack_required): proposals (workflow state; the chained review_signoff
    # records are the evidence) and the outbox the DA publish job turns into a NEW ref.threshold version.
    """CREATE TABLE IF NOT EXISTS {s}alarm_rule_flag_proposal (
        proposal_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, threshold_id TEXT NOT NULL,
        base_threshold_version INTEGER NOT NULL, base_flag BOOLEAN NOT NULL, new_flag BOOLEAN NOT NULL,
        proposed_by TEXT NOT NULL, proposed_by_id TEXT, proposed_at TEXT NOT NULL,
        rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0), status TEXT NOT NULL,
        decided_by TEXT, decided_by_id TEXT, decided_at TEXT, decision_rationale TEXT, pub_id TEXT,
        new_threshold_version INTEGER)""",
    """CREATE TABLE IF NOT EXISTS {s}ref_change_publication (
        pub_id TEXT PRIMARY KEY, kind TEXT NOT NULL, dam_id TEXT NOT NULL, proposal_id TEXT, payload TEXT NOT NULL,
        requested_by TEXT NOT NULL, requested_at TEXT NOT NULL, status TEXT NOT NULL, applied_at TEXT, message TEXT)""",
    # v1.7 DSSMP version registration (SCHEMA_CONTRACT §7 item 19). Workflow state (the chained dssmp_version_* records
    # are the evidence), uploaded documents (bytes as base64 text + server-computed sha256), and the outbox the DA's
    # load job turns into a ref.dssmp_version row. The app NEVER writes ref.dssmp_version.
    """CREATE TABLE IF NOT EXISTS {s}dssmp_registration (
        registration_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, dssmp_version_id TEXT NOT NULL,
        version_no INTEGER NOT NULL, effective_from TEXT NOT NULL, change_reason TEXT NOT NULL, source_ref TEXT NOT NULL,
        document_uri TEXT NOT NULL, document_sha256 TEXT, document_id TEXT,
        proposed_by TEXT NOT NULL, proposed_by_id TEXT, proposed_at TEXT NOT NULL,
        rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
        status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_by TEXT, decided_by_id TEXT, decided_at TEXT, decision_rationale TEXT, outbox_id TEXT,
        approval_record_hash TEXT)""",
    """CREATE TABLE IF NOT EXISTS {s}dssmp_document (
        document_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, filename TEXT NOT NULL, content_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, content_b64 TEXT NOT NULL, uploaded_by TEXT NOT NULL,
        uploaded_at TEXT NOT NULL)""",
    # DA contract (fixed): the app INSERTs at 'pending' only after CDSE approval; only the DA publisher role sets
    # status / reason_code / message / processed_at. Contract (§7 item 19): status pending|loaded|rejected
    # (OUTBOX_STATUSES; DA 6709371 dropped 'rejected_invalid' in lakebase/001 + v1_7_004) and document_sha256 NOT NULL.
    # An EXISTING table keeps whatever CHECK it was created with (CREATE TABLE IF NOT EXISTS); the app never ALTERs it
    # (owner tables: the DA's v1_7_004 / v1_7_008 converge them) - _legacy_outbox_check() only reports an old CHECK.
    """CREATE TABLE IF NOT EXISTS {s}dssmp_registration_outbox (
        outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, dssmp_version_id TEXT NOT NULL, version_no INTEGER NOT NULL,
        effective_from TEXT NOT NULL, change_reason TEXT, source_ref TEXT, document_uri TEXT, document_sha256 TEXT NOT NULL,
        proposed_by TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
        approval_record_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'loaded', 'rejected')),
        reason_code TEXT, message TEXT, processed_at TEXT)""",
    # v1.7 dam CDSE bootstrap / change (SCHEMA_CONTRACT §7 item 20). Workflow state (chained dam_cdse_* records are the
    # evidence) and the outbox the DA's load job turns into ref.dam.cdse_id. The app NEVER writes ref.dam.
    """CREATE TABLE IF NOT EXISTS {s}dam_cdse_change (
        change_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('assign', 'change')),
        previous_cdse_id TEXT, new_cdse_id TEXT NOT NULL, change_reason_code TEXT NOT NULL,
        rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0), proposed_by TEXT NOT NULL, proposed_by_id TEXT,
        proposed_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_by TEXT, decided_by_id TEXT, decided_at TEXT, decision_rationale TEXT, outbox_id TEXT,
        approval_record_hash TEXT)""",
    """CREATE TABLE IF NOT EXISTS {s}dam_cdse_outbox (
        outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, kind TEXT NOT NULL, previous_cdse_id TEXT,
        new_cdse_id TEXT NOT NULL, change_reason_code TEXT NOT NULL, proposed_by TEXT NOT NULL,
        approved_by TEXT NOT NULL, approved_at TEXT NOT NULL, approval_record_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'loaded', 'rejected')),
        reason_code TEXT, message TEXT, processed_at TEXT)""",
    # v1.7 bulk re-version of the rules set under a superseded DSSMP version (SCHEMA_CONTRACT §7 item 21).
    """CREATE TABLE IF NOT EXISTS {s}rule_reversion_proposal (
        proposal_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, to_dssmp_version_id TEXT NOT NULL,
        rules TEXT NOT NULL, proposed_by TEXT NOT NULL, proposed_by_id TEXT, proposed_at TEXT NOT NULL,
        rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
        status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
        decided_by TEXT, decided_by_id TEXT, decided_at TEXT, decision_rationale TEXT, pub_ids TEXT)""",
    # chain head: the serialization point for appends (row lock / sqlite write lock) and the tail anchor that makes a
    # deleted LAST record detectable. Updated only inside the append transaction.
    """CREATE TABLE IF NOT EXISTS {s}review_chain_head (
        chain_id TEXT PRIMARY KEY, chain_seq INTEGER NOT NULL, head_hash TEXT NOT NULL, updated_ts TEXT NOT NULL)""",
    # v1.7 (DA residual a, lakebase/001): append-only head log. One row per head advance; current head = highest
    # chain_seq. On Lakebase it is owned by dam_lakebase_owner (app SELECT + INSERT only), so the app role cannot
    # rewind or delete a head. review_chain_head above is still written (compatibility) but no longer trusted alone.
    """CREATE TABLE IF NOT EXISTS {s}review_chain_head_log (
        chain_id TEXT NOT NULL, chain_seq INTEGER NOT NULL CHECK (chain_seq >= 0), head_hash TEXT NOT NULL,
        updated_ts TEXT NOT NULL, PRIMARY KEY (chain_id, chain_seq))""",
]

APPEND_ONLY = ("review_signoff", "review_chain_head_log")
# per-chain append lock (two-key advisory lock: class key + hashtext(chain_id)); replaces SELECT ... FOR UPDATE on
# review_chain_head, which needs UPDATE privilege the app no longer has on the head log
CHAIN_LOCK_CLASS = 4702046
# Outbox rows are immutable except the publisher's bookkeeping columns (DA contract); DELETE / TRUNCATE refused.
OUTBOX_GUARDED = ("dssmp_registration_outbox", "dam_cdse_outbox")
OUTBOX_MUTABLE_COLS = ("status", "reason_code", "message", "processed_at")
# QA-045c: transaction-scoped advisory lock key shared with ../lakebase/001_app_schema.sql, so app inits and the
# data-layer script serialize against each other and no session ever sees an append-only table without its guard.
INIT_LOCK_KEY = 4702045
# QA-056: per-dam transaction-scoped advisory lock taken FIRST in every chained write (sg.append ->
# append_chained(lock_key=dam_id)), before the state re-checks and before the chain head row lock. Two-key form
# pg_advisory_xact_lock(DAM_LOCK_NS, hashtext('dam_app.dam:' || dam_id)): a separate advisory keyspace from the
# single-bigint INIT_LOCK_KEY, so it can never collide with the init lock. Lock order everywhere: dam -> chain head.
DAM_LOCK_NS = 4702047  # != INIT_LOCK_KEY 4702045 and != the DA chain-lock class 4702046 (v17_chain_head_log_state.patch)
DAM_LOCK_PREFIX = "dam_app.dam:"
# §7 items 19/20: the only outbox statuses the app treats as meaningful. Anything else is shown as not a contract
# status and handled fail closed: never as loaded, never as a refused (closed) row.
OUTBOX_STATUSES = ("pending", "loaded", "rejected")
# (table, the DA migration that converges an older status CHECK on it) - reported by _legacy_outbox_check, never altered
LEGACY_OUTBOX_MIGRATIONS = (("dssmp_registration_outbox", "v1_7_004_dssmp_outbox_contract.sql"),
                            ("dam_cdse_outbox", "v1_7_008_cdse_outbox_status.sql"))
PUB_KIND_THRESHOLD = "ref.threshold.new_version"
ACTIVE_PUB_STATUSES = ("pending", "applied")
# QA-056 belt and braces: at most one open row per key, enforced by the database. Created by the app only on tables
# it owns (sqlite / app-provisioned Lakebase); on tables owned by dam_lakebase_owner (the outboxes and
# ref_change_publication in lakebase/001) the DA adds the same indexes (tools/patches/qa056_unique_guards.patch).
# A unique violation inside a chained write maps to 409 concurrent_write (routers/signoffs.append).
UNIQUE_GUARDS = (
    ("dam_cdse_change_one_pending", "dam_cdse_change", "(dam_id) WHERE status = 'pending'"),
    ("dam_cdse_outbox_one_pending", "dam_cdse_outbox", "(dam_id) WHERE status = 'pending'"),
    ("dssmp_registration_one_open", "dssmp_registration",
     "(dssmp_version_id) WHERE status IN ('pending', 'approved')"),
    ("dssmp_outbox_one_pending", "dssmp_registration_outbox", "(dssmp_version_id) WHERE status = 'pending'"),
    ("rule_reversion_one_pending", "rule_reversion_proposal", "(dam_id) WHERE status = 'pending'"),
)
# one active (pending or applied) N+1 publication per rule version; dialect-specific JSON expressions
PUB_GUARD = ("ref_change_publication_one_version", "ref_change_publication", {
    "sqlite": "(dam_id, json_extract(payload, '$.threshold_id'), json_extract(payload, '$.insert.threshold_version'))",
    "pg": "(dam_id, (payload::jsonb ->> 'threshold_id'), (payload::jsonb -> 'insert' ->> 'threshold_version'))"},
    f"kind = '{PUB_KIND_THRESHOLD}' AND status IN ('pending', 'applied')")


def is_unique_violation(e: BaseException) -> bool:
    """sqlite3 UNIQUE IntegrityError or Postgres SQLSTATE 23505 (psycopg UniqueViolation)."""
    if isinstance(e, sqlite3.IntegrityError):
        return "UNIQUE" in str(e).upper()
    return getattr(e, "sqlstate", None) == "23505" or getattr(getattr(e, "diag", None), "sqlstate", None) == "23505"
# columns the v2 chain adds to a review_signoff table created earlier by lakebase/001 (v1 columns)
SIGNOFF_V2_COLS = ("action_type", "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id",
                   "chain_seq", "prev_hash", "record_hash", "signature", "sig_alg", "key_id", "actor_app_role",
                   "canon_version")
# canon 2 (audit_chain.CANON_VERSION): actor_app_role / canon_version, NULL on pre-v2 (canon-1) records.
_INT_SIGNOFF_COLS = ("chain_seq", "canon_version")


class StateStore:
    """Thin DB-API wrapper. `kind` is 'sqlite' or 'postgres'.

    Postgres uses a connection pool (`pg_pool`, psycopg_pool.ConnectionPool in Databricks; see
    `lakebase_pool`). Each statement borrows a connection; connection errors (scale-to-zero wake-up, HA
    failover drops every open connection) are retried with backoff on a fresh connection."""

    def __init__(self, kind: str, schema: str = "", sqlite_path: str = ":memory:", pg_pool=None,
                 retries: int | None = None):
        self.kind = kind
        self._lock = threading.RLock()
        self._pool = pg_pool
        self._conn = None
        self._tx_conn = None    # QA-045c: pinned connection while init_schema runs in one transaction
        self._tx_owner = None
        self.legacy_outbox_checks: list[str] = []   # set by init_schema (_legacy_outbox_check)
        self.retries = int(os.getenv("PG_QUERY_RETRIES", "4")) if retries is None else retries
        if kind == "sqlite":
            self.prefix = ""
            self._conn = sqlite3.connect(sqlite_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        else:
            if pg_pool is None:
                raise ValueError("postgres StateStore needs pg_pool")
            self.prefix = f'"{schema}".' if schema else ""
            self.schema = schema
        self.init_schema()

    # ------------------------------------------------------------ plumbing
    def execute(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        sql = sql.replace("{s}", self.prefix)
        if self.kind == "sqlite":
            with self._lock:
                cur = self._conn.execute(sql.replace("%s", "?"), tuple(params))
                rows = [dict(r) for r in cur.fetchall()] if cur.description else []
                self._conn.commit()
                return rows
        if self._tx_conn is not None and self._tx_owner == threading.get_ident():
            with self._tx_conn.cursor() as cur:  # inside _pg_init_tx: same connection, same transaction, no retry
                cur.execute(sql, tuple(params))
                rows = cur.fetchall() if cur.description else []
            return [dict(r) for r in rows]
        for attempt in range(self.retries):
            try:
                # the pool commits on clean exit and rolls back on error
                with self._pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(sql, tuple(params))
                    rows = cur.fetchall() if cur.description else []
                return [dict(r) for r in rows]
            except Exception as e:  # noqa: BLE001
                if attempt == self.retries - 1 or not _is_connection_error(e):
                    raise
                log.warning("Lakebase connection error (%s); retry %d/%d", type(e).__name__, attempt + 1,
                            self.retries - 1)
                time.sleep(min(0.25 * (2 ** attempt), 4.0))
        return []  # pragma: no cover - loop always returns or raises

    @contextlib.contextmanager
    def _pg_init_tx(self):
        """QA-045c: run the whole Postgres init in ONE transaction on one pooled connection, after taking the shared
        transaction-scoped advisory lock (INIT_LOCK_KEY). Concurrent inits (several app replicas, or the data-layer
        script) serialize; DDL is transactional in PostgreSQL, so other sessions see the old or the new catalog,
        never a table without its guard. The pool commits on clean exit and rolls back on error; the lock is
        released at COMMIT/ROLLBACK."""
        with self._pool.connection() as conn:
            if getattr(conn, "autocommit", False) is True:  # review: the xact lock + atomic DDL need a transaction
                raise RuntimeError("init_schema needs a non-autocommit connection (QA-045c)")
            self._tx_conn, self._tx_owner = conn, threading.get_ident()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (INIT_LOCK_KEY,))
                yield
            finally:
                self._tx_conn = self._tx_owner = None

    def init_schema(self) -> None:
        if self.kind == "sqlite":
            return self._init_schema()
        for attempt in range(max(1, self.retries)):   # review: retries=0 must still init (once, no retry)
            try:
                with self._pg_init_tx():
                    return self._init_schema()
            except Exception as e:  # noqa: BLE001
                if attempt >= self.retries - 1 or not _is_connection_error(e):
                    raise
                log.warning("Lakebase connection error during init (%s); retry %d/%d", type(e).__name__,
                            attempt + 1, self.retries - 1)
                time.sleep(min(0.25 * (2 ** attempt), 4.0))

    def _pg_not_owned(self, table: str | None = None) -> bool:
        """v1.7 Lakebase split ownership (DA lakebase/001_app_schema.sql): True if the schema (table=None) or the table
        exists and is owned by a role the current user is not a member of (dam_lakebase_owner). DDL on such objects
        fails with 'must be owner', so init verifies them instead of altering them."""
        if self.kind == "sqlite":
            return False
        if table is None:
            rows = self.execute("SELECT NOT pg_has_role(current_user, nspowner, 'MEMBER') AS foreign_owner "
                                "FROM pg_namespace WHERE nspname = %s", (self.schema or "public",))
        else:
            rows = self.execute("SELECT NOT pg_has_role(current_user, c.relowner, 'MEMBER') AS foreign_owner "
                                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                "WHERE n.nspname = %s AND c.relname = %s", (self.schema or "public", table))
        return bool(rows) and rows[0].get("foreign_owner") is True

    def _verify_provisioned(self, t: str) -> None:
        """Fail closed when an owner-owned append-only table lacks what the app needs (columns, chain index, enabled
        guard triggers): the DA script lakebase/001 must be re-run; the app may not alter it."""
        s = self.schema or "public"
        have = {r["column_name"] for r in self.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s", (s, t))}
        missing = [c for c in SIGNOFF_V2_COLS if c not in have] if t == "review_signoff" else []
        trg = {r["tgname"]: r["tgenabled"] for r in self.execute(
            "SELECT tgname, tgenabled::text AS tgenabled FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = "
            "(SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s)", (s, t))}
        enabled = [n for n, e in trg.items() if e in ("O", "A")]
        row_ok = any(n in enabled for n in (f"t_noupd_{t}", f"t_app_noupd_{t}"))
        trunc_ok = any(n in enabled for n in (f"t_notrunc_{t}", f"t_app_notrunc_{t}"))
        disabled = sorted(n for n, e in trg.items() if e == "D")
        idx = bool(self.execute("SELECT 1 AS x FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
                                (s, "review_signoff_chain_seq"))) if t == "review_signoff" else True
        # App Dev (integration of the DA patch): the canon-2 CHECK must exist too (validated, not NOT VALID), else a
        # canon-2 row without actor_app_role could be stored on Lakebase while the app's own DDL would refuse it.
        canon = bool(self.execute(
            "SELECT 1 AS x FROM pg_constraint co JOIN pg_class c ON c.oid = co.conrelid JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s AND co.conname = %s "
            "AND co.contype = 'c' AND co.convalidated", (s, t, "ck_review_signoff_canon"))) \
            if t == "review_signoff" else True
        if missing or not row_ok or not trunc_ok or disabled or not idx or not canon:
            raise RuntimeError(f"Lakebase {s}.{t} is owned by the data-layer owner role but is not provisioned as the "
                               f"app needs (missing columns {missing}, row guard {row_ok}, truncate guard {trunc_ok}, "
                               f"disabled triggers {disabled}, chain index {idx}, canon CHECK "
                               f"ck_review_signoff_canon {canon}); re-run lakebase/001_app_schema.sql")

    def _unique_guards(self) -> None:
        """QA-056: the unique partial indexes on app-owned tables (skipped, with a log line, where the table is owned
        by dam_lakebase_owner: the DA creates them there). Existing duplicates (pre-fix races) are logged and left to
        an operator; the in-transaction checks under the dam lock still refuse new ones."""
        dialect = "sqlite" if self.kind == "sqlite" else "pg"
        guards = [(n, t, c) for n, t, c in UNIQUE_GUARDS]
        name, table, exprs, where = PUB_GUARD
        guards.append((name, table, f"{exprs[dialect]} WHERE {where}"))
        for name, table, cols in guards:
            if self._pg_not_owned(table):
                log.info("unique guard %s skipped: %s is owned by the data-layer owner role (DA creates it)", name, table)
                continue
            try:
                self.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {{s}}{table} {cols}")
            except Exception as e:  # noqa: BLE001 - duplicates from before the fix: log, keep serving
                if not is_unique_violation(e):
                    raise
                log.error("unique guard %s NOT created on %s: existing duplicate rows (%s)", name, table, e)

    def lock_dam(self, q, dam_id: str) -> None:
        """QA-056: the per-dam transaction lock (Postgres advisory, released at commit/rollback). sqlite: BEGIN
        IMMEDIATE (plus the store's RLock) already serializes every write transaction, so nothing to take."""
        if self.kind != "sqlite":
            q("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (DAM_LOCK_NS, DAM_LOCK_PREFIX + str(dam_id)))

    @staticmethod
    def newer_publications(q, dam_id: str, threshold_id: str, base_version: int) -> list[int]:
        """Versions above `base_version` already queued or published (pending / applied) for this rule, read on the
        write transaction's connection (QA-056: re-checked under the dam lock)."""
        rows = q("SELECT payload FROM {s}ref_change_publication WHERE dam_id = %s AND kind = %s AND status IN "
                 "('pending', 'applied')", (dam_id, PUB_KIND_THRESHOLD))
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            v = (p.get("insert") or {}).get("threshold_version")
            if p.get("threshold_id") == threshold_id and v is not None and int(v) > int(base_version):
                out.append(int(v))
        return sorted(out)

    def _init_schema(self) -> None:
        serial = "INTEGER PRIMARY KEY AUTOINCREMENT" if self.kind == "sqlite" else "BIGSERIAL PRIMARY KEY"
        if self.kind != "sqlite" and self.schema and not self._pg_not_owned():
            self.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
        for ddl in DDL:
            self.execute(ddl.replace("{serial}", serial))
        # Stable identity (X-Forwarded-User) is bound to the admin-granted email row on first sign-in.
        if self.kind == "sqlite":
            cols = {r["name"] for r in self.execute("PRAGMA table_info(app_user)")}
            if "user_id" not in cols:
                self.execute("ALTER TABLE app_user ADD COLUMN user_id TEXT")
        else:
            self.execute("ALTER TABLE {s}app_user ADD COLUMN IF NOT EXISTS user_id TEXT")
        self.execute("CREATE UNIQUE INDEX IF NOT EXISTS app_user_user_id ON {s}app_user (user_id)")
        self._outbox_guards()
        self._legacy_outbox_check()
        self._unique_guards()
        if self.kind == "sqlite":
            have = {r["name"] for r in self.execute("PRAGMA table_info(review_signoff)")}
            for col in SIGNOFF_V2_COLS:
                if col not in have:  # pragma: no cover - only a pre-v2 local sqlite file
                    self.execute(f"ALTER TABLE review_signoff ADD COLUMN {col} {'INTEGER' if col in _INT_SIGNOFF_COLS else 'TEXT'}")
        elif self._pg_not_owned("review_signoff"):
            # v1.7: review_signoff belongs to dam_lakebase_owner (DA lakebase/001 adds the v2 + canon-2 columns, the
            # canon CHECK, the chain index and ALWAYS-enabled guards); the app verifies it and never runs DDL on it.
            # Nothing below this branch applies to an owner-owned table (index + guards are review_signoff only).
            for t in APPEND_ONLY:
                self._verify_provisioned(t)
            return
        else:
            for col in SIGNOFF_V2_COLS:
                self.execute(f"ALTER TABLE {{s}}review_signoff ADD COLUMN IF NOT EXISTS {col} "
                             f"{'BIGINT' if col == 'chain_seq' else 'INTEGER' if col == 'canon_version' else 'TEXT'}")
            # canon-2 shape check on a pre-existing table (CREATE TABLE above carries it for new ones).
            if not self.execute("SELECT 1 FROM pg_constraint WHERE conname = 'ck_review_signoff_canon' "
                                "AND conrelid = to_regclass(%s)", (f'"{self.schema}".review_signoff' if self.schema
                                                                   else "review_signoff",)):
                self.execute("ALTER TABLE {s}review_signoff ADD CONSTRAINT ck_review_signoff_canon CHECK "
                             "(canon_version IS NULL OR (canon_version = 2 AND actor_app_role IS NOT NULL "
                             "AND length(trim(actor_app_role)) > 0))")
        self.execute("CREATE UNIQUE INDEX IF NOT EXISTS review_signoff_chain_seq ON {s}review_signoff (chain_id, chain_seq)")
        self._append_only_guards()

    def _append_only_guards(self) -> None:
        """UPDATE/DELETE (QA-039) and TRUNCATE (QA-045b) on append-only tables fail in the database, not in app code."""
        for t in APPEND_ONLY:
            if self.kind == "sqlite":
                for op in ("UPDATE", "DELETE"):
                    self.execute(f"CREATE TRIGGER IF NOT EXISTS t_no{op.lower()}_{t} BEFORE {op} ON {t} "
                                 f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")
            else:
                # app-owned names, so the data layer's forbid_mutation()/t_noupd_* (lakebase/001) are never replaced;
                # no '%' in the body (params are always passed, so psycopg would parse it as a placeholder)
                self.execute("CREATE OR REPLACE FUNCTION {s}app_forbid_mutation() RETURNS trigger LANGUAGE plpgsql "
                             "AS $$ BEGIN RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || ' is append-only'; END $$")
                # QA-045c: CREATE OR REPLACE TRIGGER (PostgreSQL >= 14; Lakebase version UNVERIFIED) instead of
                # DROP + CREATE, and the whole init is one locked transaction (_pg_init_tx): no unguarded window.
                self.execute(f"CREATE OR REPLACE TRIGGER t_app_noupd_{t} BEFORE UPDATE OR DELETE ON {{s}}{t} "
                             "FOR EACH ROW EXECUTE FUNCTION {s}app_forbid_mutation()")
                # QA-045b: row triggers do not fire on TRUNCATE; PostgreSQL allows TRUNCATE triggers only
                # FOR EACH STATEMENT (PG 17 docs, CREATE TRIGGER). sqlite has no TRUNCATE.
                self.execute(f"CREATE OR REPLACE TRIGGER t_app_notrunc_{t} BEFORE TRUNCATE ON {{s}}{t} "
                             "FOR EACH STATEMENT EXECUTE FUNCTION {s}app_forbid_mutation()")

    def _legacy_outbox_check(self) -> list[str]:
        """Read-only: report (WARNING + self.legacy_outbox_checks) an outbox whose status CHECK still allows values
        outside OUTBOX_STATUSES (the pre-6709371 / pre-d6d3c9c 4-value CHECK with 'rejected_invalid'). The app never
        ALTERs it. On Lakebase the tables are dam_lakebase_owner's and the DA's migration converges them (old rows ->
        'rejected', reason kept): v1_7_004 for dssmp_registration_outbox, v1_7_008 for dam_cdse_outbox. On an
        app-owned dev/demo table the old CHECK is a superset of what the app inserts ('pending' only). Either way the
        app keeps working. Returns ["<table>:<value>", ...] for the extra values found."""
        found = []
        for t, mig in LEGACY_OUTBOX_MIGRATIONS:
            if self.kind == "sqlite":
                rows = self.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = %s", (t,))
                m = re.search(r"status\s+TEXT[^,]*?CHECK\s*\(\s*status\s+IN\s*\(([^()]*)\)",
                              (rows[0]["sql"] or "") if rows else "")
                text = m.group(1) if m else ""
            else:
                rows = self.execute(
                    "SELECT pg_get_constraintdef(co.oid) AS def FROM pg_constraint co JOIN pg_class c "
                    "ON c.oid = co.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s "
                    "AND c.relname = %s AND co.contype = 'c'", (self.schema or "public", t))
                defs = [str((r or {}).get("def") or "") for r in rows or []]
                text = " ".join(d for d in defs if "status" in d)
            extra = sorted(set(re.findall(r"'(\w+)'", text)) - set(OUTBOX_STATUSES))
            if extra:
                owner = "owned by the data-layer owner role" if self._pg_not_owned(t) else "app-owned"
                log.warning("%s (%s): status CHECK still allows %s. The app inserts only 'pending', reads only %s and "
                            "does NOT alter the table: run the DA's lakebase/migrations/%s (or re-run lakebase/001) "
                            "to converge it.", t, owner, extra, "|".join(OUTBOX_STATUSES), mig)
                found += [f"{t}:{v}" for v in extra]
        self.legacy_outbox_checks = found
        return found

    def _outbox_guards(self) -> None:
        """DSSMP outbox (DA contract): rows are immutable except the publisher's bookkeeping columns, and never deleted
        or truncated. Enforced in the database for every role (the app included). If the DA takes ownership of the
        table (owner model) its guards are the DA's to provision; the app then runs no DDL on it."""
        keep = OUTBOX_MUTABLE_COLS
        for t in OUTBOX_GUARDED:
            if self.kind == "sqlite":
                cols = [r["name"] for r in self.execute(f"PRAGMA table_info({t})") if r["name"] not in keep]
                self.execute(f"CREATE TRIGGER IF NOT EXISTS t_noupd_{t} BEFORE UPDATE OF {', '.join(cols)} ON {t} "
                             f"BEGIN SELECT RAISE(ABORT, '{t}: only status/reason_code/message/processed_at change'); END")
                self.execute(f"CREATE TRIGGER IF NOT EXISTS t_nodelete_{t} BEFORE DELETE ON {t} "
                             f"BEGIN SELECT RAISE(ABORT, '{t} rows are never deleted'); END")
                continue
            if self._pg_not_owned(t):
                continue
            arr = "ARRAY[" + ", ".join(f"'{c}'" for c in keep) + "]"
            self.execute("CREATE OR REPLACE FUNCTION {s}app_outbox_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                         "IF TG_OP IN ('DELETE', 'TRUNCATE') THEN "
                         "RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || ' rows are never deleted'; END IF; "
                         f"IF (to_jsonb(NEW) - {arr}) IS DISTINCT FROM (to_jsonb(OLD) - {arr}) THEN "
                         "RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || "
                         "': only status/reason_code/message/processed_at change'; END IF; RETURN NEW; END $$")
            self.execute(f"CREATE OR REPLACE TRIGGER t_app_outbox_row_{t} BEFORE UPDATE OR DELETE ON {{s}}{t} "
                         "FOR EACH ROW EXECUTE FUNCTION {s}app_outbox_guard()")
            self.execute(f"CREATE OR REPLACE TRIGGER t_app_outbox_trunc_{t} BEFORE TRUNCATE ON {{s}}{t} "
                         "FOR EACH STATEMENT EXECUTE FUNCTION {s}app_outbox_guard()")

    # ------------------------------------------------------------ DSSMP version registration (v1.7)
    def insert_dssmp_document(self, row: dict) -> None:
        cols = list(row)
        self.execute(f"INSERT INTO {{s}}dssmp_document ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                     tuple(row[c] for c in cols))

    def dssmp_document(self, document_id: str, content: bool = False) -> dict | None:
        sel = "*" if content else ("document_id, dam_id, filename, content_type, size_bytes, sha256, uploaded_by, "
                                   "uploaded_at")
        rows = self.execute(f"SELECT {sel} FROM {{s}}dssmp_document WHERE document_id = %s", (document_id,))
        return dict(rows[0]) if rows else None

    @staticmethod
    def insert_row(q, table: str, row: dict) -> None:
        cols = list(row)
        q(f"INSERT INTO {{s}}{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
          tuple(row[c] for c in cols))

    def insert_dssmp_registration(self, q, row: dict) -> None:
        self.insert_row(q, "dssmp_registration", row)

    def dssmp_registration(self, registration_id: str) -> dict | None:
        rows = self.execute("SELECT * FROM {s}dssmp_registration WHERE registration_id = %s", (registration_id,))
        return dict(rows[0]) if rows else None

    def dssmp_registrations(self, dam_id: str | None = None, status: str | None = None) -> list[dict]:
        where, args = [], []
        if dam_id is not None:
            where.append("dam_id = %s"); args.append(dam_id)
        if status is not None:
            where.append("status = %s"); args.append(status)
        sql = "SELECT * FROM {s}dssmp_registration" + (" WHERE " + " AND ".join(where) if where else "")
        return [dict(r) for r in self.execute(sql + " ORDER BY proposed_at DESC, registration_id", tuple(args))]

    @staticmethod
    def decide_dssmp_registration(q, registration_id: str, status: str, by: str, by_id: str | None, at: str,
                                  rationale: str, outbox_id: str | None = None,
                                  approval_record_hash: str | None = None) -> None:
        n = q("UPDATE {s}dssmp_registration SET status = %s, decided_by = %s, decided_by_id = %s, decided_at = %s, "
              "decision_rationale = %s, outbox_id = %s, approval_record_hash = %s "
              "WHERE registration_id = %s AND status = 'pending' RETURNING registration_id",
              (status, by, by_id, at, rationale, outbox_id, approval_record_hash, registration_id))
        if not n:  # decided concurrently: abort the whole transaction (chain record included)
            raise RuntimeError("registration is no longer pending")

    def insert_dssmp_outbox(self, q, row: dict) -> None:
        assert row["status"] == "pending", "the app only ever inserts pending outbox rows"
        self.insert_row(q, "dssmp_registration_outbox", row)

    def dssmp_outbox(self, dam_id: str | None = None, outbox_id: str | None = None) -> list[dict]:
        where, args = [], []
        if dam_id is not None:
            where.append("dam_id = %s"); args.append(dam_id)
        if outbox_id is not None:
            where.append("outbox_id = %s"); args.append(outbox_id)
        sql = "SELECT * FROM {s}dssmp_registration_outbox" + (" WHERE " + " AND ".join(where) if where else "")
        return [dict(r) for r in self.execute(sql + " ORDER BY approved_at, outbox_id", tuple(args))]

    def demo_publish_outbox(self, outbox_id: str, status: str, reason_code: str | None, message: str | None) -> None:
        """DEMO ONLY stand-in for the DA's publisher role: sets the bookkeeping columns, nothing else (the guard
        triggers refuse any other column). Never called outside demo mode."""
        self.execute("UPDATE {s}dssmp_registration_outbox SET status = %s, reason_code = %s, message = %s, "
                     "processed_at = %s WHERE outbox_id = %s AND status = 'pending'",
                     (status, reason_code, message, now_iso(), outbox_id))

    # ------------------------------------------------------------ generic workflow rows (cdse change, re-version)
    def rows(self, table: str, order: str, **where) -> list[dict]:
        cond = [f"{k} = %s" for k in where]
        sql = f"SELECT * FROM {{s}}{table}" + (" WHERE " + " AND ".join(cond) if cond else "") + f" ORDER BY {order}"
        return [dict(r) for r in self.execute(sql, tuple(where.values()))]

    @staticmethod
    def decide_pending(q, table: str, key: str, ident: str, fields: dict) -> None:
        """UPDATE one 'pending' workflow row; raises if it was decided concurrently (aborts the chain transaction)."""
        sets = ", ".join(f"{k} = %s" for k in fields)
        n = q(f"UPDATE {{s}}{table} SET {sets} WHERE {key} = %s AND status = 'pending' RETURNING {key}",
              (*fields.values(), ident))
        if not n:
            raise RuntimeError(f"{ident} is no longer pending")

    def demo_publish(self, table: str, outbox_id: str, status: str, reason_code, message) -> None:
        """DEMO ONLY stand-in for a DA publisher role: bookkeeping columns only (guard triggers refuse the rest)."""
        self.execute(f"UPDATE {{s}}{table} SET status = %s, reason_code = %s, message = %s, processed_at = %s "
                     "WHERE outbox_id = %s AND status = 'pending'", (status, reason_code, message, now_iso(), outbox_id))

    # ------------------------------------------------------------ users & roles
    def resolve_user(self, user_id: str, email: str) -> dict | None:
        """Look the caller up by stable user id; bind it to the email row only if that row is unbound.

        Returns None when no grant exists, or when the email row is bound to a DIFFERENT user id (an email
        that was reassigned or spoofed must not inherit another identity's roles)."""
        rows = self.execute("SELECT * FROM {s}app_user WHERE user_id = %s", (user_id,))
        if rows:
            return _bools(rows[0], "is_admin", "active")
        u = self.get_user(email) if email else None
        if u is None or (u.get("user_id") and u["user_id"] != user_id):
            return None
        self.execute("UPDATE {s}app_user SET user_id = %s WHERE email = %s AND user_id IS NULL", (user_id, u["email"]))
        self.audit(f"user:{user_id}", "user.identity_bound", None, u["email"], {"user_id": user_id})
        return self.get_user(u["email"])

    def upsert_user(self, email: str, display_name: str | None, is_admin: bool, actor: str) -> dict:
        email = email.strip().lower()
        self.execute(
            "INSERT INTO {s}app_user (email, display_name, is_admin, active, created_at, created_by) "
            "VALUES (%s, %s, %s, TRUE, %s, %s) ON CONFLICT (email) DO UPDATE SET "
            "display_name = EXCLUDED.display_name, is_admin = EXCLUDED.is_admin, active = TRUE",
            (email, display_name, bool(is_admin), now_iso(), actor))
        return self.get_user(email)

    def deactivate_user(self, email: str) -> None:
        self.execute("UPDATE {s}app_user SET active = FALSE WHERE email = %s", (email.lower(),))
        self.execute("DELETE FROM {s}dam_role WHERE email = %s", (email.lower(),))

    def get_user(self, email: str) -> dict | None:
        rows = self.execute("SELECT * FROM {s}app_user WHERE email = %s", (email.lower(),))
        return _bools(rows[0], "is_admin", "active") if rows else None

    def active_admin_count(self) -> int:
        rows = self.execute("SELECT COUNT(*) AS n FROM {s}app_user WHERE is_admin = TRUE AND active = TRUE")
        return int(rows[0]["n"])

    def list_users(self) -> list[dict]:
        users = [_bools(u, "is_admin", "active") for u in self.execute("SELECT * FROM {s}app_user ORDER BY email")]
        roles = self.execute("SELECT * FROM {s}dam_role ORDER BY dam_id")
        for u in users:
            u["roles"] = {r["dam_id"]: r["role"] for r in roles if r["email"] == u["email"]}
        return users

    def roles_for(self, email: str) -> dict[str, str]:
        rows = self.execute(
            "SELECT r.dam_id, r.role FROM {s}dam_role r JOIN {s}app_user u ON u.email = r.email "
            "WHERE r.email = %s AND u.active = TRUE", (email.lower(),))
        return {r["dam_id"]: r["role"] for r in rows}

    def set_role(self, email: str, dam_id: str, role: str | None, actor: str) -> None:
        email = email.lower()
        if role is None:
            self.execute("DELETE FROM {s}dam_role WHERE email = %s AND dam_id = %s", (email, dam_id))
            return
        if role not in ROLE_RANK:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        self.execute(
            "INSERT INTO {s}dam_role (email, dam_id, role, granted_by, granted_at) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (email, dam_id) DO UPDATE SET role = EXCLUDED.role, granted_by = EXCLUDED.granted_by, "
            "granted_at = EXCLUDED.granted_at", (email, dam_id, role, actor, now_iso()))

    def users_for_dam(self, dam_id: str) -> list[dict]:
        return self.execute(
            "SELECT u.email, u.display_name, r.role FROM {s}dam_role r JOIN {s}app_user u ON u.email = r.email "
            "WHERE r.dam_id = %s AND u.active = TRUE ORDER BY u.email", (dam_id,))

    # ------------------------------------------------------------ alarms
    def ack_states(self, event_ids: list[str]) -> dict[str, dict]:
        if not event_ids:
            return {}
        ph = ",".join(["%s"] * len(event_ids))
        rows = self.execute(f"SELECT * FROM {{s}}alarm_ack WHERE event_id IN ({ph})", event_ids)
        return {r["event_id"]: r for r in rows}

    def acknowledge(self, event_id: str, dam_id: str, actor: str, q=None) -> dict:
        """q: a transaction executor (append_chained `also`), so the state change and its chained record commit
        together; the state is re-checked inside that transaction."""
        q = q or self.execute
        cur = q("SELECT * FROM {s}alarm_ack WHERE event_id = %s", (event_id,))
        if cur and cur[0]["state"] != "open":
            raise ValueError(f"alarm {event_id} is already {cur[0]['state']}")
        q("INSERT INTO {s}alarm_ack (event_id, dam_id, state, acknowledged_by, acknowledged_at) "
          "VALUES (%s, %s, 'acknowledged', %s, %s) ON CONFLICT (event_id) DO UPDATE SET state = 'acknowledged', "
          "acknowledged_by = EXCLUDED.acknowledged_by, acknowledged_at = EXCLUDED.acknowledged_at",
          (event_id, dam_id, actor, now_iso()))
        return q("SELECT * FROM {s}alarm_ack WHERE event_id = %s", (event_id,))[0]

    def close_alarm(self, event_id: str, dam_id: str, actor: str, reason: str, q=None) -> dict:
        q = q or self.execute
        cur = q("SELECT * FROM {s}alarm_ack WHERE event_id = %s", (event_id,))
        if not cur or cur[0]["state"] != "acknowledged":
            raise ValueError(f"alarm {event_id} must be acknowledged before it is closed")
        q("UPDATE {s}alarm_ack SET state = 'closed', closed_by = %s, closed_at = %s, close_reason = %s "
          "WHERE event_id = %s", (actor, now_iso(), reason, event_id))
        return q("SELECT * FROM {s}alarm_ack WHERE event_id = %s", (event_id,))[0]

    def add_comment(self, event_id: str, dam_id: str, actor: str, body: str) -> None:
        self.execute("INSERT INTO {s}alarm_comment (event_id, dam_id, author, created_at, body) VALUES (%s,%s,%s,%s,%s)",
                     (event_id, dam_id, actor, now_iso(), body))

    def comments(self, event_id: str) -> list[dict]:
        return self.execute("SELECT * FROM {s}alarm_comment WHERE event_id = %s ORDER BY id", (event_id,))

    # ------------------------------------------------------------ onboarding
    def create_draft(self, actor: str, data: dict) -> dict:
        did = str(uuid.uuid4())
        ts = now_iso()
        self.execute("INSERT INTO {s}onboarding_draft (draft_id, created_by, created_at, updated_at, status, "
                     "proposed_dam_id, data) VALUES (%s,%s,%s,%s,'draft',%s,%s)",
                     (did, actor, ts, ts, (data.get("details") or {}).get("dam_id"), json.dumps(data)))
        return self.get_draft(did)

    def get_draft(self, draft_id: str) -> dict | None:
        rows = self.execute("SELECT * FROM {s}onboarding_draft WHERE draft_id = %s", (draft_id,))
        if not rows:
            return None
        r = rows[0]
        r["data"] = json.loads(r["data"])
        return r

    def list_drafts(self, actor: str | None = None) -> list[dict]:
        if actor:
            rows = self.execute("SELECT * FROM {s}onboarding_draft WHERE created_by = %s ORDER BY updated_at DESC", (actor,))
        else:
            rows = self.execute("SELECT * FROM {s}onboarding_draft ORDER BY updated_at DESC")
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def update_draft(self, draft_id: str, data: dict, status: str = "draft") -> dict:
        self.execute("UPDATE {s}onboarding_draft SET data = %s, status = %s, updated_at = %s, proposed_dam_id = %s "
                     "WHERE draft_id = %s",
                     (json.dumps(data), status, now_iso(), (data.get("details") or {}).get("dam_id"), draft_id))
        return self.get_draft(draft_id)

    def publish(self, draft_id: str, dam_id: str, payload: dict, actor: str, status: str = "pending") -> dict:
        pid = str(uuid.uuid4())
        self.execute("INSERT INTO {s}onboarding_publication (pub_id, draft_id, dam_id, payload, requested_by, "
                     "requested_at, status) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (pid, draft_id, dam_id, json.dumps(payload), actor, now_iso(), status))
        return {"pub_id": pid, "status": status}

    def publications(self) -> list[dict]:
        rows = self.execute("SELECT pub_id, draft_id, dam_id, requested_by, requested_at, status, applied_at, message "
                            "FROM {s}onboarding_publication ORDER BY requested_at DESC")
        return rows

    # ------------------------------------------------------------ live alarm-rule flag edits
    def flag_proposals(self, dam_id: str, status: str | None = None, threshold_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM {s}alarm_rule_flag_proposal WHERE dam_id = %s", [dam_id]
        if status:
            sql, args = sql + " AND status = %s", args + [status]
        if threshold_id:
            sql, args = sql + " AND threshold_id = %s", args + [threshold_id]
        return [_bools(r, "base_flag", "new_flag") for r in self.execute(sql + " ORDER BY proposed_at, proposal_id", args)]

    def flag_proposal(self, proposal_id: str) -> dict | None:
        r = self.execute("SELECT * FROM {s}alarm_rule_flag_proposal WHERE proposal_id = %s", (proposal_id,))
        return _bools(r[0], "base_flag", "new_flag") if r else None

    @staticmethod
    def insert_flag_proposal(q, row: dict) -> None:
        cols = list(row)
        q(f"INSERT INTO {{s}}alarm_rule_flag_proposal ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
          tuple(row[c] for c in cols))

    @staticmethod
    def decide_flag_proposal(q, proposal_id: str, status: str, by: str, by_id: str, at: str, rationale: str,
                             pub_id: str | None = None, new_version: int | None = None) -> None:
        """Pending -> applied | rejected, re-checked inside the chain transaction (a concurrent decision -> 409)."""
        cur = q("SELECT status FROM {s}alarm_rule_flag_proposal WHERE proposal_id = %s", (proposal_id,))
        if not cur or cur[0]["status"] != "pending":
            raise ValueError(f"proposal {proposal_id} is no longer pending")
        n = q("UPDATE {s}alarm_rule_flag_proposal SET status = %s, decided_by = %s, decided_by_id = %s, "
              "decided_at = %s, decision_rationale = %s, pub_id = %s, new_threshold_version = %s "
              "WHERE proposal_id = %s AND status = 'pending' RETURNING proposal_id",
              (status, by, by_id, at, rationale, pub_id, new_version, proposal_id))
        if not n:  # QA-056: decided concurrently between the read and the update (Postgres READ COMMITTED)
            raise ValueError(f"proposal {proposal_id} is no longer pending")

    @staticmethod
    def insert_ref_publication(q, pub_id: str, kind: str, dam_id: str, proposal_id: str | None, payload: dict,
                               actor: str, status: str) -> None:
        q("INSERT INTO {s}ref_change_publication (pub_id, kind, dam_id, proposal_id, payload, requested_by, "
          "requested_at, status, applied_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
          (pub_id, kind, dam_id, proposal_id, json.dumps(payload, sort_keys=True, default=str), actor, now_iso(),
           status, now_iso() if status == "applied" else None))

    def ref_publications(self, dam_id: str) -> list[dict]:
        rows = self.execute("SELECT * FROM {s}ref_change_publication WHERE dam_id = %s ORDER BY requested_at, pub_id",
                            (dam_id,))
        return [{**r, "payload": json.loads(r["payload"])} for r in rows]

    # ------------------------------------------------------------ reports, PFM reviews
    def save_report(self, dam_id: str, start: str, end: str, actor: str, content_md: str) -> dict:
        rid = str(uuid.uuid4())
        self.execute("INSERT INTO {s}report_draft (report_id, dam_id, period_start, period_end, created_by, "
                     "created_at, status, content_md) VALUES (%s,%s,%s,%s,%s,%s,'draft',%s)",
                     (rid, dam_id, start, end, actor, now_iso(), content_md))
        return self.get_report(rid)

    def get_report(self, report_id: str) -> dict | None:
        rows = self.execute("SELECT * FROM {s}report_draft WHERE report_id = %s", (report_id,))
        return rows[0] if rows else None

    def list_reports(self, dam_id: str) -> list[dict]:
        return self.execute("SELECT report_id, dam_id, period_start, period_end, created_by, created_at, status "
                            "FROM {s}report_draft WHERE dam_id = %s ORDER BY created_at DESC", (dam_id,))

    def add_pfm_review(self, pfm_id: str, dam_id: str, actor: str, status: str, notes: str) -> None:
        self.execute("INSERT INTO {s}pfm_review (pfm_id, dam_id, reviewed_by, reviewed_at, status, notes) "
                     "VALUES (%s,%s,%s,%s,%s,%s)", (pfm_id, dam_id, actor, now_iso(), status, notes))

    def pfm_reviews(self, dam_id: str) -> list[dict]:
        return self.execute("SELECT * FROM {s}pfm_review WHERE dam_id = %s ORDER BY id DESC", (dam_id,))

    # ------------------------------------------------------------ transactions
    @contextlib.contextmanager
    def transaction(self):
        """One atomic unit on ONE connection; yields q(sql, params) -> rows. Commits on clean exit, rolls back on
        error. sqlite: BEGIN IMMEDIATE takes the database write lock up front, so concurrent writers (threads or a
        second process on the same file) are serialized. Postgres: one pooled connection; the pool commits on exit
        and rolls back on error. No retry inside a transaction (a dropped connection aborts it; the caller retries)."""
        if self.kind == "sqlite":
            with self._lock:
                conn = self._conn
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield lambda sql, params=(): self._q_sqlite(conn, sql, params)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            def q(sql, params=()):
                cur.execute(sql.replace("{s}", self.prefix), tuple(params))
                return [dict(r) for r in cur.fetchall()] if cur.description else []
            yield q

    def _q_sqlite(self, conn, sql, params):
        cur = conn.execute(sql.replace("{s}", self.prefix).replace("%s", "?"), tuple(params))
        return [dict(r) for r in cur.fetchall()] if cur.description else []

    # ------------------------------------------------------------ hash-chained review records (QA-039 v2, insert-only)
    SIGNOFF_COLS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                    "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash", "action_type",
                    "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id", "chain_seq",
                    "prev_hash", "record_hash", "signature", "sig_alg", "key_id", "actor_app_role", "canon_version")

    def append_chained(self, chain_id: str, genesis: str, finalize, also=None, also_row=None, *,
                       lock_key: str | None = None, check=None) -> dict:
        """Append one record to `chain_id` atomically. Under the per-chain lock (Postgres: transaction-scoped
        advisory lock CHAIN_LOCK_CLASS / hashtext(chain_id); sqlite: the BEGIN IMMEDIATE write lock) read the current
        head = the highest chain_seq in review_chain_head_log (seeded once from the legacy review_chain_head, else
        genesis at seq 0), call finalize(seq + 1, head_hash) -> complete row, INSERT it, APPEND the new head to the
        log (and keep the legacy head row in step for older readers), then run also(q) / also_row(q, row) in the SAME
        transaction. The PRIMARY KEY (chain_id, chain_seq) of the log and the UNIQUE (chain_id, chain_seq) index of
        review_signoff are the backstops. Nothing is ever updated in review_signoff or review_chain_head_log.
        QA-056: with `lock_key` the per-dam lock is taken FIRST, then check(q) re-runs the caller's state checks on
        this transaction's connection (raise to abort: nothing is written), and only then the chain head is locked.
        Lock order is therefore always dam -> chain head (no deadlock with appends that take no dam lock)."""
        with self.transaction() as q:
            if lock_key is not None:
                self.lock_dam(q, lock_key)
            if check is not None:
                check(q)
            if self.kind != "sqlite":   # chain lock AFTER lock_dam + check(q): order dam -> chain (QA-056)
                q("SELECT pg_advisory_xact_lock(%s, hashtext(%s)) AS locked", (CHAIN_LOCK_CLASS, chain_id))
            top = "SELECT chain_seq, head_hash FROM {s}review_chain_head_log WHERE chain_id = %s ORDER BY chain_seq DESC LIMIT 1"
            head = q(top, (chain_id,))
            if not head:
                legacy = q("SELECT chain_seq, head_hash FROM {s}review_chain_head WHERE chain_id = %s", (chain_id,))
                seed = (int(legacy[0]["chain_seq"]), legacy[0]["head_hash"]) if legacy else (0, genesis)
                q("INSERT INTO {s}review_chain_head_log (chain_id, chain_seq, head_hash, updated_ts) "
                  "VALUES (%s, %s, %s, %s) ON CONFLICT (chain_id, chain_seq) DO NOTHING",
                  (chain_id, seed[0], seed[1], now_iso()))
                head = q(top, (chain_id,))
            head = head[0]
            row = finalize(int(head["chain_seq"]) + 1, head["head_hash"])
            cols = self.SIGNOFF_COLS
            q(f"INSERT INTO {{s}}review_signoff ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
              tuple(row.get(c) for c in cols))
            q("INSERT INTO {s}review_chain_head_log (chain_id, chain_seq, head_hash, updated_ts) VALUES (%s, %s, %s, %s)",
              (chain_id, row["chain_seq"], row["record_hash"], now_iso()))
            q("INSERT INTO {s}review_chain_head (chain_id, chain_seq, head_hash, updated_ts) VALUES (%s, %s, %s, %s) "
              "ON CONFLICT (chain_id) DO UPDATE SET chain_seq = excluded.chain_seq, head_hash = excluded.head_hash, "
              "updated_ts = excluded.updated_ts", (chain_id, row["chain_seq"], row["record_hash"], now_iso()))
            if also is not None:
                also(q)
            if also_row is not None:  # needs the finalized record (e.g. its record_hash) in the same transaction
                also_row(q, row)
        return dict(row)

    def chain_rows(self, chain_id: str) -> list[dict]:
        return self.execute("SELECT * FROM {s}review_signoff WHERE chain_id = %s ORDER BY chain_seq, review_id",
                            (chain_id,))

    def chain_head(self, chain_id: str) -> dict | None:
        """Current head: the highest chain_seq in the append-only log; the legacy row only if the log has none."""
        r = self.execute("SELECT chain_id, chain_seq, head_hash, updated_ts FROM {s}review_chain_head_log "
                         "WHERE chain_id = %s ORDER BY chain_seq DESC LIMIT 1", (chain_id,))
        if not r:
            r = self.execute("SELECT * FROM {s}review_chain_head WHERE chain_id = %s", (chain_id,))
        return r[0] if r else None

    def chain_ids(self) -> list[str]:
        ids = {r["chain_id"] for r in self.execute("SELECT DISTINCT chain_id FROM {s}review_signoff")}
        ids |= {r["chain_id"] for r in self.execute("SELECT chain_id FROM {s}review_chain_head")}
        ids |= {r["chain_id"] for r in self.execute("SELECT DISTINCT chain_id FROM {s}review_chain_head_log")}
        return sorted(i for i in ids if i)

    def review_signoffs(self, subject_type: str, subject_ids: list[str]) -> list[dict]:
        if not subject_ids:
            return []
        marks = ", ".join(["%s"] * len(subject_ids))
        return self.execute(f"SELECT * FROM {{s}}review_signoff WHERE subject_type = %s AND subject_id IN ({marks}) "
                            "ORDER BY chain_id, chain_seq", (subject_type, *subject_ids))

    # ------------------------------------------------------------ audit
    def audit(self, actor: str, action: str, dam_id: str | None = None, target: str | None = None,
              detail: dict | None = None) -> None:
        self.execute("INSERT INTO {s}audit_log (ts, actor, action, dam_id, target, detail) VALUES (%s,%s,%s,%s,%s,%s)",
                     (now_iso(), actor, action, dam_id, target, json.dumps(detail or {})))

    def audit_entries(self, dam_id: str | None = None, limit: int = 200) -> list[dict]:
        if dam_id:
            rows = self.execute("SELECT * FROM {s}audit_log WHERE dam_id = %s ORDER BY id DESC LIMIT %s", (dam_id, limit))
        else:
            rows = self.execute("SELECT * FROM {s}audit_log ORDER BY id DESC LIMIT %s", (limit,))
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r.get("detail") else {}
        return rows


def _bools(row: dict, *keys: str) -> dict:
    for k in keys:
        row[k] = bool(row[k])
    return row


def _is_connection_error(e: Exception) -> bool:
    name = type(e).__name__
    return name in {"OperationalError", "InterfaceError", "PoolTimeout", "TooManyRequests"} or \
        "closed" in str(e).lower()


# ---------------------------------------------------------------- Lakebase (service principal)
# docs.databricks.com/aws/en/oltp/projects/authentication + tutorial-databricks-apps-autoscaling (KB 05):
# the password is a Lakebase database credential for the app's service principal, minted with
# w.postgres.generate_database_credential(endpoint="projects/<p>/branches/<b>/endpoints/<e>"). It lasts 1 h and is
# checked only at login, so every NEW connection gets a credential that is not about to expire. The workspace
# OAuth token (w.config.oauth_token()) is NOT a valid Lakebase password.
TOKEN_REFRESH_MARGIN_S = 300.0      # re-mint when the cached credential expires within 5 min
DEFAULT_TOKEN_LIFETIME_S = 3600.0   # documented lifetime, used only when expire_time is missing
KEEPALIVE_KW = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}


def _expire_epoch(exp: Any, now: float) -> float:
    """expire_time from the SDK (protobuf Timestamp, datetime or RFC 3339 text) -> epoch seconds."""
    try:
        if exp is None:
            raise ValueError
        if hasattr(exp, "ToDatetime"):
            return exp.ToDatetime(tzinfo=timezone.utc).timestamp()
        if isinstance(exp, datetime):
            return (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)).timestamp()
        return datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return now + DEFAULT_TOKEN_LIFETIME_S


class LakebaseCredentials:
    """Caches one database credential and re-mints it before expire_time (never logs the token)."""

    def __init__(self, endpoint_name: str, client=None, clock=time.time):
        if not endpoint_name or not endpoint_name.startswith("projects/") or "/endpoints/" not in endpoint_name:
            raise ValueError("ENDPOINT_NAME must be projects/<project>/branches/<branch>/endpoints/<endpoint>")
        self.endpoint_name = endpoint_name
        self._client = client
        self._clock = clock
        self._tok: str | None = None
        self._exp = 0.0
        self._lock = threading.Lock()

    def _w(self):
        if self._client is None:
            from databricks.sdk import WorkspaceClient
            self._client = WorkspaceClient()
        return self._client

    def token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._tok is None or now >= self._exp - TOKEN_REFRESH_MARGIN_S:
                cred = self._w().postgres.generate_database_credential(endpoint=self.endpoint_name)
                if not cred.token:
                    raise RuntimeError("Lakebase generate_database_credential returned no token")
                self._tok, self._exp = cred.token, _expire_epoch(cred.expire_time, now)
                log.info("Minted Lakebase credential for %s (expires in %.0f s)", self.endpoint_name,
                         self._exp - now)
            return self._tok

    def invalidate(self) -> None:
        with self._lock:
            self._tok, self._exp = None, 0.0


def lakebase_conninfo(env: dict | None = None) -> dict:
    """Connection kwargs from the env vars Databricks Apps injects for the `postgres` resource."""
    env = os.environ if env is None else env
    missing = [k for k in ("PGHOST", "PGDATABASE", "PGUSER") if not env.get(k)]
    if missing:
        raise RuntimeError(f"Lakebase resource env missing: {', '.join(missing)}")
    return {"host": env["PGHOST"], "port": int(env.get("PGPORT") or 5432), "dbname": env["PGDATABASE"],
            "user": env["PGUSER"], "sslmode": env.get("PGSSLMODE") or "require",
            "application_name": env.get("PGAPPNAME") or "dam-safety-app", "connect_timeout": 10, **KEEPALIVE_KW}


def lakebase_pool(env: dict | None = None, creds: LakebaseCredentials | None = None, pool_cls=None,
                  min_size: int = 1, max_size: int = 10):
    """psycopg_pool.ConnectionPool whose connection class injects a fresh credential per new connection
    (docs example), with TCP keepalives and a health check on checkout so connections dropped by an HA
    failover are replaced instead of handed out."""
    import psycopg
    from psycopg.rows import dict_row

    env = os.environ if env is None else env
    creds = creds or LakebaseCredentials(env.get("ENDPOINT_NAME", ""))
    kwargs = lakebase_conninfo(env)

    class OAuthConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kw):
            kw["password"] = creds.token()
            try:
                return super().connect(conninfo, **kw)
            except psycopg.OperationalError:
                creds.invalidate()   # an expired/revoked credential and a cold endpoint look alike
                raise

    if pool_cls is None:
        from psycopg_pool import ConnectionPool as pool_cls
    return pool_cls(kwargs=kwargs | {"row_factory": dict_row}, connection_class=OAuthConnection,
                    min_size=min_size, max_size=max_size, open=True, check=pool_cls.check_connection,
                    reconnect_timeout=60, name="dam-lakebase")
