"""Internal arithmetic regression, not vendor parity; all restricted values read by path.

Set DAM_IVANCICH_CSV to an original CNR-IRPI CSV outside the repo. No snapshots,
private expected values or dataset bytes are written by this test.
"""
import csv
import math
import os
from pathlib import Path

import pytest

from dam_safety.inclinometer.installation import Probe
from dam_safety.inclinometer.reduction import reduce_survey


def test_ivancich_raw_reduction_by_path():
    value = os.environ.get("DAM_IVANCICH_CSV")
    if not value or not Path(value).is_file():
        pytest.skip("DAM_IVANCICH_CSV absent; restricted regression reads by path only")
    source = Path(value).resolve()
    repo = Path(__file__).resolve().parents[1]
    assert not source.is_relative_to(repo), "restricted source must remain outside the repo"
    with source.open(encoding="latin-1", newline="") as f:
        records = list(csv.reader(f, delimiter=";"))
    header_i = next(i for i, r in enumerate(records) if r and r[0] == "ID_Inclinometer")
    # Sensitivity is metadata in the source, never a hard-coded restricted constant.
    sensitivity = {}
    for r in records[:header_i]:
        for axis in ("A", "B"):
            marker = "Sens " + axis + ":"
            if marker in r:
                sensitivity[axis] = float(r[r.index(marker) + 1].replace(",", "."))
    header = records[header_i]
    rows = [dict(zip(header, r)) for r in records[header_i + 1:]
            if len(r) == len(header) and r[0] and r[0] != header[0]]
    assert rows and set(sensitivity) == {"A", "B"}
    # Exercise one complete acquisition. L comes from its depth grid, not a guessed Pace unit.
    first = rows[0]["ID_Acquisition"]
    rows = [r for r in rows if r["ID_Acquisition"] == first]
    num = lambda x: float(x.replace(",", "."))
    depths = sorted(num(r["Depth [m]"]) for r in rows)
    steps = [b - a for a, b in zip(depths, depths[1:])]
    assert steps and all(s > 0 for s in steps)
    L = steps[0]
    assert all(math.isclose(s, L, abs_tol=1e-8) for s in steps)
    for axis in ("A", "B"):
        K = sensitivity[axis]
        assert K > 0
        D = {(num(r["Depth [m]"]), axis): num(r[axis + "0"]) - num(r[axis + "180"])
             for r in rows}
        reduced = reduce_survey(D, None, Probe(L, K, "source digits"), axis)
        expected = {d: L * D[(d, axis)] * 1000 / (2 * K) for d in depths}
        assert len(reduced) == len(depths)
        for r in reduced:
            assert r.dev_mm == pytest.approx(expected[r.depth_top_m])
            assert r.cum_dev_mm == pytest.approx(math.fsum(
                expected[d] for d in depths if d >= r.depth_top_m))
