"""QA-045d: append-only audit.chain_head; the verifier reads the latest stored head (never a caller-supplied one) and
checks every stored head as a checkpoint. Adversarial: tail truncation (1 and several records), rewound head, missing
head row, forged head_hash, head written for the wrong chain. Synthetic data (tests/fixtures/review_chain_vector.json)."""
import copy
import datetime as dt
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import audit_chain as ac  # noqa: E402
import lakebase_extract as lx  # noqa: E402
import verify_review_chain as vj  # noqa: E402

VEC = json.load(open(os.path.join(ROOT, "tests", "fixtures", "review_chain_vector.json")))
ROWS = VEC["rows"]
DAM = ROWS[0]["dam_id"]
DDL08 = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()


def H(seq, hh=None, t="2026-11-09T15:00:00", chain=DAM, dam=DAM, run="r1"):
    if hh is None:
        hh = ac.GENESIS if seq == 0 else ROWS[seq - 1]["record_hash"]
    return {"dam_id": dam, "chain_id": chain, "chain_seq": seq, "head_hash": hh, "computed_utc": t, "job_run_id": run}


def V(rows, heads):
    v = ac.verify_with_chain_heads(copy.deepcopy(rows), heads, DAM)
    return v["ok"], v["reason"], v["first_broken_seq"]


def test_intact_chain_with_stored_heads_verifies():
    assert len(ROWS) >= 3
    heads = [H(1, t="2026-11-09T10:00:00"), H(len(ROWS), t="2026-11-09T11:00:00")]
    assert V(ROWS, heads) == (True, None, None)


def test_missing_head_row_is_head_missing_never_ok():
    assert V(ROWS, []) == (False, "head_missing", len(ROWS) + 1)
    assert V(ROWS, [H(len(ROWS), chain="OTHER-DAM", dam="OTHER-DAM")]) == (False, "head_missing", len(ROWS) + 1)


def test_tail_truncation_of_one_record_is_detected():
    assert V(ROWS[:-1], [H(len(ROWS))]) == (False, "head_mismatch", len(ROWS))


def test_tail_truncation_of_several_records_is_detected():
    assert V(ROWS[:1], [H(len(ROWS))]) == (False, "head_mismatch", 2)
    assert V([], [H(len(ROWS))]) == (False, "head_mismatch", 1)           # every record deleted


def test_rewound_head_hides_nothing_because_earlier_heads_are_checkpoints():
    n = len(ROWS)
    old, rewound = H(n, t="2026-11-09T10:00:00"), H(n - 1, t="2026-11-09T12:00:00", run="attacker")
    # the app alone accepts a consistent rewind + truncation (vector case head_rewound_and_tail_deleted)...
    assert ac.verify_records(copy.deepcopy(ROWS[:-1]), None, rewound)["ok"]
    # ...the stored history does not
    assert V(ROWS[:-1], [old, rewound]) == (False, "head_mismatch", n)
    # rewound head with all records still present
    assert V(ROWS, [old, rewound]) == (False, "head_mismatch", n - 1)        # app rule: first seq past the head


def test_forged_head_hash_is_detected_latest_and_historical():
    n = len(ROWS)
    assert V(ROWS, [H(n, hh="f" * 64)]) == (False, "head_mismatch", n)
    assert V(ROWS, [H(1, hh="e" * 64, t="2026-11-09T09:00:00"), H(n)]) == (False, "head_mismatch", 1)


def test_head_written_for_the_wrong_chain_is_detected():
    n = len(ROWS)
    # a head row that claims this chain but carries another chain's hash
    assert V(ROWS, [H(n, hh="b" * 64, run="wrong-chain")])[1] == "head_mismatch"
    # a head of another chain passed straight to verify_records (chain_id differs) even with the right seq/hash
    v = ac.verify_records(copy.deepcopy(ROWS), None, {"chain_id": "OTHER", "chain_seq": n,
                                                      "head_hash": ROWS[-1]["record_hash"]})
    assert (v["ok"], v["reason"], v["first_broken_seq"]) == (False, "head_mismatch", n)
    # the latest-head lookup ignores rows of other chains/dams
    assert ac.latest_chain_head([H(n, chain="X", dam="X", t="2099-01-01")], DAM, DAM) is None


def test_records_of_another_chain_are_a_caller_bug():
    with pytest.raises(ValueError):
        ac.verify_with_chain_heads([dict(ROWS[0], chain_id="X")], [], DAM)


def test_verify_job_checks_every_chain_with_records_or_heads():
    res = vj.verify_all(copy.deepcopy(ROWS), [H(len(ROWS)), H(2, chain="GONE", dam="GONE",
                                                               hh="c" * 64)], catalog="dam_dev")
    by = {v["dam_id"]: v for v in res}
    assert by[DAM]["ok"] and not by["GONE"]["ok"] and by["GONE"]["reason"] == "head_mismatch"


# ---------------------------------------------------------------- the job writes heads
NOW = dt.datetime(2026, 11, 9, 16, 0, tzinfo=dt.timezone.utc)


def test_extract_appends_only_new_or_changed_heads():
    lb = [{"chain_id": DAM, "chain_seq": 3, "head_hash": ROWS[2]["record_hash"]},
          {"chain_id": "D2", "chain_seq": 1, "head_hash": "a" * 64}]
    rows = lx.chain_head_rows(lb, {DAM: (3, ROWS[2]["record_hash"])}, NOW, "run-7")
    assert rows == [{"dam_id": "D2", "chain_id": "D2", "chain_seq": 1, "head_hash": "a" * 64, "computed_utc": NOW,
                     "job_run_id": "run-7", "source": "APP:lakebase:review_chain_head"}]
    assert set(rows[0]) == set(ac.CHAIN_HEAD_COLS)
    # a backwards move is recorded (evidence), the verifier then flags it
    back = lx.chain_head_rows([{"chain_id": DAM, "chain_seq": 2, "head_hash": ROWS[1]["record_hash"]}],
                              {DAM: (3, ROWS[2]["record_hash"])}, NOW, "run-8")
    assert back[0]["chain_seq"] == 2


@pytest.mark.parametrize("bad", [{"chain_seq": -1, "head_hash": "a" * 64}, {"chain_seq": 1, "head_hash": "XYZ"}])
def test_extract_refuses_malformed_heads(bad):
    with pytest.raises(ValueError):
        lx.chain_head_rows([{"chain_id": DAM, **bad}], {}, NOW, "r")


def test_extract_reads_heads_in_the_same_snapshot_and_after_records():
    src = open(os.path.join(ROOT, "src", "jobs", "lakebase_extract.py")).read()
    assert "conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ" in src
    main = src[src.index("def main"):]
    assert main.index("audit.review_record\")") < main.index("FROM review_chain_head")
    jobs = open(os.path.join(ROOT, "resources", "jobs.yml")).read()
    assert '"--job_run_id", "{{job.run_id}}"' in jobs and "python_file: ../src/jobs/verify_review_chain.py" in jobs


def test_chain_head_ddl_is_append_only_with_guarded_columns():
    m = re.search(r"CREATE TABLE IF NOT EXISTS audit\.chain_head \((.*?)\n\) TBLPROPERTIES \(delta\.appendOnly = true\)",
                  DDL08, re.S)
    assert m
    cols = re.findall(r"^\s*(\w+)\s+(\w+)", m.group(1), re.M)
    assert [c for c, _ in cols] == list(ac.CHAIN_HEAD_COLS)
    assert "ADD CONSTRAINT ck_chain_head CHECK" in DDL08 and "chain_id = dam_id" in DDL08
    for f in ("10a_row_filters_table.sql", "10a_row_filters_abac.sql"):
        assert "audit.chain_head" in open(os.path.join(ROOT, "sql", "ddl", f)).read()
