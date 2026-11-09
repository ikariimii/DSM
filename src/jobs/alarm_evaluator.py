"""Alarm evaluator (job-owned ops.alarm_state / ops.alarm_transition / ops.exceedance).

Per sensor: resume ops.alarm_state, read current reduced readings after the watermark
(silver.v_reading_current), apply Lakebase acknowledgements extracted to audit.event_log
(event_type='ALARM_ACK', subject_id = RAISE/ESCALATE transition_id; app state
'acknowledged' -> ACK), run dam_safety.alarms.evaluate_sensor_batch (unit-tested), then:
  * APPEND transitions (immutable history) and exceedances (RAISE/ESCALATE),
  * MERGE the alarm_state projection (the only mutable ops table, by design).
Notifications: none here. SQL alerts on gold.threshold_exceedances do that (KB rec.).
Threshold/reading unit mismatch -> threshold skipped and logged (no silent conversion).
"""
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime

import _common  # noqa: F401
from _common import args, spark as get_spark

from dam_safety.alarms import evaluate_sensor_batch
from dam_safety.inclinometer.batch import installation_version_at
from dam_safety.spark.engine import fq

a = args(("lookback_days", "7"))
spark = get_spark()
C = a.catalog
run_id = f"al-{uuid.uuid4()}"
now = datetime.utcnow()

th = spark.sql(f"""
  SELECT t.*, s.sensor_id AS scope_sensor FROM {fq(C,'ref','threshold')} t
  JOIN {fq(C,'ref','sensor')} s ON s.instrument_id = t.instrument_id AND (t.sensor_id IS NULL OR t.sensor_id = s.sensor_id)
  WHERE t.is_current AND t.effective_to IS NULL""").collect()
specs = defaultdict(list)
for r in th:
    specs[r.scope_sensor].append(r.asDict())
sensors = list(specs)
if not sensors:
    print("no thresholds configured"); raise SystemExit(0)

states = {r.sensor_id: r.asDict() for r in spark.table(fq(C, "ops", "alarm_state")).collect()}
min_wm = min([s["evaluated_through_ts"] for s in states.values() if s.get("evaluated_through_ts")], default=None)
rd = spark.table(fq(C, "silver", "v_reading_current")).where(f"sensor_id IN ({','.join(repr(s) for s in sensors)})")
rd = rd.where(f"ts_utc > timestamp'{min_wm}'" if min_wm and len(states) == len(sensors)
              else f"ts_utc > current_timestamp() - INTERVAL {int(a.lookback_days)} DAYS")
readings = defaultdict(list)
for r in rd.select("reading_id", "dam_id", "sensor_id", "ts_utc", "corrected_value", "corrected_unit", "qa_flag", "qa_reason",
                   "calib_id", "cal_version", "formula_id", "formula_version", "recompute_batch_id").collect():
    readings[r.sensor_id].append({"reading_id": r.reading_id, "dam_id": r.dam_id, "ts_utc": r.ts_utc,
                                  "value": r.corrected_value, "unit": r.corrected_unit, "qa_flag": r.qa_flag, "qa_reason": r.qa_reason,
                                  "calib_id": r.calib_id, "cal_version": r.cal_version, "formula_id": r.formula_id,
                                  "formula_version": r.formula_version, "recompute_batch_id": r.recompute_batch_id})

applied = {r.comment for r in spark.sql(
    f"SELECT comment FROM {fq(C,'ops','alarm_transition')} WHERE event='ACK' AND comment LIKE 'lakebase_ack:%'").collect()}
acks = defaultdict(list)
for r in spark.sql(f"""
    SELECT e.subject_id, e.actor, e.event_ts, t.sensor_id FROM {fq(C,'audit','event_log')} e
    JOIN {fq(C,'ops','alarm_transition')} t ON t.transition_id = e.subject_id
    WHERE e.event_type = 'ALARM_ACK'""").collect():
    tag = f"lakebase_ack:{r.subject_id}"
    if tag not in applied:
        acks[r.sensor_id].append({"ts": r.event_ts, "actor": r.actor, "comment": tag})

sens_rows = spark.table(fq(C, "ref", "sensor")).select("sensor_id", "dam_id", "instrument_id").collect()
dam_of = {r.sensor_id: r.dam_id for r in sens_rows}
# v1.6 (app SCHEMA_CONTRACT §7 10d): inclinometer alarms carry installation_version = effective_from of the
# ref.incl_installation row valid at the reading's ts (half-open; same rule as the reduction, batch.installation_row_at).
# A sensor is an inclinometer sensor when its instrument has ref.incl_installation rows. No covering row -> NULL
# (the app then withholds the value, LINEAGE_INCOMPLETE) and an issue is logged; overlapping rows -> logged, NULL.
inst_of = {r.sensor_id: r.instrument_id for r in sens_rows}
incl_rows = defaultdict(list)
for r in spark.table(fq(C, "ref", "incl_installation")).select("instrument_id", "effective_from", "effective_to").collect():
    incl_rows[r.instrument_id].append(r.asDict())


def inst_version(sid, ts, log):
    rows = incl_rows.get(inst_of.get(sid))
    if not rows:
        return None
    try:
        v = installation_version_at(rows, ts)
    except ValueError as e:
        log.append({"sensor_id": sid, "issue": f"installation_version: {e}"})
        return None
    if v is None:
        log.append({"sensor_id": sid, "issue": f"installation_version: no ref.incl_installation row valid at {ts}"})
    return v


tr_rows, ex_rows, st_rows, log = [], [], [], []
for sid in sensors:
    rows = readings.get(sid, [])
    units = {r["unit"] for r in rows if r["unit"]}
    ok_specs = [t for t in specs[sid] if not units or t["unit"] in units]
    for t in specs[sid]:
        if t not in ok_specs:
            log.append({"sensor_id": sid, "threshold_id": t["threshold_id"], "issue": f"unit {t['unit']} not in {sorted(units)}"})
    if not rows and not acks.get(sid):
        continue
    state_row, trs, n_late = evaluate_sensor_batch(sid, rows, ok_specs, states.get(sid), acks.get(sid, []))
    by_ts = {r["ts_utc"]: r for r in rows}
    if n_late:
        log.append({"sensor_id": sid, "issue": f"{n_late} readings at/behind watermark not evaluated"})
    for t in trs:
        tid = hashlib.sha256(f"{t.key}|{t.ts}|{t.event}|{t.to_state}|{t.comment}".encode()).hexdigest()
        src = by_ts.get(t.ts, {}) if t.event != "ACK" else {}
        rid = src.get("reading_id")
        iv = inst_version(sid, t.ts, log) if t.event != "ACK" else None
        tr_rows.append((tid, t.key, dam_of[sid], sid, t.ts, t.event, t.from_state, t.to_state, t.value, t.metric_value,
                        t.threshold_id, t.threshold_version, t.actor, t.comment, rid, run_id, now,
                        src.get("calib_id"), src.get("cal_version"), src.get("formula_id"), src.get("formula_version"),
                        src.get("recompute_batch_id"), src.get("qa_flag"), iv))
        if t.event in ("RAISE", "ESCALATE") and rid:
            spec = next(s for s in ok_specs if s["threshold_id"] == t.threshold_id)
            ex_rows.append((f"ex-{tid[:32]}", dam_of[sid], rid, tid, t.threshold_id, t.threshold_version, t.ts,
                            spec["tier"], int(spec["level"]), t.value, t.metric_value, now))
    state_row.update(dam_id=dam_of[sid], sensor_id=sid, updated_ts=now)
    st_rows.append(state_row)

# v1.4 guard (mirrors ck_alarm_transition_value): never write a reading-driven transition with NULL value/lineage.
bad = [r for r in tr_rows if r[5] != "ACK" and (r[8] is None or r[9] is None or r[14] is None)]
if bad:
    raise RuntimeError(f"{len(bad)} non-ACK transitions with NULL value/metric_value/reading_id, e.g. {bad[0][:6]}")
if tr_rows:
    spark.createDataFrame(tr_rows, spark.table(fq(C, "ops", "alarm_transition")).schema).write.mode("append") \
        .saveAsTable(fq(C, "ops", "alarm_transition"))
if ex_rows:
    spark.createDataFrame(ex_rows, spark.table(fq(C, "ops", "exceedance")).schema).write.mode("append") \
        .saveAsTable(fq(C, "ops", "exceedance"))
if st_rows:
    sch = spark.table(fq(C, "ops", "alarm_state")).schema
    df = spark.createDataFrame([tuple(r.get(f.name) for f in sch.fields) for r in st_rows], sch)
    df.createOrReplaceTempView("new_state")
    spark.sql(f"""MERGE INTO {fq(C,'ops','alarm_state')} t USING new_state s ON t.alarm_key = s.alarm_key
                  WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *""")
spark.createDataFrame([(str(uuid.uuid4()), None, "NOTE", "alarm_evaluator_run", run_id, "system", now,
                        json.dumps({"transitions": len(tr_rows), "exceedances": len(ex_rows), "issues": log[:200]}), now)],
                      spark.table(fq(C, "audit", "event_log")).schema).write.mode("append").saveAsTable(fq(C, "audit", "event_log"))
print(f"{run_id}: {len(tr_rows)} transitions, {len(ex_rows)} exceedances, {len(log)} issues")
