"""v1.7 cdse_ack_required (DSSMP; App Dev agreed 2026-11-09). Per-dam, per-rule flag on ref.threshold
(BOOLEAN NOT NULL DEFAULT false, SCD2 per rule version), snapshotted on the opening RAISE transition as
ops.alarm_transition.cdse_ack_required_at_open (immutable), exposed with the current rule flag on ops.alarm_episode
and in the ALARM_ACK extract payload. Enforcement is the app's. Synthetic data."""
import json
import os
import re
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
import lakebase_extract as lx  # noqa: E402
from dam_safety import alarms  # noqa: E402

V11 = open(os.path.join(ROOT, "sql", "ddl", "11_gold_views.sql")).read()


def _view(name):
    v = V11[V11.index(f"CREATE OR REPLACE VIEW {name}"):]
    return v[:v.index(";")]


def _spec(flag, version=1, tid="T1", level=1, value=10.0):
    return {"threshold_id": tid, "threshold_version": version, "level": level, "limit_type": "magnitude",
            "direction": "upper", "value": value, "deadband": 0.0, "persistence_on": 1, "persistence_off": 1,
            "level_name": "ALERT", "cdse_ack_required": flag}


def _rows(vals, start=0):
    t0 = datetime(2026, 1, 1)
    return [dict(reading_id=f"r{start + i}", ts_utc=t0 + timedelta(hours=start + i), value=v, unit="m", qa_flag="OK")
            for i, v in enumerate(vals)]


def test_threshold_ddl_column_is_not_null_default_false():
    ddl = open(os.path.join(ROOT, "sql", "ddl", "02_ref_calibration_threshold.sql")).read()
    body = ddl[ddl.index("CREATE TABLE IF NOT EXISTS ref.threshold"):]
    body = body[:body.index("\n) TBLPROPERTIES")] + body[body.index("\n) TBLPROPERTIES"):].split("\n")[1]
    assert re.search(r"cdse_ack_required BOOLEAN NOT NULL DEFAULT false", body)
    assert "'delta.feature.allowColumnDefaults' = 'supported'" in body


def test_snapshot_only_on_raise_and_from_the_raising_rule_version():
    specs = [_spec(True, version=3), _spec(False, version=2)]
    assert alarms.cdse_ack_snapshot("RAISE", "T1", 3, specs) is True
    assert alarms.cdse_ack_snapshot("RAISE", "T1", 2, specs) is False
    for ev in ("ESCALATE", "DEESCALATE", "CLEAR", "RTN_UNACK", "ACK"):
        assert alarms.cdse_ack_snapshot(ev, "T1", 3, specs) is None
    with pytest.raises(ValueError):                                     # fails closed, never guesses false
        alarms.cdse_ack_snapshot("RAISE", "T1", 9, specs)
    with pytest.raises(ValueError):
        alarms.cdse_ack_snapshot("RAISE", "T1", 1, [{k: v for k, v in _spec(True).items() if k != "cdse_ack_required"}])


def test_unflagging_the_rule_mid_episode_leaves_the_snapshot_true():
    """Run 1: rule v1 flagged, the alarm RAISEs -> snapshot TRUE on the RAISE row. The DSSMP then un-flags the rule
    (new SCD2 version v2, flag false). Run 2 continues the SAME episode (no new RAISE): no transition carries a new
    snapshot, the stored RAISE row is untouched (append-only), so the episode keeps cdse_ack_required_at_open = TRUE
    while the current rule flag reads FALSE. A NEW episode after the clear snapshots FALSE."""
    written = []                                        # what alarm_evaluator appends to ops.alarm_transition

    def run(specs, rows, state):
        st, trs, _ = alarms.evaluate_sensor_batch("S1", rows, specs, state, [])
        for t in trs:
            written.append({"event": t.event, "threshold_id": t.threshold_id, "threshold_version": t.threshold_version,
                            "cdse_ack_required_at_open": alarms.cdse_ack_snapshot(t.event, t.threshold_id,
                                                                                  t.threshold_version, specs)})
        return st
    st = run([_spec(True, version=1)], _rows([12.0, 13.0]), None)
    assert [w["event"] for w in written] == ["RAISE"] and written[0]["cdse_ack_required_at_open"] is True
    raise_row = dict(written[0])
    current_rule = [_spec(False, version=2)]                              # un-flagged mid-episode
    st = run(current_rule, _rows([14.0, 15.0], start=2), st)
    assert len(written) == 1                                              # still the same open episode
    assert written[0] == raise_row                                        # snapshot never rewritten
    # episode view semantics: snapshot from the RAISE row, current flag from the rule's is_current version
    episode = {"cdse_ack_required_at_open": max(w["cdse_ack_required_at_open"] for w in written if w["event"] == "RAISE"),
               "cdse_ack_required": current_rule[0]["cdse_ack_required"]}
    assert episode == {"cdse_ack_required_at_open": True, "cdse_ack_required": False}
    # the episode closes (return to normal) and a new one opens under the un-flagged rule
    st = run(current_rule, _rows([1.0, 1.0, 12.0], start=4), st)
    raises = [w for w in written if w["event"] == "RAISE"]
    assert [r["cdse_ack_required_at_open"] for r in raises] == [True, False]
    assert all(w["cdse_ack_required_at_open"] is None for w in written if w["event"] != "RAISE")


def test_episode_view_exposes_snapshot_and_current_rule_flag():
    ev = _view("ops.alarm_event")
    assert "cdse_ack_required_at_open" in ev
    ep = _view("ops.alarm_episode")
    assert "max(CASE WHEN event = 'RAISE' THEN cdse_ack_required_at_open END) AS cdse_ack_required_at_open" in ep
    assert "max(CASE WHEN event = 'RAISE' THEN threshold_id END) AS raise_threshold_id" in ep
    assert "cr.cdse_ack_required             AS cdse_ack_required" in ep
    assert "LEFT JOIN ref.threshold cr ON cr.threshold_id = g.raise_threshold_id AND cr.is_current" in ep


def test_evaluator_writes_the_snapshot_and_never_stops_alarming_over_it():
    src = open(os.path.join(ROOT, "src", "jobs", "alarm_evaluator.py")).read()
    assert "ca = cdse_ack_snapshot(t.event, t.threshold_id, t.threshold_version, ok_specs)" in src
    assert "ca = True" in src and "conservative" in src


def test_extract_ack_payload_carries_both_flags():
    rows = [{"event_id": "E1", "dam_id": "D1", "state": "acknowledged", "acknowledged_by": "eng@x.org",
             "acknowledged_at": "2026-01-01T03:00:00+00:00"}]
    now = datetime(2026, 1, 2)
    base = lx.ack_rows_to_events(rows, now)
    flagged = lx.ack_rows_to_events(rows, now, episodes={"E1": {"cdse_ack_required_at_open": True,
                                                                "cdse_ack_required": False}})
    assert base[0]["event_id"] == flagged[0]["event_id"]                    # id unchanged (dedup keys stable)
    p = json.loads(flagged[0]["payload"])
    assert (p["cdse_ack_required_at_open"], p["cdse_ack_required"]) == (True, False)
    assert "cdse_ack_required_at_open" not in json.loads(base[0]["payload"])
