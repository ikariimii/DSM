"""Hydrostatic-Season-Time (HST) behaviour model.

Model  Y = Ŷ + ε                                   [SALAZAR-2017 Eq1 p3]
       Ŷ = F1(h) + F2(s) + F3(t)                    [SALAZAR-2017 Eq2-5 p4]
F1 = a0 + a1·h + a2·h² + a3·h³ + a4·h⁴, h = reservoir level normalised to [0,1] on the training
     range (formula_library.md §6 engine note: normalise before building h⁴).
F2 variant SALAZAR2017:   cos s, sin s, sin² s, sin s·cos s     [SALAZAR-2017 Eq4 p4]
   variant ANCOLD_FRANCE: cos S, sin S, sin 2S, cos 2S          [ANCOLD-FRANCE p2]
   s = 2π·d/365.25 with d = day of year (days since 1 January).
F3 = a·t + b·e^(−t/t0), t in years since the model reference date [ANCOLD-FRANCE p2]; the printed
     Salazar 'a9 log t + a10 e^t' is ambiguous (formula_library.md §6) and is NOT used.
t0 is chosen by grid search (not a linear coefficient; formula_library.md §6).
Fit: ordinary least squares on a calibration period. Adequacy: ≥ 2-3 years of normal operation
(ICOLD via SALAZAR-2017 p29; secondary citation) - shorter fits are flagged, not refused.
Residual σ uses ddof = number of parameters (verify_examples.py hst_residual_sigma_DEMO); a robust
σ = 1.4826·MAD is also reported (formula_library.md §7)."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np

VARIANTS = ("SALAZAR2017", "ANCOLD_FRANCE")
DEFAULT_T0_GRID_YEARS = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0)
MIN_YEARS = 2.0          # ICOLD (via SALAZAR-2017 p29)
SWISS_CYCLES = 5.0       # Swiss Committee (via SALAZAR-2017 p29)
MISSING_SENTINELS = (-9999999.99999,)   # USBR-EDIM-1987 PDF p224 'NOT READ'/'BROKEN'


def term_names(variant: str) -> list[str]:
    if variant not in VARIANTS:
        raise ValueError(f"unknown HST variant {variant!r}")
    season = (["cos_s", "sin_s", "sin2_s", "sin_cos_s"] if variant == "SALAZAR2017"
              else ["cos_S", "sin_S", "sin_2S", "cos_2S"])
    return ["a0", "h", "h2", "h3", "h4"] + season + ["t", "exp_t_t0"]


def design(h_norm, day_of_year, t_years, variant: str, t0_years: float) -> np.ndarray:
    h = np.asarray(h_norm, float)
    s = 2.0 * np.pi * np.asarray(day_of_year, float) / 365.25
    t = np.asarray(t_years, float)
    cols = [np.ones_like(h), h, h ** 2, h ** 3, h ** 4]
    if variant == "SALAZAR2017":
        cols += [np.cos(s), np.sin(s), np.sin(s) ** 2, np.sin(s) * np.cos(s)]
    elif variant == "ANCOLD_FRANCE":
        cols += [np.cos(s), np.sin(s), np.sin(2 * s), np.cos(2 * s)]
    else:
        raise ValueError(f"unknown HST variant {variant!r}")
    cols += [t, np.exp(-t / t0_years)]
    return np.column_stack(cols)


@dataclass
class HSTModel:
    variant: str
    coefficients: dict
    t0_years: float
    ref_date: dt.date | None          # None => numeric day axis supplied by caller
    h_min: float
    h_max: float
    n_obs: int
    r2: float
    rmse: float
    resid_sigma: float
    resid_mad_sigma: float
    span_years: float
    adequacy: str                     # ADEQUATE | SHORT_LT_2Y | SHORT_LT_5_CYCLES
    warnings: list = field(default_factory=list)

    def coef_vector(self):
        return np.array([self.coefficients[k] for k in term_names(self.variant)])


def _to_days(ts):
    """Accepts datetimes/dates or numeric day offsets; returns (day_of_year, abs_days float array, ref_date)."""
    ts = list(ts)
    if ts and isinstance(ts[0], (dt.date, dt.datetime)):
        d = [x.date() if isinstance(x, dt.datetime) else x for x in ts]
        frac = [(x.hour * 3600 + x.minute * 60 + x.second) / 86400.0 if isinstance(x, dt.datetime) else 0.0 for x in ts]
        ref = min(d)
        absd = np.array([(a - ref).days + f for a, f in zip(d, frac)], float)
        doy = np.array([a.timetuple().tm_yday - 1 + f for a, f in zip(d, frac)], float)
        return doy, absd, ref
    absd = np.asarray(ts, float)          # numeric: day numbers on the caller's own axis (day 0 = 1 January)
    return absd % 365.25, absd, None


def robust_sigma(r) -> float:
    r = np.asarray(r, float)
    return float(1.4826 * np.median(np.abs(r - np.median(r))))


def clean(y, h):
    """Mask for usable rows: finite and not a USBR missing sentinel (USBR-EDIM-1987 PDF p224)."""
    y = np.asarray(y, float)
    h = np.asarray(h, float)
    m = np.isfinite(y) & np.isfinite(h)
    for s in MISSING_SENTINELS:
        m &= ~np.isclose(y, s) & ~np.isclose(h, s)
    return m


def fit(ts, h, y, variant: str = "ANCOLD_FRANCE", t0_grid=DEFAULT_T0_GRID_YEARS) -> HSTModel:
    doy, absd, ref = _to_days(ts)
    h = np.asarray(h, float)
    y = np.asarray(y, float)
    m = clean(y, h)
    doy, absd, h, y = doy[m], absd[m], h[m], y[m]
    names = term_names(variant)
    if len(y) <= len(names) + 1:
        raise ValueError(f"need > {len(names) + 1} valid observations, got {len(y)}")
    h_min, h_max = float(h.min()), float(h.max())
    if h_max - h_min <= 0:
        raise ValueError("reservoir level is constant - hydrostatic terms not identifiable")
    hn = (h - h_min) / (h_max - h_min)
    t = absd / 365.25
    best = None
    for t0 in t0_grid:
        X = design(hn, doy, t, variant, t0)
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        rss = float(np.sum((y - X @ coef) ** 2))
        if best is None or rss < best[0]:
            best = (rss, t0, coef, X)
    rss, t0, coef, X = best
    resid = y - X @ coef
    p = X.shape[1]
    sigma = float(np.sqrt(rss / (len(y) - p)))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    span = float((absd.max() - absd.min()) / 365.25)
    warnings = []
    if span < MIN_YEARS:
        adequacy = "SHORT_LT_2Y"
        warnings.append(f"calibration span {span:.2f} y < 2 y (ICOLD via SALAZAR-2017 p29)")
    elif span < SWISS_CYCLES:
        adequacy = "SHORT_LT_5_CYCLES"
        warnings.append(f"calibration span {span:.2f} y < 5 yearly cycles (Swiss Committee via SALAZAR-2017 p29)")
    else:
        adequacy = "ADEQUATE"
    if np.linalg.cond(X) > 1e8:
        warnings.append("design matrix ill-conditioned (h-polynomial collinearity); coefficients are not individually interpretable")
    return HSTModel(variant, dict(zip(names, map(float, coef))), float(t0), ref, h_min, h_max, int(len(y)),
                    1.0 - rss / ss_tot if ss_tot > 0 else float("nan"), math.sqrt(rss / len(y)), sigma,
                    robust_sigma(resid), span, adequacy, warnings)


def predict(model: HSTModel, ts, h) -> np.ndarray:
    ts = list(ts)
    if ts and isinstance(ts[0], (dt.date, dt.datetime)):
        doy, _, _ = _to_days(ts)
        absd = np.array([((x.date() if isinstance(x, dt.datetime) else x) - model.ref_date).days
                         + ((x.hour * 3600 + x.minute * 60 + x.second) / 86400.0 if isinstance(x, dt.datetime) else 0.0)
                         for x in ts], float)
    else:
        absd = np.asarray(ts, float)
        doy = absd % 365.25
    hn = (np.asarray(h, float) - model.h_min) / (model.h_max - model.h_min)
    X = design(hn, doy, absd / 365.25, model.variant, model.t0_years)
    return X @ model.coef_vector()


def validate(model: HSTModel, ts, h, y) -> dict:
    """Hold-out check: RMSE, bias, and coverage of the ±2σ band (expected ≈ 95.4 % if residuals are
    Gaussian and the model extrapolates; lower coverage = model drift or behaviour change)."""
    y = np.asarray(y, float)
    h = np.asarray(h, float)
    m = clean(y, h)
    pred = predict(model, [x for x, k in zip(ts, m) if k], h[m])
    r = y[m] - pred
    out_of_range = float(np.mean((h[m] < model.h_min) | (h[m] > model.h_max))) if m.any() else 0.0
    return {"n": int(m.sum()), "rmse": float(np.sqrt(np.mean(r ** 2))), "bias": float(r.mean()),
            "coverage_2s": float(np.mean(np.abs(r) <= 2 * model.resid_sigma)),
            "frac_h_outside_training": out_of_range,
            "extrapolating": out_of_range > 0.05}
