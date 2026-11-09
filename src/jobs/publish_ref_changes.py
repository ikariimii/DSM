"""Job dam_ref_publish (task publish_ref_changes): turn the app's Lakebase outbox dam_app.ref_change_publication
(kind 'ref.threshold.new_version', app SCHEMA_CONTRACT §7 items 13 / 15) into NEW ref.threshold versions.

Per run: read the pending outbox rows (Lakebase), the current ref.threshold rows they name and ref.dssmp_version
(UC); plan every row with dam_safety.ref_change_publish.plan (validation of every ref.threshold NOT NULL column and of
dssmp_version_id against ref.dssmp_version for the SAME dam - Delta FKs are informational only - with a reason for each
rejection); run ONE conditional MERGE for all valid rows (stale rows are re-checked inside the MERGE); check that no
existing version changed a rule field in place; then write each row's outcome back to the outbox (status applied |
rejected_stale | rejected_invalid, message, applied_at). Rows deferred behind an older pending row of the same rule stay
pending. A crash between the MERGE and the outbox update is safe: the next run sees the version already current
(already_applied -> applied) instead of a stale reject.
§7 item 21: a row whose payload has cause 'dssmp_reversion' is authorised by its rule_reversion_approve record
(audit.review_record, payload approval_record_hash) checked against ref.v_dam_reviewer_authority at signed_ts
(dam_safety.rule_reversion_authority); the approval context is read only when such rows are pending.

Job dam_ref_publish (resources/jobs.yml; its own job so it can get its own run_as). Runs as the job's run_as principal
(the setup service principal; signed-in users never write ref.*). Lakebase: its login must be a member of the NOLOGIN
role dam_lakebase_publisher (lakebase/001, GUC dam.publisher_login): SELECT + column-level UPDATE (status, message,
applied_at) on the two publication outboxes only; the extract role stays SELECT-only. UNVERIFIED: never run on a workspace (Delta MERGE with an EXISTS subquery over the target,
the Lakebase grants on a deployed Lakebase, the run_as principal's MODIFY on ref.threshold).
"""
import datetime as dt
import json

try:
    from . import _common  # noqa: F401
except ImportError:        # executed as a script by spark_python_task
    import _common         # noqa: F401
from dam_safety.ref_change_publish import KIND, merge_source_rows, merge_sql, payload_of, plan
from dam_safety.rule_flag_authority import second_approver_required
from dam_safety.rule_reversion_authority import CAUSE, SUBJECT
from dam_safety.threshold_publish import assert_versions_immutable

OUTBOX_SQL = ("SELECT pub_id, kind, dam_id, proposal_id, payload, requested_by, requested_at, status "
              "FROM ref_change_publication WHERE status = 'pending' AND kind = %s ORDER BY requested_at, pub_id")
UPDATE_SQL = ("UPDATE ref_change_publication SET status = %s, message = %s, applied_at = %s "
              "WHERE pub_id = %s AND status = 'pending'")
SOURCE_VIEW = "ref_change_publication_merge_src"


def dssmp_index(spark, catalog):
    """ref.dssmp_version rows by id (the columns dam_safety.dssmp_check judges)."""
    return {r.dssmp_version_id: r.asDict() for r in spark.table(f"{catalog}.ref.dssmp_version")
            .select("dssmp_version_id", "dam_id", "status", "effective_from", "effective_to").collect()}


def outbox_updates(decisions, now_iso):
    """(status, message, applied_at, pub_id) per decided row; deferred rows are left pending (no update)."""
    return [(d["status"], d["message"], now_iso if d["status"] == "applied" else None, d["pub_id"])
            for d in decisions if d["status"] is not None]


SECOND_APPROVER_ARG = "cdse_flag_require_second_approver"


def reversion_context(spark, catalog, pending, second):
    """None unless a pending row is a DSSMP re-version; else the approval records (by record_hash) and authority."""
    if not any(payload_of(r).get("cause") == CAUSE for r in pending):
        return None
    rr = spark.table(f"{catalog}.audit.review_record")
    recs = [r.asDict() for r in rr.where((rr.subject_type == SUBJECT) & rr.record_hash.isNotNull()).collect()]
    authority = [r.asDict() for r in spark.sql(
        f"SELECT dam_id, principal, CAST(granted_ts AS STRING) AS granted_ts, CAST(revoked_ts AS STRING) AS revoked_ts "
        f"FROM {catalog}.ref.v_dam_reviewer_authority").collect()]
    return {"records_by_hash": {r["record_hash"]: r for r in recs}, "authority": authority,
            "now": dt.datetime.now(dt.timezone.utc), "require_second_approver": second}


def main():
    a = _common.args(("lb_project", "dam-safety"), ("lb_branch", "production"), ("lb_endpoint", "primary"),
                     ("lb_schema", "dam_app"), (SECOND_APPROVER_ARG, None))
    second = second_approver_required({"CDSE_FLAG_REQUIRE_SECOND_APPROVER": getattr(a, SECOND_APPROVER_ARG)}
                                      if getattr(a, SECOND_APPROVER_ARG) is not None else None)
    spark = _common.spark()
    from lakebase_extract import _connect          # same OAuth connection helper as the extract task
    from psycopg.rows import dict_row
    tgt = f"{a.catalog}.ref.threshold"
    with _connect(a.lb_project, a.lb_branch, a.lb_endpoint, a.lb_schema) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(OUTBOX_SQL, (KIND,))
        pending = cur.fetchall()
        if not pending:
            print(json.dumps({"pending": 0}))
            return
        tids = sorted({payload_of(r).get("threshold_id") for r in pending} - {None})
        th = spark.table(tgt)
        before = [r.asDict() for r in th.where(th.threshold_id.isin(tids)).collect()]
        current = [r for r in before if r.get("is_current")]
        dssmp = dssmp_index(spark, a.catalog)
        decisions = plan(pending, current, dssmp, reversion_context(spark, a.catalog, pending, second))
        src = merge_source_rows(decisions)
        if src:
            schema = th.schema
            from pyspark.sql.types import IntegerType, StringType, StructField, StructType
            s_schema = StructType(list(schema.fields) + [StructField("merge_action", StringType()),
                                                         StructField("merge_version", IntegerType()),
                                                         StructField("supersedes_version", IntegerType()),
                                                         StructField("pub_id", StringType())])
            names = [f.name for f in s_schema.fields]
            spark.createDataFrame([tuple(r.get(n) for n in names) for r in src], s_schema) \
                .createOrReplaceTempView(SOURCE_VIEW)
            spark.sql(merge_sql(tgt, SOURCE_VIEW))
            after = [r.asDict() for r in th.where(th.threshold_id.isin(tids)).collect()]
            assert_versions_immutable(before, after)       # raises: an in-place rule-field change is a hard failure
            live = {(r["threshold_id"], r["threshold_version"]) for r in after}
            for d in decisions:                           # the MERGE's own stale re-check skipped it: report stale
                if d["decision"] == "applied" and (d["insert"]["threshold_id"],
                                                   d["insert"]["threshold_version"]) not in live:
                    d.update(decision="rejected_stale", status="rejected_stale",
                             message=d["message"] + "; skipped by the MERGE stale re-check (concurrent change)")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for u in outbox_updates(decisions, now):
            cur.execute(UPDATE_SQL, u)
        conn.commit()
    for d in decisions:
        print(json.dumps({k: d[k] for k in ("pub_id", "dam_id", "decision", "status", "message")}, default=str))


if __name__ == "__main__":
    main()
