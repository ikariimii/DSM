"""Bronze (raw) — Lakeflow Declarative Pipeline, serverless. Append-only Auto Loader ingest.

KB recommendations adopted: cloudFiles.useManagedFileEvents (file events on the external
landing volume; the pipeline must still run at least weekly), schema hints + rescued data,
expectations warn/drop + quarantine table so nothing is silently lost.
Tables are published to <catalog>.raw.* (pipeline `catalog`/`schema` = ${var.catalog}/raw).
UNVERIFIED locally: no pyspark/LDP runtime on the build box (docs/VERIFICATION.md).
"""
from pyspark import pipelines as dp
from pyspark.sql import functions as F

CATALOG = spark.conf.get("dam.catalog")                      # noqa: F821 (spark injected by LDP)
# UC row filter on every published dataset carrying dam_id (SCHEMA_CONTRACT v1.1 §6). The UDF is created
# by sql/ddl/10_security.sql, which runs before the pipeline (uc_setup). LDP row_filter is Public Preview: UNVERIFIED on a live workspace.
SECURITY_MODE = spark.conf.get("dam.security_mode", "table")   # noqa: F821  table | abac (databricks.yml)
# Managed only here, never by ALTER TABLE (KB 03). abac mode: no table-level filter (UC_ABAC_MULTIPLE_ROW_FILTERS).
DAM_ROW_FILTER = f"ROW FILTER {CATALOG}.ref.dam_row_filter ON (dam_id)" if SECURITY_MODE == "table" else None
LANDING = spark.conf.get("dam.landing_path", f"/Volumes/{CATALOG}/raw/landing")  # noqa: F821

READING_SCHEMA = ("dam_id STRING, logger_id STRING, sensor_id STRING, ts_utc STRING, raw_value DOUBLE, "
                  "raw_unit STRING, temp_raw DOUBLE, temp_c DOUBLE, baro_kpa DOUBLE, reading_type STRING, "
                  "source STRING, reservoir_el_m DOUBLE, datum_id STRING")  # datum_id: CONVENTIONS.md schema addition 2; optional (null = single datum)
INCL_SCHEMA = ("instrument_id STRING, survey_ts STRING, depth_m DOUBLE, a0 DOUBLE, a180 DOUBLE, "
               "b0 DOUBLE, b180 DOUBLE, probe_serial STRING, operator STRING")

READING_RULES = {
    "has_sensor": "sensor_id IS NOT NULL",
    "has_dam": "dam_id IS NOT NULL",
    "parsable_ts": "ts_parsed IS NOT NULL",
    "no_rescued": "_rescued_data IS NULL",
}
INCL_RULES = {
    "has_instrument": "instrument_id IS NOT NULL",
    "parsable_ts": "ts_parsed IS NOT NULL",
    "has_depth": "depth_m IS NOT NULL",
    "no_rescued": "_rescued_data IS NULL",
}


def _autoloader(glob: str, schema_hints: str):
    return (spark.readStream.format("cloudFiles")                       # noqa: F821
            .option("cloudFiles.format", "csv")
            .option("header", "true")
            .option("cloudFiles.useManagedFileEvents", "true")
            .option("cloudFiles.schemaHints", schema_hints)
            .option("cloudFiles.inferColumnTypes", "false")
            .option("rescuedDataColumn", "_rescued_data")
            .option("pathGlobFilter", "*.csv")
            .load(f"{LANDING}/{glob}")
            .withColumn("source_file", F.col("_metadata.file_path"))
            .withColumn("file_mod_ts", F.col("_metadata.file_modification_time"))
            .withColumn("ingest_ts", F.current_timestamp()))


def _all_ok(rules):
    return " AND ".join(f"({r})" for r in rules.values())


@dp.table(name="reading_landing", private=True)
def reading_landing():
    df = _autoloader("*/readings/", READING_SCHEMA)
    return df.withColumn("ts_parsed", F.to_timestamp("ts_utc"))


@dp.table(name="reading_raw", row_filter=DAM_ROW_FILTER, comment="Append-only ADAS + manual readings as received (FERC canvas ops.reading_raw)",
          table_properties={"delta.appendOnly": "true", "quality": "bronze"},
          cluster_by=["dam_id", "sensor_id"])
@dp.expect_all_or_drop(READING_RULES)
def reading_raw():
    return (dp.read_stream("reading_landing").withColumn("ts_utc", F.col("ts_parsed")).drop("ts_parsed"))


@dp.table(name="reading_quarantine", row_filter=DAM_ROW_FILTER, comment="Bronze rows that failed parse expectations",
          table_properties={"delta.appendOnly": "true"})
def reading_quarantine():
    df = dp.read_stream("reading_landing")
    reason = F.concat_ws(",", *[F.when(~F.expr(c), F.lit(n)) for n, c in READING_RULES.items()])
    return df.where(f"NOT ({_all_ok(READING_RULES)})").withColumn("quarantine_reason", reason)


@dp.table(name="incl_landing", private=True)
def incl_landing():
    return _autoloader("*/inclinometer/", INCL_SCHEMA).withColumn("ts_parsed", F.to_timestamp("survey_ts"))


@dp.table(name="incl_survey_raw", table_properties={"delta.appendOnly": "true", "quality": "bronze"})
@dp.expect_all_or_drop(INCL_RULES)
def incl_survey_raw():
    return dp.read_stream("incl_landing").withColumn("survey_ts", F.col("ts_parsed")).drop("ts_parsed")


@dp.table(name="incl_quarantine", table_properties={"delta.appendOnly": "true"})
def incl_quarantine():
    df = dp.read_stream("incl_landing")
    reason = F.concat_ws(",", *[F.when(~F.expr(c), F.lit(n)) for n, c in INCL_RULES.items()])
    return df.where(f"NOT ({_all_ok(INCL_RULES)})").withColumn("quarantine_reason", reason)
