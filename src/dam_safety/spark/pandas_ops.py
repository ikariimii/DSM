"""pandas-level wrappers around the pure-Python cores.

These are what the Spark UDFs / applyInPandas call, and they are unit-testable locally with
pandas only (no pyspark). Column names follow the canonical schema in
/workspace/dam-refs/CONVENTIONS.md and docs/api_contract.md.
"""
from __future__ import annotations

import json
from typing import Optional

import pandas as pd

from ..alarms import AlarmState, ThresholdSpec, run_series
from ..datum import DATUM_CHANGE, SeriesState, datum_changes, rebaseline_series
from ..formulas import core
from ..formulas.registry import apply_formula
from ..qc import QcParams, qc_series

REDUCED_COLUMNS = [
    "reading_id", "dam_id", "sensor_id", "instrument_id", "ts_utc", "datum_id", "raw_value", "raw_unit",
    "eng_value", "eng_unit", "corrected_value", "corrected_unit", "head_m",
    "temp_corr", "baro_corr", "is_dry",
    "calib_id", "cal_version", "formula_id", "formula_version",
    "sign_mult", "offset", "needs_baseline", "reduce_error",   # internal; needs_baseline/reduce_error dropped before write
]
INTERNAL_COLUMNS = ["sign_mult", "offset", "needs_baseline", "reduce_error"]


def _params(v) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        return json.loads(v)
    return dict(v)  # Spark MAP arrives as list of tuples / dict


def reduce_readings_pdf(pdf: pd.DataFrame) -> pd.DataFrame:
    """Row-wise reduction. ``pdf`` = readings already as-of-joined to their calibration
    (columns calib_id, cal_version, formula_id, formula_version, params)."""
    rows = []
    for r in pdf.to_dict("records"):
        cal = {"calib_id": r.get("calib_id"), "cal_version": r.get("cal_version"),
               "formula_id": r.get("formula_id"), "formula_version": r.get("formula_version"),
               "params": _params(r.get("params")),
               "sign_mult": r.get("sign_mult"), "offset": r.get("offset")}
        out = {c: r.get(c) for c in ("reading_id", "dam_id", "sensor_id", "instrument_id", "ts_utc", "datum_id",
                                     "raw_value", "raw_unit", "sign_mult", "offset")}
        if cal["formula_id"] is None:
            out.update({"eng_value": None, "corrected_value": None, "formula_id": None})
        else:
            try:
                out.update(apply_formula(r, cal))
            except (ValueError, KeyError) as e:   # bad calibration row must not kill the batch
                out.update({"eng_value": None, "corrected_value": None, "formula_id": cal["formula_id"],
                            "formula_version": cal["formula_version"], "calib_id": cal["calib_id"],
                            "cal_version": cal["cal_version"], "reduce_error": f"CALIB_ERROR:{e}"[:200]})
        rows.append(out)
    res = pd.DataFrame(rows)
    for c in REDUCED_COLUMNS:
        if c not in res.columns:
            res[c] = None
    return res[REDUCED_COLUMNS]


def _apply_datum(pdf: pd.DataFrame, series_state: Optional[SeriesState]) -> pd.DataFrame:
    """Datum handling (CONVENTIONS.md 'Schema additions'; settlement procedure §0/§7):
    * SETTLEMENT_DELTA rows (needs_baseline): eng/corrected from rebaseline_series
      (sign_mult applied, positive-down, carried across datum breaks);
    * every series: a datum_id change marks the row for DATUM_CHANGE (set after QC)."""
    pdf = pdf.copy()
    pdf["_datum_change"] = False
    if "datum_id" not in pdf.columns:
        return pdf
    datums = [None if (isinstance(d, float) and pd.isna(d)) else d for d in pdf["datum_id"]]
    nb = pdf["needs_baseline"].fillna(False).astype(bool) if "needs_baseline" in pdf.columns else pd.Series(False, index=pdf.index)
    if nb.any():
        sub = pdf[nb]
        sm = sub["sign_mult"].dropna().iloc[0] if "sign_mult" in sub and sub["sign_mult"].notna().any() else 1
        off = sub["offset"].dropna().iloc[0] if "offset" in sub and sub["offset"].notna().any() else 0.0
        rows = [{"ts_utc": r.ts_utc, "raw_value": None if pd.isna(r.raw_value) else float(r.raw_value),
                 "datum_id": None if (isinstance(r.datum_id, float) and pd.isna(r.datum_id)) else r.datum_id}
                for r in sub.itertuples()]
        res, _ = rebaseline_series(rows, int(sm), str(sub["raw_unit"].iloc[0] or "mm"), float(off), None, series_state)
        pdf.loc[nb, "eng_value"] = [o["eng_value"] for o in res]
        pdf.loc[nb, "corrected_value"] = [o["eng_value"] for o in res]
        pdf.loc[nb, "_datum_change"] = [o["datum_change"] for o in res]
    prev = series_state.last_datum if series_state and series_state.seen else None
    flags = datum_changes(datums, prev, bool(series_state and series_state.seen))
    pdf["_datum_change"] = pdf["_datum_change"] | pd.Series(flags, index=pdf.index)
    return pdf


def qc_pdf(pdf: pd.DataFrame, qc_params: Optional[dict] = None, history: int = 0,
           series_state: Optional[SeriesState] = None) -> pd.DataFrame:
    """Per-sensor QC (applyInPandas grouped by sensor_id). ``pdf`` sorted by ts may start
    with ``history`` already-QC'd rows (trailing context) which are dropped from output."""
    pdf = pdf.sort_values("ts_utc").reset_index(drop=True)
    pdf = _apply_datum(pdf, series_state)
    qp = QcParams(**(qc_params or {}))
    if "min_valid" in pdf.columns and pdf["min_valid"].notna().any():
        qp.min_valid = float(pdf["min_valid"].dropna().iloc[0])
    if "max_valid" in pdf.columns and pdf["max_valid"].notna().any():
        qp.max_valid = float(pdf["max_valid"].dropna().iloc[0])
    vals = [None if pd.isna(v) else float(v) for v in pdf["corrected_value"].fillna(pdf.get("eng_value"))]
    res = qc_series(list(pdf["ts_utc"]), vals, qp)
    pdf["qa_flag"] = [r.flag for r in res]
    pdf["qa_reason"] = [r.reason for r in res]
    if "is_dry" in pdf.columns:
        dry = pdf["is_dry"].fillna(False).astype(bool)
        pdf.loc[dry & (pdf["qa_flag"] == "BAD") & (pdf["qa_reason"] == "NULL_VALUE"), ["qa_flag", "qa_reason"]] = ["SUSPECT", "DRY"]
    # DATUM_CHANGE: SUSPECT until engineer confirms (settlement procedure §7); never overrides BAD.
    dc = pdf["_datum_change"] & (pdf["qa_flag"] != "BAD")
    pdf.loc[dc, ["qa_flag", "qa_reason"]] = ["SUSPECT", DATUM_CHANGE]
    if "reduce_error" in pdf.columns:
        err = pdf["reduce_error"].notna()
        pdf.loc[err, "qa_flag"] = "BAD"
        pdf.loc[err, "qa_reason"] = pdf.loc[err, "reduce_error"]
    pdf = pdf.drop(columns=["_datum_change"])
    return pdf.iloc[history:].reset_index(drop=True)


def incl_profile_pdf(pdf: pd.DataFrame) -> pd.DataFrame:
    """applyInPandas grouped by (instrument_id, survey_ts). Expects the survey rows and the
    initial-survey rows (is_initial=True) for the same instrument, plus interval_m, k,
    fixed_end, checksum_tol columns from the calibration."""
    cur = pdf[~pdf["is_initial"]]
    ini = pdf[pdf["is_initial"]]
    L = float(pdf["interval_m"].iloc[0]) * 1000.0  # mm
    K = float(pdf["k"].iloc[0])
    fixed = str(pdf["fixed_end"].iloc[0])
    tol = float(pdf["checksum_tol"].iloc[0]) if pd.notna(pdf["checksum_tol"].iloc[0]) else None
    mk = lambda d: [core.InclDepthReading(r.depth_m, r.a0, r.a180, r.b0, r.b180) for r in d.itertuples()]
    prof = core.incl_profile(mk(cur), mk(ini), L, K, fixed)
    df = pd.DataFrame([p.__dict__ for p in prof]).rename(columns={"depth": "depth_m"})
    df["instrument_id"] = pdf["instrument_id"].iloc[0]
    df["survey_ts"] = cur["survey_ts"].iloc[0] if len(cur) else None
    flags = core.incl_checksum_flags(df["checksum_a"], tol) if tol is not None else [False] * len(df)
    df["qa_flag"] = ["SUSPECT" if f else "OK" for f in flags]
    df["qa_reason"] = ["CHECKSUM" if f else None for f in flags]
    return df


def alarms_pdf(pdf: pd.DataFrame, specs_by_sensor: dict, state_by_sensor: dict, acks_by_sensor: dict):
    """Run the alarm machine for one sensor group. Returns (transitions_df, state_row)."""
    key = pdf["sensor_id"].iloc[0]
    rows = [{"ts_utc": r.ts_utc, "value": None if pd.isna(r.value) else float(r.value), "qa_flag": r.qa_flag}
            for r in pdf.sort_values("ts_utc").itertuples()]
    specs = [ThresholdSpec(**s) for s in specs_by_sensor.get(key, [])]
    st0 = state_by_sensor.get(key)
    st = AlarmState(key, **st0) if st0 else AlarmState(key)
    st, trs = run_series(key, rows, specs, st, acks_by_sensor.get(key, []))
    return pd.DataFrame([t.__dict__ for t in trs]), st
