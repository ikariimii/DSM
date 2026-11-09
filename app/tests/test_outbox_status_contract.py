"""§7 item 19 (DA 6709371): dssmp_registration_outbox status is pending | loaded | rejected. No 'rejected_invalid'
in the app's CHECK, code or UI; an EXISTING table with the old CHECK is reported, never altered; a non-contract status
is handled fail closed (never loaded, never a closed CDSE assignment). Live owner-model counterpart:
test_lakebase_owner_live.py::test_old_outbox_status_check_is_reported_never_altered_and_v1_7_004_converges_it."""
import logging
import re
import sqlite3
from pathlib import Path

import pytest

from backend import onboarding as ob
from backend.repos import state as state_mod
from backend.repos.state import OUTBOX_STATUSES, StateStore

APP = Path(__file__).resolve().parents[1]
OLD_DDL = """CREATE TABLE dssmp_registration_outbox (
        outbox_id TEXT PRIMARY KEY, dam_id TEXT NOT NULL, dssmp_version_id TEXT NOT NULL, version_no INTEGER NOT NULL,
        effective_from TEXT NOT NULL, change_reason TEXT, source_ref TEXT, document_uri TEXT, document_sha256 TEXT NOT NULL,
        proposed_by TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
        approval_record_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'loaded', 'rejected', 'rejected_invalid')),
        reason_code TEXT, message TEXT, processed_at TEXT)"""
ROW = ("INSERT INTO dssmp_registration_outbox (outbox_id, dam_id, dssmp_version_id, version_no, effective_from, "
       "document_sha256, proposed_by, approved_by, approved_at, approval_record_hash, status) "
       "VALUES (?, 'D1', ?, 1, 't', 'h', 'p', 'c', 't', 'h', ?)")


def _ddl(table):
    return next(d for d in state_mod.DDL if f"{{s}}{table} (" in d)


@pytest.mark.parametrize("table", ["dssmp_registration_outbox", "dam_cdse_outbox"])
def test_app_ddl_allows_exactly_the_contract_statuses(table):
    m = re.search(r"status TEXT NOT NULL CHECK \(status IN \(([^)]*)\)\)", _ddl(table))
    assert tuple(re.findall(r"'(\w+)'", m.group(1))) == OUTBOX_STATUSES == ("pending", "loaded", "rejected")


def test_no_app_source_accepts_rejected_invalid_for_an_outbox():
    """No Python literal 'rejected_invalid' in the backend (the DDL CHECKs are checked above), and no UI badge key
    for it. Only ref_change_publication (publisher statuses) may still produce it, read through the DA's message."""
    import io
    import tokenize
    for path in (APP / "backend").rglob("*.py"):
        for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
            if tok.type == tokenize.STRING:
                assert tok.string.strip("'\"") != "rejected_invalid", (path, tok.start)
    for path in (APP / "frontend" / "src").rglob("*.ts*"):
        assert not re.search(r"\brejected_invalid\s*:", path.read_text()), path


def test_old_cdse_outbox_check_is_reported_with_its_own_migration(tmp_path, caplog):
    db = tmp_path / "old_cdse.sqlite"
    con = sqlite3.connect(db)
    con.execute(_ddl("dam_cdse_outbox").replace("{s}", "").replace("IF NOT EXISTS ", "").replace(
        "'pending', 'loaded', 'rejected'", "'pending', 'loaded', 'rejected', 'rejected_invalid'"))
    con.commit()
    con.close()
    caplog.set_level(logging.WARNING)
    st = StateStore("sqlite", sqlite_path=str(db))
    assert st.legacy_outbox_checks == ["dam_cdse_outbox:rejected_invalid"]
    assert any("v1_7_008_cdse_outbox_status.sql" in r.getMessage() for r in caplog.records)


def test_fresh_store_refuses_rejected_invalid_and_reports_nothing():
    st = StateStore("sqlite")
    assert st.legacy_outbox_checks == []
    with pytest.raises(sqlite3.IntegrityError):
        st._conn.execute(ROW, ("OB-1", "DS-1", "rejected_invalid"))
    for i, s in enumerate(OUTBOX_STATUSES):
        st._conn.execute(ROW, (f"OB-{s}", f"DS-{i}", s))


def test_existing_table_with_the_old_check_is_reported_not_altered(tmp_path, caplog):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute(OLD_DDL)
    con.execute(ROW, ("OB-RI", "DS-RI", "rejected_invalid"))
    con.commit()
    con.close()
    caplog.set_level(logging.WARNING)
    st = StateStore("sqlite", sqlite_path=str(db))                            # init_schema runs in the constructor
    assert st.legacy_outbox_checks == ["dssmp_registration_outbox:rejected_invalid"]
    msg = [r.getMessage() for r in caplog.records if "dssmp_registration_outbox" in r.getMessage()]
    assert msg and "app-owned" in msg[0] and "v1_7_004" in msg[0] and "does NOT alter" in msg[0]
    sql = st.execute("SELECT sql FROM sqlite_master WHERE name = 'dssmp_registration_outbox'")[0]["sql"]
    assert "'rejected_invalid'" in sql                                       # untouched
    st._conn.execute(ROW, ("OB-NEW", "DS-NEW", "pending"))                     # the app keeps working


def test_a_non_contract_outbox_status_is_fail_closed_and_explained():
    regs = [{"dam_id": "D1", "dssmp_version_id": "DS-RI", "status": "approved", "outbox_id": "OB-RI"}]
    note = ob._pending_note("D1", "DS-RI", regs, [{"outbox_id": "OB-RI", "status": "rejected_invalid"}])
    assert "not a contract status" in note and "v1_7_004" in note
    ok = ob._pending_note("D1", "DS-RI", regs, [{"outbox_id": "OB-RI", "status": "rejected", "reason_code": "x"}])
    assert "rejected outbox OB-RI: x" in ok


def test_cdse_assignment_is_closed_only_by_the_contract_rejected():
    """Only the contract 'rejected' closes a first-CDSE assignment; any other outbox status keeps it open (409
    cdse_assign_pending, fail closed) instead of letting a second assignment through."""
    from backend.routers import dam_cdse
    for status, open_ in (("rejected", False), ("rejected_invalid", True), ("pending", True), ("loaded", True),
                          (None, True)):
        rows = [{"change_id": "C1", "outbox_status": status}]
        assert (dam_cdse._open_assignment_q(lambda sql, params=(), _r=rows: _r, "D1") is not None) is open_, status


def test_ui_accepts_only_contract_outbox_statuses():
    src = {p.name: p.read_text() for p in (APP / "frontend" / "src" / "pages").glob("*.tsx")}
    assert "rejected_invalid" not in src["DssmpRegistrations.tsx"] + src["CdsePanel.tsx"]
    badge = re.search(r"const OUTBOX_BADGE[^=]*= \{([^}]*)\}", src["DssmpRegistrations.tsx"]).group(1)
    assert set(re.findall(r"(\w+):", badge)) == set(OUTBOX_STATUSES)
    assert "not a contract status" in src["DssmpRegistrations.tsx"] and "not a contract status" in src["CdsePanel.tsx"]
