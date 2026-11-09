"""Job dam_lakebase_extract: incremental copy of app-entered data from Lakebase (Postgres) to UC.

* manual_reading -> CSV file in the landing volume (/Volumes/<cat>/raw/landing/<dam>/readings/),
  so manual readings take exactly the same Auto Loader -> LDP -> reduce path as ADAS data
  (one validation path; KB rec 'single ingestion path').
* review_signoff (insert-only, column-compatible) -> audit.review_record (v1.5, QA-039; v1.6 all 24 columns incl. the
  per-dam hash chain, in chain_id, chain_seq order): one UC row per sign-off,
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
import re

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


# v1.6 (QA-039 v2, app SCHEMA_CONTRACT §7 10b): the 12 per-dam hash-chain columns, so the UC copy can be re-verified
# (dam_safety.audit_chain.verify_records). 12 -> 24 columns, same order as sql/ddl/08_audit.sql.
CHAIN_COLS = ("action_type", "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id",
              "chain_seq", "prev_hash", "record_hash", "signature", "sig_alg", "key_id")
REVIEW_RECORD_COLS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                      "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash", "source", *CHAIN_COLS)
# NOT NULL on extract (mirrors the DDL NOT NULLs and ck_review_record_chain). Nullable: comment (the CHECKs require it
# for incl_correction / alarm_event), reviewer_pe_license, source (set here), reviewer_email, reviewer_sub,
# signature, sig_alg, key_id.
REVIEW_REQUIRED = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "reviewer_id",
                   "signed_ts", "signature_hash", "action_type", "original_value", "corrected_value", "chain_id",
                   "chain_seq", "prev_hash", "record_hash")
REVIEW_SOURCE = "APP:lakebase:review_signoff"
REVIEW_ORDER_SQL = "ORDER BY chain_id, chain_seq"


def _utc(ts):
    t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t.astimezone(dt.timezone.utc)


CDSE_LEVEL = "CDSE"
LEGACY_FLAG = "review_level_legacy_unverified"


def _ts_or_none(v):
    return None if v in (None, "") else _utc(v)


def review_level_legacy_unverified(rec, authority) -> bool:
    """v1.6 ROLE-LEVEL (Genie-confirmed): a record at review_level 'CDSE' is verified only if its reviewer_id or
    reviewer_email (case-insensitive) is a ref.v_dam_reviewer_authority principal for that dam at signed_ts: the dam's
    cdse_id (no history: current value) or a CDSE membership with granted_ts <= signed_ts < revoked_ts. Same rule as
    gold.v_review_level_check. authority: iterable of dicts with dam_id, principal, granted_ts, revoked_ts. Derived
    only: the stored record is never changed (audit.review_record is append-only and hash-chained)."""
    if str(rec.get("review_level") or "").upper() != CDSE_LEVEL:
        return False
    who = {str(x).strip().lower() for x in (rec.get("reviewer_id"), rec.get("reviewer_email")) if x and str(x).strip()}
    at = _utc(rec["signed_ts"])
    for a in authority:
        if a.get("dam_id") != rec.get("dam_id") or str(a.get("principal") or "").strip().lower() not in who:
            continue
        g, v = _ts_or_none(a.get("granted_ts")), _ts_or_none(a.get("revoked_ts"))
        if (g is None or g <= at) and (v is None or v > at):
            return False
    return True


def review_rows_to_records(rows, known_ids=(), authority=None):
    """Lakebase review_signoff rows -> audit.review_record rows (v1.5 QA-039, v1.6 chain columns). Skips review_ids
    already in UC (at-least-once re-extract) and duplicates within the batch. signed_ts text -> UTC TIMESTAMP: the
    canonical form normalises timestamps, so signature_hash and record_hash still verify (tests/test_review_signature.py,
    tests/test_audit_chain.py). A row missing a REVIEW_REQUIRED column raises instead of being dropped silently.
    Output is in chain order (chain_id, chain_seq). No approval rule runs here: corrections.approval_problem is for
    incl_correction rows only and is applied by the reduction job, never to alarm_event rows. With `authority`
    (ref.v_dam_reviewer_authority rows) each record also gets the derived key review_level_legacy_unverified."""
    seen, out = set(known_ids), []
    for r in rows:
        missing = [c for c in REVIEW_REQUIRED if r.get(c) in (None, "")]
        if missing:
            raise ValueError(f"review_signoff {r.get('review_id')}: missing {missing}")
        if r["review_id"] in seen:
            continue
        seen.add(r["review_id"])
        rec = {c: r.get(c) for c in REVIEW_RECORD_COLS}
        rec["signed_ts"] = _utc(r["signed_ts"])
        rec["chain_seq"] = int(r["chain_seq"])
        rec["source"] = REVIEW_SOURCE
        if authority is not None:  # derived key, not an audit.review_record column (the write selects schema fields)
            rec[LEGACY_FLAG] = review_level_legacy_unverified(rec, authority)
        out.append(rec)
    out.sort(key=lambda x: (str(x["chain_id"]), x["chain_seq"]))
    return out


def envelope(table, pk_col, rows, extract_ts):
    return [{"table_name": table, "pk": str(r[pk_col]), "dam_id": r.get("dam_id"),
             "payload": json.dumps(r, default=str, sort_keys=True), "extract_ts": extract_ts} for r in rows]


def _sha(*parts):
    import hashlib
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def ack_rows_to_events(rows, recorded_ts, episodes=None):
    """alarm_ack rows (acknowledged_at > watermark) -> audit.event_log rows. Deterministic event_id,
    so a re-extract after a failed run yields the same ids (and the evaluator dedups on subject_id).
    v1.7: with `episodes` ({event_id: ops.alarm_episode row}) the payload also carries the episode's
    cdse_ack_required_at_open (snapshot at open, binding) and cdse_ack_required (current rule flag), so the audit
    trail shows whether a CDSE acknowledgement was required. Recorded only; enforcement is the app's."""
    out = []
    for r in rows:
        if r.get("acknowledged_at") and r.get("acknowledged_by") and r["state"] in ("acknowledged", "closed"):
            payload = {"source": "lakebase:alarm_ack", "state": r["state"]}
            if episodes is not None:
                ep = episodes.get(r["event_id"]) or {}
                payload.update(cdse_ack_required_at_open=ep.get("cdse_ack_required_at_open"),
                               cdse_ack_required=ep.get("cdse_ack_required"))
            out.append({"event_id": _sha("ALARM_ACK", r["event_id"], r["acknowledged_at"]), "dam_id": r["dam_id"],
                        "event_type": "ALARM_ACK", "subject_type": "alarm_transition", "subject_id": r["event_id"],
                        "actor": r["acknowledged_by"], "event_ts": dt.datetime.fromisoformat(r["acknowledged_at"]),
                        "payload": json.dumps(payload, sort_keys=True), "recorded_ts": recorded_ts})
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


# QA-045d: the app's Lakebase review_chain_head -> append-only audit.chain_head (one row per chain whose head changed).
CHAIN_HEAD_SOURCE = "APP:lakebase:review_chain_head"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def chain_head_rows(lb_heads, uc_latest, computed_utc, job_run_id):
    """lb_heads: Lakebase review_chain_head rows {chain_id, chain_seq, head_hash}, read in the SAME REPEATABLE READ
    snapshot as the review_signoff rows of this run. uc_latest: {chain_id: (chain_seq, head_hash)} of the latest
    audit.chain_head rows. Returns the audit.chain_head rows to append: new chains and changed heads only (an
    unchanged head is not re-written). A head that moved BACKWARDS is still written (it is evidence; the verifier
    reports it as a rewound head). A malformed head raises (never stored)."""
    out = []
    for h in sorted(lb_heads, key=lambda h: h["chain_id"]):
        seq, hh = int(h["chain_seq"]), str(h["head_hash"])
        if seq < 0 or not _HEX64.match(hh):
            raise ValueError(f"malformed Lakebase chain head for {h['chain_id']}: seq={seq} hash={hh[:16]}...")
        if uc_latest.get(h["chain_id"]) == (seq, hh):
            continue
        out.append({"dam_id": h["chain_id"], "chain_id": h["chain_id"], "chain_seq": seq, "head_hash": hh,
                    "computed_utc": computed_utc, "job_run_id": job_run_id, "source": CHAIN_HEAD_SOURCE})
    return out


def _connect(project, branch, endpoint):
    import psycopg
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    name = f"projects/{project}/branches/{branch}/endpoints/{endpoint}"
    token = w.postgres.generate_database_credential(endpoint=name).token
    host = w.postgres.get_endpoint(name=name).status.hosts.host    # UNVERIFIED attribute path
    user = w.current_user.me().user_name
    conn = psycopg.connect(host=host, dbname="databricks_postgres", user=user, password=token, sslmode="require")
    # QA-045d: one snapshot for the whole run, so review_chain_head and review_signoff are read consistently (the app
    # advances the head in the same transaction as the INSERT).
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    return conn


def main():
    a = _common.args(("lb_project", "dam-safety"), ("lb_branch", "production"), ("lb_endpoint", "primary"),
                     ("job_run_id", None))
    a.job_run_id = a.job_run_id or "manual-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
        cur.execute("SELECT * FROM review_signoff WHERE signed_ts >= %s " + REVIEW_ORDER_SQL,
                    (wr["last_ts"] or "",))
        rs = cur.fetchall()
        if rs:
            rr = spark.table(f"{a.catalog}.audit.review_record")
            known = {x.review_id for x in rr.where(rr.review_id.isin([r["review_id"] for r in rs]))
                     .select("review_id").collect()}
            auth = [r.asDict() for r in spark.table(f"{a.catalog}.ref.v_dam_reviewer_authority")
                    .select("dam_id", "principal", "granted_ts", "revoked_ts").collect()]
            recs = review_rows_to_records(rs, known, authority=auth)
            legacy = [r["review_id"] for r in recs if r[LEGACY_FLAG]]
            if legacy:  # kept unchanged (append-only); surfaced here and in gold.v_review_level_check
                print(f"WARN {len(legacy)} review_signoff record(s) at review_level CDSE by a reviewer who was not the "
                      f"dam's CDSE at signed_ts ({LEGACY_FLAG}): {legacy[:20]}")
            if recs:
                spark.createDataFrame([tuple(r[f.name] for f in rr.schema.fields) for r in recs], rr.schema) \
                    .write.mode("append").saveAsTable(f"{a.catalog}.audit.review_record")
        save("review_signoff", last_ts=next_watermark(rs, "signed_ts", wr["last_ts"]))
        # QA-045d: chain heads, appended AFTER the records (a failure in between leaves the head behind, which the
        # verifier reports until the next run; it never lets a head get ahead of its records)
        cur.execute("SELECT chain_id, chain_seq, head_hash FROM review_chain_head ORDER BY chain_id")
        ch_tbl = f"{a.catalog}.audit.chain_head"
        latest = {}
        for x in spark.sql(f"SELECT chain_id, chain_seq, head_hash FROM (SELECT *, row_number() OVER (PARTITION BY "
                           f"dam_id, chain_id ORDER BY computed_utc DESC, chain_seq DESC, head_hash DESC) AS rn "
                           f"FROM {ch_tbl}) WHERE rn = 1").collect():
            latest[x.chain_id] = (int(x.chain_seq), x.head_hash)
        heads = chain_head_rows(cur.fetchall(), latest, now, a.job_run_id)
        for h in heads:
            if h["chain_id"] in latest and h["chain_seq"] < latest[h["chain_id"]][0]:
                print(f"WARN chain head of {h['chain_id']} moved backwards in Lakebase "
                      f"({latest[h['chain_id']][0]} -> {h['chain_seq']}); recorded for the verifier")
        if heads:
            sch = spark.table(ch_tbl).schema
            spark.createDataFrame([tuple(h[f.name] for f in sch.fields) for h in heads], sch) \
                .write.mode("append").saveAsTable(ch_tbl)

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
        eps = {}
        if acked:  # v1.7: CDSE-ack flags of the acknowledged episodes (snapshot + current) into the ALARM_ACK payload
            ep_df = spark.table(f"{a.catalog}.ops.alarm_episode")
            eps = {x.event_id: x.asDict() for x in ep_df.where(ep_df.event_id.isin([r["event_id"] for r in acked]))
                   .select("event_id", "cdse_ack_required_at_open", "cdse_ack_required").collect()}
        ev = ack_rows_to_events(acked, now, episodes=eps) + close_rows_to_events(closed, now)
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
