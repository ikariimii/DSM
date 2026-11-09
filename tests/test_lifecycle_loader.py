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
