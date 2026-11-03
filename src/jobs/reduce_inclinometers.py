"""Inclinometer reduction job: silver.incl_survey + ref.incl_installation (row valid at survey time) + ref.incl_correction ->
silver.incl_reduced (append-only, job-owned; SCHEMA_CONTRACT §5.5). All maths is in the pure-Python core
dam_safety.inclinometer.batch.reduce_instrument (tested locally, tests/test_inclinometer_batch.py); this file only
moves rows. UNTESTED HERE: no pyspark/Java on the build box (docs/VERIFICATION.md INC-WIRE). Not deployed.

Per instrument (cogroup surveys x correction records, applyInPandas) the core reads ALL surveys of the instrument
(baseline + QA-020 reference) and emits only target surveys:
  - surveys never reduced, or
  - surveys with a correction record (timestamp_utc / reviewed_utc) newer than their last reduced_ts, or
  - every survey of an instrument whose current installation row is newer than its last reduce, or
  - surveys whose latest row carries another formula_version or qc_rule_version (engine or BIAS_RULE_VERSION bump, e.g. QA021-v2), or
  - everything with --full_recompute true.
A recompute APPENDS rows with a new recompute_batch_id (append-only; current row = latest reduced_ts).
Bias QC (rule batch.BIAS_RULE_VERSION) proposals are flagged on the rows (BIAS_PROPOSED_A/B) and are never applied or written to
ref.incl_correction here; a human turns them into a record (qc.bias_proposal_record).
Bias-rule hook: batch.DEFAULT_BIAS_RULE (qc.between_set_bias_qc, rule QA021-v2)."""
import time
import uuid
from datetime import datetime, timezone

import _common  # noqa: F401  (sys.path)
from _common import args, spark as get_spark

from dam_safety.inclinometer import batch

try:
    from pyspark.sql import functions as F
    from pyspark.sql import types as T

    OUT_SCHEMA = T.StructType(
        [T.StructField(c, T.StringType()) for c in ("dam_id", "instrument_id", "survey_id")]
        + [T.StructField(c, T.TimestampType()) for c in ("survey_ts", "initial_survey_ts")]
        + [T.StructField(c, T.DoubleType()) for c in ("depth_m", "diff_a", "diff_b", "checksum_a", "checksum_b",
                                                      "dev_a", "dev_b", "inc_a", "inc_b", "cum_a", "cum_b",
                                                      "resultant")]
        + [T.StructField(c, T.StringType()) for c in ("fixed_end", "qa_flag", "qa_reason", "calib_id", "formula_id",
                                                      "formula_version", "recompute_batch_id")]
        + [T.StructField("reduced_ts", T.TimestampType()), T.StructField("depth_reference", T.StringType()),
           T.StructField("baseline_survey_id", T.StringType()),
           T.StructField("correction_record_ids", T.ArrayType(T.StringType())),
           T.StructField("raw_depth_label", T.StringType()), T.StructField("qc_rule_version", T.StringType()),
           T.StructField("installation_version", T.TimestampType()), T.StructField("probe_constants", T.StringType())])
    assert tuple(f.name for f in OUT_SCHEMA.fields) == batch.REDUCED_COLUMNS
except ImportError:                     # importable without pyspark (tests)
    OUT_SCHEMA = None

INST_COLS = ("instrument_id", "dam_id", "system", "probe_L_m", "probe_K", "raw_unit", "casing_depth_m",
             "depth_reference", "a0_azimuth_deg", "sign_mult_A", "sign_mult_B", "baseline_survey_id",
             "spiral_survey_id", "stable_zone_top_m")
REVIEW_COLS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
               "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash")
SURVEY_COLS = ("instrument_id", "survey_id", "survey_ts", "depth_m", "a0", "a180", "b0", "b180")


def make_udf(batch_id, reduced_ts):
    import pandas as pd

    def _reduce(surveys: "pd.DataFrame", corrs: "pd.DataFrame") -> "pd.DataFrame":
        if surveys.empty:
            return pd.DataFrame(columns=list(batch.REDUCED_COLUMNS))
        clean = lambda v: None if pd.isna(v) else v                  # noqa: E731
        # v1.5: every SCD2 installation row any survey was range-joined to; batch.reduce_instrument picks, per
        # survey, the row valid at survey_ts (and raises on overlapping rows, which would duplicate survey rows here).
        inst = []
        for _f, g in surveys.groupby("i_from", sort=True, dropna=False):
            r0 = g.iloc[0]
            row = {c: clean(r0[f"i_{c}"]) for c in INST_COLS}
            row.update(effective_from=clean(r0["i_from"]), effective_to=clean(r0["i_to"]))
            inst.append(row)
        rows = [{c: clean(r[c]) for c in SURVEY_COLS} for r in surveys.to_dict("records")]
        targets = set(surveys.loc[surveys["_target"], "survey_id"])
        crow, reviews = [], {}
        for r in corrs.to_dict("records"):
            r = {k: (v if isinstance(v, (list, tuple)) else clean(v)) for k, v in r.items()}
            rv = {k[2:]: r.pop(k) for k in list(r) if k.startswith("r_")}
            if rv.get("review_id"):
                reviews[rv["review_id"]] = rv
            crow.append(r)
        # v1.5 (QA-039): approvals are the audit.review_record rows, verified in C.approval_problem
        out, _proposals = batch.reduce_instrument(inst, rows, crow, batch_id, reduced_ts, target_surveys=targets,
                                                  review_rows=list(reviews.values()))
        return pd.DataFrame(out, columns=list(batch.REDUCED_COLUMNS))
    return _reduce


def main():
    a = args(("full_recompute", "false"))
    spark = get_spark()
    fq = lambda s, t: f"{a.catalog}.{s}.{t}"                          # noqa: E731
    full = str(a.full_recompute).lower() == "true"
    batch_id = f"incl-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    reduced_ts = datetime.now(timezone.utc).replace(tzinfo=None)     # UTC, naive for Arrow TimestampType

    # v1.5 (SCHEMA_CONTRACT §5.5): ALL SCD2 rows; each survey joins the row valid at survey time
    # (effective_from <= survey_ts < effective_to, NULL = open). Not the current row.
    inst = (spark.read.table(fq("ref", "incl_installation"))
            .select(*[F.col(c).alias(f"i_{c}") for c in INST_COLS], F.col("effective_from").alias("i_from"),
                    F.col("effective_to").alias("i_to")))
    surv = spark.read.table(fq("silver", "incl_survey")).select(*SURVEY_COLS)
    corr = spark.read.table(fq("ref", "incl_correction"))
    rv = (spark.read.table(fq("audit", "review_record")).where(F.col("subject_type") == "incl_correction")
          .select(*[F.col(c).alias(f"r_{c}") for c in REVIEW_COLS]))
    corr_rv = corr.join(rv, corr.review_id == rv.r_review_id, "left")       # approving review row (or NULLs)
    version = f"{batch.ENGINE_VERSION}+{batch.BIAS_RULE_VERSION}"     # formula_version + qc_rule_version
    last = (spark.read.table(fq("silver", "incl_reduced")).groupBy("instrument_id", "survey_id")
            .agg(F.max("reduced_ts").alias("last_ts"),
                 F.max_by(F.concat_ws("+", "formula_version", "qc_rule_version"), "reduced_ts").alias("last_version"),
                 F.max_by("installation_version", "reduced_ts").alias("last_inst_version")))
    corr_ts = (corr.groupBy("instrument_id", "survey_id")
               .agg(F.max(F.greatest("timestamp_utc", F.coalesce("reviewed_utc", "timestamp_utc"))).alias("corr_ts")))

    valid = ((surv.instrument_id == inst.i_instrument_id) & (surv.survey_ts >= inst.i_from)
             & (inst.i_to.isNull() | (surv.survey_ts < inst.i_to)))
    s = (surv.join(inst, valid, "inner")                       # no installation row valid at survey time: skipped
             .join(last, ["instrument_id", "survey_id"], "left")
             .join(corr_ts, ["instrument_id", "survey_id"], "left"))
    s = s.withColumn("_target", F.lit(True) if full else (
        F.col("last_ts").isNull() | (F.col("corr_ts") > F.col("last_ts")) | ~F.col("last_inst_version").eqNullSafe(F.col("i_from"))
        | ~F.col("last_version").eqNullSafe(F.lit(version))))                 # bias-rule / engine version bump
    keep = s.groupBy("instrument_id").agg(F.max(F.col("_target").cast("int")).alias("_any")).where("_any = 1")
    s = s.join(keep.select("instrument_id"), ["instrument_id"], "inner").drop("last_ts", "last_inst_version",
                                                                             "corr_ts", "last_version")
    out = (s.groupBy("instrument_id").cogroup(corr_rv.groupBy("instrument_id"))
            .applyInPandas(make_udf(batch_id, reduced_ts), schema=OUT_SCHEMA))
    out.write.mode("append").saveAsTable(fq("silver", "incl_reduced"))   # silver.incl_reduced is appendOnly


if __name__ == "__main__":
    main()
