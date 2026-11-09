"""v1.8 lifecycle wired into the existing jobs: alarm policy in alarm_evaluator (rule 1), the derived go-live in
apply_cdse_assignments (is_live), and the dam_archived refusal in the other dam_ref_publish loaders (rules 2 / 5).
Fake Spark / cursor only: UNVERIFIED on a workspace. Synthetic data only."""
import datetime as dt
import json
import os
import sys

from dam_safety import lifecycle as L
from dam_safety import lifecycle_load as LL
from lifecycle_fixtures import PATH_TO, U, history

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))


# ---------------------------------------------------------------- rule 1: alarms
def test_alarm_filter_keeps_only_production_readings():
    hist = [{"dam_id": "D1", "lifecycle_state": L.LIVE, "effective_from": "2026-03-01T00:00:00Z",
             "effective_to": "2026-06-01T00:00:00Z"},
            {"dam_id": "D1", "lifecycle_state": L.DECOM, "effective_from": "2026-06-01T00:00:00Z", "effective_to": None},
            {"dam_id": "D2", "lifecycle_state": L.DRAFT, "effective_from": "2026-01-01T00:00:00Z", "effective_to": None}]
    rd = lambda *ts: [{"reading_id": t, "ts_utc": U(t)} for t in ts]
    readings = {"S1": rd("2026-02-01T00:00:00Z", "2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z"),
                "S2": rd("2026-04-01T00:00:00Z"), "S3": rd("2026-04-01T00:00:00Z")}
    dam_of = {"S1": "D1", "S2": "D2", "S3": "D3"}
    kept, issues = L.alarm_filter(readings, dam_of, hist)
    assert [r["reading_id"] for r in kept["S1"]] == ["2026-04-01T00:00:00Z"]         # pre-go-live + decommissioned out
    assert kept["S2"] == [] and len(kept["S3"]) == 1                                  # draft test-only; no rows: on
    assert sorted(i["issue"] for i in issues) == [
        "lifecycle: 1 readings not alarmed (dam D1 alarms off)", "lifecycle: 1 readings not alarmed (dam D1 alarms test_only)",
        "lifecycle: 1 readings not alarmed (dam D2 alarms test_only)"]
    kept, issues = L.alarm_filter(readings, dam_of, None)                             # unreadable: alarms on
    assert kept == readings and issues[0]["issue"].startswith("lifecycle_unverifiable")


def test_alarm_evaluator_applies_the_filter_before_evaluating():
    src = open(os.path.join(ROOT, "src", "jobs", "alarm_evaluator.py")).read()
    assert "from dam_safety.lifecycle import alarm_filter" in src
    i = src.index("readings, lc_issues = alarm_filter(readings, dam_of, lc_hist)")
    assert src.index('fq(C, "ref", "dam_lifecycle_state")') < i < src.index("evaluate_sensor_batch(sid")
    assert "lc_hist = None" in src and "list(lc_issues)" in src


# ---------------------------------------------------------------- is_live: the derived go-live
def test_fold_go_live_takes_the_earliest_and_keeps_ambiguous():
    m = {"A": U("2026-05-01T00:00:00Z"), "B": "ambiguous", "C": U("2026-01-01T00:00:00Z")}
    rows = [{"dam_id": "A", "go_live_at": "2026-04-01T00:00:00Z"}, {"dam_id": "B", "go_live_at": "2026-01-01T00:00:00Z"},
            {"dam_id": "C", "go_live_at": "2026-02-01T00:00:00Z"}, {"dam_id": "N", "go_live_at": dt.datetime(2026, 3, 1)},
            {"dam_id": "X", "go_live_at": None}]
    out = L.fold_go_live(m, rows)
    assert out == {"A": U("2026-04-01T00:00:00Z"), "B": "ambiguous", "C": U("2026-01-01T00:00:00Z"),
                   "N": U("2026-03-01T00:00:00Z")}
    assert L.fold_go_live(m, None) == m


def test_apply_cdse_assignments_reads_the_lifecycle_go_live():
    from test_go_live_marker import D, _Spark
    import apply_cdse_assignments as job
    spark = _Spark({"audit.app_extract": [], "ref.v_dam_go_live": [{"dam_id": D, "go_live_at": "2026-02-01T00:00:00Z"}]},
                   dams=[{"dam_id": D, "cdse_id": None, "status": "onboarding", "live_since": None}])
    dam_state = job.load_context(spark, "dam_dev")[4]
    assert "dam_dev.ref.v_dam_go_live" in spark.seen
    assert dam_state[D]["go_live_at"] == U("2026-02-01T00:00:00Z")


# ---------------------------------------------------------------- rules 2 / 5: read-only records
def test_archived_split_and_audit_rows():
    cur = L.current_states(history("OLD", PATH_TO[L.TRANSFERRED]) + history("GONE", PATH_TO[L.DECOM]) +
                           history("LIVE", PATH_TO[L.LIVE]) + history("VOID", PATH_TO[L.VOID]))
    pending = [{"outbox_id": f"O-{d}", "dam_id": d} for d in ("OLD", "GONE", "LIVE", "VOID", "NEW")]
    keep, refused = LL.archived_split(pending, cur, "outbox_id")
    assert [r["dam_id"] for r in keep] == ["LIVE", "VOID", "NEW"]
    assert [(x["id"], x["message"].split(":")[0]) for x in refused] == [("O-OLD", "dam_archived"),
                                                                       ("O-GONE", "dam_archived")]
    rows = LL.archived_audit_rows(refused, "job", "dam_cdse_outbox", "r1", U("2026-11-09T20:00:00Z"))
    assert [r["event_id"] for r in rows] == ["dam_archived:dam_cdse_outbox:O-OLD", "dam_archived:dam_cdse_outbox:O-GONE"]
    assert {r["event_type"] for r in rows} == {"DAM_ARCHIVED_REFUSAL"} and set(rows[0]) == set(LL.LOG_COLS)
    assert json.loads(rows[0]["payload"])["reason_code"] == "dam_archived"
    assert LL.archived_split(pending, None, "outbox_id") == (pending, [])          # unreadable: nothing blocked here
    bad = L.current_states([dict(e, citation_code="x") for e in history("OLD", PATH_TO[L.TRANSFERRED])])
    assert LL.archived_split(pending[:1], bad, "outbox_id") == (pending[:1], [])


class _Row(dict):
    def asDict(self):
        return dict(self)


class _DF:
    def __init__(self, rows):
        self.rows = rows
        self.schema = None

    def collect(self):
        return [_Row(r) for r in self.rows]

    def createOrReplaceTempView(self, name):
        pass


class _Schema(list):
    def __getitem__(self, k):
        return k


class _Spark:
    def __init__(self, events):
        self.events, self.sqls, self.frames = events, [], []

    def table(self, name):
        if name.endswith("audit.lifecycle_event"):
            if self.events is None:
                raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
            return _DF(self.events)
        df = _DF([])
        df.schema = _Schema()
        return df

    def createDataFrame(self, rows, schema):
        self.frames.append(rows)
        return _DF(rows)

    def sql(self, q):
        self.sqls.append(q)


class _Cur:
    def __init__(self):
        self.calls = []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))


def test_refuse_archived_writes_back_and_audits_for_each_loader():
    import _lifecycle_guard as G
    import apply_cdse_assignments as cdse
    import load_dssmp_registrations as dssmp
    import publish_ref_changes as pub
    evs = history("OLD", PATH_TO[L.TRANSFERRED])
    for job, key, outbox, args in (
            (cdse, "outbox_id", "dam_cdse_outbox", lambda x, t: ("rejected", "dam_archived", x["message"], t, x["id"])),
            (dssmp, "outbox_id", "dssmp_registration_outbox",
             lambda x, t: ("rejected", "dam_archived", x["message"], t, x["id"])),
            (pub, "pub_id", "ref_change_publication", lambda x, t: ("rejected_invalid", x["message"], None, x["id"]))):
        src = open(job.__file__).read()
        assert f'refuse_archived(spark, a.catalog, cur, pending, id_key="{key}", outbox="{outbox}"' in src
        assert src.index("refuse_archived(") < src.index(" = plan(")
        spark, cur = _Spark(evs), _Cur()
        keep = G.refuse_archived(spark, "dam_dev", cur, [{key: "1", "dam_id": "OLD"}, {key: "2", "dam_id": "D1"}],
                                 id_key=key, outbox=outbox, job="j", update_sql=job.UPDATE_SQL, update_args=args)
        assert keep == [{key: "2", "dam_id": "D1"}]
        (sql, a), = cur.calls
        assert sql == job.UPDATE_SQL and a[-1] == "1" and "dam_archived: OLD is transferred" in " ".join(map(str, a))
        assert sql.count("%s") == len(a)
        assert len(spark.frames) == 1 and "dam_dev.audit.event_log" in spark.sqls[0] and "WHERE NOT EXISTS" in spark.sqls[0]
        spark, cur = _Spark(None), _Cur()                                    # table missing: nothing refused
        assert G.refuse_archived(spark, "dam_dev", cur, [{key: "1", "dam_id": "OLD"}], id_key=key, outbox=outbox,
                                 job="j", update_sql=job.UPDATE_SQL, update_args=args) == [{key: "1", "dam_id": "OLD"}]
        assert cur.calls == []
