"""v1.8 shared glue for the dam_ref_publish loaders that write per-dam reference data (apply_cdse_assignments,
load_dssmp_registrations, publish_ref_changes): refuse pending outbox rows of a read-only dam (transferred /
decommissioned_archived -> 'dam_archived'), write the refusal back to the outbox and audit it (audit.event_log
DAM_ARCHIVED_REFUSAL, idempotent). Pure logic: dam_safety.lifecycle_load.archived_split / archived_audit_rows.
UNVERIFIED on a workspace."""
import datetime as dt
import json

from dam_safety import lifecycle as L
from dam_safety.lifecycle_load import LOG_COLS, archived_audit_rows, archived_split, insert_sql

AUDIT_VIEW = "dam_archived_refusal_src"


def lifecycle_current(spark, catalog):
    """current_states of audit.lifecycle_event; None when it cannot be read (table missing before v1.8: no refusal)."""
    try:
        return L.current_states([r.asDict() for r in spark.table(f"{catalog}.audit.lifecycle_event").collect()])
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"lifecycle_event_unreadable": str(e)[:300]}))
        return None


def refuse_archived(spark, catalog, cur, pending, *, id_key, outbox, job, update_sql, update_args, job_run_id=None):
    """Writes the refusals (cur.execute(update_sql, update_args(refusal, now_iso))) and their audit rows; returns the
    rows still to plan. The caller commits the Lakebase transaction."""
    keep, refused = archived_split(pending, lifecycle_current(spark, catalog), id_key)
    if not refused:
        return keep
    now = dt.datetime.now(dt.timezone.utc)
    rows = archived_audit_rows(refused, job, outbox, job_run_id, now)
    sch = spark.table(f"{catalog}.audit.event_log").schema
    spark.createDataFrame([tuple(r[c] for c in LOG_COLS) for r in rows], type(sch)([sch[c] for c in LOG_COLS])) \
        .createOrReplaceTempView(AUDIT_VIEW)
    spark.sql(insert_sql(f"{catalog}.audit.event_log", AUDIT_VIEW, LOG_COLS, "event_id"))
    for x in refused:
        cur.execute(update_sql, update_args(x, now.isoformat()))
        print(json.dumps({id_key: x["id"], "dam_id": x["dam_id"], "status": "rejected", "reason_code": "dam_archived",
                          "message": x["message"]}))
    return keep
