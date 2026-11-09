"""QA-045d (independent of tests/test_chain_head.py): audit.chain_head + verify_with_chain_heads.

Derived from SCHEMA_CONTRACT / engine audit_chain.py + lakebase_extract.chain_head_rows intent.
"""
from __future__ import annotations

import copy
import hashlib
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "jobs"))
sys.path.insert(0, str(ROOT / "app"))

from dam_safety import audit_chain as ac  # noqa: E402
import lakebase_extract as lx  # noqa: E402


def _row(n, dam="CRD", prev=None):
    prev = prev or ac.GENESIS
    base = {"review_id": f"RS-{n}", "dam_id": dam, "subject_type": "alarm_event", "subject_id": f"E{n}",
            "review_level": "engineer", "decision": "acknowledge", "comment": f"c{n}", "reviewer_id": "u",
            "reviewer_pe_license": None, "signed_ts": "2026-11-09T00:00:00.000000+00:00",
            "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
            "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}",
            "chain_id": dam, "chain_seq": n, "prev_hash": prev, "signature": None, "sig_alg": None, "key_id": None}
    base["record_hash"] = ac.record_hash(base)
    return base


def _chain(n=4, dam="CRD"):
    rows, prev = [], ac.GENESIS
    for i in range(1, n + 1):
        r = _row(i, dam=dam, prev=prev)
        rows.append(r)
        prev = r["record_hash"]
    return rows


def _H(seq, rows, hh=None, t="2026-11-09T12:00:00", chain="CRD", dam="CRD", run="r1"):
    if hh is None:
        hh = ac.GENESIS if seq == 0 else rows[seq - 1]["record_hash"]
    return {"dam_id": dam, "chain_id": chain, "chain_seq": seq, "head_hash": hh,
            "computed_utc": t, "job_run_id": run, "source": lx.CHAIN_HEAD_SOURCE}


def test_verify_records_head_none_with_rows_is_head_missing():
    rows = _chain()
    v = ac.verify_records(rows, head=None)
    assert v["ok"] is False and v["reason"] == "head_missing"


def test_tail_truncation_detected_via_stored_head():
    rows = _chain(4)
    heads = [_H(4, rows)]
    assert ac.verify_with_chain_heads(rows[:-1], heads, "CRD")["reason"] == "head_mismatch"
    assert ac.verify_with_chain_heads(rows[:1], heads, "CRD")["reason"] == "head_mismatch"
    assert ac.verify_with_chain_heads([], heads, "CRD")["reason"] == "head_mismatch"


def test_head_past_last_row_or_wrong_hash_caught():
    rows = _chain(3)
    assert ac.verify_records(rows, head={"chain_id": "CRD", "chain_seq": 99,
                                         "head_hash": rows[-1]["record_hash"]})["reason"] == "head_mismatch"
    assert ac.verify_records(rows, head={"chain_id": "CRD", "chain_seq": 3,
                                         "head_hash": "f" * 64})["reason"] == "head_mismatch"


def test_rewound_latest_head_caught_by_earlier_checkpoint():
    rows = _chain(4)
    old = _H(4, rows, t="2026-11-09T10:00:00")
    rewound = _H(3, rows, t="2026-11-09T13:00:00", run="attacker")
    # consistent rewind+truncation alone would look ok to verify_records
    assert ac.verify_records(rows[:-1], head=rewound)["ok"] is True
    # stored history catches it
    v = ac.verify_with_chain_heads(rows[:-1], [old, rewound], "CRD")
    assert v["ok"] is False and v["reason"] == "head_mismatch"


def test_extract_appends_only_changed_heads_never_updates():
    rows = _chain(2)
    lb = [{"chain_id": "CRD", "chain_seq": 2, "head_hash": rows[-1]["record_hash"]}]
    first = lx.chain_head_rows(lb, {}, "2026-11-09T10:00:00", "run-1")
    assert len(first) == 1 and first[0]["chain_seq"] == 2
    # uc_latest is {chain_id: (seq, hash)} — unchanged → nothing to append
    latest = {"CRD": (2, rows[-1]["record_hash"])}
    assert lx.chain_head_rows(lb, latest, "2026-11-09T11:00:00", "run-2") == []
    # advanced → one new append row (prior rows stay; table is append-only)
    lb2 = [{"chain_id": "CRD", "chain_seq": 3, "head_hash": "a" * 64}]
    nxt = lx.chain_head_rows(lb2, latest, "2026-11-09T12:00:00", "run-3")
    assert len(nxt) == 1 and nxt[0]["chain_seq"] == 3 and nxt[0]["job_run_id"] == "run-3"


def test_ddl_declares_append_only_chain_head():
    ddl = (ROOT / "sql/ddl/08_audit.sql").read_text()
    mig = (ROOT / "sql/migrations/v1_6_002_chain_head.sql").read_text()
    assert "delta.appendOnly" in ddl and "delta.appendOnly" in mig
    assert "SET TBLPROPERTIES ('delta.appendOnly' = 'true')" in mig
    # migration DML: no UPDATE/DELETE of existing rows (comment text may mention the words)
    code = "\n".join(l for l in mig.splitlines() if not l.lstrip().startswith("--"))
    assert "UPDATE" not in code.upper()
    assert "DELETE FROM" not in code.upper()
    assert "Nothing is updated or deleted" in mig or "updated or deleted" in mig.lower()


def test_concurrent_head_advance_serialized_by_latest_selection():
    """Simulated concurrent extract writers: latest_chain_head picks max computed_utc then seq (deterministic)."""
    rows = _chain(3)
    h_a = _H(2, rows, t="2026-11-09T12:00:00.100000", run="a")
    h_b = _H(3, rows, t="2026-11-09T12:00:00.100000", run="b")  # same ts, higher seq wins
    h_c = _H(1, rows, t="2026-11-09T12:00:00.200000", run="c")  # later ts wins even if lower seq
    assert ac.latest_chain_head([h_a, h_b], "CRD", "CRD")["job_run_id"] == "b"
    assert ac.latest_chain_head([h_a, h_b, h_c], "CRD", "CRD")["job_run_id"] == "c"
    # verify uses that latest; earlier heads remain checkpoints
    v = ac.verify_with_chain_heads(rows, [h_a, h_b], "CRD")
    assert v["ok"] is True
    # if latest points past available rows → mismatch
    assert ac.verify_with_chain_heads(rows[:2], [h_a, h_b], "CRD")["reason"] == "head_mismatch"
