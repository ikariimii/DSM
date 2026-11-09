"""Job dam_ref_publish, task apply_cdse_assignments (runs FIRST, so a CDSE assigned or changed before a DSSMP
registration in the same run already has authority). App SCHEMA_CONTRACT §7 item 20.

Per run: read the pending rows of the app outbox dam_app.dam_cdse_outbox (Lakebase; publisher role); read every
subject_type 'dam' record of audit.review_record (the app's chained dam_cdse_assign / dam_cdse_change_* records),
ref.dam_cdse_assignment, ref.dam (cdse_id, status), the go-live time (first ref.threshold.effective_from), platform
admins and CDSE memberships (ref.project_membership); plan with dam_safety.cdse_load.plan (engine
dam_safety.cdse_assignment re-checks each record; refusals cdse_exists / cdse_stale / approval_*); ONE conditional
MERGE adds the new assignment rows and closes the replaced ones (stale re-check inside the statement); a second MERGE
syncs ref.dam.cdse_id to the open assignment (dams that have a ref.dam row); one audit.event_log row per load outcome
(event_type CDSE_ASSIGNMENT_LOAD, deterministic event_id, inserted WHERE NOT EXISTS); then the outcome is written
back to the outbox (status / reason_code / message / processed_at only). A crash between the MERGE and the outbox
update is safe: the next run finds the assignment (already_applied -> loaded) and does not duplicate the audit row.
UNVERIFIED: never run on a workspace (Delta MERGE with subqueries in the USING source; MODIFY on ref.dam and
ref.dam_cdse_assignment and INSERT on audit.event_log for the run_as principal; the publisher Lakebase grants).
"""
import datetime as dt
import json

try:
    from . import _common  # noqa: F401
except ImportError:        # executed as a script by spark_python_task
    import _common         # noqa: F401
from dam_safety.cdse_assignment import ASSIGNMENT_COLS, dam_sync_sql, merge_source_rows, merge_sql
from dam_safety.cdse_load import (EVENT_COLS, OUTBOX_COLS, audit_insert_sql, audit_rows, mark_merge_skipped,
                                  outbox_updates, plan)
from dam_safety.rule_flag_authority import second_approver_required

OUTBOX_SQL = (f"SELECT {', '.join(OUTBOX_COLS)} FROM dam_cdse_outbox WHERE status = 'pending' "
              "ORDER BY approved_at, outbox_id")
UPDATE_SQL = ("UPDATE dam_cdse_outbox SET status = %s, reason_code = %s, message = %s, processed_at = %s "
              "WHERE outbox_id = %s AND status = 'pending'")
SOURCE_VIEW = "dam_cdse_assignment_merge_src"
AUDIT_VIEW = "dam_cdse_load_audit_src"
RECORD_COLS = ("review_id", "dam_id", "subject_type", "subject_id", "decision", "comment", "reviewer_id",
               "reviewer_email", "signed_ts", "action_type", "original_value", "corrected_value", "chain_id",
               "chain_seq", "record_hash")
SECOND_APPROVER_ARG = "cdse_flag_require_second_approver"


def load_context(spark, catalog):
    """(records, assignments, legacy, admins, dam_state, memberships, dam_cdse) from UC; also used by
    verify_review_chain."""
    rr = spark.table(f"{catalog}.audit.review_record")
    records = [r.asDict() for r in rr.where(rr.subject_type == "dam").select(*RECORD_COLS).collect()]
    assignments = [r.asDict() for r in spark.table(f"{catalog}.ref.dam_cdse_assignment").collect()]
    assigned = {a["dam_id"] for a in assignments}
    dams = spark.sql(f"SELECT d.dam_id, d.cdse_id, d.status, min(t.effective_from) AS live_since "
                     f"FROM {catalog}.ref.dam d LEFT JOIN {catalog}.ref.threshold t ON t.dam_id = d.dam_id "
                     f"GROUP BY d.dam_id, d.cdse_id, d.status").collect()
    legacy = {r.dam_id: r.cdse_id for r in dams if r.cdse_id and r.dam_id not in assigned}
    dam_state = {r.dam_id: {"status": r.status, "live_since": r.live_since} for r in dams}
    dam_cdse = {r.dam_id: r.cdse_id for r in dams}
    pm = [r.asDict() for r in spark.sql(
        f"SELECT principal, dam_id, upper(role) AS role, CAST(granted_ts AS STRING) AS granted_ts, "
        f"CAST(revoked_ts AS STRING) AS revoked_ts FROM {catalog}.ref.project_membership "
        f"WHERE upper(role) IN ('ADMIN', 'CDSE')").collect()]
    admins = [m for m in pm if m["role"] == "ADMIN" and m["dam_id"] == "*"]
    memberships = [m for m in pm if m["role"] == "CDSE" and m["dam_id"] != "*"]
    return records, assignments, legacy, admins, dam_state, memberships, dam_cdse


def main():
    a = _common.args(("lb_project", "dam-safety"), ("lb_branch", "production"), ("lb_endpoint", "primary"),
                     ("job_run_id", None), (SECOND_APPROVER_ARG, None))
    run_id = a.job_run_id or "manual-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    second = second_approver_required({"CDSE_FLAG_REQUIRE_SECOND_APPROVER": getattr(a, SECOND_APPROVER_ARG)}
                                      if getattr(a, SECOND_APPROVER_ARG) is not None else None)
    spark = _common.spark()
    from lakebase_extract import _connect
    from psycopg.rows import dict_row
    tgt = f"{a.catalog}.ref.dam_cdse_assignment"
    with _connect(a.lb_project, a.lb_branch, a.lb_endpoint) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET search_path TO dam_app")
        cur.execute(OUTBOX_SQL)
        pending = cur.fetchall()
        if not pending:
            print(json.dumps({"pending": 0}))
            return
        records, assignments, legacy, admins, dam_state, memberships, dam_cdse = load_context(spark, a.catalog)
        now = dt.datetime.now(dt.timezone.utc)
        decisions = plan(pending, records, assignments, legacy, admins, dam_state, dam_cdse, now, memberships, second)
        src = merge_source_rows([x for d in decisions if d["decision"] == "applied" for x in d["rows"]])
        if src:
            from pyspark.sql.types import StringType, StructField, StructType
            t_schema = spark.table(tgt).schema
            s_schema = StructType([t_schema[c] for c in ASSIGNMENT_COLS] + [
                StructField("merge_action", StringType()), StructField("merge_key", StringType()),
                StructField("supersedes_id", StringType())])
            names = [f.name for f in s_schema.fields]
            spark.createDataFrame([tuple(r.get(n) for n in names) for r in src], s_schema) \
                .createOrReplaceTempView(SOURCE_VIEW)
            spark.sql(merge_sql(tgt, SOURCE_VIEW))
            live_ids = {r.assignment_id for r in spark.table(tgt).select("assignment_id").collect()}
            mark_merge_skipped(decisions, live_ids)
        spark.sql(dam_sync_sql(f"{a.catalog}.ref.dam", tgt))
        audit = audit_rows(decisions, run_id, now)
        if audit:
            ev = spark.table(f"{a.catalog}.audit.event_log").schema
            spark.createDataFrame([tuple(r[c] for c in EVENT_COLS) for r in audit],
                                  type(ev)([ev[c] for c in EVENT_COLS])).createOrReplaceTempView(AUDIT_VIEW)
            spark.sql(audit_insert_sql(f"{a.catalog}.audit.event_log", AUDIT_VIEW))
        for u in outbox_updates(decisions, now.isoformat()):
            cur.execute(UPDATE_SQL, u)
        conn.commit()
    for d in decisions:
        print(json.dumps({k: d[k] for k in ("outbox_id", "dam_id", "decision", "status", "reason_code", "message")},
                         default=str))


if __name__ == "__main__":
    main()
