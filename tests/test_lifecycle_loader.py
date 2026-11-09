"""v1.8 loader plan (dam_safety.lifecycle_load, job dam_ref_publish task apply_lifecycle_transitions): outbox row ->
chain records -> engine -> events; deferrals, refusals, idempotence, the SCD-2 projection and the event INSERT that
never forks a chain (executed on PostgreSQL when DAM_TEST_PG is set). Synthetic data only."""
import datetime as dt
import json
import os
import re
import shutil

import pytest

from dam_safety import lifecycle as L
from dam_safety import lifecycle_load as LL
from lifecycle_fixtures import ADMIN, CDSE, CDSE2, H, PATH_TO, U, ctx, history, req

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = U("2026-11-09T20:00:00Z")


def outbox(r, oid="O1", **kw):
    row = {c: None for c in LL.OUTBOX_COLS}
    row.update({k: v for k, v in r.items() if k in row})
    row.update(outbox_id=oid, evidence_uri=json.dumps(r["evidence_uri"]), proposed_at="2026-11-09T17:00:00Z",
               approved_at="2026-11-09T18:00:00Z", proposal_record_hash=H("p" + oid),
               approval_record_hash=H("a" + oid), status="pending", created_at="2026-11-09T18:00:00Z")
    row.update(kw)
    return row


def records(row, **over):
    cv = json.dumps({"from_state": row["from_state"], "to_state": row["to_state"],
                     "citation_code": row["citation_code"], "outbox_id": row["outbox_id"]})
    p = {"record_hash": row["proposal_record_hash"], "dam_id": row["dam_id"], "subject_type": "dam_lifecycle",
         "action_type": "lifecycle_transition_propose", "reviewer_email": row["proposed_by"],
         "signed_ts": row["proposed_at"], "corrected_value": cv}
    a = dict(p, record_hash=row["approval_record_hash"], action_type="lifecycle_transition_approve",
             reviewer_email=row["approved_by"], signed_ts=row["approved_at"])
    a.update(over)
    return [p, a]


def plan(rows, recs, states, **kw):
    c, evs = ctx(states)
    c.pop("current")
    c.pop("proposed_at")
    c.pop("approved_at")
    c.update(kw)
    events = kw.pop("events", evs)
    c.pop("events", None)
    return LL.plan(rows, recs, events, now=NOW, **c), evs


def test_an_approved_transition_becomes_a_chained_event_and_a_loaded_outcome():
    row = outbox(req(L.LIVE, L.P_TRANSFER))
    out, evs = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    (d,) = out["decisions"]
    assert (d["decision"], d["status"], d["reason_code"]) == ("applied", "loaded", None)
    (e,) = out["events"]
    assert e["event_id"] == f"lc:{row['approval_record_hash']}" and e["prev_event_hash"] == evs[-1]["event_hash"]
    assert e["event_hash"] == L.event_hash(e) and L.verify_chain(evs + [e]) == {"D1": None}
    assert L.ts_text(e["effective_ts"]) == "2026-11-20T12:00:00Z"
    assert LL.outbox_updates(out["decisions"], "T") == [("loaded", None, d["message"], "T", "O1")]
    (a,) = LL.audit_rows(out["decisions"], "run-1", NOW)
    assert a["event_id"] == "lifecycle_load:O1:loaded" and a["event_type"] == "LIFECYCLE_LOAD"
    assert json.loads(a["payload"])["event_ids"] == [e["event_id"]]
    # the stored event makes a re-run idempotent
    out2, _ = plan([row], records(row), {}, events=evs + [e])
    assert out2["decisions"][0]["decision"] == "already_applied" and out2["events"] == []


def test_refusals_are_written_back_with_every_message():
    row = outbox(req(L.LIVE, L.SUSP, cdse_basis=None, dssmp_version_id=None))
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    (d,) = out["decisions"]
    assert d["status"] == "rejected" and d["reason_code"] == "lifecycle_cdse_basis_required"
    assert d["codes"] == ["lifecycle_cdse_basis_required", "lifecycle_dssmp_required"] and out["events"] == []


def test_deferred_until_the_records_are_extracted_then_rejected():
    row = outbox(req(L.LIVE, L.P_TRANSFER), approved_at="2026-11-09T19:30:00Z")
    out, _ = plan([row], [], {"D1": PATH_TO[L.LIVE]})
    assert out["decisions"][0]["status"] is None and LL.outbox_updates(out["decisions"], "T") == []
    assert LL.audit_rows(out["decisions"], "r", NOW) == []
    row = outbox(req(L.LIVE, L.P_TRANSFER))
    out, _ = plan([row], [], {"D1": PATH_TO[L.LIVE]})
    assert out["decisions"][0]["reason_code"] == "approval_record_missing"


@pytest.mark.parametrize("over,code", [
    ({"action_type": "lifecycle_transition_reject"}, "approval_record_wrong_action"),
    ({"subject_type": "dam"}, "approval_record_wrong_action"),
    ({"reviewer_email": "someone@example.org"}, "approval_record_mismatch"),
    ({"signed_ts": "2026-11-09T18:00:01Z"}, "approval_record_mismatch"),
    ({"dam_id": "D2"}, "approval_record_mismatch"),
    ({"corrected_value": json.dumps({"from_state": "live", "to_state": "pending_surrender",
                                     "citation_code": "surrender_filed_6_1"})}, "approval_record_mismatch"),
])
def test_the_outbox_row_must_match_its_signed_chain_records(over, code):
    row = outbox(req(L.LIVE, L.P_TRANSFER))
    out, _ = plan([row], records(row, **over), {"D1": PATH_TO[L.LIVE]})
    assert out["decisions"][0]["reason_code"] == code and out["events"] == []


@pytest.mark.parametrize("field,value", [("effective_ts", "soon"), ("from_state", "active"),
                                         ("approval_record_hash", "ABC"), ("withdrawal_filed_date", "13/2026"),
                                         ("notification_checklist", "{"), ("citation_code", " ")])
def test_malformed_rows_are_rejected(field, value):
    row = outbox(req(L.LIVE, L.P_TRANSFER), **{field: value})
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    assert out["decisions"][0]["reason_code"] in ("invalid_value", "missing_required_column")


def test_one_transition_per_dam_per_run_and_the_transfer_counterpart_is_busy_too():
    r1 = outbox(req(L.P_TRANSFER, L.TRANSFERRED), "O1")
    r2 = outbox(req(L.DRAFT, L.LIVE, dam="NEW"), "O2", approved_at="2026-11-09T18:00:05Z")
    out, _ = plan([r1, r2], records(r1) + records(r2), {"D1": PATH_TO[L.P_TRANSFER]})
    assert [d["decision"] for d in out["decisions"]] == ["applied", "deferred"]
    assert [e["dam_id"] for e in out["events"]] == ["D1", "NEW"]


def test_unreadable_chain_refuses_everything():
    row = outbox(req(L.LIVE, L.P_TRANSFER))
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]}, events=None)
    assert out["decisions"][0]["reason_code"] == "lifecycle_unverifiable"


def test_warnings_are_appended_to_a_loaded_message():
    row = outbox(req(L.P_SURRENDER, L.LIVE, citation_code="surrender_withdrawn_385_216",
                     withdrawal_filed_date="2026-11-12"))
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.P_SURRENDER]})
    (d,) = out["decisions"]
    assert d["status"] == "loaded" and "warning lifecycle_withdrawal_15_day" in d["message"]


def test_insert_skipped_by_a_concurrent_writer_is_deferred():
    row = outbox(req(L.LIVE, L.P_TRANSFER))
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    LL.mark_insert_skipped(out["decisions"], {})
    assert out["decisions"][0]["status"] is None and out["decisions"][0]["decision"] == "deferred_concurrent_write"
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    LL.mark_insert_skipped(out["decisions"], {e["event_id"]: e["event_hash"] for e in out["events"]})
    assert out["decisions"][0]["status"] == "loaded"


def test_projection_is_scd2_over_the_whole_history():
    evs = history("D1", [L.LIVE, L.SUSP, L.LIVE]) + history("D2", [L.VOID])
    rows = LL.project_states(evs)
    d1 = [r for r in rows if r["dam_id"] == "D1"]
    assert [r["lifecycle_state"] for r in d1] == [L.LIVE, L.SUSP, L.LIVE]
    assert [r["effective_to"] for r in d1[:-1]] == [r["effective_from"] for r in d1[1:]] and d1[-1]["effective_to"] is None
    assert [(r["dam_id"], r["lifecycle_state"]) for r in rows if r["dam_id"] == "D2"] == [("D2", L.VOID)]


def test_job_wiring():
    src = open(os.path.join(ROOT, "src", "jobs", "apply_lifecycle_transitions.py")).read()
    for s in ("plan(", "event_insert_sql(", "mark_insert_skipped(", "state_merge_sql(", "dam_mirror_sql(",
              "audit_rows(", "outbox_updates(", "WHERE outbox_id = %s AND status = 'pending'", "UNVERIFIED"):
        assert s in src, s
    assert src.index("event_insert_sql(") < src.index("mark_insert_skipped(") < src.index("outbox_updates(")


# ---------------------------------------------------------------- the SQL on PostgreSQL
PG = os.environ.get("DAM_TEST_PG")


def _pg(sql):
    return sql.replace("<=>", "IS NOT DISTINCT FROM")


@pytest.mark.skipif(not PG, reason="set DAM_TEST_PG")
def test_event_insert_is_idempotent_and_never_forks_and_the_merges_heal():
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS lc_t CASCADE; CREATE SCHEMA lc_t")
        try:
            c.execute(f"CREATE TABLE lc_t.ev ({', '.join(c_ + ' TEXT' for c_ in L.EVENT_COLS)})")
            c.execute(f"CREATE TABLE lc_t.src (LIKE lc_t.ev)")
            ins = lambda rows: [c.execute(f"INSERT INTO lc_t.src VALUES ({', '.join(['%s'] * len(L.EVENT_COLS))})",
                                          [str(r[k]) if r.get(k) is not None else None for k in L.EVENT_COLS]) for r in rows]
            evs = history("D1", [L.LIVE, L.SUSP])
            ins(evs)
            sql = LL.event_insert_sql("lc_t.ev", "lc_t.src")
            c.execute(sql)
            c.execute(sql)                                                  # idempotent
            assert c.execute("SELECT count(*) FROM lc_t.ev").fetchone()[0] == 2
            fork = dict(evs[1], event_id="E-fork", to_state=L.P_SURRENDER)  # same prev hash as a stored event
            c.execute("TRUNCATE lc_t.src")
            ins([fork])
            c.execute(sql)
            assert c.execute("SELECT count(*) FROM lc_t.ev WHERE event_id = 'E-fork'").fetchone()[0] == 0
            # SCD-2 merge + mirror
            c.execute("CREATE TABLE lc_t.st (state_id TEXT, dam_id TEXT, lifecycle_state TEXT, effective_from "
                      "TIMESTAMPTZ, effective_to TIMESTAMPTZ, event_id TEXT)")
            c.execute("CREATE TABLE lc_t.st_src (LIKE lc_t.st)")
            c.execute("CREATE TABLE lc_t.dam (dam_id TEXT, lifecycle_state TEXT, lifecycle_effective_from TIMESTAMPTZ)")
            c.execute("INSERT INTO lc_t.dam VALUES ('D1', NULL, NULL)")
            for hist in (evs[:1], evs, evs):                                 # the second pass closes the first row
                c.execute("TRUNCATE lc_t.st_src")
                for r in LL.project_states(hist):
                    c.execute("INSERT INTO lc_t.st_src VALUES (%s, %s, %s, %s, %s, %s)", [r[k] for k in LL.STATE_COLS])
                c.execute(_pg(LL.state_merge_sql("lc_t.st", "lc_t.st_src")))
                c.execute(_pg(LL.dam_mirror_sql("lc_t.dam", "lc_t.st")))
            rows = c.execute("SELECT lifecycle_state, effective_to IS NULL FROM lc_t.st ORDER BY effective_from").fetchall()
            assert rows == [(L.LIVE, False), (L.SUSP, True)]
            assert c.execute("SELECT lifecycle_state FROM lc_t.dam").fetchone()[0] == L.SUSP
        finally:
            c.execute("DROP SCHEMA IF EXISTS lc_t CASCADE")


@pytest.mark.skipif(not PG, reason="set DAM_TEST_PG")
def test_a_transfer_is_written_all_or_nothing():
    """Brief: transfer is one transaction. Both events go in ONE INSERT; if either would fork its chain (another
    writer moved the new record), NEITHER is written and the outbox row is deferred, never half a transfer."""
    import uuid

    import psycopg
    row = outbox(req(L.P_TRANSFER, L.TRANSFERRED))
    out, evs = plan([row], records(row), {"D1": PATH_TO[L.P_TRANSFER]})
    pair = out["events"]
    assert [e["dam_id"] for e in pair] == ["D1", "NEW"] and len({e["approval_record_hash"] for e in pair}) == 1
    db = "lclt_" + uuid.uuid4().hex[:8]
    with psycopg.connect(PG, autocommit=True) as root:
        root.execute(f"CREATE DATABASE {db}")
    try:
        with psycopg.connect(f"{PG} dbname={db}", autocommit=True) as c:
            c.execute(f"CREATE TABLE ev ({', '.join(x + ' TEXT' for x in L.EVENT_COLS)})")
            c.execute("CREATE TABLE src (LIKE ev)")

            def put(table, rows):
                for r in rows:
                    c.execute(f"INSERT INTO {table} VALUES ({', '.join(['%s'] * len(L.EVENT_COLS))})",
                              [str(r[k]) if r.get(k) is not None else None for k in L.EVENT_COLS])

            put("ev", evs)
            other = dict(pair[1], event_id="someone-else", approval_record_hash="f" * 64)   # NEW moved meanwhile
            put("ev", [other])
            put("src", pair)
            sql = LL.event_insert_sql("ev", "src")
            c.execute(sql)
            got = {r[0]: r[1] for r in c.execute("SELECT event_id, event_hash FROM ev").fetchall()}
            assert not any(e["event_id"] in got for e in pair)                  # neither half
            LL.mark_insert_skipped(out["decisions"], got)
            assert out["decisions"][0]["status"] is None
            c.execute("DELETE FROM ev WHERE event_id = 'someone-else'")
            c.execute(sql)
            c.execute(sql)                                                      # idempotent
            got = {r[0]: r[1] for r in c.execute("SELECT event_id, event_hash FROM ev").fetchall()}
            assert all(got.get(e["event_id"]) == e["event_hash"] for e in pair) and len(got) == len(evs) + 2
    finally:
        with psycopg.connect(PG, autocommit=True) as root:
            root.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


# ---------------------------------------------------------------- v1.8.003: safety flag, 'other', voided dams
def _drawdown(flag="absent"):
    r = req(L.LIVE, L.SUSP)
    row = outbox(r)
    if flag != "absent":
        row["safety_related_ops_event"] = flag
    return row


def test_safety_flag_from_the_outbox_null_is_false_true_derives_the_cite_and_warns_12_10():
    for flag in (None, False, "absent"):
        row = _drawdown(flag)
        out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
        (d,), (e,) = out["decisions"], out["events"]
        assert d["status"] == "loaded" and "lifecycle_12_10_review" not in d["message"], flag
        assert e["safety_related_ops_event"] is False and e["derived_citation"] is None
    row = _drawdown(True)
    out, evs = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    (d,), (e,) = out["decisions"], out["events"]
    assert d["status"] == "loaded" and "warning lifecycle_12_10_review" in d["message"] and "12.10" in d["message"]
    assert e["safety_related_ops_event"] is True and e["derived_citation"] == "12.12(a)(1)(iii)(A)"
    assert L.verify_chain(evs + [e]) == {"D1": None}
    assert set(L.EXT_HASH_FIELDS) <= set(e) and LL.OUTBOX_COLS[-1] == "safety_related_ops_event"
    row = _drawdown("true")
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    assert out["decisions"][0]["message"] == "invalid_value: safety_related_ops_event must be a boolean"


def test_other_from_the_outbox_with_empty_evidence_and_the_cdse_basis():
    r = req(L.LIVE, L.SUSP, suspension_reason="other", citation_code="suspend_other_internal", evidence_uri=[],
            cdse_basis="Unplanned survey after a felt earthquake nearby", cdse_basis_by=CDSE)
    row = outbox(r)
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    assert out["decisions"][0]["status"] == "loaded" and out["events"][0]["evidence_uri"] == []
    row = outbox(dict(r, cdse_basis="too short"))
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.LIVE]})
    assert out["decisions"][0]["reason_code"] == "lifecycle_rationale_required" and out["events"] == []


def test_a_voided_dam_refuses_every_transition_with_dam_archived():
    row = outbox(req(L.VOID, L.LIVE, citation_code="go_live_dssmp_12_12"))
    out, _ = plan([row], records(row), {"D1": PATH_TO[L.VOID]})
    (d,) = out["decisions"]
    assert (d["status"], d["reason_code"]) == ("rejected", "dam_archived")
    assert d["message"] == "dam_archived: dam D1 lifecycle_state=voided_in_error (read-only)"
