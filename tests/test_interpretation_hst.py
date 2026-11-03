"""HST model + residual anomaly detection.
Sources: /workspace/dam-refs/formula_library.md §6-8; /workspace/dam-refs/verify_examples.py (HST/anomaly DEMO
blocks, lines ~119-154); /workspace/dam-refs/piezometers/verify.py WE6 and its output
/workspace/dam-refs/datasets/piezometers/synthetic/syn_pz_hst.csv; /workspace/dam-refs/Salazar_2017 Eq1-5 p3-4;
/workspace/dam-refs/ANCOLD_France p2; FERC Ch14 PDF p36 (rate limits)."""
import csv
import datetime as dt
import os

import numpy as np
import pytest

from conftest import DAM_REFS, DATASETS
from dam_safety.interpretation import anomaly, hst

SYN = os.path.join(DATASETS, "piezometers", "synthetic", "syn_pz_hst.csv")


@pytest.mark.skipif(not os.path.exists(SYN), reason="dam-refs syn_pz_hst.csv absent")
@pytest.mark.parametrize("variant", hst.VARIANTS)
def test_we6_syn_pz_hst_recovery(variant):
    """piezometers/verify.py WE6: a2 (t) 0.3±0.02, a7 (cos S) 0.8±0.02, residual std 0.05±0.005.
    Our h is normalised differently from WE6's z = (zmax-H)/(zmax-zmin), so only the season/time terms
    (which are independent of the h scaling) are compared."""
    rows = list(csv.DictReader(open(SYN)))
    d = [float(r["day"]) for r in rows]
    m = hst.fit(d, [float(r["reservoir_m"]) for r in rows], [float(r["piezo_level_m"]) for r in rows], variant)
    c = m.coefficients
    assert c["t"] == pytest.approx(0.3, abs=0.02)
    assert c["cos_s" if variant == "SALAZAR2017" else "cos_S"] == pytest.approx(0.8, abs=0.02)
    assert c["sin_s" if variant == "SALAZAR2017" else "sin_S"] == pytest.approx(-0.4, abs=0.02)   # WE6 a8
    assert c["exp_t_t0"] == pytest.approx(-1.5, abs=0.1)                                         # WE6 a1, t0 = 2 y
    assert m.t0_years == 2.0                                                                     # grid search finds true t0
    assert m.resid_sigma == pytest.approx(0.05, abs=0.005)
    assert m.adequacy == "ADEQUATE" and m.span_years > 5


def _demo():
    """Replicates verify_examples.py DEMO generator exactly (seed 42, n=3650, ANCOLD form, t0=2 y)."""
    rng = np.random.default_rng(42)
    n = 3650
    day = np.arange(n) % 365
    t = np.arange(n) / 365.25
    hn = 0.5 + 0.5 * np.sin(2 * np.pi * np.arange(n) / 365.25 + 1.0) + 0.05 * rng.standard_normal(n)
    X = hst.design(hn, day, t, "ANCOLD_FRANCE", 2.0)
    true = np.array([1.0, 2.0, -0.5, 0.3, 0.1, 0.8, -0.4, 0.2, 0.1, 0.05, -0.6])
    y = X @ true + 0.02 * rng.standard_normal(n)
    return X, y, true


def test_demo_design_matches_and_fit():
    X, y, true = _demo()
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    assert np.max(np.abs(coef - true)) <= 0.15                       # hst_fit_max_coef_error_DEMO
    resid = y - X @ coef
    assert resid.std(ddof=X.shape[1]) == pytest.approx(0.02, abs=0.002)   # hst_residual_sigma_DEMO


def test_demo_anomaly_spike_fp_mad_rate():
    X, y, _ = _demo()
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    sig = (y - X @ coef).std(ddof=X.shape[1])
    y2 = y.copy()
    y2[3000] += 10 * sig
    y2[3200:3260] += np.linspace(0, 6 * sig, 60)
    r2 = y2 - X @ coef
    z, bands = anomaly.score(r2, sig)
    assert bands[3000] == "OUTSIDE_3S"                                               # anomaly_spike_detected_DEMO
    fp = sum(1 for i in range(2990) if bands[i] == "OUTSIDE_3S")
    assert fp / 2990 == pytest.approx(0.0027, abs=0.003)                             # anomaly_false_pos_rate_DEMO
    assert hst.robust_sigma(r2) == pytest.approx(sig, abs=0.15 * sig)                # robust_mad_sigma_DEMO
    hits = np.where(anomaly.rate_flags(r2, sig, anomaly.AnomalyParams(rate_window=30, k_rate=3)))[0]
    assert any(3200 <= i <= 3290 for i in hits)                                      # rate_of_change_drift_detected_DEMO


def test_usbr_missing_sentinel_excluded():
    """USBR-EDIM-1987 PDF p224: -9999999.99999 = not read/broken -> must not enter the fit."""
    rows = list(csv.DictReader(open(SYN))) if os.path.exists(SYN) else None
    if rows is None:
        pytest.skip("dam-refs absent")
    d = [float(r["day"]) for r in rows]
    H = [float(r["reservoir_m"]) for r in rows]
    y = [float(r["piezo_level_m"]) for r in rows]
    y[100] = -9999999.99999
    m = hst.fit(d, H, y)
    assert m.n_obs == len(y) - 1 and m.resid_sigma == pytest.approx(0.05, abs=0.005)


def test_short_calibration_flagged_not_refused():
    """ICOLD ≥2-3 y (via Salazar-2017 p29): short fits are flagged."""
    X, y, _ = _demo()
    rng = np.random.default_rng(1)
    days = np.arange(400)
    H = 90 + 5 * np.sin(2 * np.pi * days / 365.25) + rng.normal(0, 0.5, 400)
    m = hst.fit(days, H, y[:400])
    assert m.adequacy == "SHORT_LT_2Y" and m.warnings


def test_band_edges():
    """formula_library.md §7: |ε| > k·σ (strict)."""
    assert anomaly.band(2.0) == "WITHIN" and anomaly.band(2.01) == "OUTSIDE_2S"
    assert anomaly.band(-3.5) == "OUTSIDE_3S" and anomaly.band(float("nan")) == "WITHIN"
    assert list(anomaly.run_lengths([0, 1, 1, 0, 1])) == [0, 1, 2, 0, 1]


def test_predict_with_dates_round_trip():
    base = dt.date(2015, 1, 1)
    days = [base + dt.timedelta(days=i) for i in range(4 * 365)]
    rng = np.random.default_rng(3)
    H = np.array([95 + 4 * np.sin(2 * np.pi * i / 365.25 + 0.7) + rng.normal(0, 0.3) for i in range(len(days))])
    s = np.array([2 * np.pi * (d.timetuple().tm_yday - 1) / 365.25 for d in days])
    y = 10 + 0.2 * (H - 95) + 0.5 * np.cos(s) + rng.normal(0, 0.01, len(days))
    m = hst.fit(days[:3 * 365], H[:3 * 365], y[:3 * 365])
    v = hst.validate(m, days[3 * 365:], H[3 * 365:], y[3 * 365:])
    assert v["rmse"] < 0.05 and v["coverage_2s"] > 0.8


TELLER = os.path.join(DATASETS, "seepage", "raw", "usgs_teller")


def _rdb(path, suffix):
    out = {}
    lines = [ln.rstrip("\n").split("\t") for ln in open(path) if not ln.startswith("#")]
    i = [k for k, h in enumerate(lines[0]) if h.endswith(suffix)][0]
    for r in lines[2:]:
        try:
            out[dt.date.fromisoformat(r[2])] = float(r[i])
        except (ValueError, IndexError):
            pass
    return out


@pytest.mark.skipif(not os.path.isdir(TELLER), reason="dam-refs Teller data absent")
def test_real_data_teller_east_seep_hst():
    """REAL DATA (unrestricted, US public domain; manifest USGS-TELLER-SEEPS / USGS-TELLER-RES in
    /workspace/dam-refs/datasets/seepage/manifest.csv). Calibrate 2001-10..2009-09, hold out 2009-10..2012-09
    (pre-2013 incident, cases.csv TEL-RA-01). No published benchmark exists for this fit, so the thresholds
    below are REGRESSION BASELINES, not literature values (VERIFICATION.md INT-REAL-1). The hold-out shows
    a strong negative bias: the linear+exponential time term extrapolates poorly -> documented gap."""
    el = _rdb(os.path.join(TELLER, "dv_07099233_00054_62614.rdb"), "62614_32400")
    q = _rdb(os.path.join(TELLER, "dv_382629104493000_00060.rdb"), "00060_00003")
    d = sorted(set(el) & set(q))
    cal = [x for x in d if x < dt.date(2009, 10, 1)]
    val = [x for x in d if dt.date(2009, 10, 1) <= x < dt.date(2012, 10, 1)]
    m = hst.fit(cal, [el[x] for x in cal], [q[x] for x in cal], "ANCOLD_FRANCE")
    assert m.adequacy == "ADEQUATE" and m.r2 > 0.5
    v = hst.validate(m, val, [el[x] for x in val], [q[x] for x in val])
    assert v["n"] > 900
    assert v["coverage_2s"] < 0.5          # documents the gap: the model does NOT transfer out of sample
