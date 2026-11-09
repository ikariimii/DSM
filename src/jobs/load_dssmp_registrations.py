"""Job dam_ref_publish, task load_dssmp_registrations (runs BEFORE publish_ref_changes, so a threshold published in the
same run can name the new DSSMP version): app outbox dam_app.dssmp_registration_outbox -> ref.dssmp_version.

Per run: read the pending outbox rows (Lakebase); read ref.dssmp_version for their dams, the approve records named by
approval_record_hash (UC audit.review_record, extracted from the app's chained review_signoff) and
ref.v_dam_reviewer_authority; plan with dam_safety.dssmp_registration.plan (refuses rows without a matching CDSE
dssmp_version_approve chain record, stale rows, duplicates); ONE conditional MERGE inserts each new approved version
and closes the dam's previous approved one in the same statement; then the outcome is written back to the outbox
(status / reason_code / message / processed_at only: role dam_lakebase_publisher). Before the MERGE each
version's document is copied from dam_app.dssmp_document to /Volumes/<catalog>/ref/dssmp_documents/<dam_id>/
<dssmp_version_id>/<file> with sha256 checked before and after (dam_safety.dssmp_documents; volume_uri). A crash between the MERGE and the outbox update is
safe: the next run finds the version with loaded_from_outbox_id = outbox_id (already_applied -> applied).
UNVERIFIED: never run on a workspace (Delta MERGE with subqueries in the USING source; the publisher Lakebase role on
a deployed Lakebase; the run_as principal's MODIFY on ref.dssmp_version and SELECT on audit.review_record).
"""
import datetime as dt
import json

try:
    from . import _common  # noqa: F401
except ImportError:        # executed as a script by spark_python_task
    import _common         # noqa: F401
from dam_safety.dssmp_documents import DOC_SQL, VolumeFS, apply_documents, document_ids
from dam_safety.dssmp_registration import DSSMP_COLS, OUTBOX_COLS, merge_source_rows, merge_sql, plan

OUTBOX_SQL = (f"SELECT {', '.join(OUTBOX_COLS)} FROM dssmp_registration_outbox WHERE status = 'pending' "
              "ORDER BY approved_at, outbox_id")
UPDATE_SQL = ("UPDATE dssmp_registration_outbox SET status = %s, reason_code = %s, message = %s, processed_at = %s "
              "WHERE outbox_id = %s AND status = 'pending'")
SOURCE_VIEW = "dssmp_registration_merge_src"
APPROVAL_COLS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                 "reviewer_id", "reviewer_email", "signed_ts", "action_type", "original_value", "corrected_value",
                 "chain_id", "chain_seq", "record_hash", "actor_app_role", "canon_version")


def outbox_updates(decisions, now_iso):
    return [(d["status"], d["reason_code"], d["message"], now_iso, d["outbox_id"])
            for d in decisions if d["status"] is not None]


def main():
    a = _common.args(("lb_project", "dam-safety"), ("lb_branch", "production"), ("lb_endpoint", "primary"),
                     ("lb_schema", "dam_app"))
    spark = _common.spark()
    from lakebase_extract import _connect
    from psycopg.rows import dict_row
    tgt = f"{a.catalog}.ref.dssmp_version"
    with _connect(a.lb_project, a.lb_branch, a.lb_endpoint, a.lb_schema) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(OUTBOX_SQL)
        pending = cur.fetchall()
        if not pending:
            print(json.dumps({"pending": 0}))
            return
        dams = sorted({r["dam_id"] for r in pending if r.get("dam_id")})
        ids = sorted({r["dssmp_version_id"] for r in pending if r.get("dssmp_version_id")})
        dv = spark.table(tgt)
        versions = [r.asDict() for r in dv.where(dv.dam_id.isin(dams) | dv.dssmp_version_id.isin(ids)).collect()]
        hashes = sorted({r["approval_record_hash"] for r in pending if r.get("approval_record_hash")})
        rr = spark.table(f"{a.catalog}.audit.review_record")
        approvals = {r.record_hash: r.asDict() for r in rr.where(rr.record_hash.isin(hashes))
                     .select(*APPROVAL_COLS).collect()}
        authority = [r.asDict() for r in spark.sql(
            f"SELECT dam_id, principal, CAST(granted_ts AS STRING) AS granted_ts, CAST(revoked_ts AS STRING) AS "
            f"revoked_ts FROM {a.catalog}.ref.v_dam_reviewer_authority").collect()]
        now = dt.datetime.now(dt.timezone.utc)
        decisions = plan(pending, versions, approvals, authority, now)
        if any(d["decision"] == "applied" for d in decisions):   # copy + sha256 before/after, refuse on mismatch
            cur.execute(DOC_SQL, (document_ids(pending),))
            docs = {r["document_id"]: r for r in cur.fetchall()}
            apply_documents(decisions, pending, docs, a.catalog, VolumeFS())
        src = merge_source_rows(decisions)
        if src:
            from pyspark.sql.types import StringType, StructField, StructType
            s_schema = StructType(list(dv.schema.fields) + [StructField("merge_action", StringType()),
                                                            StructField("merge_key", StringType()),
                                                            StructField("supersedes_id", StringType())])
            names = [f.name for f in s_schema.fields]
            spark.createDataFrame([tuple(r.get(n) for n in names) for r in src], s_schema) \
                .createOrReplaceTempView(SOURCE_VIEW)
            spark.sql(merge_sql(tgt, SOURCE_VIEW))
            live = {r.dssmp_version_id: r.loaded_from_outbox_id
                    for r in dv.where(dv.dam_id.isin(dams)).select("dssmp_version_id", "loaded_from_outbox_id").collect()}
            for d in decisions:                   # the MERGE's own stale re-check skipped it
                if d["decision"] == "applied" and live.get(d["insert"]["dssmp_version_id"]) != d["outbox_id"]:
                    d.update(decision="rejected_stale", status="rejected", reason_code="stale",
                             message="stale: skipped by the MERGE stale re-check (concurrent change)",
                             codes=["stale"])
        for u in outbox_updates(decisions, now.isoformat()):
            cur.execute(UPDATE_SQL, u)
        conn.commit()
    for d in decisions:
        print(json.dumps({k: d[k] for k in ("outbox_id", "dam_id", "decision", "status", "message")}, default=str))


if __name__ == "__main__":
    main()
