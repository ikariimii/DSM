"""Residual 5: the not-live check uses a durable go-live marker instead of the ref.dam status.
What the app records at go-live (app/backend/routers/onboarding.py golive): an onboarding_publication outbox row
(not extracted) and a Lakebase audit_log row action `onboarding_golive` (dam_id, ts), which dam_lakebase_extract
copies to the append-only audit.app_extract (table_name 'audit_log'). No chain action and no column. The engine
(dam_safety.cdse_assignment.go_live_markers / is_live) reads that record; unreadable = live (fail closed)."""
import datetime as dt
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "src", "jobs"), os.path.dirname(__file__)]
from dam_safety import cdse_assignment as ca  # noqa: E402
from test_cdse_assignment import ADMIN, BOOT, D, codes, ev, rec  # noqa: E402

GO = "2026-04-01T00:00:00Z"                                  # BOOT is signed 2026-03; a late assign 2026-06


def golive_row(dam=D, ts=GO, action="onboarding_golive", table="audit_log", env_dam=None, payload=None, pk="7"):
    p = payload if payload is not None else json.dumps(
        {"id": int(pk), "ts": ts, "actor": "admin@x.org", "action": action, "dam_id": dam, "target": "DRAFT-1",
         "detail": json.dumps({"publication_id": "PUB-1", "status": "pending"})}, sort_keys=True)
    return {"table_name": table, "pk": pk, "dam_id": dam if env_dam is None else env_dam, "payload": p}


def U(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- the marker
def test_markers_earliest_record_wins_and_other_rows_are_ignored():
    m = ca.go_live_markers([golive_row(ts="2026-05-01T00:00:00Z", pk="9"), golive_row(pk="7"), golive_row(pk="7"),
                            golive_row(dam="X", action="onboarding_create"), golive_row(dam="Y", table="pfm_review")])
    assert m == {D: U(GO)}


@pytest.mark.parametrize("row", [
    golive_row(ts=None), golive_row(ts="not a time"), golive_row(payload='{"action": "onboarding_golive", broken'),
    golive_row(env_dam="OTHER"),                                    # envelope and payload disagree: both ambiguous
])
def test_unreadable_go_live_record_is_ambiguous_and_counts_as_live(row):
    m = ca.go_live_markers([row, golive_row(pk="8")])
    assert m[D] == ca.GO_LIVE_AMBIGUOUS
    assert ca.is_live(D, U("2000-01-01T00:00:00Z"), {D: {"status": "onboarding", "live_since": None,
                                                          "go_live_at": m[D]}})


# ---------------------------------------------------------------- a dam that went live WITHOUT thresholds
def _state(**kw):
    s = {"status": "onboarding", "live_since": None, "go_live_at": None}
    s.update(kw)
    return {D: s}


def test_went_live_without_thresholds_and_status_still_onboarding_is_live():
    late = rec(1, "dam_cdse_assign", ADMIN, "2026-06-01T00:00:00Z")
    before = ev([late], dam_state=_state())                          # no marker: pre-live status, no thresholds
    assert before[0] == []                                           # (the old status-only answer: not live)
    probs, rows = ev([late], dam_state=_state(go_live_at=U(GO)))
    assert codes(probs) == ["dam_cdse_assign_dam_live"] and rows == []
    # a bootstrap signed BEFORE the go-live record is still fine (no retroactive refusal)
    assert ev([BOOT], dam_state=_state(go_live_at=U(GO)))[0] == []


def test_went_live_before_the_publisher_loaded_ref_dam():
    late = rec(1, "dam_cdse_assign", ADMIN, "2026-06-01T00:00:00Z")
    state = {D: {"status": None, "live_since": None, "go_live_at": U(GO), "has_ref_dam": False}}
    assert codes(ev([late], dam_state=state)[0]) == ["dam_cdse_assign_dam_live"]
    assert ev([late], dam_state={})[0] == []                         # no row, no record: not live (app §7 item 20)


def test_ambiguous_record_refuses_even_an_early_bootstrap():
    assert codes(ev([BOOT], dam_state=_state(go_live_at=ca.GO_LIVE_AMBIGUOUS))[0]) == ["dam_cdse_assign_dam_live"]


def test_earliest_evidence_decides_and_status_never_makes_a_dam_not_live():
    st = _state(go_live_at=U("2026-07-01T00:00:00Z"), live_since="2026-04-01T00:00:00Z")
    assert ca.is_live(D, U("2026-05-01T00:00:00Z"), st)             # threshold earlier than the record
    st = _state(go_live_at=U(GO), live_since="2026-08-01T00:00:00Z")
    assert ca.is_live(D, U("2026-05-01T00:00:00Z"), st)             # record earlier than the threshold
    for status in ("onboarding", "draft", "pending", "planned", None, ""):
        assert ca.is_live(D, U("2026-05-01T00:00:00Z"), _state(status=status, go_live_at=U(GO)))
    assert ca.is_live(D, U("2026-05-01T00:00:00Z"), _state(status="active"))       # no time: live status = live
    assert not ca.is_live(D, U("2026-05-01T00:00:00Z"), _state(status="onboarding"))


def test_naive_and_string_times_are_accepted():
    assert ca.is_live(D, U("2026-05-01T00:00:00Z"), _state(go_live_at=dt.datetime(2026, 4, 1)))
    assert ca.is_live(D, U("2026-05-01T00:00:00Z"), _state(live_since=dt.datetime(2026, 4, 1)))


# ---------------------------------------------------------------- load_context wiring (fake Spark)
class _Col:
    def __init__(self, name, pred=None):
        self.name, self.pred = name, pred or (lambda r: r.get(name))

    def __eq__(self, other):
        return _Col(self.name, lambda r, f=self.pred: f(r) == other)

    def contains(self, s):
        return _Col(self.name, lambda r, f=self.pred: s in str(f(r) or ""))

    def __and__(self, other):
        return _Col(self.name, lambda r, a=self.pred, b=other.pred: bool(a(r)) and bool(b(r)))


class _Row(dict):
    __getattr__ = dict.get

    def asDict(self):
        return dict(self)


class _DF:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, name):
        return _Col(name)

    def where(self, col):
        return _DF([r for r in self.rows if col.pred(r)])

    def select(self, *cols):
        return _DF([{c: r.get(c) for c in cols} for r in self.rows])

    def collect(self):
        return [_Row(r) for r in self.rows]


class _Spark:
    def __init__(self, tables, dams):
        self.tables, self.dams, self.seen = tables, dams, []

    def table(self, name):
        self.seen.append(name)
        return _DF(self.tables.get(name.split(".", 1)[1], []))

    def sql(self, q):
        self.seen.append(q)
        return _DF(self.dams if "ref.dam d LEFT JOIN" in q else [])


def test_load_context_reads_the_marker_from_audit_app_extract():
    import apply_cdse_assignments as job
    spark = _Spark({"audit.app_extract": [golive_row(), golive_row(dam="NEW", pk="8"),
                                          golive_row(dam="Z", action="onboarding_create", pk="9")]},
                   dams=[{"dam_id": D, "cdse_id": None, "status": "onboarding", "live_since": None}])
    dam_state = job.load_context(spark, "dam_dev")[4]
    assert "dam_dev.audit.app_extract" in spark.seen
    assert dam_state[D]["go_live_at"] == U(GO)
    assert dam_state["NEW"] == {"status": None, "live_since": None, "go_live_at": U(GO), "has_ref_dam": False}
    assert "Z" not in dam_state
    late = rec(1, "dam_cdse_assign", ADMIN, "2026-06-01T00:00:00Z")
    assert codes(ev([late], dam_state=dam_state)[0]) == ["dam_cdse_assign_dam_live"]
