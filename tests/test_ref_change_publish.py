"""v1.7 publisher for the app outbox ref_change_publication (dam_safety.ref_change_publish, job dam_ref_publish):
validation with reason codes, stale rejection, idempotent re-run, the single conditional MERGE (executed on a real
PostgreSQL >= 15 when DAM_TEST_PG is set: same MERGE text; Delta itself UNVERIFIED), and an app-generated outbox row.
Synthetic data only."""
import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import ref_change_publish as rp  # noqa: E402
import publish_ref_changes as job  # noqa: E402

D = "SYN-DAM-01"
DSSMP = {"DSSMP-1": {"dam_id": D, "status": "approved", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None},
         "DSSMP-OLD": {"dam_id": D, "status": "superseded", "effective_from": "2025-01-01T00:00:00Z",
                       "effective_to": "2026-01-01T00:00:00Z"},
         "DSSMP-DRAFT": {"dam_id": D, "status": "draft", "effective_from": "2026-12-01T00:00:00Z", "effective_to": None},
         "DSSMP-FUT": {"dam_id": D, "status": "approved", "effective_from": "2027-01-01T00:00:00Z", "effective_to": None},
         "DSSMP-ODD": {"dam_id": D, "status": "pending_review", "effective_from": "2026-01-01T00:00:00Z",
                       "effective_to": None},
         "DSSMP-OTHER": {"dam_id": "SYN-DAM-02", "status": "approved", "effective_from": "2026-01-01T00:00:00Z",
                         "effective_to": None}}
V1 = {"threshold_id": "T-1", "threshold_version": 1, "dam_id": D, "instrument_id": "I-1", "sensor_id": None,
      "pfm_id": "PFM-1", "dssmp_version_id": "DSSMP-1", "tier": "AL1", "level": 1, "level_name": "ALERT",
      "limit_type": "magnitude", "direction": "upper", "value": 10.0, "unit": "m", "rate_window": None,
      "deadband": 0.2, "persistence_on": 2, "persistence_off": 3, "condition_expr": None, "basis_text": None,
      "required_actions": "notify", "eap_level_id": None, "set_by": "seed", "effective_from": "2026-02-01T00:00:00Z",
      "effective_to": None, "is_current": True, "cdse_ack_required": False}


def outbox(pub_id="P-1", sup=1, requested_at="2026-11-09T16:00:00Z", **ins_kw):
    ins = {**V1, "threshold_version": sup + 1, "cdse_ack_required": True, "effective_from": "2026-11-09T16:00:00+00:00",
           "set_by": "cdse@example.org", **ins_kw}
    ins = {k: v for k, v in ins.items() if v is not ...}
    return {"pub_id": pub_id, "kind": rp.KIND, "dam_id": D, "proposal_id": "RFP-1", "requested_by": "cdse@example.org",
            "requested_at": requested_at, "status": "pending",
            "payload": json.dumps({"kind": rp.KIND, "threshold_id": "T-1", "dam_id": D, "supersedes_version": sup,
                                   "insert": ins})}


def one(row, current=(V1,), dssmp=DSSMP):
    [d] = rp.plan([row], list(current), dssmp)
    return d


# ---------------------------------------------------------------- contract
def _ddl_cols():
    ddl = open(os.path.join(ROOT, "sql", "ddl", "02_ref_calibration_threshold.sql")).read()
    body = re.search(r"CREATE TABLE IF NOT EXISTS ref\.threshold \((.*?)\n\) TBLPROPERTIES", ddl, re.S).group(1)
    cols = [(m.group(1), "NOT NULL" in l) for l in body.splitlines()
            if (m := re.match(r"\s+(\w+)\s+\w+", l)) and m.group(1) != "CONSTRAINT"]
    return cols


def test_columns_and_not_nulls_match_the_ddl_and_the_app():
    cols = _ddl_cols()
    assert tuple(c for c, _ in cols) == rp.THRESHOLD_COLS
    assert set(rp.NOT_NULL) == {c for c, nn in cols if nn} - {"cdse_ack_required"}
    app = open(os.path.join(ROOT, "app", "backend", "onboarding.py")).read()
    assert 'PUBLISH_REQUIRED = ("dssmp_version_id", "tier", "deadband", "persistence_on", "persistence_off")' in app
    assert rp.PUBLISH_REQUIRED == ("dssmp_version_id", "tier", "deadband", "persistence_on", "persistence_off")
    assert rp.TIER_RE.pattern in app


# ---------------------------------------------------------------- plan
def test_valid_row_is_applied_with_close_of_n_and_insert_of_n_plus_1():
    d = one(outbox())
    assert d["decision"] == d["status"] == "applied" and d["codes"] == []
    assert d["close"] == {"threshold_id": "T-1", "threshold_version": 1, "is_current": False,
                          "effective_to": dt.datetime(2026, 11, 9, 16, tzinfo=dt.timezone.utc)}
    assert d["insert"]["threshold_version"] == 2 and d["insert"]["cdse_ack_required"] is True
    assert d["insert"]["effective_to"] is None and d["insert"]["is_current"] is True


@pytest.mark.parametrize("col", rp.PUBLISH_REQUIRED)
@pytest.mark.parametrize("empty", [..., None, "", "  "])
def test_each_missing_required_column_is_rejected_with_its_name(col, empty):
    d = one(outbox(**{col: empty}))
    assert d["status"] == "rejected_invalid" and "missing_required_column" in d["codes"] and col in d["message"]
    assert d["insert"] is None


@pytest.mark.parametrize("col,val", [("tier", "L1"), ("tier", "al1"), ("deadband", -1), ("deadband", "x"),
                                     ("persistence_on", 0), ("persistence_off", 1.5), ("persistence_on", True),
                                     ("cdse_ack_required", "true"), ("level", "one"), ("direction", "sideways"),
                                     ("is_current", False), ("effective_to", "2027-01-01T00:00:00Z")])
def test_invalid_values_are_rejected(col, val):
    d = one(outbox(**{col: val}))
    assert d["status"] == "rejected_invalid" and "invalid_value" in d["codes"], d


@pytest.mark.parametrize("dv,code", [("DSSMP-NOPE", "dssmp_not_found"), ("DSSMP-OTHER", "dssmp_other_dam"),
                                     ("DSSMP-DRAFT", "dssmp_draft"), ("DSSMP-OLD", "dssmp_superseded"),
                                     ("DSSMP-FUT", "dssmp_not_yet_effective"), ("DSSMP-ODD", "dssmp_status_unknown")])
def test_dssmp_version_must_exist_be_the_dams_approved_and_in_effect(dv, code):
    d = one(outbox(dssmp_version_id=dv))
    assert d["status"] == "rejected_invalid" and d["codes"] == [code], d


def test_payload_shape_kind_and_version_problems():
    r = outbox()
    r["kind"] = "ref.pfm.new_version"
    assert "unknown_kind" in one(r)["codes"]
    assert "payload_invalid" in one(outbox(threshold_version=5))["codes"]          # not N+1
    assert "payload_invalid" in one(outbox(dam_id="SYN-DAM-02"))["codes"]          # dam mismatch
    assert "unknown_columns" in one(outbox(output_name="x"))["codes"]
    bad = outbox()
    bad["payload"] = "{not json"
    assert one(bad)["status"] == "rejected_invalid"


def test_stale_rows_are_rejected_and_nothing_is_planned():
    cur = {**V1, "threshold_version": 2, "effective_from": "2026-10-01T00:00:00Z", "set_by": "someone else"}
    d = one(outbox(sup=1), current=[cur])
    assert d["decision"] == d["status"] == "rejected_stale" and d["insert"] is None and "v2" in d["message"]
    assert one(outbox(), current=[])["status"] == "rejected_stale"                   # no current version at all


def test_rerun_after_merge_is_already_applied_not_stale():
    row = outbox()
    applied = one(row)["insert"]
    d = one(row, current=[applied])
    assert d["decision"] == "already_applied" and d["status"] == "applied" and d["insert"] is None


def test_effective_from_not_after_version_n_is_invalid():
    d = one(outbox(effective_from="2026-01-15T00:00:00Z"))
    assert d["status"] == "rejected_invalid" and "invalid_value" in d["codes"]


def test_second_pending_row_for_the_same_rule_is_deferred():
    ds = rp.plan([outbox("P-2", requested_at="2026-11-09T17:00:00Z"), outbox("P-1")], [V1], DSSMP)
    assert [(d["pub_id"], d["decision"], d["status"]) for d in ds] == [("P-1", "applied", "applied"),
                                                                         ("P-2", "deferred", None)]
    assert [u[3] for u in job.outbox_updates(ds, "now")] == ["P-1"]


def test_outbox_updates_and_merge_text():
    ds = rp.plan([outbox()], [V1], DSSMP)
    assert job.outbox_updates(ds, "T") == [("applied", ds[0]["message"], "T", "P-1")]
    sql = rp.merge_sql("cat.ref.threshold", "src")
    assert sql.count("MERGE INTO") == 1 and "WHEN MATCHED AND s.merge_action = 'close'" in sql
    upd = re.search(r"UPDATE SET (.*)\n", sql).group(1)
    assert {c.split("=")[0].strip() for c in upd.split(",")} == {"effective_to", "is_current"}   # bookkeeping only
    assert "c.threshold_version = s.supersedes_version" in sql                                  # stale re-check


def test_app_generated_outbox_row_is_accepted_after_dropping_demo_only_fields():
    """A real outbox row written by App Dev's alarm_rules._apply (demo mode). The demo seed adds `output_name`, which
    is not a ref.threshold column (the live app reads SELECT t.* from ref.threshold, so live rows lack it): the
    publisher rejects it rather than dropping data silently."""
    sys.path.insert(0, os.path.join(ROOT, "app"))
    try:
        from fastapi.testclient import TestClient
        from backend import main as app_main
        from backend.config import Settings
    except Exception as e:  # pragma: no cover
        pytest.skip(f"app not importable: {e}")
    finally:
        sys.path.remove(os.path.join(ROOT, "app"))
    app = app_main.create_app(Settings(demo_mode=True, static_dir="/nonexistent"))
    tc = TestClient(app)
    an = app.state.analytics_factory(None)
    rule = next(t for t in an.thresholds("CRD") if t["threshold_id"] == "CRD-PZ-1-T1")
    v_n = {c: rule.get(c) for c in rp.THRESHOLD_COLS}
    r = tc.post("/api/dams/CRD/alarm-rules/CRD-PZ-1-T1/cdse-flag",
                headers={"x-forwarded-user": "uid-cdse", "x-forwarded-email": "cdse.crd@demo.local"},
                json={"cdse_ack_required": not bool(rule.get("cdse_ack_required")), "rationale": "publisher test"})
    assert r.status_code == 200, r.text
    pub = app.state.store.ref_publications("CRD")[-1]
    dssmp = {d["dssmp_version_id"]: d for d in an.d["dssmp_versions"]}
    row = {**pub, "status": "pending", "payload": json.dumps(pub["payload"])}
    d = one(row, current=[v_n], dssmp=dssmp)
    assert d["codes"] == ["unknown_columns"] and "output_name" in d["message"]
    live = copy.deepcopy(pub["payload"])
    live["insert"].pop("output_name")
    d = one({**row, "payload": json.dumps(live)}, current=[v_n], dssmp=dssmp)
    assert d["decision"] == "applied", d["message"]


def test_job_is_wired_into_its_own_bundle_job():
    jobs = open(os.path.join(ROOT, "resources", "jobs.yml")).read()
    blk = jobs[jobs.index("    dam_ref_publish:"):jobs.index("    dam_interpretation:")]
    assert "python_file: ../src/jobs/publish_ref_changes.py" in blk and '"--catalog", "${var.catalog}"' in blk
    src = open(os.path.join(ROOT, "src", "jobs", "publish_ref_changes.py")).read()
    body = src[src.index("def main"):]
    assert body.count("spark.sql(merge_sql(") == 1                       # one MERGE per run
    assert body.index("assert_versions_immutable") < body.index("outbox_updates(")   # post-check before status


# ---------------------------------------------------------------- the MERGE, executed on PostgreSQL (>= 15)
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")
PGTYPES = {"threshold_version": "INT", "level": "INT", "value": "DOUBLE PRECISION", "deadband": "DOUBLE PRECISION",
           "persistence_on": "INT", "persistence_off": "INT", "effective_from": "TIMESTAMPTZ",
           "effective_to": "TIMESTAMPTZ", "is_current": "BOOLEAN", "cdse_ack_required": "BOOLEAN"}


def _psql(conn, sql):
    return subprocess.run(["psql", conn, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True)


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dt.datetime):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


@pytest.fixture()
def pg():
    db = f"pub_{uuid.uuid4().hex[:8]}"
    assert _psql(PG, f"CREATE DATABASE {db}").returncode == 0
    conn = f"{PG} dbname={db}"
    cols = ", ".join(f"{c} {PGTYPES.get(c, 'TEXT')}" for c in rp.THRESHOLD_COLS)
    extra = "merge_action TEXT, merge_version INT, supersedes_version INT, pub_id TEXT"
    r = _psql(conn, f"CREATE TABLE threshold ({cols}, PRIMARY KEY (threshold_id, threshold_version)); "
                    f"CREATE TABLE src ({cols}, {extra});")
    assert r.returncode == 0, r.stderr
    yield conn
    _psql(PG, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _insert(conn, table, rows, cols):
    for r in rows:
        q = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(_lit(r.get(c)) for c in cols)})"
        assert _psql(conn, q).returncode == 0


def _state(conn):
    out = _psql(conn, "SELECT threshold_id || ':' || threshold_version || ':' || is_current || ':' || "
                      "coalesce(to_char(effective_to AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24MI'), '-') || ':' || cdse_ack_required FROM threshold ORDER BY 1")
    return out.stdout.split()


SRC_COLS = rp.THRESHOLD_COLS + ("merge_action", "merge_version", "supersedes_version", "pub_id")


@live
def test_merge_closes_n_inserts_n_plus_1_and_is_idempotent(pg):
    _insert(pg, "threshold", [V1], rp.THRESHOLD_COLS)
    ds = rp.plan([outbox()], [V1], DSSMP)
    _insert(pg, "src", rp.merge_source_rows(ds), SRC_COLS)
    sql = rp.merge_sql("threshold", "src")
    r = _psql(pg, sql)
    assert r.returncode == 0, r.stderr
    st = _state(pg)
    assert st == ["T-1:1:false:2026-11-09T1600:false", "T-1:2:true:-:true"], st
    assert _psql(pg, sql).returncode == 0 and _state(pg) == st                 # re-run: nothing more happens


@live
def test_merge_rechecks_staleness_inside_the_statement(pg):
    """The plan saw v1 current, but a concurrent writer made v2 current before the MERGE: nothing is written."""
    _insert(pg, "threshold", [{**V1, "is_current": False, "effective_to": "2026-10-01T00:00:00Z"},
                              {**V1, "threshold_version": 2, "effective_from": "2026-10-01T00:00:00Z",
                               "set_by": "other"}], rp.THRESHOLD_COLS)
    before = _state(pg)
    ds = rp.plan([outbox()], [V1], DSSMP)                                  # stale view of the world
    _insert(pg, "src", rp.merge_source_rows(ds), SRC_COLS)
    assert _psql(pg, rp.merge_sql("threshold", "src")).returncode == 0
    assert _state(pg) == before
