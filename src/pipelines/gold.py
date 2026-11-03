"""Gold materialized views (pipeline-owned, serverless incremental refresh).
gold.threshold_exceedances is a plain VIEW (sql/ddl/11_gold_views.sql) over the job-owned
alarm state, so SQL alerts always see the state machine's current output."""
from pyspark import pipelines as dp
from pyspark.sql import Window, functions as F

CATALOG = spark.conf.get("dam.catalog")    # noqa: F821
# UC row filter on every published dataset carrying dam_id (SCHEMA_CONTRACT v1.1 §6). The UDF is created
# by sql/ddl/10_security.sql, which runs before the pipeline (uc_setup). LDP row_filter is Public Preview: UNVERIFIED on a live workspace.
SECURITY_MODE = spark.conf.get("dam.security_mode", "table")   # noqa: F821  table | abac (databricks.yml)
# Managed only here, never by ALTER TABLE (KB 03). abac mode: no table-level filter (UC_ABAC_MULTIPLE_ROW_FILTERS).
DAM_ROW_FILTER = f"ROW FILTER {CATALOG}.ref.dam_row_filter ON (dam_id)" if SECURITY_MODE == "table" else None
GAP_FACTOR = float(spark.conf.get("dam.gap_factor", "2.0"))  # noqa: F821  (UNVERIFIED default)


def _current():
    r = spark.read.table(f"{CATALOG}.silver.reading_reduced")   # noqa: F821
    w = Window.partitionBy("reading_id").orderBy(F.col("reduced_ts").desc())
    return r.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")


@dp.materialized_view(name=f"{CATALOG}.gold.instrument_latest", row_filter=DAM_ROW_FILTER,
                      comment="Latest current reduced reading per sensor (served to the app via SQL warehouse OBO; UC row filter; no synced table, SCHEMA_CONTRACT v1.1 Q12)")
def instrument_latest():
    c = _current()
    w = Window.partitionBy("sensor_id").orderBy(F.col("ts_utc").desc())
    return (c.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")
             .select("dam_id", "instrument_id", "sensor_id", "ts_utc", "corrected_value", "corrected_unit",
                     "eng_value", "eng_unit", "qa_flag", "qa_reason", "calib_id", "cal_version", "formula_id",
                     "formula_version", "recompute_batch_id"))


@dp.materialized_view(name=f"{CATALOG}.gold.instrument_daily", row_filter=DAM_ROW_FILTER)
def instrument_daily():
    c = _current().where("qa_flag IN ('OK','SUSPECT')")
    return (c.groupBy("dam_id", "instrument_id", "sensor_id", F.to_date("ts_utc").alias("day"))
             .agg(F.min("corrected_value").alias("min_value"), F.max("corrected_value").alias("max_value"),
                  F.avg("corrected_value").alias("mean_value"), F.count("*").alias("n_readings"),
                  F.sum(F.when(F.col("qa_flag") == "SUSPECT", 1).otherwise(0)).alias("n_suspect"),
                  F.first("corrected_unit").alias("unit")))


@dp.materialized_view(name=f"{CATALOG}.gold.data_freshness", row_filter=DAM_ROW_FILTER,
                      comment="REQ-18..21 reading-frequency compliance: is_stale when last reading older than gap_factor x expected interval")
def data_freshness():
    last = spark.read.table(f"{CATALOG}.silver.reading_clean").groupBy("sensor_id").agg(   # noqa: F821
        F.max("ts_utc").alias("last_ts"))
    sched = spark.read.table(f"{CATALOG}.ref.reading_schedule")   # noqa: F821
    sensor = spark.read.table(f"{CATALOG}.ref.sensor").select("sensor_id", "instrument_id", "dam_id")  # noqa: F821
    cur = sched.where("effective_to IS NULL").groupBy("instrument_id").agg(F.min("expected_interval_s").alias("expected_interval_s"))
    j = sensor.join(cur, "instrument_id", "inner").join(last, "sensor_id", "left")
    age = F.unix_timestamp(F.current_timestamp()) - F.unix_timestamp("last_ts")
    return (j.withColumn("age_s", age)
             .withColumn("is_stale", F.col("last_ts").isNull() | (F.col("age_s") > F.lit(GAP_FACTOR) * F.col("expected_interval_s")))
             .withColumn("computed_ts", F.current_timestamp()))
