"""Alarm-episode traceability (SCHEMA_CONTRACT §4.3; app SCHEMA_CONTRACT open question 6).
Static checks: the evaluator writes every ops.alarm_transition column in DDL order, and the
app-facing ops.alarm_episode view exposes reading_id + calibration version."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cols(ddl_file, table):
    txt = open(os.path.join(ROOT, "sql", "ddl", ddl_file)).read()
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \((.*?)\n\)", txt, re.S).group(1)
    names = []
    for line in body.splitlines():
        line = line.split("--")[0].strip()
        m = re.match(r"([a-z_]+)\s+(STRING|INT|DOUBLE|TIMESTAMP|BOOLEAN)", line)
        if m:
            names.append(m.group(1))
    return names


def test_transition_has_lineage_columns():
    cols = _cols("07_ops.sql", "ops.alarm_transition")
    for c in ("reading_id", "calib_id", "cal_version", "formula_id", "formula_version", "recompute_batch_id"):
        assert c in cols


def test_evaluator_tuple_matches_transition_schema():
    src = open(os.path.join(ROOT, "src", "jobs", "alarm_evaluator.py")).read()
    tup = re.search(r"tr_rows\.append\(\((.*?)\)\)\n", src, re.S).group(1)
    n = len([x for x in re.split(r",\s*", tup.replace("\n", " ")) if x.strip()])
    assert n == len(_cols("07_ops.sql", "ops.alarm_transition"))


def test_alarm_episode_view_exposes_lineage():
    v = open(os.path.join(ROOT, "sql", "ddl", "11_gold_views.sql")).read()
    ep = v[v.index("CREATE OR REPLACE VIEW ops.alarm_episode"):]
    ep = ep[:ep.index(";")]
    for c in ("AS reading_id", "AS cal_version", "AS calibration_version", "AS calib_id", "AS raise_reading_id", "AS mixed_calibration"):
        assert c in ep


def test_v14_episode_has_formula_id_and_qa_flag_and_never_null_value():
    v = open(os.path.join(ROOT, "sql", "ddl", "11_gold_views.sql")).read()
    ep = v[v.index("CREATE OR REPLACE VIEW ops.alarm_episode"):]
    ep = ep[:ep.index(";")]
    for c in ("AS formula_id", "AS formula_version", "AS qa_flag"):
        assert c in ep, c
    assert "max_by(value, CASE WHEN reading_id IS NOT NULL THEN event_ts END) AS value" in ep   # ACK rows skipped
    assert "max_by(value, event_ts)" not in ep
    assert "qa_flag" in _cols("07_ops.sql", "ops.alarm_transition")


def test_v14_null_value_constraint_and_evaluator_guard():
    ddl = open(os.path.join(ROOT, "sql", "ddl", "07_ops.sql")).read()
    assert re.search(r"ADD CONSTRAINT ck_alarm_transition_value\s+CHECK \(event = 'ACK' OR \(value IS NOT NULL AND "
                     r"reading_id IS NOT NULL", ddl)
    src = open(os.path.join(ROOT, "src", "jobs", "alarm_evaluator.py")).read()
    cols = _cols("07_ops.sql", "ops.alarm_transition")
    assert (cols[5], cols[8], cols[9], cols[14]) == ("event", "value", "metric_value", "reading_id")   # guard indices
    assert 'r[5] != "ACK" and (r[8] is None or r[9] is None or r[14] is None)' in src and "raise RuntimeError" in src


def test_v14_engine_never_emits_reading_transition_with_null_value():
    from datetime import datetime, timedelta
    from dam_safety import alarms
    spec = {"threshold_id": "T1", "threshold_version": 1, "level": 1, "limit_type": "magnitude", "direction": "upper",
            "value": 10, "deadband": 0.0, "persistence_on": 1, "persistence_off": 1, "level_name": "ALERT"}
    t0 = datetime(2026, 1, 1)
    rows = [dict(reading_id=f"r{i}", ts_utc=t0 + timedelta(hours=i), value=v, unit="m", qa_flag=q)
            for i, (v, q) in enumerate([(12.0, "OK"), (None, "OK"), (13.0, "BAD"), (5.0, "OK"), (15.0, "OK")])]
    _state, trs, _late = alarms.evaluate_sensor_batch("S1", rows, [spec], None, [])
    assert [t.event for t in trs][:1] == ["RAISE"]
    assert trs and all(t.value is not None for t in trs if t.event != "ACK")



# ---- v1.5 / QA-035: every non-ACK transition carries value AND metric_value (ck_alarm_transition_value) -----------
def _specs(two_levels=False, limit_type="magnitude"):
    s = [{"threshold_id": "T1", "threshold_version": 1, "level": 1, "limit_type": limit_type, "direction": "upper",
          "value": 10, "deadband": 0.0, "persistence_on": 1, "persistence_off": 1, "level_name": "ALERT"}]
    if two_levels:
        s.append(dict(s[0], threshold_id="T2", level=2, value=20, level_name="ALARM"))
    return s


def _rows(vals, prefix="r"):
    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1)
    return [dict(reading_id=f"{prefix}{i}", ts_utc=t0 + timedelta(hours=i), value=v, unit="m", qa_flag="OK")
            for i, v in enumerate(vals)]


def _guard_rows(sensor, rows, trs):
    """The evaluator's tuple fields used by its pre-write guard: (event, value, metric_value, reading_id)."""
    by_ts = {r["ts_utc"]: r for r in rows}
    return [(t.event, t.value, t.metric_value, (by_ts.get(t.ts, {}) if t.event != "ACK" else {}).get("reading_id"))
            for t in trs]


def test_v15_metric_value_on_every_transition_type():
    from datetime import datetime, timedelta
    from dam_safety import alarms
    seen = set()
    for two, vals, ack in ((False, [12.0, 5.0], False), (False, [12.0, 5.0], True),
                           (True, [12.0, 25.0, 15.0, 5.0], False), (True, [12.0, 25.0, 15.0, 5.0], True)):
        acks = [dict(ts=datetime(2026, 1, 1) + timedelta(minutes=30), actor="u", comment="c")] if ack else []
        rows = _rows(vals)
        _st, trs, _ = alarms.evaluate_sensor_batch("S1", rows, _specs(two), None, acks)
        for ev, v, m, rid in _guard_rows("S1", rows, trs):
            seen.add(ev)
            if ev != "ACK":
                assert v is not None and m is not None and rid is not None, (ev, v, m, rid)
    assert {"RAISE", "ESCALATE", "DEESCALATE", "CLEAR", "RTN_UNACK", "ACK"} <= seen


def test_v15_clear_metric_is_the_evaluated_metric_of_the_level_left():
    from dam_safety import alarms
    _st, trs, _ = alarms.evaluate_sensor_batch("S1", _rows([12.0, 25.0, 15.0, 5.0]), _specs(True), None, [])
    assert [(t.event, t.metric_value) for t in trs] == [("RAISE", 12.0), ("ESCALATE", 25.0), ("DEESCALATE", 15.0),
                                                         ("RTN_UNACK", 5.0)]


def test_v15_rate_reading_not_evaluable_never_clears():
    """First rate reading after a datum re-baseline has no metric: it must neither clear nor emit a NULL metric."""
    from dam_safety import alarms
    rows = _rows([0.0, 48.0, 49.0])                       # rate 48/h*24 -> RAISE; then DATUM_CHANGE re-baseline
    rows[2]["qa_reason"] = "DATUM_CHANGE"
    _st, trs, _ = alarms.evaluate_sensor_batch("S1", rows, _specs(limit_type="rate"), None, [])
    assert [t.event for t in trs] == ["RAISE"] and all(t.metric_value is not None for t in trs)


def test_v15_one_sensor_returning_to_normal_does_not_halt_the_others():
    """Mirror of the evaluator loop + pre-write guard over several sensors: S2's return to normal used to carry
    metric_value=None, the guard raised and NO sensor's transitions were written (QA-035)."""
    from dam_safety import alarms
    batch = {"S1": _rows([12.0, 13.0], "a"), "S2": _rows([12.0, 5.0], "b"), "S3": _rows([25.0, 15.0, 5.0], "c")}
    tr_rows = []
    for sid, rows in batch.items():
        _st, trs, _ = alarms.evaluate_sensor_batch(sid, rows, _specs(sid == "S3"), None, [])
        tr_rows += [(sid,) + g for g in _guard_rows(sid, rows, trs)]
    bad = [r for r in tr_rows if r[1] != "ACK" and (r[2] is None or r[3] is None or r[4] is None)]
    assert bad == []                                       # evaluator would not raise -> all sensors written
    assert {r[0] for r in tr_rows} == {"S1", "S2", "S3"}
    assert ("S2", "RTN_UNACK") in {(r[0], r[1]) for r in tr_rows}


def test_v15_alarm_episode_carries_erroneous_mark_with_who_and_when():
    """App Developer mismatch 2: erroneous mark + who/when on the episode, so the app stops reading ops.reading."""
    v = open(os.path.join(ROOT, "sql", "ddl", "11_gold_views.sql")).read()
    ep = v[v.index("CREATE OR REPLACE VIEW ops.alarm_episode"):]
    ep = ep[:ep.index("CREATE OR REPLACE VIEW", 10)]
    for c in ("AS erroneous_mark", "AS erroneous_reason", "AS erroneous_marked_by", "AS erroneous_marked_ts"):
        assert c in ep, c
    assert "coalesce(mk.is_erroneous, false) AS erroneous_mark" in ep
    assert "PARTITION BY reading_id ORDER BY marked_ts DESC" in ep and "WHERE rn = 1" in ep      # latest mark wins
    assert "LEFT JOIN mk ON mk.reading_id = g.reading_id" in ep                                 # never drops episodes
    assert "ops.reading " not in ep.replace("ops.reading_erroneous_mark", "")
    assert {"marked_by", "marked_ts", "erroneous_reason"} <= set(_cols("07_ops.sql", "ops.reading_erroneous_mark"))


# ---- v1.6 (app SCHEMA_CONTRACT §7 10d): inclinometer installation_version on alarms ----
def test_v16_installation_version_column_view_and_evaluator():
    cols = _cols("07_ops.sql", "ops.alarm_transition")
    assert cols[-1] == "installation_version"                     # appended: positional tuple + additive migration
    v = open(os.path.join(ROOT, "sql", "ddl", "11_gold_views.sql")).read()
    ev = v[v.index("CREATE OR REPLACE VIEW ops.alarm_event"):]
    assert "installation_version" in ev[:ev.index(";")]
    ep = v[v.index("CREATE OR REPLACE VIEW ops.alarm_episode"):]
    ep = ep[:ep.index(";")]
    assert "max_by(installation_version, CASE WHEN reading_id IS NOT NULL THEN event_ts END) AS installation_version" in ep
    src = open(os.path.join(ROOT, "src", "jobs", "alarm_evaluator.py")).read()
    assert 'src.get("qa_flag"), iv))' in src and 'inst_version(sid, t.ts, log) if t.event != "ACK" else None' in src
    assert '"ref", "incl_installation"' in src


def test_v16_installation_version_at_is_the_row_valid_at_reading_time():
    import sys
    from datetime import datetime
    import pytest
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from dam_safety.inclinometer.batch import installation_version_at
    rows = [dict(instrument_id="I1", effective_from="2025-01-01T00:00:00Z", effective_to="2026-01-01T00:00:00Z"),
            dict(instrument_id="I1", effective_from="2026-01-01T00:00:00Z", effective_to=None)]
    assert installation_version_at(rows, datetime(2025, 6, 1)) == datetime(2025, 1, 1)
    assert installation_version_at(rows, "2026-01-01T00:00:00Z") == datetime(2026, 1, 1)   # half-open
    assert installation_version_at(rows, "2024-12-31T23:59:59Z") is None
    with pytest.raises(ValueError):
        installation_version_at(rows + [dict(instrument_id="I1", effective_from="2025-06-01", effective_to=None)],
                                "2026-02-01")
