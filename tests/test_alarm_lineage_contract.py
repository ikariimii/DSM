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
