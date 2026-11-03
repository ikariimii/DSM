"""Silver — validated, de-duplicated readings (pipeline-owned). Reductions are JOB-owned
(src/jobs/reduce_readings.py) so history recompute can append lineage rows.

Expectations: unknown sensor / unknown dam / future timestamp -> silver.reading_quarantine
(never silently dropped; FERC canvas REQ-15 'invalid data retained and flagged').
Note: QC flags (range/spike/stuck/missing/checksum) are computed AFTER reduction in the job,
because limits are in engineering units (ref.calibration.min/max_valid).
"""
from pyspark import pipelines as dp
from pyspark.sql import functions as F

CATALOG = spark.conf.get("dam.catalog")    # noqa: F821
# UC row filter on every published dataset carrying dam_id (SCHEMA_CONTRACT v1.1 §6). The UDF is created
# by sql/ddl/10_security.sql, which runs before the pipeline (uc_setup). LDP row_filter is Public Preview: UNVERIFIED on a live workspace.
SECURITY_MODE = spark.conf.get("dam.security_mode", "table")   # noqa: F821  table | abac (databricks.yml)
# Managed only here, never by ALTER TABLE (KB 03). abac mode: no table-level filter (UC_ABAC_MULTIPLE_ROW_FILTERS).
DAM_ROW_FILTER = f"ROW FILTER {CATALOG}.ref.dam_row_filter ON (dam_id)" if SECURITY_MODE == "table" else None
MAX_FUTURE_H = int(spark.conf.get("dam.max_future_hours", "1"))  # noqa: F821  (UNVERIFIED site setting)


def _sensor():
    return spark.read.table(f"{CATALOG}.ref.sensor").select("sensor_id", "instrument_id",  # noqa: F821
                                                            F.col("dam_id").alias("ref_dam_id"))


SILVER_RULES = {
    "known_sensor": "instrument_id IS NOT NULL",
    "dam_matches_ref": "ref_dam_id = dam_id",
    "not_future": f"ts_utc <= current_timestamp() + INTERVAL {MAX_FUTURE_H} HOURS",
}


@dp.view(name="reading_joined")
def reading_joined():
    raw = dp.read_stream(f"{CATALOG}.raw.reading_raw").withWatermark("ingest_ts", "1 day")
    raw = raw.dropDuplicatesWithinWatermark(["sensor_id", "ts_utc", "source_file"])
    j = raw.join(F.broadcast(_sensor()), "sensor_id", "left")
    return j.withColumn("reading_id", F.sha2(F.concat_ws("|", "sensor_id", F.col("ts_utc").cast("string"), "source_file"), 256))


@dp.table(name=f"{CATALOG}.silver.reading_clean", row_filter=DAM_ROW_FILTER, comment="Validated readings, one row per reading_id",
          table_properties={"delta.appendOnly": "true", "quality": "silver"},
          cluster_by=["dam_id", "sensor_id", "ts_utc"])
@dp.expect_all_or_drop(SILVER_RULES)
def reading_clean():
    return dp.read_stream("reading_joined").drop("ref_dam_id")


@dp.table(name=f"{CATALOG}.silver.reading_quarantine", row_filter=DAM_ROW_FILTER, table_properties={"delta.appendOnly": "true"})
def reading_quarantine():
    df = dp.read_stream("reading_joined")
    ok = " AND ".join(f"coalesce({c}, false)" for c in SILVER_RULES.values())
    reason = F.concat_ws(",", *[F.when(~F.coalesce(F.expr(c), F.lit(False)), F.lit(n)) for n, c in SILVER_RULES.items()])
    return df.where(f"NOT ({ok})").withColumn("quarantine_reason", reason)


@dp.table(name=f"{CATALOG}.silver.incl_survey", row_filter=DAM_ROW_FILTER, table_properties={"quality": "silver"})
@dp.expect_or_drop("known_instrument", "instrument_type IS NOT NULL")
def incl_survey():
    inst = spark.read.table(f"{CATALOG}.ref.instrument").select("instrument_id", "dam_id",  # noqa: F821
                                                                F.col("instrument_type"))
    s = dp.read_stream(f"{CATALOG}.raw.incl_survey_raw")
    s = s.withColumn("survey_id", F.sha2(F.concat_ws("|", "instrument_id", F.col("survey_ts").cast("string")), 256))
    s = s.join(F.broadcast(inst), "instrument_id", "left")
    # baseline = earliest survey per instrument is decided in the reduction job (needs state);
    # is_baseline here is the uploader's flag when present in the file name convention *_BASELINE.csv
    return s.withColumn("is_baseline", F.col("source_file").rlike("(?i)_baseline\\.csv$"))
