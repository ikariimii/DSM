"""QA-058 (app side of DA 6420d98 / lakebase v1_7_010): rule_reversion_proposal is owned by dam_lakebase_owner; the app
role has SELECT, INSERT and UPDATE(status, decided_by, decided_by_id, decided_at, decision_rationale, pub_ids) only,
and the status-transition trigger t_status_rule_reversion_proposal is ENABLE ALWAYS. The app runs no DDL on it on
Postgres and refuses to start (codes rule_reversion_not_owner_owned / rule_reversion_transition_guard_disabled)
unless it is owner-owned with the trigger ENABLE ALWAYS (QA-060: only tgenabled 'A'). SCHEMA_CONTRACT §7 item 23.

Always run: every statement the real routers issue against the table (sqlite trace) is a SELECT, an INSERT or an UPDATE
of only the six granted columns; no DELETE / other write path exists in app/backend; decide_pending refuses another
column; the startup check's verdicts on a fake pool. Live (DAM_TEST_PG + psql): as the RESTRICTED app role on a
lakebase/001-provisioned DB, INSERT and the allowed UPDATE work; DELETE, an UPDATE of another column and a bad status
transition are refused; an origin-only / disabled / replica-only trigger or a non-owner owner refuses startup, and a
change after startup is reported by both chain verify endpoints."""
import contextlib
import re
from pathlib import Path

import pytest
from test_lakebase_owner_live import PG, lakebase  # noqa: F401  (fixture: scratch DB provisioned by lakebase/001)
from test_rule_reversion import CDSE, DAMADMIN, NEWV, RV, _approved_dssmp, _st, k  # noqa: F401  (fixture k)

from backend.repos import state as st_mod
from backend.repos.state import (CODE_REVERSION_GUARD_DISABLED, CODE_REVERSION_NOT_OWNER_OWNED, OwnerSchemaError,
                                 REVERSION_UPDATE_COLS, StateStore)

T = "rule_reversion_proposal"
SIX = {"status", "decided_by", "decided_by_id", "decided_at", "decision_rationale", "pub_ids"}
BACKEND = Path(__file__).resolve().parents[1] / "backend"
live = pytest.mark.skipif(not PG or not __import__("shutil").which("psql"),
                          reason="set DAM_TEST_PG (superuser libpq conninfo) and have psql to run")


def test_the_six_columns_are_the_v1_7_010_grant():
    assert set(REVERSION_UPDATE_COLS) == SIX and len(REVERSION_UPDATE_COLS) == 6
    sql = (Path(__file__).resolve().parents[2] / "lakebase" / "migrations" / "v1_7_010_rule_reversion_owner.sql").read_text()
    m = re.search(r"GRANT UPDATE \(([^)]*)\)", sql)        # the one column grant (built with format())
    assert m and {c.strip() for c in m.group(1).split(",")} == SIX


def _set_cols(stmt):
    m = re.search(r"\bSET\b(.*?)\bWHERE\b", stmt, re.S | re.I)
    assert m, stmt
    return set(re.findall(r"(?:^|,)\s*\"?(\w+)\"?\s*=", m.group(1)))


def test_every_statement_the_routers_issue_is_insert_or_a_six_column_update(k):
    """Propose -> reject and propose -> approve through the REAL routers; every SQL statement sqlite executes is traced."""
    _approved_dssmp(k)
    trace = []
    conn = _st(k)._conn
    conn.set_trace_callback(trace.append)
    try:
        pid = k.post(RV, user=DAMADMIN, json={"to_dssmp_version_id": NEWV, "rationale": "x"}).json()["proposal_id"]
        assert k.post(f"{RV}/{pid}/reject", user=CDSE, json={"rationale": "no"}).status_code == 200
        pid = k.post(RV, user=DAMADMIN, json={"to_dssmp_version_id": NEWV, "rationale": "y"}).json()["proposal_id"]
        assert k.post(f"{RV}/{pid}/approve", user=CDSE, json={"rationale": "ok"}).status_code == 200
    finally:
        conn.set_trace_callback(None)
    mine = [s for s in trace if re.search(rf"\b{T}\b", s)]
    kinds = {s.split()[0].upper() for s in mine}
    assert {"INSERT", "UPDATE"} <= kinds and kinds <= {"SELECT", "INSERT", "UPDATE"}, kinds
    updates = [s for s in mine if s.split()[0].upper() == "UPDATE"]
    assert len(updates) == 2
    for s in updates:
        assert re.match(rf"UPDATE\s+\S*{T}\s+SET", s, re.I), s
        assert _set_cols(s) <= SIX, (_set_cols(s) - SIX, s)
    assert not any(re.search(r"\b(DELETE|TRUNCATE|ALTER|DROP|CREATE)\b", s, re.I) for s in mine)


def test_set_col_parser_catches_another_column():
    s = f"UPDATE {T} SET status = 'applied', rationale = 'x' WHERE proposal_id = 'P'"
    assert _set_cols(s) - SIX == {"rationale"}


def test_no_other_write_path_in_app_backend():
    """Writes go through st.insert_row / st.decide_pending only; decide_pending dicts name only the six columns."""
    calls = 0
    for f in BACKEND.rglob("*.py"):
        src = f.read_text()
        assert not re.search(rf"\b(DELETE\s+FROM|UPDATE|TRUNCATE|ALTER\s+TABLE|DROP\s+TABLE)\s+(\{{s\}}|\S*\.)?{T}\b",
                             src, re.I), f
        for m in re.finditer(rf"decide_pending\(q, \"{T}\", \"proposal_id\", proposal_id, \{{(.*?)\}}\)", src, re.S):
            calls += 1
            assert set(re.findall(r'"(\w+)":', m.group(1))) <= SIX, (f, m.group(1))
        for m in re.finditer(rf"\.(\w+)\(\s*q,\s*\"{T}\"", src):
            assert m.group(1) in ("insert_row", "decide_pending"), (f, m.group(0))
    assert calls == 2                                               # approve + reject


def test_decide_pending_refuses_any_other_column():
    st = StateStore("sqlite")
    with pytest.raises(ValueError, match="rationale"):
        with st.transaction() as q:
            st.decide_pending(q, T, "proposal_id", "P", {"status": "applied", "rationale": "rewritten"})


# --------------------------------------------------------------- startup check on a fake pool (no DB)
class _Cur:
    def __init__(self, ans, log):
        self.ans, self.log, self.description, self._rows = ans, log, None, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.log.append(sql)
        self.description = [("x",)] if sql.lstrip().startswith("SELECT") else None
        self._rows = ([{"owner": self.ans["owner"]}] if self.ans["owner"] else []) if "pg_get_userbyid(c.relowner)" in sql \
            else ([{"tgenabled": self.ans["tg"]}] if self.ans["tg"] else []) if "tgenabled" in sql else []

    def fetchall(self):
        return self._rows


class _Pool:
    def __init__(self, owner, tg):
        self.ans, self.log = {"owner": owner, "tg": tg}, []

    @contextlib.contextmanager
    def connection(self):
        yield self

    def cursor(self):
        return _Cur(self.ans, self.log)


@pytest.mark.parametrize("owner,tg,code", [
    (None, "A", CODE_REVERSION_NOT_OWNER_OWNED), ("app_role", "A", CODE_REVERSION_NOT_OWNER_OWNED),
    ("postgres", "A", CODE_REVERSION_NOT_OWNER_OWNED),
    ("dam_lakebase_owner", None, CODE_REVERSION_GUARD_DISABLED), ("dam_lakebase_owner", "D", CODE_REVERSION_GUARD_DISABLED),
    ("dam_lakebase_owner", "R", CODE_REVERSION_GUARD_DISABLED),
    ("dam_lakebase_owner", "O", CODE_REVERSION_GUARD_DISABLED)])        # QA-060: origin-only is refused too
def test_startup_fails_closed(owner, tg, code):
    with pytest.raises(OwnerSchemaError) as e:
        StateStore("postgres", schema="dam_app", pg_pool=_Pool(owner, tg))
    assert e.value.code == code and str(e.value).startswith(code + ":")


def test_startup_accepts_only_always_and_runs_no_ddl_on_the_table():
    pool = _Pool("dam_lakebase_owner", "A")
    st = StateStore("postgres", schema="dam_app", pg_pool=pool)
    assert not [s for s in pool.log if T in s and not s.lstrip().startswith("SELECT")]
    assert not any("rule_reversion_one_pending" in s for s in pool.log)
    assert st.verify_owner_tables() is None
    pool.ans["tg"] = "O"                                    # changed after startup: the on-demand re-check reports it
    assert st.verify_owner_tables().code == CODE_REVERSION_GUARD_DISABLED
    pool.ans.update(tg="A", owner="someone_else")
    assert st.verify_owner_tables().code == CODE_REVERSION_NOT_OWNER_OWNED


NAMES = ["rule_reversion_proposal", '"rule_reversion_proposal"', "RULE_REVERSION_PROPOSAL", "Rule_Reversion_Proposal",
         '"Rule_Reversion_Proposal"', "dam_app.rule_reversion_proposal", '"dam_app"."RULE_REVERSION_PROPOSAL"',
         '"dam.app".rule_reversion_proposal', "{s}rule_reversion_proposal", " rule_reversion_proposal ",
         'public . "rule_reversion_proposal"']


@pytest.mark.parametrize("name", NAMES)
def test_allowlist_holds_however_the_table_name_is_cased_or_quoted(name):
    """QA-058 residual: quoting, casing or a schema prefix must not bypass the six-column allowlist."""
    assert st_mod._table_key(name) == T
    st = StateStore("sqlite")
    with pytest.raises(ValueError, match="rationale"):
        with st.transaction() as q:
            st.decide_pending(q, name, "proposal_id", "P", {"status": "applied", "rationale": "rewritten"})


def test_other_tables_are_not_caught_by_the_normalization():
    assert {st_mod._table_key(x) for x in ("rule_reversion_proposal_x", "dam_cdse_change", '"a"."b"')} == {
        "rule_reversion_proposal_x", "dam_cdse_change", "b"}


def test_verify_endpoints_rerun_the_owner_check(k, monkeypatch):  # noqa: F811
    """QA-058 residual (as QA-053 for the signer): both chain verifications re-run the check and fail closed."""
    from conftest import ADMIN
    assert k.get("/api/audit/signoffs/verify", user=ADMIN).json()["ok"]          # sqlite demo: no owner model
    err = OwnerSchemaError(CODE_REVERSION_GUARD_DISABLED, "trigger disabled")
    monkeypatch.setattr(_st(k), "verify_owner_tables", lambda: err)
    _approved_dssmp(k)                                                           # a chain with records
    v = k.get("/api/audit/signoffs/verify", user=ADMIN).json()
    assert v["ok"] is False and v["reason"] == CODE_REVERSION_GUARD_DISABLED
    assert v["chains"] and all(c["ok"] is False and c["reason"] == CODE_REVERSION_GUARD_DISABLED for c in v["chains"])
    d = k.get("/api/dams/CRD/audit/signoffs/verify", user=CDSE).json()
    assert d["ok"] is False and d["reason"] == CODE_REVERSION_GUARD_DISABLED and d["chain_id"] == "CRD"


def test_sqlite_demo_still_creates_the_table():
    assert any(f"{{s}}{T} (" in d for d in st_mod.DDL) and T in st_mod.OWNER_MANAGED


# --------------------------------------------------------------- live, as the restricted app role
def _pool(conn):
    pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    return ConnectionPool(conn, min_size=1, max_size=2, kwargs={"row_factory": dict_row}, open=True)


def _row(pid, dam):
    return {"proposal_id": pid, "dam_id": dam, "to_dssmp_version_id": "DS-2", "rules": "[]", "proposed_by": "a@x.org",
            "proposed_by_id": None, "proposed_at": "2026-11-09T19:00:00Z", "rationale": "r", "status": "pending",
            "decided_by": None, "decided_by_id": None, "decided_at": None, "decision_rationale": None, "pub_ids": None}


def _su(su, sql):
    import psycopg
    with psycopg.connect(su, autocommit=True) as c:
        c.execute(sql)


@live
def test_live_app_role_insert_and_allowed_update_work_everything_else_is_refused(lakebase):  # noqa: F811
    import psycopg
    su, app = lakebase
    pool = _pool(app)
    try:
        st = StateStore("postgres", schema="dam_app", pg_pool=pool, retries=1)     # startup check passes
        with st.transaction() as q:
            st.insert_row(q, T, _row("P1", "D1"))
            st.insert_row(q, T, _row("P2", "D2"))
        with st.transaction() as q:                                               # the allowed UPDATE (approve)
            st.decide_pending(q, T, "proposal_id", "P1", {"status": "applied", "decided_by": "c@x", "decided_by_id": "u",
                                                          "decided_at": "2026-11-09T19:01:00Z",
                                                          "decision_rationale": "ok", "pub_ids": '["PUB-1"]'})
        with st.transaction() as q:                                               # reject
            st.decide_pending(q, T, "proposal_id", "P2", {"status": "rejected", "decided_by": "c@x",
                                                          "decided_by_id": "u", "decided_at": "2026-11-09T19:02:00Z",
                                                          "decision_rationale": "no"})
        got = {r["proposal_id"]: r["status"] for r in st.execute("SELECT proposal_id, status FROM dam_app." + T)}
        assert got == {"P1": "applied", "P2": "rejected"}
        with st.transaction() as q:
            st.insert_row(q, T, _row("P3", "D3"))
        refused = [
            ("DELETE FROM {s}rule_reversion_proposal WHERE proposal_id = 'P3'", psycopg.errors.InsufficientPrivilege),
            ("UPDATE {s}rule_reversion_proposal SET rationale = 'x' WHERE proposal_id = 'P3'",
             psycopg.errors.InsufficientPrivilege),
            ("UPDATE {s}rule_reversion_proposal SET rules = '[]' WHERE proposal_id = 'P3'",
             psycopg.errors.InsufficientPrivilege),
            ("UPDATE {s}rule_reversion_proposal SET status = 'pending' WHERE proposal_id = 'P1'",
             psycopg.errors.CheckViolation),                                      # applied -> pending
            ("UPDATE {s}rule_reversion_proposal SET status = 'applied' WHERE proposal_id = 'P2'",
             psycopg.errors.CheckViolation),                                      # rejected -> applied
            ("UPDATE {s}rule_reversion_proposal SET decision_rationale = 'rewritten' WHERE proposal_id = 'P1'",
             psycopg.errors.CheckViolation),                                      # a final row is frozen
            ("ALTER TABLE {s}rule_reversion_proposal DISABLE TRIGGER t_status_rule_reversion_proposal",
             psycopg.errors.InsufficientPrivilege),
        ]
        for sql, err in refused:
            with pytest.raises(err):
                with st.transaction() as q:
                    q(sql)
        with pytest.raises(ValueError):                                           # app-side guard, before any SQL
            with st.transaction() as q:
                st.decide_pending(q, T, "proposal_id", "P3", {"status": "applied", "rationale": "x"})
        rows = st.execute("SELECT proposal_id, status, rationale FROM dam_app." + T + " ORDER BY proposal_id")
        assert [(r["proposal_id"], r["status"], r["rationale"]) for r in rows] == [
            ("P1", "applied", "r"), ("P2", "rejected", "r"), ("P3", "pending", "r")]
    finally:
        pool.close()


@live
@pytest.mark.parametrize("change,code", [
    ("DISABLE TRIGGER t_status_rule_reversion_proposal", CODE_REVERSION_GUARD_DISABLED),
    ("ENABLE REPLICA TRIGGER t_status_rule_reversion_proposal", CODE_REVERSION_GUARD_DISABLED),
    ("ENABLE TRIGGER t_status_rule_reversion_proposal", CODE_REVERSION_GUARD_DISABLED),       # QA-060: origin
    ("OWNER TO {app}", CODE_REVERSION_NOT_OWNER_OWNED),
    (None, CODE_REVERSION_GUARD_DISABLED)])                                        # trigger dropped
def test_live_startup_fails_closed(lakebase, change, code):  # noqa: F811
    su, app = lakebase
    role = app.split("user=")[-1]
    if change is None:
        _su(su, f"DROP TRIGGER t_status_rule_reversion_proposal ON dam_app.{T}")
    else:
        _su(su, f"ALTER TABLE dam_app.{T} " + change.format(app=role))
    pool = _pool(app)
    try:
        with pytest.raises(OwnerSchemaError) as e:
            StateStore("postgres", schema="dam_app", pg_pool=pool, retries=1)
        assert e.value.code == code
    finally:
        pool.close()


@live
def test_live_origin_only_trigger_refuses_startup_qa060(lakebase):  # noqa: F811
    """QA-060: with tgenabled 'O' the guard does not fire under session_replication_role = replica (a frozen row can
    go applied -> pending), so the app must refuse to start; with 'A' (lakebase/001) the same move is refused."""
    import psycopg
    su, app = lakebase
    pool = _pool(app)
    try:
        st = StateStore("postgres", schema="dam_app", pg_pool=pool, retries=1)
        with st.transaction() as q:
            st.insert_row(q, T, _row("PO", "DO"))
        with st.transaction() as q:
            st.decide_pending(q, T, "proposal_id", "PO", {"status": "applied"})
    finally:
        pool.close()
    replica = f"SET session_replication_role = replica; UPDATE dam_app.{T} SET status = 'pending' WHERE proposal_id = 'PO'"
    with pytest.raises(psycopg.errors.CheckViolation):                          # 'A': fires under replica too
        _su(su, replica)
    _su(su, f"ALTER TABLE dam_app.{T} ENABLE TRIGGER t_status_rule_reversion_proposal")
    with psycopg.connect(su) as c:
        assert c.execute("SELECT tgenabled::text FROM pg_trigger WHERE tgname = 't_status_rule_reversion_proposal'"
                         ).fetchone()[0] == "O"
        c.execute("BEGIN")
        c.execute("SET LOCAL session_replication_role = replica")
        c.execute(f"UPDATE dam_app.{T} SET status = 'pending' WHERE proposal_id = 'PO'")   # the QA-060 hole
        assert c.execute(f"SELECT status FROM dam_app.{T} WHERE proposal_id = 'PO'").fetchone()[0] == "pending"
        c.rollback()                                                                       # keep the row applied
    pool = _pool(app)
    try:
        with pytest.raises(OwnerSchemaError) as e:
            StateStore("postgres", schema="dam_app", pg_pool=pool, retries=1)
        assert e.value.code == CODE_REVERSION_GUARD_DISABLED and "origin-only" in str(e.value)
    finally:
        pool.close()


@live
def test_live_verify_endpoints_report_a_change_after_startup(lakebase, k, monkeypatch):  # noqa: F811
    """QA-058 residual, live: a StateStore starts on a correct lakebase/001 DB as the restricted role; the trigger is
    then set to origin-only / disabled or the owner is changed - both verify endpoints report it, and recover after.
    The endpoints run on the demo app (its users / roles); their owner re-check is the live store's."""
    from conftest import ADMIN, ENG
    su, app = lakebase
    role = app.split("user=")[-1]
    pool = _pool(app)
    try:
        st = StateStore("postgres", schema="dam_app", pg_pool=pool, retries=1)
        monkeypatch.setattr(_st(k), "verify_owner_tables", st.verify_owner_tables)
        get_all = lambda: k.get("/api/audit/signoffs/verify", user=ADMIN).json()
        get_dam = lambda: k.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()
        assert st.verify_owner_tables() is None and get_all()["ok"] is True
        for change, code in ((f"ENABLE TRIGGER {st_mod.REVERSION_TRIGGER}", CODE_REVERSION_GUARD_DISABLED),
                             (f"DISABLE TRIGGER {st_mod.REVERSION_TRIGGER}", CODE_REVERSION_GUARD_DISABLED),
                             (f"OWNER TO {role}", CODE_REVERSION_NOT_OWNER_OWNED)):
            _su(su, f"ALTER TABLE dam_app.{T} {change}")
            a, d = get_all(), get_dam()
            assert a["ok"] is False and a["reason"] == code, a
            assert d["ok"] is False and d["reason"] == code, d
            _su(su, f"ALTER TABLE dam_app.{T} OWNER TO dam_lakebase_owner")
            _su(su, f"ALTER TABLE dam_app.{T} ENABLE ALWAYS TRIGGER {st_mod.REVERSION_TRIGGER}")
            assert get_all()["ok"] is True and st.verify_owner_tables() is None
    finally:
        pool.close()
