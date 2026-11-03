"""Synthetic sample data (tools/generate_sample_data.py) matches the bronze landing contract and is
deterministic. Sources: bronze schemas = src/pipelines/bronze.py READING_SCHEMA/INCL_SCHEMA;
reading_type vocabulary = /workspace/dam-refs/CONVENTIONS.md (reading_type manual/auto);
inclinometer reduction = /workspace/dam-refs/procedures/inclinometers.md (D = A0 - A180,
deviation = L*D/(2K)); probe constants = installation.METRIC_DIGITILT [procedure §3]."""
import csv
import filecmp
import glob
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "sample_data")


def _gen():
    spec = importlib.util.spec_from_file_location("gen", os.path.join(ROOT, "tools", "generate_sample_data.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _cols(schema: str):
    return [c.strip().split(" ")[0] for c in schema.split(",")]


def _bronze_schemas():
    src = open(os.path.join(ROOT, "src", "pipelines", "bronze.py")).read()
    ns = {}
    for name in ("READING_SCHEMA", "INCL_SCHEMA"):
        i = src.index(name + " = ")
        j = src.index(")", i)
        ns[name] = eval(src[i + len(name) + 3:j + 1])   # string literal tuple-concatenation only
    return _cols(ns["READING_SCHEMA"]), _cols(ns["INCL_SCHEMA"])


def test_five_dams_and_headers_match_bronze():
    rcols, icols = _bronze_schemas()
    files = sorted(glob.glob(os.path.join(SAMPLE, "landing", "*", "readings", "*.csv")))
    assert len(files) == 5
    for f in files:
        assert next(csv.reader(open(f))) == rcols
    inc = glob.glob(os.path.join(SAMPLE, "landing", "*", "inclinometer", "*.csv"))
    assert len(inc) == 2
    for f in inc:
        assert next(csv.reader(open(f))) == icols


def test_regeneration_is_byte_identical(tmp_path):
    _gen().write_all(str(tmp_path))
    for f in glob.glob(os.path.join(SAMPLE, "**", "*.csv"), recursive=True):
        rel = os.path.relpath(f, SAMPLE)
        assert filecmp.cmp(f, tmp_path / rel, shallow=False), rel


def test_defects_present_and_vocab():
    for f in glob.glob(os.path.join(SAMPLE, "landing", "*", "readings", "*.csv")):
        rows = list(csv.DictReader(open(f)))
        assert {r["reading_type"] for r in rows} == {"auto"}
        assert sum(1 for r in rows if r["sensor_id"] == "") == 1
        assert sum(1 for r in rows if r["ts_utc"].startswith("2026-13")) == 1
        assert sum(1 for r in rows if r["raw_value"] == "") >= 1
        assert {r["source"] for r in rows} == {"SYNTHETIC"}


def test_sensor_ids_resolve_to_ref():
    sensors = {r["sensor_id"] for r in csv.DictReader(open(os.path.join(SAMPLE, "ref", "sensor.csv")))}
    for f in glob.glob(os.path.join(SAMPLE, "landing", "*", "readings", "*.csv")):
        used = {r["sensor_id"] for r in csv.DictReader(open(f)) if r["sensor_id"]}
        assert used <= sensors


def test_syn_dam_02_shear_zone_reduces_to_1p5mm_per_survey():
    from dam_safety.inclinometer.installation import METRIC_DIGITILT
    from dam_safety.inclinometer.reduction import reduce_survey, top_cum_change_mm
    rows = list(csv.DictReader(open(os.path.join(SAMPLE, "landing", "SYN-DAM-02", "inclinometer", "SYN-DAM-02-IN-01.csv"))))
    by = {}
    for r in rows:
        by.setdefault(r["survey_ts"], {})[(float(r["depth_m"]), "A")] = float(r["a0"]) - float(r["a180"])
    ts = sorted(by)
    assert len(ts) == 6
    base = by[ts[0]]
    last = reduce_survey(by[ts[-1]], base, METRIC_DIGITILT, "A")
    # 5 surveys x 1.5 mm; noise sd 2 digits/reading -> ~0.03 mm/interval -> top tolerance 0.5 mm
    assert top_cum_change_mm(last) == pytest.approx(7.5, abs=0.5)
    shallow = [r for r in last if r.depth_top_m < 7.5]
    deep = [r for r in last if r.depth_top_m > 8.5]
    assert abs(deep[0].cum_change_mm) < 0.5           # no movement below the shear zone
    assert shallow[-1].cum_change_mm == pytest.approx(7.5, abs=0.5)


# ---- v1.5 (App Developer mismatch 6): demo rows reference real sample data ------------------------------------
def _csv(*p):
    with open(os.path.join(SAMPLE, *p), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_v15_demo_inclinometer_rows_have_null_calib_and_the_installation_row_valid_at_survey_time():
    from datetime import datetime
    from dam_safety.inclinometer import batch
    inst = _csv("ref", "incl_installation.csv")
    demo = _csv("demo", "incl_surveys.csv")
    assert demo and all(r["calib_id"] == "" for r in demo)                       # NULL, never a calibration id
    seen = set()
    for r in demo:
        rows = [dict(i, effective_to=i["effective_to"] or None) for i in inst if i["instrument_id"] == r["instrument_id"]]
        v = batch.installation_row_at(rows, r["survey_ts"])
        assert v is not None and r["installation_version"] == v["effective_from"]
        seen.add(r["installation_version"])
        landing = _csv("landing", r["dam_id"], "inclinometer", f"{r['instrument_id']}.csv")
        assert int(r["n_depths"]) == sum(1 for x in landing if x["survey_ts"] == r["survey_ts"])
    assert len(seen) == 2                                                        # both SCD2 rows are exercised
    # not "the current row": surveys before the change name the CLOSED row
    early = [r for r in demo if r["survey_ts"] < "2026-03-17"]
    assert early and all(r["installation_version"] == "2026-01-01T00:00:00Z" for r in early)


def test_v15_demo_alarm_reading_refs_are_real_sample_readings():
    gen = _gen()
    for t in _csv("demo", "alarm_examples.csv"):
        rel = t["reading_source_file"].split("/raw/landing/")[1]
        rows = _csv("landing", *rel.split("/"))
        hit = [r for r in rows if r["sensor_id"] == t["sensor_id"] and r["ts_utc"] == t["reading_ts_utc"]]
        assert len(hit) == 1 and hit[0]["raw_value"] == t["value"] and t["metric_value"] != ""
        assert t["reading_id"] == gen.silver_reading_id(t["sensor_id"], t["reading_ts_utc"], t["reading_source_file"])
    assert [t["event"] for t in _csv("demo", "alarm_examples.csv")] == ["RAISE", "ESCALATE", "RTN_UNACK"]


def test_v15_reading_id_mirrors_the_silver_expression():
    src = open(os.path.join(ROOT, "src", "pipelines", "silver.py")).read()
    assert 'F.sha2(F.concat_ws("|", "sensor_id", F.col("ts_utc").cast("string"), "source_file"), 256)' in src
    assert 'F.sha2(F.concat_ws("|", "instrument_id", F.col("survey_ts").cast("string")), 256)' in src


def test_v15_new_outputs_are_deterministic(tmp_path):
    gen = _gen()
    gen.write_all(str(tmp_path))
    for p in ("ref/incl_installation.csv", "demo/incl_surveys.csv", "demo/alarm_examples.csv"):
        assert filecmp.cmp(os.path.join(SAMPLE, p), os.path.join(tmp_path, p), shallow=False), p
