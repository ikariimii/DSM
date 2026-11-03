"""Incremental reduction: silver.reading_clean (append-only streaming table) ->
silver.reading_reduced (append-only, job-owned). Structured Streaming availableNow with a
checkpoint in the ops volume gives exactly-once batches; each micro-batch is reduced with the
as-of calibration (SCD2) and QC'd, then appended with recompute_batch_id = 'inc-<epoch>'."""
import _common  # noqa: F401  (sys.path)
from _common import args, spark as get_spark

from dam_safety.spark import engine

a = args(("checkpoint", None))
spark = get_spark()
cp = a.checkpoint or f"/Volumes/{a.catalog}/ops/checkpoints/reduce_readings"


def _batch(df, epoch_id):
    if df.isEmpty():
        return
    out = engine.reduce_and_qc(spark, a.catalog, df, f"inc-{epoch_id}", False)
    engine.write_reduced(spark, a.catalog, out)


(spark.readStream.table(engine.fq(a.catalog, "silver", "reading_clean"))
      .writeStream.foreachBatch(_batch).option("checkpointLocation", cp)
      .trigger(availableNow=True).start().awaitTermination())
