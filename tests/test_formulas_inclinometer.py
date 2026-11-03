"""Probe inclinometer worked examples.
source files: /workspace/dam-refs/DGSI_Digitilt_Probe.pdf (PDF p.12) and
/workspace/dam-refs/data_reduction.pdf (Slope Indicator 2001, p.2-3)."""
import pytest

from dam_safety.formulas import core
from dam_safety.formulas.core import InclDepthReading as R


def test_combined_reading_349():
    # source: DGSI_Digitilt_Probe.pdf PDF p.12: A0=359, A180=-339 -> combined 349
    assert core.incl_combined(359, -339) == 349


def test_deviation_0_4188_in():
    # source: DGSI_Digitilt_Probe.pdf PDF p.12: L=24 in, K=20000 -> 0.4188 in
    assert core.incl_deviation(359, -339, 24, 20000) == pytest.approx(0.4188, abs=5e-5)


def test_displacement_0_0012_in():
    # source: DGSI_Digitilt_Probe.pdf PDF p.12: diff 700 vs 698 -> 0.0012 in
    assert 24 * (700 - 698) / (2 * 20000) == pytest.approx(0.0012, abs=1e-5)
    p = core.incl_profile([R(10, 350, -350)], [R(10, 349, -349)], 24, 20000)
    assert p[0].inc_a == pytest.approx(0.0012, abs=1e-5)


def test_checksum_20():
    # source: DGSI_Digitilt_Probe.pdf p.12 / data_reduction.pdf p.2: 359 + (-339) = 20
    assert core.incl_checksum(359, -339) == 20


def test_bias_cancels_in_deviation():
    # source: data_reduction.pdf p.2 (two-pass survey cancels probe bias)
    bias = 15
    assert core.incl_deviation(359 + bias, -339 + bias, 24, 20000) == core.incl_deviation(359, -339, 24, 20000)


def _surveys():
    init = [R(0.0, 0, 0, 0, 0), R(0.5, 0, 0, 0, 0), R(1.0, 0, 0, 0, 0)]
    # each interval moves +10 reading units (diff +20) on A; L=500 mm, K=25000 -> 0.2 mm each
    cur = [R(0.0, 10, -10, 0, 0), R(0.5, 10, -10, 0, 0), R(1.0, 10, -10, 0, 0)]
    return cur, init


def test_profile_bottom_fixed_cumulates_upward():
    # source: data_reduction.pdf p.3 (incremental); cumulative summed from fixed bottom
    # [/workspace/dam-refs/6000_Inclinometer_Probe.pdf §2; dam-refs/CONVENTIONS.md]
    cur, init = _surveys()
    p = core.incl_profile(cur, init, 500, 25000, fixed="bottom")
    assert [round(x.inc_a, 9) for x in p] == [0.2, 0.2, 0.2]
    assert [round(x.cum_a, 9) for x in p] == [0.6, 0.4, 0.2]
    assert p[0].resultant == pytest.approx(0.6)


def test_profile_top_fixed():
    # source: data_reduction.pdf p.3; top-fixed: cum(d) = -sum(inc above d), zero at top
    cur, init = _surveys()
    p = core.incl_profile(cur, init, 500, 25000, fixed="top")
    assert [round(x.cum_a, 9) for x in p] == [0.0, -0.2, -0.4]


def test_metric_constant():
    # source: DGSI_Digitilt_Probe.pdf p.12: 25000 x sin(1°) ≈ 436.3
    import math
    assert 25000 * math.sin(math.radians(1)) == pytest.approx(436.3, abs=0.1)


def test_checksum_flags_outlier():
    # source: data_reduction.pdf p.2 (checksum ~constant); tolerance is a site setting (UNVERIFIED default)
    flags = core.incl_checksum_flags([20, 21, 19, 20, 60, 20], tolerance=10)
    assert flags == [False, False, False, False, True, False]
