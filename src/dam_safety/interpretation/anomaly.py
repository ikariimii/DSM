"""Residual-based anomaly detection on HST residuals.

z = ε/σ_ε, bands at k·σ (k_warn = 2, k_alert = 3)           [formula_library.md §7-8; SALAZAR-2017 Eq1 p3, p25, p29]
   k values are common practice but UNVERIFIED against a free primary source -> configurable, engineer-owned.
σ: robust σ = 1.4826·MAD of calibration residuals so past anomalies do not inflate the band [formula_library §7].
Rate of change: |ε(t) − ε(t−w)| > k_r·σ_ε  - FERC 'magnitude and rate of change limits'
   [FERC-CH14 §14.4.1 PDF p36 (printed 14-33)]; w and k_r are UNVERIFIED engineering choices.
Residual scores are ADVISORY: FERC-CH14 PDF p36 'statistical analysis ... no substitute for judgment'."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AnomalyParams:
    k_warn: float = 2.0
    k_alert: float = 3.0
    rate_window: int = 30        # samples (daily series -> 30 days); UNVERIFIED
    k_rate: float = 3.0          # UNVERIFIED


def band(z: float, p: AnomalyParams = AnomalyParams()) -> str:
    if z is None or not np.isfinite(z):
        return "WITHIN"
    a = abs(z)
    return "OUTSIDE_3S" if a > p.k_alert else ("OUTSIDE_2S" if a > p.k_warn else "WITHIN")


def score(residuals, sigma: float, p: AnomalyParams = AnomalyParams()):
    if not sigma or sigma <= 0:
        raise ValueError("sigma must be > 0")
    r = np.asarray(residuals, float)
    z = r / sigma
    return z, [band(v, p) for v in z]


def rate_flags(residuals, sigma: float, p: AnomalyParams = AnomalyParams()) -> np.ndarray:
    """Boolean per sample: |ε_i − ε_{i−w}| > k_r·σ (first w samples False)."""
    r = np.asarray(residuals, float)
    out = np.zeros(len(r), bool)
    w = p.rate_window
    if len(r) > w:
        d = np.abs(r[w:] - r[:-w])
        out[w:] = d > p.k_rate * sigma
    return out


def run_lengths(flags) -> np.ndarray:
    """Length of the consecutive True run ending at each index (persistence of an excursion)."""
    out = np.zeros(len(flags), int)
    c = 0
    for i, f in enumerate(flags):
        c = c + 1 if f else 0
        out[i] = c
    return out
