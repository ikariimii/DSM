"""PFM-linked rule engine. Each monitored PFM (ref.pfm) lists instruments (ref.pfm_instrument) with a
role and an expected_signature. The SMP must cover each Category II PFM and give action limits per
instrument [FERC-CH14 §14 PDF p34]; this engine turns that mapping into advisory findings
(ops.rule_finding) - it does NOT replace thresholds or the alarm state machine.

Signatures (evaluated on HST residuals when a model exists, else on corrected values):
  RISING_TREND              OLS slope > 0 with t-stat > t_crit over the window
  FALLING_TREND             slope < 0, t-stat < −t_crit
  RISING_HEAD_WITH_POOL     value ~ a + b·t + c·pool with b > 0 (t-stat > t_crit) AND c > 0 (t-stat > t_crit):
                            head responds to the pool AND rises beyond that response
  ACCELERATING_DISPLACEMENT quadratic term of value ~ a + b·t + c·t² has c > 0 with t-stat > t_crit
                            and the recent slope > 0
t_crit = 2 is an UNVERIFIED engineering defaults (VERIFICATION.md INT-PFM).
Severity: INFO (only SUPPORTING/CONTEXT instruments match), WATCH (a PRIMARY instrument matches),
CONCERN (≥ 2 instruments match incl. a PRIMARY and none is classified SENSOR_FAULT)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

RULE_VERSION = "1.0.0"
SIGNATURES = ("RISING_TREND", "FALLING_TREND", "RISING_HEAD_WITH_POOL", "ACCELERATING_DISPLACEMENT")


def _ols(t, y, deg):
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    X = np.column_stack([t ** k for k in range(deg + 1)])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    r = y - X @ coef
    dof = len(y) - X.shape[1]
    if dof <= 0:
        return coef, np.full(len(coef), np.inf)
    s2 = float(r @ r) / dof
    cov = s2 * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.maximum(np.diag(cov), 0))
    return coef, se


def _tstat(c, se):
    if se == 0:
        return np.inf if c > 0 else (-np.inf if c < 0 else 0.0)
    return c / se


def signature_matches(signature: str, t_days, values, pool=None, t_crit: float = 2.0):
    """Returns (matched: bool, stats: dict)."""
    t = np.asarray(t_days, float)
    y = np.asarray(values, float)
    m = np.isfinite(t) & np.isfinite(y)
    t, y = t[m], y[m]
    if len(y) < 6:
        return False, {"n": int(len(y)), "reason": "too few points"}
    tc = t - t.mean()
    coef, se = _ols(tc, y, 1)
    ts = _tstat(coef[1], se[1])
    stats = {"n": int(len(y)), "slope_per_day": float(coef[1]), "t_slope": float(ts)}
    if signature == "RISING_TREND":
        return bool(ts > t_crit), stats
    if signature == "FALLING_TREND":
        return bool(ts < -t_crit), stats
    if signature == "RISING_HEAD_WITH_POOL":
        if pool is None:
            return False, dict(stats, reason="no pool series")
        p = np.asarray(pool, float)[m]
        r = float(np.corrcoef(y, p)[0, 1]) if np.std(p) > 0 and np.std(y) > 0 else 0.0
        # y = a + b·t + c·pool : head trend b after removing the pool response c
        X = np.column_stack([np.ones_like(tc), tc, p])
        coef3, *_ = np.linalg.lstsq(X, y, rcond=None)
        res = y - X @ coef3
        dof = len(y) - 3
        se3 = np.sqrt(np.maximum(np.diag(float(res @ res) / dof * np.linalg.pinv(X.T @ X)), 0))
        tb, tp = _tstat(coef3[1], se3[1]), _tstat(coef3[2], se3[2])
        stats.update({"corr_pool": r, "trend_after_pool_per_day": float(coef3[1]), "t_trend_after_pool": float(tb),
                      "pool_coef": float(coef3[2]), "t_pool": float(tp)})
        return bool(tb > t_crit and tp > t_crit), stats
    if signature == "ACCELERATING_DISPLACEMENT":
        c2, se2 = _ols(tc, y, 2)
        t2 = _tstat(c2[2], se2[2])
        recent_slope = c2[1] + 2 * c2[2] * tc[-1]
        stats.update({"quad": float(c2[2]), "t_quad": float(t2), "recent_slope_per_day": float(recent_slope)})
        return bool(t2 > t_crit and recent_slope > 0), stats
    raise ValueError(f"unknown signature {signature!r}")


@dataclass
class InstrumentSeries:
    instrument_id: str
    role: str                      # PRIMARY | SUPPORTING | CONTEXT
    expected_signature: str
    t_days: list
    values: list
    pool: list | None = None
    classification: str | None = None    # from fault_classifier for the latest anomaly, if any
    reading_ids: list | None = None


def evaluate_pfm(dam_id: str, pfm_id: str, series: list, window_start=None, window_end=None,
                 created_ts=None) -> dict | None:
    matches = []
    for s in series:
        ok, st = signature_matches(s.expected_signature, s.t_days, s.values, s.pool)
        if ok:
            matches.append((s, st))
    if not matches:
        return None
    primary = [s for s, _ in matches if s.role == "PRIMARY"]
    faulty = [s for s, _ in matches if s.classification == "SENSOR_FAULT"]
    if primary and len(matches) >= 2 and not faulty:
        severity = "CONCERN"
    elif primary:
        severity = "WATCH"
    else:
        severity = "INFO"
    classes = {s.classification for s, _ in matches if s.classification}
    if faulty and len(faulty) == len(matches):
        cls = "SENSOR_FAULT"
    elif len(matches) >= 2 and not faulty:
        cls = "REAL_BEHAVIOUR"
    else:
        cls = "INDETERMINATE"
    conf = {"CONCERN": 0.7, "WATCH": 0.5, "INFO": 0.3}[severity] * (0.5 if cls == "INDETERMINATE" else 1.0)
    evidence = {"instruments": [{"instrument_id": s.instrument_id, "role": s.role, "signature": s.expected_signature,
                                 "stats": st, "classification": s.classification,
                                 "reading_ids": (s.reading_ids or [])[-20:]} for s, st in matches],
                "unmatched": [s.instrument_id for s in series if s not in [m[0] for m in matches]],
                "observed_classes": sorted(classes), "rule_version": RULE_VERSION,
                "basis": "FERC-CH14 PDF p34 (SMP per Category II PFM); thresholds UNVERIFIED defaults"}
    fid = hashlib.sha256(f"{dam_id}|{pfm_id}|{window_start}|{window_end}|{RULE_VERSION}".encode()).hexdigest()[:32]
    return {"finding_id": fid, "dam_id": dam_id, "pfm_id": pfm_id, "rule_id": f"PFM_SIGNATURE:{pfm_id}",
            "rule_version": RULE_VERSION, "severity": severity, "window_start": window_start,
            "window_end": window_end, "evidence": json.dumps(evidence, default=str, sort_keys=True),
            "classification": cls, "confidence": round(conf, 3), "requires_engineer_review": True,
            "created_ts": created_ts}
