"""QC flags. Rule basis: /workspace/dam-refs/FERC_Eng_Guidelines_Ch9_Instrumentation.pdf PDF p.47
(limits per instrument), /workspace/dam-refs/data_reduction.pdf p.2 (inclinometer checksums),
/workspace/dam-refs/CONVENTIONS.md (qa_flag vocabulary). Numeric defaults are UNVERIFIED site
settings (docs/VERIFICATION.md)."""
from datetime import datetime, timedelta

from dam_safety.qc import QcParams, checksum_qc, find_gaps, qc_series, range_check, robust_z

T0 = datetime(2025, 1, 1)
ts = [T0 + timedelta(hours=i) for i in range(40)]


def test_range():
    p = QcParams(min_valid=0, max_valid=10)
    assert range_check(11, p) and range_check(-1, p) and range_check(5, p) is None
    assert range_check(None, p)  # null -> BAD


def test_spike_flagged_and_excluded_from_history():
    vals = [10.0 + 0.01 * (i % 3) for i in range(40)]
    vals[20] = 15.0
    r = qc_series(ts, vals, QcParams(spike_window=12, spike_n_sigma=6, stuck_count=100))
    assert r[20].flag == "SUSPECT" and "SPIKE" in r[20].reasons
    assert all(x.flag == "OK" for i, x in enumerate(r) if i != 20)


def test_stuck():
    vals = [5.0] * 30
    r = qc_series(ts[:30], vals, QcParams(stuck_count=24, spike_window=1000))
    assert r[22].flag == "OK" and r[23].flag == "SUSPECT" and "STUCK" in r[23].reasons


def test_gaps_and_stale_tail():
    t = [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=5)]
    g = find_gaps(t, 3600, 2.0, until=T0 + timedelta(hours=10))
    assert g == [(t[1], t[2]), (t[2], T0 + timedelta(hours=10))]


def test_checksum_qc():
    # data_reduction.pdf p.2: checksum ~constant through survey
    r = checksum_qc([20, 20, 21, 80], tol=10)
    assert [x.flag for x in r] == ["OK", "OK", "OK", "SUSPECT"]


def test_robust_z_zero_mad_safe():
    assert robust_z(1.0, [1.0, 1.0, 1.0]) >= 0
