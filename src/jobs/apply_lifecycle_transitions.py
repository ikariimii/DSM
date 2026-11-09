"""Job dam_ref_publish, task apply_lifecycle_transitions (v1.8; after load_dssmp_registrations so a DSSMP approved in
the same run counts for a suspension / resume, before publish_ref_changes so that task already sees a record that
became read-only).

Per run: read the pending rows of the app outbox dam_app.lifecycle_transition_outbox (Lakebase; publisher role);
read audit.review_record subject_type 'dam_lifecycle' (the app's chained lifecycle_transition_propose / _approve
records), audit.lifecycle_event (the whole per-dam chain; unreadable -> every row refused lifecycle_unverifiable),
ref.lifecycle_citation + ref.lifecycle_suspension_reason (enums are DATA), ref.dssmp_version,
ref.v_dam_go_live_readiness, ref.v_dam_reviewer_authority and the platform admins (ref.project_membership ADMIN '*');
plan with dam_safety.lifecycle_load.plan (engine dam_safety.lifecycle re-checks every rule at decision time); append
the new events (idempotent, never forking the chain); MERGE the SCD-2 projection ref.dam_lifecycle_state from the
whole history (self-healing) and mirror the open row onto ref.dam.lifecycle_state / lifecycle_effective_from; one
audit.event_log LIFECYCLE_LOAD row per outcome (deterministic event_id, inserted WHERE NOT EXISTS); then the outcome
is written back to the outbox (status / reason_code / message / processed_at only). A crash between the event insert
and the outbox update is safe: the next run finds the event (already_applied -> loaded).
UNVERIFIED: never run on a workspace (Delta INSERT ... WHERE NOT EXISTS on an appendOnly table, the MERGEs, ARRAY
columns through createDataFrame; MODIFY on ref.dam / ref.dam_lifecycle_state and INSERT on audit.lifecycle_event /
audit.event_log for the run_as principal; ref.v_dam_reviewer_authority row filter for that principal; the publisher
Lakebase grants).
"""
import datetime as dt
import json

try:
    from . import _common  # noqa: F401
except ImportError:        # executed as a script by spark_python_task
    import _common         # noqa: F401
from dam_safety import lifecycle as L
from dam_safety.lifecycle_load import (LOG_COLS, OUTBOX_COLS, STATE_COLS, SUBJECT, audit_rows, dam_mirror_sql,
                                       event_insert_sql, insert_sql, mark_insert_skipped, outbox_updates, plan,
                                       project_states, state_merge_sql)

OUTBOX_SQL = (f"SELECT {', '.join(OUTBOX_COLS)} FROM lifecycle_transition_outbox WHERE status = 'pending' "
              "ORDER BY approved_at, outbox_id")
UPDATE_SQL = ("UPDATE lifecycle_transition_outbox SET status = %s, reason_code = %s, message = %s, processed_at = %s "
              "WHERE outbox_id = %s AND status = 'pending'")
RECORD_COLS = ("review_id", "dam_id", "subject_type", "subject_id", "decision", "reviewer_id", "reviewer_email",
               "signed_ts", "action_type", "corrected_value", "chain_id", "chain_seq", "record_hash")
EVENT_VIEW = "lifecycle_event_src"
STATE_VIEW = "dam_lifecycle_state_src"
AUDIT_VIEW = "lifecycle_load_audit_src"


def _rows(df):
    return [r.asDict() for r in df.collect()]


def read_events(spark, catalog):
    """Every audit.lifecycle_event row, or None when the table cannot be read (fail closed: unverifiable)."""
    try:
        return _rows(spark.table(f"{catalog}.audit.lifecycle_event"))
    except Exception as e:  # noqa: BLE001 - any read failure means the state is unknown
        print(json.dumps({"lifecycle_event_unreadable": str(e)[:500]}))
        return None


def load_context(spark, catalog):
    c = catalog
    rr = spark.table(f"{c}.audit.review_record")
    records = _rows(rr.where(rr.subject_type == SUBJECT).select(*RECORD_COLS))
    try:
        citations = _rows(spark.table(f"{c}.ref.lifecycle_citation"))
        reasons = {r["suspension_reason"] for r in _rows(spark.table(f"{c}.ref.lifecycle_suspension_reason"))
                   if r["active"]}
    except Exception as e:  # noqa: BLE001 - unreadable enums: every row refused lifecycle_unverifiable
        print(json.dumps({"lifecycle_enums_unreadable": str(e)[:500]}))
        citations, reasons = None, None
    return {
        "records": records,
        "has_ref_dam": {r.dam_id for r in spark.table(f"{c}.ref.dam").select("dam_id").collect()},
        "citations": L.load_citations(citations), "reasons": reasons,
        "dssmp": _rows(spark.table(f"{c}.ref.dssmp_version").select(
            "dam_id", "dssmp_version_id", "status", "effective_from", "effective_to")),
        "readiness": {r.dam_id: bool(r.ready) for r in spark.table(f"{c}.ref.v_dam_go_live_readiness").collect()},
        "authority": _rows(spark.table(f"{c}.ref.v_dam_reviewer_authority").select(
            "dam_id", "principal", "granted_ts", "revoked_ts")),
        "admins": _rows(spark.sql(
            f"SELECT principal, dam_id, upper(role) AS role, granted_ts, revoked_ts FROM {c}.ref.project_membership "
            "WHERE upper(role) = 'ADMIN' AND dam_id = '*'")),
    }


def _df(spark, sch, cols, rows):
    return spark.createDataFrame([tuple(r[k] for k in cols) for r in rows], type(sch)([sch[k] for k in cols]))


def main():
    a = _common.args(("lb_project", "dam-safety"), ("lb_branch", "production"), ("lb_endpoint", "primary"),
                     ("lb_schema", "dam_app"), ("job_run_id", None))
    run_id = a.job_run_id or "manual-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spark = _common.spark()
    c = a.catalog
    ev_t = f"{c}.audit.lifecycle_event"
    st_t = f"{c}.ref.dam_lifecycle_state"
    from lakebase_extract import _connect
    from psycopg.rows import dict_row
    with _connect(a.lb_project, a.lb_branch, a.lb_endpoint, a.lb_schema) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(OUTBOX_SQL)
        pending = cur.fetchall()
        events = read_events(spark, c)
        decisions = []
        if pending:
            ctx = load_context(spark, c)
            now = dt.datetime.now(dt.timezone.utc)
            out = plan(pending, ctx.pop("records"), events, now=now, **ctx)
            decisions = out["decisions"]
            if out["events"]:
                rows = [dict(e, evidence_uri=list(e["evidence_uri"])) for e in out["events"]]
                _df(spark, spark.table(ev_t).schema, L.EVENT_COLS, rows).createOrReplaceTempView(EVENT_VIEW)
                spark.sql(event_insert_sql(ev_t, EVENT_VIEW))
                ids = [e["event_id"] for e in out["events"]]
                ev = spark.table(ev_t)
                stored = {r.event_id: r.event_hash for r in ev.where(ev.event_id.isin(ids))
                          .select("event_id", "event_hash").collect()}
                mark_insert_skipped(decisions, stored)
                events = read_events(spark, c)
        if events:                                   # projection + mirror every run (heals a crash between steps)
            states = project_states(events)
            _df(spark, spark.table(st_t).schema, STATE_COLS, states).createOrReplaceTempView(STATE_VIEW)
            spark.sql(state_merge_sql(st_t, STATE_VIEW))
            spark.sql(dam_mirror_sql(f"{c}.ref.dam", st_t))
        if not pending:
            print(json.dumps({"pending": 0}))
            return
        audit = audit_rows(decisions, run_id, now)
        if audit:
            log_schema = spark.table(f"{c}.audit.event_log").schema
            _df(spark, log_schema, LOG_COLS, audit).createOrReplaceTempView(AUDIT_VIEW)
            spark.sql(insert_sql(f"{c}.audit.event_log", AUDIT_VIEW, LOG_COLS, "event_id"))
        for u in outbox_updates(decisions, now.isoformat()):
            cur.execute(UPDATE_SQL, u)
        conn.commit()
    for d in decisions:
        print(json.dumps({k: d[k] for k in ("outbox_id", "dam_id", "decision", "status", "reason_code", "message")},
                         default=str))


if __name__ == "__main__":
    main()
