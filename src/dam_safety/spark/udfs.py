"""Spark UDF / applyInPandas wrappers (require pyspark; used on Databricks only)."""
from __future__ import annotations

from pyspark.sql import DataFrame, functions as F, types as T

from . import pandas_ops

REDUCED_SCHEMA = T.StructType([
    T.StructField("reading_id", T.StringType()), T.StructField("dam_id", T.StringType()),
    T.StructField("sensor_id", T.StringType()),
    T.StructField("instrument_id", T.StringType()), T.StructField("ts_utc", T.TimestampType()),
    T.StructField("datum_id", T.StringType()),
    T.StructField("raw_value", T.DoubleType()), T.StructField("raw_unit", T.StringType()),
    T.StructField("eng_value", T.DoubleType()), T.StructField("eng_unit", T.StringType()),
    T.StructField("corrected_value", T.DoubleType()), T.StructField("corrected_unit", T.StringType()),
    T.StructField("head_m", T.DoubleType()), T.StructField("temp_corr", T.DoubleType()),
    T.StructField("baro_corr", T.DoubleType()), T.StructField("is_dry", T.BooleanType()),
    T.StructField("calib_id", T.StringType()), T.StructField("cal_version", T.IntegerType()),
    T.StructField("formula_id", T.StringType()), T.StructField("formula_version", T.StringType()),
    T.StructField("sign_mult", T.IntegerType()), T.StructField("offset", T.DoubleType()),
    T.StructField("needs_baseline", T.BooleanType()), T.StructField("reduce_error", T.StringType()),
])


def reduce_df(joined: DataFrame) -> DataFrame:
    """Apply the formula registry to readings joined as-of to ref.calibration.
    Grouped by sensor so each pandas batch has a single calibration lineage."""
    return joined.groupBy("sensor_id").applyInPandas(pandas_ops.reduce_readings_pdf, REDUCED_SCHEMA)


def qc_df(reduced: DataFrame, schema: T.StructType, history: dict | None = None) -> DataFrame:
    """Per-sensor QC. NOTE (docs/VERIFICATION.md 'QC-CONTEXT'): spike/stuck tests see only
    the rows in this batch (no trailing history from earlier batches) - UNVERIFIED for
    incremental runs with small batches; recompute runs over full windows are unaffected."""
    out_schema = T.StructType(schema.fields + [T.StructField("qa_flag", T.StringType()), T.StructField("qa_reason", T.StringType())])
    from ..datum import state_from_history
    states = {k: state_from_history(v) for k, v in (history or {}).items()}   # small, shipped in the closure
    return reduced.groupBy("sensor_id").applyInPandas(
        lambda p: pandas_ops.qc_pdf(p, series_state=states.get(p["sensor_id"].iloc[0])), out_schema)


@F.pandas_udf(T.DoubleType())
def vw_digits_from_hz_udf(hz):
    return hz * hz / 1000.0  # GEOKON-4500 Eq.1 PDF p.22 (vectorised core.vw_digits_from_hz)
