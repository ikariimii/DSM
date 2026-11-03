"""Job dam_lakebase_extract: incremental copy of app-entered data from Lakebase (Postgres) to UC.

* manual_reading -> CSV file in the landing volume (/Volumes/<cat>/raw/landing/<dam>/readings/),
  so manual readings take exactly the same Auto Loader -> LDP -> reduce path as ADAS data
  (one validation path; KB rec 'single ingestion path').
* review_signoff (insert-only, column-compatible) -> audit.review_record (v1.5, QA-039): one UC row per sign-off,
  deduplicated on review_id, signature_hash copied unchanged (the reduction job re-verifies it), source
  'APP:lakebase:review_signoff'. This is the approval the reduction job requires for an applied correction.
* Other immutable app tables (exceedance_status, audit_log, alarm_comment, pfm_review, inspection_note)
  -> audit.app_extract (generic insert-only envelope: table_name, pk, payload JSON).
* alarm_ack (app lifecycle open -> acknowledged -> closed, mutable, PK event_id) -> audit.event_log
  event_type='ALARM_ACK' (subject_id = RAISE transition_id = ops.alarm_event.event_id). The alarm
  evaluator turns each into exactly one ACK transition (dedup tag lakebase_ack:<event_id>).
  'closed' is app-only workflow state and is written as event_type='NOTE' (no machine effect).
  OWNERSHIP (SCHEMA_CONTRACT v1.1 §7.2): the app writes Lakebase; this job is the only writer of
  ALARM_ACK to UC; the alarm evaluator is the only writer of ACK transitions.
* report_draft (mutable) -> rpt.dssmr_draft (append-only snapshot per content change, sha256) and
  rpt.dssmr upsert with status='draft' ONLY while the UC row is still a draft. certified/submitted are
  set by the PE-sign-off path (audit.review_record), never by the app.
Watermarks live in Lakebase extract_watermark (id-based; tables are BIGSERIAL or text-PK + ts).

Auth: OAuth token from w.postgres.generate_database_credential(endpoint=...) (1 h lifetime),
psycopg, dbname databricks_postgres, sslmode=require.  UNVERIFIED on a live workspace (no CLI/
workspace available while building) - see docs/VERIFICATION.md LAKEBASE-AUTH."""
import csv
import datetime as dt
import io
import json

try:
    from . import _common  # noqa: F401
except ImportError:        # executed as a script by spark_python_task
    import _common         # noqa: F401

MANUAL_COLUMNS = ["dam_id", "logger_id", "sensor_id", "ts_utc", "raw_value", "raw_unit", "temp_raw", "temp_c",
                  "baro_kpa", "reading_type", "source", "reservoir_el_m", "datum_id"]
# (table, id column, ordering column) - id column must be monotonically increasing or a sortable ts
IMMUTABLE = [("exceedance_status", "id", "id"),
             ("audit_log", "id", "id"), ("alarm_comment", "id", "id"), ("pfm_review", "id", "id"),
             ("inspection_note", "id", "id")]


def manual_rows_to_csv(rows):
    """rows: dicts from manual_reading. Returns {dam_id: csv_text} in the bronze READING_SCHEMA layout."""
    out = {}
    for r in rows:
        buf = out.setdefault(r["dam_id"], io.StringIO())
        w = csv.DictWriter(buf, fieldnames=MANUAL_COLUMNS, lineterminator="\n")
        if buf.tell() == 0:
            w.writeheader()
        w.writerow({"dam_id": r["dam_id"], "logger_id": "MANUAL", "sensor_id": r["sensor_id"], "ts_utc": r["ts_utc"],
                    "raw_value": r["raw_value"], "raw_unit": r["raw_unit"], "temp_raw": None,
                    "temp_c": r.get("temp_c"), "baro_kpa": None, "reading_type": "MANUAL",
                    "source": f"lakebase:manual_reading:{r['id']}:{r['entered_by']}",
                    "reservoir_el_m": r.get("reservoir_el_m"), "datum_id": r.get("datum_id")})
    return {k: v.getvalue() for k, v in out.items()}


REVIEW_RECORD_COLS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                      "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash", "source")
REVIEW_SOURCE = "APP:lakebase:review_signoff"


def _utc(ts):
    t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t.astimezone(dt.timezone.utc)


def review_rows_to_records(rows, known_ids=()):
    """Lakebase review_signoff rows -> audit.review_record rows (v1.5, QA-039). Skips review_ids already in UC
    (at-least-once re-extract) and duplicates within the batch. signed_ts text -> UTC TIMESTAMP: the signature's
    canonical form normalises timestamps, so the hash still verifies (tests/test_review_signature.py). A row missing
    a NOT NULL column raises instead of being dropped silently."""
    seen, out = set(known_ids), []
    for r in rows:
        missing = [c for c in REVIEW_RECORD_COLS if c not in ("comment", "reviewer_pe_license", "source")
                   and r.get(c) in (None, "")]
        if missing:
            raise ValueError(f"review_signoff {r.get('review_id')}: missing {missing}")
        if r["review_id"] in seen:
            continue
        seen.add(r["review_id"])
        rec = {c: r.get(c) for c in REVIEW_RECORD_COLS}
        rec["signed_ts"] = _utc(r["signed_ts"])
        rec["source"] = REVIEW_SOURCE
        out.append(rec)
    return out


def envelope(table, pk_col, rows, extract_ts):
    return [{"table_name": table, "pk": str(r[pk_col]), "dam_id": r.get("dam_id"),
             "payload": json.dumps(r, default=str, sort_keys=True), "extract_ts": extract_ts} for r in rows]


def _sha(*parts):
    import hashlib
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def ack_rows_to_events(rows, recorded_ts):
    """alarm_ack rows (acknowledged_at > watermark) -> audit.event_log rows. Deterministic event_id,
    so a re-extract after a failed run yields the same ids (and the evaluator dedups on subject_id)."""
    out = []
    for r in rows:
        if r.get("acknowledged_at") and r.get("acknowledged_by") and r["state"] in ("acknowledged", "closed"):
            out.append({"event_id": _sha("ALARM_ACK", r["event_id"], r["acknowledged_at"]), "dam_id": r["dam_id"],
                        "event_type": "ALARM_ACK", "subject_type": "alarm_transition", "subject_id": r["event_id"],
                        "actor": r["acknowledged_by"], "event_ts": dt.datetime.fromisoformat(r["acknowledged_at"]),
                        "payload": json.dumps({"source": "lakebase:alarm_ack", "state": r["state"]}, sort_keys=True),
                        "recorded_ts": recorded_ts})
    return out


def close_rows_to_events(rows, recorded_ts):
    return [{"event_id": _sha("ALARM_CLOSE", r["event_id"], r["closed_at"]), "dam_id": r["dam_id"], "event_type": "NOTE",
             "subject_type": "alarm_transition", "subject_id": r["event_id"], "actor": r["closed_by"] or "unknown",
             "event_ts": dt.datetime.fromisoformat(r["closed_at"]),
             "payload": json.dumps({"source": "lakebase:alarm_ack", "app_state": "closed", "close_reason": r.get("close_reason")},
                                   sort_keys=True), "recorded_ts": recorded_ts}
            for r in rows if r.get("state") == "closed" and r.get("closed_at")]


def draft_snapshots(rows, known_hashes, extract_ts):
    """report_draft rows -> new rpt.dssmr_draft snapshot rows (only when content changed)."""
    out = []
    for r in rows:
        h = _sha(r["report_id"], r["status"], r["period_start"], r["period_end"], r["content_md"])
        if (r["report_id"], h) in known_hashes:
            continue
        out.append({"report_id": r["report_id"], "dam_id": r["dam_id"], "period_start": r["period_start"],
                    "period_end": r["period_end"], "app_status": r["status"], "content_md": r["content_md"],
                    "content_sha256": h, "created_by": r["created_by"], "created_at": r["created_at"],
                    "extract_ts": extract_ts})
    return out


def next_watermark(rows, col, current):
    vals = [r[col] for r in rows if r.get(col) is not None]
    return max(vals) if vals else current


def _connect(project, branch, endpoint):
    import psycopg
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    name = f"projects/{project}/branches/{branch}/endpoints/{endpoint}"
    token = w.postgres.generate_database_credential(endpoint=name).token
    host = w.postgres.get_endpoint(name=name).status.hosts.host    # UNVERIFIED attribute path
    user = w.current_user.me().user_name
    return psycopg.connect(host=host, dbname="databricks_postgres", user=user, password=token, sslmode="require")


def main():
    a = _common.args(("lb_project", "dam-safety"), ("lb_branch", "production"), ("lb_endpoint", "primary"))
    spark = _common.spark()
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    from psycopg.rows import dict_row
    with _connect(a.lb_project, a.lb_branch, a.lb_endpoint) as conn, conn.cursor(row_factory=dict_row) as cur:
        def wm(t):
            cur.execute("SELECT last_id, last_ts FROM extract_watermark WHERE table_name=%s", (t,))
            r = cur.fetchone()
            return r or {"last_id": 0, "last_ts": ""}

        def save(t, last_id=None, last_ts=None):
            cur.execute("INSERT INTO extract_watermark(table_name,last_id,last_ts,updated_at) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (table_name) DO UPDATE SET last_id=EXCLUDED.last_id, last_ts=EXCLUDED.last_ts, "
                        "updated_at=EXCLUDED.updated_at", (t, last_id, last_ts, now.isoformat()))

        w0 = wm("manual_reading")
        cur.execute("SELECT * FROM manual_reading WHERE id > %s ORDER BY id", (w0["last_id"] or 0,))
        rows = cur.fetchall()
        for dam, text in manual_rows_to_csv(rows).items():
            path = f"/Volumes/{a.catalog}/raw/landing/{dam}/readings/manual_{stamp}.csv"
            with open(path, "w", encoding="utf-8") as f:   # UC volume FUSE path on serverless
                f.write(text)
        # watermark only after the files are written (at-least-once; bronze dedups on sensor/ts/file)
        save("manual_reading", last_id=next_watermark(rows, "id", w0["last_id"]))

        # review_signoff -> audit.review_record (>= watermark: app timestamps are second precision; dedup on review_id)
        wr = wm("review_signoff")
        cur.execute("SELECT * FROM review_signoff WHERE signed_ts >= %s ORDER BY signed_ts, review_id",
                    (wr["last_ts"] or "",))
        rs = cur.fetchall()
        if rs:
            rr = spark.table(f"{a.catalog}.audit.review_record")
            known = {x.review_id for x in rr.where(rr.review_id.isin([r["review_id"] for r in rs]))
                     .select("review_id").collect()}
            recs = review_rows_to_records(rs, known)
            if recs:
                spark.createDataFrame([tuple(r[f.name] for f in rr.schema.fields) for r in recs], rr.schema) \
                    .write.mode("append").saveAsTable(f"{a.catalog}.audit.review_record")
        save("review_signoff", last_ts=next_watermark(rs, "signed_ts", wr["last_ts"]))

        out = []
        for table, pk, order in IMMUTABLE:
            w1 = wm(table)
            if order == "id":
                cur.execute(f"SELECT * FROM {table} WHERE id > %s ORDER BY id", (w1["last_id"] or 0,))
            else:
                cur.execute(f"SELECT * FROM {table} WHERE {order} > %s ORDER BY {order}", (w1["last_ts"] or "",))
            rs = cur.fetchall()
            out += envelope(table, pk, rs, now)
            if order == "id":
                save(table, last_id=next_watermark(rs, "id", w1["last_id"]))
            else:
                save(table, last_ts=next_watermark(rs, order, w1["last_ts"]))
        if out:
            spark.createDataFrame(out).write.mode("append").saveAsTable(f"{a.catalog}.audit.app_extract")

        # alarm_ack -> audit.event_log (ALARM_ACK / closed NOTE). App writes second-precision ISO text (state.now_iso), so >= watermark + dedup on the deterministic event_id.
        wa, wc = wm("alarm_ack"), wm("alarm_ack_closed")
        cur.execute("SELECT * FROM alarm_ack WHERE acknowledged_at >= %s ORDER BY acknowledged_at", (wa["last_ts"] or "",))
        acked = cur.fetchall()
        cur.execute("SELECT * FROM alarm_ack WHERE closed_at >= %s ORDER BY closed_at", (wc["last_ts"] or "",))
        closed = cur.fetchall()
        ev = ack_rows_to_events(acked, now) + close_rows_to_events(closed, now)
        if ev:
            el = spark.table(f"{a.catalog}.audit.event_log")
            ids = {r.event_id for r in el.where(el.event_id.isin([e["event_id"] for e in ev])).select("event_id").collect()}
            new_ev = [tuple(e[f.name] for f in el.schema.fields) for e in ev if e["event_id"] not in ids]
            if new_ev:
                spark.createDataFrame(new_ev, el.schema).write.mode("append").saveAsTable(f"{a.catalog}.audit.event_log")
        save("alarm_ack", last_ts=next_watermark(acked, "acknowledged_at", wa["last_ts"]))
        save("alarm_ack_closed", last_ts=next_watermark(closed, "closed_at", wc["last_ts"]))

        # report_draft -> rpt.dssmr_draft (append-only snapshots) + rpt.dssmr draft upsert
        cur.execute("SELECT * FROM report_draft")
        drafts = cur.fetchall()
        known = {(r.report_id, r.content_sha256) for r in
                 spark.table(f"{a.catalog}.rpt.dssmr_draft").select("report_id", "content_sha256").collect()}
        snaps = draft_snapshots(drafts, known, now)
        if snaps:
            sch = spark.table(f"{a.catalog}.rpt.dssmr_draft").schema
            spark.createDataFrame([tuple(s_[f.name] for f in sch.fields) for s_ in snaps], sch) \
                .write.mode("append").saveAsTable(f"{a.catalog}.rpt.dssmr_draft")
            spark.sql(f"""
              MERGE INTO {a.catalog}.rpt.dssmr t
              USING (SELECT d.report_id, d.dam_id, CAST(d.period_start AS DATE) AS period_start,
                            CAST(d.period_end AS DATE) AS period_end, v.dssmp_version_id, d.created_by
                     FROM {a.catalog}.rpt.v_dssmr_draft_current d
                     JOIN {a.catalog}.ref.dssmp_version v
                       ON v.dam_id = d.dam_id AND v.status = 'approved' AND v.effective_to IS NULL) s
              ON t.dssmr_id = s.report_id
              WHEN MATCHED AND t.status = 'draft' THEN UPDATE SET t.period_start = s.period_start,
                   t.period_end = s.period_end, t.dssmp_version_id = s.dssmp_version_id
              WHEN NOT MATCHED THEN INSERT (dssmr_id, dam_id, period_start, period_end, dssmp_version_id, preparer_id, status)
                   VALUES (s.report_id, s.dam_id, s.period_start, s.period_end, s.dssmp_version_id, s.created_by, 'draft')""")
        conn.commit()


if __name__ == "__main__":
    main()
