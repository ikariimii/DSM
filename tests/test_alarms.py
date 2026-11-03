"""Alarm state machine. Basis: FERC Ch.9 PDF p.47 (magnitude AND rate limits)
[/workspace/dam-refs/FERC_Eng_Guidelines_Ch9_Instrumentation.pdf]; FERC canvas REQ-15 (BAD
data never drives alarms) and REQ-22..25 (logged transitions). Deadband/persistence are site
settings (UNVERIFIED defaults)."""
from datetime import datetime, timedelta

from dam_safety.alarms import AlarmState, ThresholdSpec, acknowledge, evaluate, run_series

T0 = datetime(2025, 1, 1)
SPECS = [ThresholdSpec("T-A", 1, 1, "magnitude", "high", 10.0, deadband=0.5, persistence_on=2, persistence_off=2),
         ThresholdSpec("T-X", 1, 2, "magnitude", "high", 20.0, deadband=0.5)]


def rows(vals, flag="OK"):
    return [{"ts_utc": T0 + timedelta(hours=i), "value": v, "qa_flag": flag} for i, v in enumerate(vals)]


def events(trs):
    return [(t.event, t.to_state) for t in trs]


def test_persistence_then_raise():
    st, trs = run_series("S", rows([5, 11, 11]), SPECS)
    assert events(trs) == [("RAISE", "ALERT_UNACK")]
    assert trs[0].ts == T0 + timedelta(hours=2) and trs[0].threshold_id == "T-A"


def test_single_excursion_does_not_raise():
    _, trs = run_series("S", rows([11, 5, 11, 5]), SPECS)
    assert trs == []


def test_escalate_resets_ack_and_hysteresis_clear():
    acks = [{"ts": T0 + timedelta(hours=2, minutes=30), "actor": "eng1"}]
    st, trs = run_series("S", rows([11, 11, 12, 25, 15, 9.8, 9.4, 9.4]), SPECS, acks=acks)
    assert events(trs) == [
        ("RAISE", "ALERT_UNACK"), ("ACK", "ALERT_ACK"), ("ESCALATE", "ACTION_UNACK"),
        ("DEESCALATE", "ALERT_UNACK"), ("RTN_UNACK", "CLEARED_UNACK")]
    # 9.8 is inside deadband (10-0.5) so it holds; clears after 2 readings below 9.5
    assert st.state == "CLEARED_UNACK"
    tr = acknowledge(st, T0 + timedelta(days=1), "eng1")
    assert events(tr) == [("ACK", "NORMAL")]


def test_acked_alarm_clears_to_normal():
    acks = [{"ts": T0 + timedelta(hours=1, minutes=30), "actor": "eng1"}]
    st, trs = run_series("S", rows([11, 11, 9, 9]), SPECS, acks=acks)
    assert events(trs)[-1] == ("CLEAR", "NORMAL") and st.state == "NORMAL"


def test_bad_data_ignored():
    _, trs = run_series("S", rows([50, 50, 50], flag="BAD"), SPECS)
    assert trs == []


def test_rate_limit():
    spec = [ThresholdSpec("T-R", 1, 1, "rate", "high", 1.0)]   # > 1 unit/day
    st = AlarmState("S")
    assert evaluate(st, T0, 0.0, "OK", spec) == []
    trs = evaluate(st, T0 + timedelta(hours=12), 1.0, "OK", spec)   # 2 /day
    assert events(trs) == [("RAISE", "ALERT_UNACK")]


def test_low_direction():
    spec = [ThresholdSpec("T-L", 1, 1, "magnitude", "low", 2.0)]
    _, trs = run_series("S", rows([3, 1]), spec)
    assert events(trs) == [("RAISE", "ALERT_UNACK")]


def test_ack_when_normal_is_noop():
    assert acknowledge(AlarmState("S"), T0, "x") == []


def test_threshold_row_mapping_canvas_vocabulary():
    # FERC canvas ref.threshold uses direction upper|lower (SCHEMA_CONTRACT.md §4)
    from dam_safety.alarms import spec_from_threshold_row
    s = spec_from_threshold_row({"threshold_id": "T", "threshold_version": "2", "level": 2, "limit_type": "Magnitude",
                                 "direction": "upper", "value": "12.5", "deadband": None, "persistence_on": 3})
    assert (s.direction, s.level, s.value, s.deadband, s.persistence_on, s.persistence_off) == ("high", 2, 12.5, 0.0, 3, 1)
    assert spec_from_threshold_row({"threshold_id": "T", "threshold_version": 1, "level": 1, "limit_type": "rate",
                                    "direction": "lower", "value": 1}).direction == "low"
    import pytest
    with pytest.raises(ValueError):
        spec_from_threshold_row({"threshold_id": "T", "threshold_version": 1, "level": 1, "limit_type": "cyclic",
                                 "direction": "upper", "value": 1})


def test_state_roundtrip_resumes_identically():
    from dam_safety.alarms import state_from_row, state_to_row
    st, _ = run_series("S", rows([11, 11]), SPECS)
    st2 = state_from_row(state_to_row(st))
    a = evaluate(st, T0 + timedelta(hours=5), 25, "OK", SPECS)
    b = evaluate(st2, T0 + timedelta(hours=5), 25, "OK", SPECS)
    assert [x.event for x in a] == [x.event for x in b] == ["ESCALATE"]


def test_evaluate_sensor_batch_watermark_and_ack():
    # Batch evaluator used by src/jobs/alarm_evaluator.py; SCHEMA_CONTRACT §4 (ack from Lakebase)
    from dam_safety.alarms import evaluate_sensor_batch
    th = [{"threshold_id": "A", "threshold_version": 1, "level": 1, "limit_type": "magnitude", "direction": "upper",
           "value": 10, "deadband": 0.5, "persistence_on": 2, "persistence_off": 2, "level_name": "ALERT"}]
    rd = [{"ts_utc": T0 + timedelta(hours=i), "value": v, "qa_flag": "OK"} for i, v in enumerate([5, 11, 11])]
    row, trs, late = evaluate_sensor_batch("S", rd, th, None, [])
    assert [t.event for t in trs] == ["RAISE"] and row["state"] == "ALERT_UNACK" and late == 0
    assert row["evaluated_through_ts"] == T0 + timedelta(hours=2)
    # re-run with the same readings + an ack: readings are behind the watermark, only the ACK applies
    row2, trs2, late2 = evaluate_sensor_batch("S", rd, th, row, [{"ts": T0 + timedelta(hours=3), "actor": "eng@x"}])
    assert late2 == 3 and [t.event for t in trs2] == ["ACK"] and row2["state"] == "ALERT_ACK"
    assert row2["evaluated_through_ts"] == row["evaluated_through_ts"]
