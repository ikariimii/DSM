"""v1.7 DSSMP registration (Genie 2026-11-09): app outbox dssmp_registration_outbox -> ref.dssmp_version
(dam_safety.dssmp_registration, task load_dssmp_registrations), migration v1_7_003 + down, and the MERGE executed on
PostgreSQL >= 15 when DAM_TEST_PG is set (same text; Delta itself UNVERIFIED). Synthetic data only."""
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import dssmp_registration as dr  # noqa: E402
import load_dssmp_registrations as job  # noqa: E402

D, D2 = "SYN-DAM-01", "SYN-DAM-02"
CDSE, ADMIN = "cdse1@example.org", "admin@example.org"
AUTH = [{"dam_id": D, "principal": CDSE, "granted_ts": None, "revoked_ts": None}]
SHA, H1, H2 = "ab" * 32, "c1" * 32, "c2" * 32
NOW = "2026-11-09T18:00:00Z"
V1 = {"dssmp_version_id": "DS-1", "dam_id": D, "version_no": 1, "effective_from": "2026-01-01T00:00:00Z",
      "effective_to": None, "status": "approved", "document_sha256": SHA, "approved_by": CDSE,
      "approved_at": "2025-12-01T00:00:00Z", "approval_record_hash": "d0" * 32, "loaded_from_outbox_id": "OB-0"}


def outbox(oid="OB-1", vid="DS-2", h=H1, **kw):
    """Exactly the agreed app contract (app SCHEMA_CONTRACT §7 item 19)."""
    return {"outbox_id": oid, "dam_id": D, "dssmp_version_id": vid, "version_no": 2,
            "effective_from": "2026-12-01T00:00:00Z", "change_reason": "annual review", "source_ref": "DSSMR-2026",
            "document_uri": "lakebase:dam_app.dssmp_document/DOC-1", "document_sha256": SHA,
            "proposed_by": "damadmin@example.org", "approved_by": CDSE, "approved_at": "2026-11-09T15:00:00Z",
            "approval_record_hash": h, "status": "pending", "reason_code": None, "message": None,
            "processed_at": None, **kw}


def approve_rec(h=H1, who=CDSE, vid="DS-2", **kw):
    cv = {"dssmp_version_id": vid, "dam_id": D, "version_no": 2, "effective_from": "2026-12-01T00:00:00Z",
          "document_sha256": SHA}
    cv.update(kw.pop("cv", {}))
    r = {"review_id": "RS-9", "dam_id": D, "chain_id": D, "chain_seq": 9, "subject_type": "dssmp_version",
         "subject_id": vid, "action_type": "dssmp_version_approve", "decision": "approve", "comment": "approved",
         "reviewer_id": who, "reviewer_email": who, "review_level": "CDSE", "signed_ts": "2026-11-09T15:00:00Z",
         "record_hash": h, "corrected_value": json.dumps(cv), "original_value": json.dumps({})}
    r.update(kw)
    return r


def one(row, versions=(V1,), approvals=None, now=NOW, authority=AUTH):
    approvals = {H1: approve_rec()} if approvals is None else approvals
    [d] = dr.plan([row], list(versions), approvals, authority, now)
    return d


def test_valid_registration_inserts_approved_and_closes_the_previous_version():
    d = one(outbox())
    assert d["decision"] == "applied" and d["codes"] == []
    assert d["insert"]["status"] == "approved" and d["insert"]["loaded_from_outbox_id"] == "OB-1"
    assert d["insert"]["approval_record_hash"] == H1 and d["insert"]["effective_to"] is None
    assert d["close"] == {"dssmp_version_id": "DS-1", "effective_to": dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)}
    assert d["status"] == "loaded" and d["reason_code"] is None
    assert job.outbox_updates([d], "T") == [("loaded", None, d["message"], "T", "OB-1")]


def test_first_version_of_a_new_dam_has_nothing_to_close():
    d = one(outbox(), versions=[])
    assert d["decision"] == "applied" and d["close"] is None


def test_rows_without_a_cdse_approve_chain_record_are_refused():
    assert one(outbox(), approvals={}, now="2026-11-09T15:30:00Z")["decision"] == "deferred"     # extract lag
    d = one(outbox(), approvals={})
    assert d["status"] == "rejected" and d["codes"] == ["approval_record_missing"]
    assert d["reason_code"] == "approval_record_missing"
    assert job.outbox_updates([d], "T") == [("rejected", "approval_record_missing", d["message"], "T", "OB-1")]


@pytest.mark.parametrize("rec,code", [
    (approve_rec(action_type="dssmp_version_propose", decision="propose"), "approval_record_wrong_action"),
    (approve_rec(subject_type="alarm_rule"), "approval_record_wrong_action"),
    (approve_rec(signed_ts="2026-11-09T15:00:05Z"), "approval_record_mismatch"),
    (approve_rec(cv={"document_sha256": "ef" * 32}), "approval_record_mismatch"),
    (approve_rec(cv={"version_no": 3}), "approval_record_mismatch"),
    (approve_rec(vid="DS-OTHER"), "approval_record_mismatch"),
])
def test_approve_record_must_match_the_outbox_row(rec, code):
    d = one(outbox(), approvals={H1: rec})
    assert d["status"] == "rejected" and code in d["codes"], d


def test_approver_must_hold_cdse_authority_admin_is_refused():
    d = one(outbox(approved_by=ADMIN), approvals={H1: approve_rec(who=ADMIN, canon_version=2,
                                                                   actor_app_role="admin")})
    assert d["status"] == "rejected"
    assert "approval_not_cdse" in d["codes"] and "dssmp_approve_by_admin" in d["codes"]


@pytest.mark.parametrize("kw,code", [(dict(version_no=None), "missing_required_column"),
                                     (dict(approval_record_hash=""), "missing_required_column"),
                                     (dict(document_sha256="XYZ"), "invalid_value"),
                                     (dict(version_no=0), "invalid_value"),
                                     (dict(effective_from="soon"), "invalid_value")])
def test_outbox_row_validation(kw, code):
    d = one(outbox(**kw))
    assert d["status"] == "rejected" and code in d["codes"]


def test_duplicate_id_version_order_and_effective_from():
    assert one(outbox(vid="DS-1"), approvals={H1: approve_rec(vid="DS-1")})["codes"] == ["dssmp_version_exists"]
    d = one(outbox(version_no=1), approvals={H1: approve_rec(cv={"version_no": 1})})
    assert d["codes"] == ["version_not_newer"]
    d = one(outbox(effective_from="2025-06-01T00:00:00Z"),
            approvals={H1: approve_rec(cv={"effective_from": "2025-06-01T00:00:00Z"})})
    assert d["codes"] == ["effective_from_not_after_current"]


def test_stale_and_rerun():
    two_open = [V1, {**V1, "dssmp_version_id": "DS-1B", "loaded_from_outbox_id": "OB-X"}]
    d = one(outbox(), versions=two_open)
    assert d["decision"] == "rejected_stale" and d["status"] == "rejected" and d["reason_code"] == "stale"
    applied = {**V1, "dssmp_version_id": "DS-2", "loaded_from_outbox_id": "OB-1", "version_no": 2}
    d = one(outbox(), versions=[{**V1, "status": "superseded", "effective_to": "2026-12-01T00:00:00Z"}, applied])
    assert d["decision"] == "already_applied" and d["status"] == "loaded"


def test_second_row_for_the_same_dam_is_deferred():
    ds = dr.plan([outbox("OB-2", "DS-3", h=H2, approved_at="2026-11-09T15:10:00Z"), outbox()], [V1],
                 {H1: approve_rec(), H2: approve_rec(h=H2, vid="DS-3", signed_ts="2026-11-09T15:10:00Z")}, AUTH, NOW)
    assert [(d["outbox_id"], d["decision"]) for d in ds] == [("OB-1", "applied"), ("OB-2", "deferred")]


def test_codes_constant_is_tested():
    src = open(__file__).read()
    for c in dr.CODES:
        assert f'"{c}"' in src or c == "stale", c


def test_columns_match_ddl_and_job_wiring():
    ddl = open(os.path.join(ROOT, "sql", "ddl", "04_ref_dssmp_pfm_eap.sql")).read()
    body = re.search(r"CREATE TABLE IF NOT EXISTS ref\.dssmp_version \((.*?)\n\);", ddl, re.S).group(1)
    cols = tuple(m.group(1) for l in body.splitlines() if (m := re.match(r"\s+(\w+)\s+[A-Z]", l))
                 and m.group(1) != "CONSTRAINT")
    assert cols == dr.DSSMP_COLS
    jobs = open(os.path.join(ROOT, "resources", "jobs.yml")).read()
    blk = jobs[jobs.index("    dam_ref_publish:"):jobs.index("    dam_interpretation:")]
    assert blk.index("task_key: load_dssmp_registrations") < blk.index("task_key: publish_ref_changes")
    assert "depends_on: [ { task_key: load_dssmp_registrations } ]" in blk
    lb = open(os.path.join(ROOT, "lakebase", "001_app_schema.sql")).read()
    m = re.search(r"CREATE TABLE IF NOT EXISTS dssmp_registration_outbox \((.*?)\);", lb, re.S)
    assert m, "outbox missing from lakebase/001"
    assert tuple(re.findall(r"(?:^|,)\s*(\w+) (?:TEXT|INTEGER)", m.group(1))) == dr.OUTBOX_COLS


# ---------------------------------------------------------------- UC CHECKs of v1_7_003 (executed in SQLite)
def _ck(name):
    ddl = open(os.path.join(ROOT, "sql", "ddl", "04_ref_dssmp_pfm_eap.sql")).read()
    return re.search(rf"ADD CONSTRAINT {name} CHECK \((.*?)\);\n", ddl, re.S).group(1).replace(" RLIKE ", " REGEXP ")


def _db():
    con = sqlite3.connect(":memory:")
    con.create_function("REGEXP", 2, lambda p, s: s is not None and re.search(p, s) is not None)
    con.execute(f"CREATE TABLE v ({', '.join(dr.DSSMP_COLS)}, CHECK ({_ck('ck_dssmp_version_status')}), "
                f"CHECK ({_ck('ck_dssmp_version_approval')}))")
    return con


def _ins(con, **kw):
    r = {**V1, **kw}
    con.execute(f"INSERT INTO v VALUES ({','.join('?' * len(dr.DSSMP_COLS))})", tuple(r.get(c) for c in dr.DSSMP_COLS))


def test_uc_checks_accept_valid_rows_and_drafts_without_approval():
    con = _db()
    _ins(con)
    _ins(con, dssmp_version_id="DS-D", status="draft", approved_by=None, approved_at=None, document_sha256=None,
         approval_record_hash=None)
    _ins(con, dssmp_version_id="DS-S", status="superseded", effective_to="2026-06-01T00:00:00Z")


@pytest.mark.parametrize("kw", [dict(status="retired"), dict(version_no=0), dict(effective_to="2025-01-01T00:00:00Z"),
                                dict(approved_by=None), dict(approved_by=" "), dict(approved_at=None),
                                dict(document_sha256=None), dict(document_sha256="AB" * 32),
                                dict(approval_record_hash="short")])
def test_uc_checks_refuse(kw):
    with pytest.raises(sqlite3.IntegrityError):
        _ins(_db(), **kw)


def test_migration_and_down_headers():
    mig = open(os.path.join(ROOT, "sql", "migrations", "v1_7_003_dssmp_registration.sql")).read()
    down = open(os.path.join(ROOT, "sql", "migrations", "v1_7_003_dssmp_registration_down.sql")).read()
    assert "PRE-CHECK" in mig and "UNVERIFIED" in mig and "_down.sql" in mig
    assert "RESTORE" in down and "PRE-CHECK" in down
    code = "\n".join(l for l in down.splitlines() if not l.startswith("--"))
    assert not re.search(r"DROP COLUMN|DROP TABLE|DELETE|UPDATE|TRUNCATE", code)
    assert code.count("DROP CONSTRAINT IF EXISTS") == 5   # + ck_dam_cdse_assignment (table and rows stay)


# ---------------------------------------------------------------- the MERGE, executed on PostgreSQL (>= 15)
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")
T = {"version_no": "INT", "effective_from": "TIMESTAMPTZ", "effective_to": "TIMESTAMPTZ", "approved_at": "TIMESTAMPTZ"}


def _psql(conn, sql):
    return subprocess.run(["psql", conn, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True)


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return "'" + (v.isoformat() if isinstance(v, dt.datetime) else str(v)).replace("'", "''") + "'"


@pytest.fixture()
def pg():
    db = f"dssmp_{uuid.uuid4().hex[:8]}"
    assert _psql(PG, f"CREATE DATABASE {db}").returncode == 0
    conn = f"{PG} dbname={db}"
    cols = ", ".join(f"{c} {T.get(c, 'TEXT')}" for c in dr.DSSMP_COLS)
    assert _psql(conn, f"CREATE TABLE v ({cols}, PRIMARY KEY (dssmp_version_id)); "
                       f"CREATE TABLE s ({cols}, merge_action TEXT, merge_key TEXT, supersedes_id TEXT)").returncode == 0
    yield conn
    _psql(PG, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _put(conn, table, rows, cols):
    for r in rows:
        assert _psql(conn, f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
                           f"({', '.join(_lit(r.get(c)) for c in cols)})").returncode == 0


def _state(conn):
    return _psql(conn, "SELECT dssmp_version_id || ':' || status || ':' || coalesce(to_char(effective_to AT TIME ZONE "
                       "'UTC', 'YYYY-MM-DD'), '-') FROM v ORDER BY 1").stdout.split()


SRC = dr.DSSMP_COLS + ("merge_action", "merge_key", "supersedes_id")


@live
def test_merge_inserts_and_closes_in_one_statement_and_is_idempotent(pg):
    _put(pg, "v", [V1], dr.DSSMP_COLS)
    _put(pg, "s", dr.merge_source_rows(dr.plan([outbox()], [V1], {H1: approve_rec()}, AUTH, NOW)), SRC)
    sql = dr.merge_sql("v", "s")
    assert sql.count("MERGE INTO") == 1
    r = _psql(pg, sql)
    assert r.returncode == 0, r.stderr
    assert _state(pg) == ["DS-1:superseded:2026-12-01", "DS-2:approved:-"]
    assert _psql(pg, sql).returncode == 0 and _state(pg) == ["DS-1:superseded:2026-12-01", "DS-2:approved:-"]


@live
def test_merge_rechecks_staleness_and_first_version(pg):
    _put(pg, "v", [{**V1, "status": "superseded", "effective_to": "2026-06-01T00:00:00Z"},
                   {**V1, "dssmp_version_id": "DS-9", "version_no": 9, "effective_from": "2026-06-01T00:00:00Z"}],
         dr.DSSMP_COLS)
    before = _state(pg)
    _put(pg, "s", dr.merge_source_rows(dr.plan([outbox()], [V1], {H1: approve_rec()}, AUTH, NOW)), SRC)
    assert _psql(pg, dr.merge_sql("v", "s")).returncode == 0 and _state(pg) == before        # stale: nothing
    assert _psql(pg, "DELETE FROM s; DELETE FROM v").returncode == 0
    _put(pg, "s", dr.merge_source_rows(dr.plan([outbox()], [], {H1: approve_rec()}, AUTH, NOW)), SRC)
    assert _psql(pg, dr.merge_sql("v", "s")).returncode == 0 and _state(pg) == ["DS-2:approved:-"]
