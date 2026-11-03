"""SCD2 calibration versioning and recompute planning.
source: FERC canvas REQ-23 (append-only history, recompute with lineage) — see
/workspace/dam-safety-app/SCHEMA_CONTRACT.md §3 and /workspace/dam-refs/CONVENTIONS.md
(calibration validity windows effective_from/effective_to)."""
from datetime import datetime as dt

import pytest

from dam_safety.calibration import (Version, correct_version, payload_hash, plan_recompute,
                                    resolve, scd2_insert, windows_to_sql_predicate)

V1 = Version("S1", "c1", dt(2024, 1, 1), None, "h1")


def test_resolve_picks_valid_version():
    vs = scd2_insert([V1], Version("S1", "c2", dt(2024, 6, 1), None, "h2"))
    assert resolve(vs, dt(2024, 3, 1)).version_id == "c1"
    assert resolve(vs, dt(2024, 6, 1)).version_id == "c2"
    assert resolve(vs, dt(2023, 1, 1)) is None


def test_scd2_closes_previous_and_backdated_insert():
    vs = scd2_insert([V1], Version("S1", "c3", dt(2024, 9, 1), None, "h3"))
    vs = scd2_insert(vs, Version("S1", "c2", dt(2024, 6, 1), None, "h2"))   # back-dated
    by = {v.version_id: v for v in vs}
    assert by["c1"].effective_to == dt(2024, 6, 1)
    assert by["c2"].effective_to == dt(2024, 9, 1)
    assert by["c3"].effective_to is None


def test_duplicate_effective_from_rejected():
    with pytest.raises(ValueError):
        scd2_insert([V1], Version("S1", "cX", dt(2024, 1, 1), None, "hx"))


def test_plan_recompute_new_version_window():
    old = [V1]
    new = scd2_insert(old, Version("S1", "c2", dt(2024, 6, 1), None, "h2"))
    w = plan_recompute(old, new)
    assert len(w) == 1 and w[0].start == dt(2024, 6, 1) and w[0].end is None
    assert (w[0].old_version_id, w[0].new_version_id) == ("c1", "c2")


def test_plan_recompute_correction_same_interval():
    old = scd2_insert([V1], Version("S1", "c2", dt(2024, 6, 1), None, "h2"))
    new = correct_version(old, "c1", "c1b", "h1b")
    w = plan_recompute(old, new)
    assert [(x.start, x.end, x.old_version_id, x.new_version_id) for x in w] == [
        (dt(2024, 1, 1), dt(2024, 6, 1), "c1", "c1b")]
    # retired row kept for audit but never resolves
    assert resolve(new, dt(2024, 2, 1)).version_id == "c1b"
    assert any(v.version_id == "c1" for v in new)


def test_no_change_no_windows():
    assert plan_recompute([V1], [V1]) == []


def test_payload_hash_stable_and_sensitive():
    assert payload_hash({"G": 1, "R0": 2}, "F", "1") == payload_hash({"R0": 2, "G": 1}, "F", "1")
    assert payload_hash({"G": 1}, "F", "1") != payload_hash({"G": 1.1}, "F", "1")


def test_sql_predicate_escapes_and_bounds():
    old = [Version("S'1", "c1", dt(2024, 1, 1), None, "h")]
    new = scd2_insert(old, Version("S'1", "c2", dt(2024, 6, 1), dt(2024, 7, 1), "h2"))
    p = windows_to_sql_predicate(plan_recompute(old, new))
    assert "S''1" in p and "TIMESTAMP'2024-06-01T00:00:00'" in p and "< TIMESTAMP'2024-07-01T00:00:00'" in p
    assert windows_to_sql_predicate([]) == "FALSE"
