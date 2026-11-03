"""History recompute after a calibration insert/correction (FERC canvas REQ-23 lineage).
Compares the SCD2 versions for the sensor(s) as of --before_ts with the current table
(Delta time travel), plans windows with dam_safety.calibration.plan_recompute, appends
re-reduced rows and logs audit.recompute_log + audit.event_log(RECOMPUTE). Alarm re-evaluation
of the window is NOT automatic: differences go to audit.alarm_recompute_diff for engineer
review (re-alarming history would re-notify; SCHEMA_CONTRACT §6)."""
import json
import uuid
from datetime import datetime

import _common  # noqa: F401
from _common import args, spark as get_spark

from dam_safety.calibration import Version, plan_recompute
from dam_safety.spark import engine

a = args(("sensor_ids", ""), ("before_ts", None), ("requested_by", "system"), ("reason", "calibration change"))
spark = get_spark()
cal = engine.fq(a.catalog, "ref", "calibration")
if not a.before_ts:
    raise SystemExit("--before_ts (timestamp of the calibration table before the change) is required")
sensors = [s for s in a.sensor_ids.split(",") if s]


def versions(df):
    if sensors:
        df = df.where(df.sensor_id.isin(sensors))
    return [Version(key=r.sensor_id, version_id=r.calib_id, effective_from=r.effective_from, effective_to=r.effective_to,
                    payload_hash=r.payload_hash)
            for r in df.where("NOT is_retired").select("sensor_id", "calib_id", "effective_from", "effective_to",
                                                         "payload_hash").collect()]


old = versions(spark.read.option("timestampAsOf", a.before_ts).table(cal))
new = versions(spark.table(cal))
windows = plan_recompute(old, new)
if not windows:
    print("no recompute needed")
else:
    bid = engine.recompute(spark, a.catalog, windows, a.requested_by, a.reason, f"rc-{uuid.uuid4()}")
    spark.createDataFrame([(str(uuid.uuid4()), None, "RECOMPUTE", "recompute_batch", bid, a.requested_by,
                            datetime.utcnow(), json.dumps({"windows": len(windows), "reason": a.reason}),
                            datetime.utcnow())],
                          "event_id string, dam_id string, event_type string, subject_type string, subject_id string, "
                          "actor string, event_ts timestamp, payload string, recorded_ts timestamp") \
        .write.mode("append").saveAsTable(engine.fq(a.catalog, "audit", "event_log"))
    print(f"recompute batch {bid}: {len(windows)} windows")
