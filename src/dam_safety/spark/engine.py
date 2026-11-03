"""Spark execution helpers: as-of calibration join, incremental reduce, recompute.

Ownership (see README 'Table ownership'): silver.reading_reduced, ops.alarm_* and
audit.recompute_log are JOB-owned Delta tables (not pipeline-owned), because history
recompute appends replacement rows with lineage (recompute_batch_id; superseded rows are
identified by silver.v_reading_lineage.superseded_by, never updated) — an external writer cannot do
that to a Lakeflow streaming table.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Iterable, Optional

from pyspark.sql import DataFrame, SparkSession, functions as F

from ..calibration import RecomputeWindow, windows_to_sql_predicate
from . import udfs


def fq(catalog: str, schema: str, table: str) -> str:
    return f"`{catalog}`.`{schema}`.`{table}`"


def asof_join_calibration(readings: DataFrame, calib: DataFrame) -> DataFrame:
    """Each reading gets the calibration version valid at ts_utc (SCD2 half-open
    [effective_from, effective_to)). Readings with no valid calibration keep null
    formula_id and are flagged by QC (NO_CALIBRATION) downstream."""
    c = calib.select("sensor_id", "calib_id", "cal_version", "formula_id", "formula_version",
                     "params", "sign_mult", "offset", "effective_from", "effective_to", "min_valid",
                     "max_valid").alias("c")
    r = readings.alias("r")
    cond = (F.col("r.sensor_id") == F.col("c.sensor_id")) & (F.col("r.ts_utc") >= F.col("c.effective_from")) & (
        F.col("c.effective_to").isNull() | (F.col("r.ts_utc") < F.col("c.effective_to")))
    return r.join(c, cond, "left").select("r.*", "c.calib_id", "c.cal_version", "c.formula_id",
                                          "c.formula_version", "c.params", "c.sign_mult", "c.offset",
                                          "c.min_valid", "c.max_valid")


def _series_history(spark: SparkSession, catalog: str, reduced: DataFrame) -> dict:
    """Prior reduced rows for baseline-type (SETTLEMENT_DELTA) sensors in this batch, as
    {sensor_id: [ {ts_utc, datum_id, raw_value, eng_value}, ... ]} (only rows strictly before
    the batch's earliest ts per sensor). Volume is small: one row per reading of a few
    settlement points; capped via first-per-datum + last row."""
    sens = [r.sensor_id for r in reduced.where("needs_baseline").select("sensor_id").distinct().collect()]
    if not sens:
        return {}
    mins = reduced.where("needs_baseline").groupBy("sensor_id").agg(F.min("ts_utc").alias("batch_min"))
    cur = spark.table(fq(catalog, "silver", "v_reading_current")).where(F.col("sensor_id").isin(sens))
    prior = cur.join(mins, "sensor_id").where("ts_utc < batch_min")
    from pyspark.sql import Window
    w_d = Window.partitionBy("sensor_id", "datum_id").orderBy("ts_utc")
    w_s = Window.partitionBy("sensor_id").orderBy(F.col("ts_utc").desc())
    keep = (prior.withColumn("rd", F.row_number().over(w_d)).withColumn("rs", F.row_number().over(w_s))
                 .where("rd = 1 OR rs = 1").select("sensor_id", "ts_utc", "datum_id", "raw_value", "eng_value"))
    out: dict = {}
    for r in keep.collect():
        out.setdefault(r.sensor_id, []).append(r.asDict())
    return out


def reduce_and_qc(spark: SparkSession, catalog: str, readings: DataFrame, batch_id: str, is_recompute: bool) -> DataFrame:
    calib = spark.table(fq(catalog, "ref", "calibration")).where("NOT is_retired")
    joined = asof_join_calibration(readings, calib)
    reduced = udfs.reduce_df(joined)
    reduced = reduced.join(joined.select("reading_id", "min_valid", "max_valid"), "reading_id", "left")
    qcd = udfs.qc_df(reduced, reduced.schema, history=_series_history(spark, catalog, reduced))
    return (qcd.withColumn("recompute_batch_id", F.lit(batch_id))
               .withColumn("is_recompute", F.lit(is_recompute))
               .withColumn("reduced_ts", F.current_timestamp())
               .drop("min_valid", "max_valid", "needs_baseline", "reduce_error", "sign_mult", "offset"))


def write_reduced(spark: SparkSession, catalog: str, df: DataFrame) -> None:
    """APPEND-ONLY (delta.appendOnly=true; FERC canvas REQ-23 / 'Databricks implementation
    notes': no UPDATE or DELETE, corrections are new rows). A recompute appends new rows
    for the same reading_id with a later reduced_ts and its recompute_batch_id; the
    current row is selected by silver.v_reading_current (latest reduced_ts per reading_id)
    and the superseded row is identified by silver.v_reading_lineage.superseded_by."""
    df.write.mode("append").saveAsTable(fq(catalog, "silver", "reading_reduced"))


def recompute(spark: SparkSession, catalog: str, windows: Iterable[RecomputeWindow], requested_by: str,
              reason: str, batch_id: Optional[str] = None) -> str:
    windows = list(windows)
    batch_id = batch_id or f"rc-{uuid.uuid4()}"
    pred = windows_to_sql_predicate(windows)
    readings = spark.table(fq(catalog, "silver", "reading_clean")).where(pred)
    out = reduce_and_qc(spark, catalog, readings, batch_id, True)
    write_reduced(spark, catalog, out)
    rows = [(batch_id, w.key, w.start, w.end, w.old_version_id, w.new_version_id, requested_by, reason, datetime.utcnow())
            for w in windows]
    spark.createDataFrame(rows, "batch_id string, sensor_id string, window_start timestamp, window_end timestamp, "
                                "old_calib_id string, new_calib_id string, requested_by string, reason string, "
                                "logged_ts timestamp").write.mode("append").saveAsTable(fq(catalog, "audit", "recompute_log"))
    return batch_id
