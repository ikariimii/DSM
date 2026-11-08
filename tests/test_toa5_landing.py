"""TOA5 -> bronze landing adapter (src/dam_safety/toa5_landing.py) on top of the App Developer's reader
src/common/toa5.py. Sources: Campbell LoggerNet manual 'TOA5' (help.campbellsci.com/loggernet-manual/ln_manual/
campbell_scientific_file_formats/toa5.htm): no zone in timestamps; CR1000X help 'NAN and INF' for sentinels;
GEOKON-4500 manual Sec 6.1 Eq 1 digits = Hz^2/1000 (/workspace/dam-refs/4500_Piezometer.pdf p.22)."""
import os
from datetime import datetime, timezone

import pytest

from common.toa5 import parse_text
from dam_safety import toa5_landing as tl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAM = ('"TOA5","DAM1","CR1000X","12345","CR1000X.Std.07","CPU:dam.CR1X","4242","Hourly"\r\n'
       '"TIMESTAMP","RECORD","PZ1_Freq","PZ1_Therm","PZ2_Long"\r\n"TS","RN","Hz","Ohm",""\r\n'
       '"","","Smp","Smp","Smp"\r\n'
       '"2026-01-01 00:00:00.5",0,3013.66,3000,-2147483648\r\n"2026-01-01 01:00:00",1,NAN,5000,7\r\n')


def _rows(**kw):
    return tl.to_landing_rows(parse_text(DAM), "CRD", kw.pop("off", -300),
                              {"PZ1_Freq": "CRD-PZ1", "PZ2_Long": "CRD-PZ2"}, {"PZ1_Freq": "PZ1_Therm"}, **kw)


def test_columns_equal_bronze_reading_schema():
    src = open(os.path.join(ROOT, "src", "pipelines", "bronze.py")).read()
    block = src[src.index("READING_SCHEMA = ("):src.index("INCL_SCHEMA")]
    text = "".join(x.split('"')[1] for x in block.split("\n") if '"' in x)
    assert tuple(c.strip().split()[0] for c in text.split(",")) == tl.LANDING_COLUMNS


def test_offset_applied_and_vw_value():
    r = _rows()
    assert r[0]["ts_utc"] == "2026-01-01T05:00:00.500000Z"            # logger UTC-5 -> UTC
    assert r[0]["logger_id"] == "CR1000X:12345" and r[0]["temp_raw"] == 3000.0 and r[0]["raw_unit"] == "Hz"
    assert abs(r[0]["raw_value"] ** 2 / 1000 - 9082.1) < 0.1          # GEOKON-4500 Eq 1


def test_nan_and_declared_sentinel_become_null():
    r = _rows(int_sentinel_fields=frozenset({"PZ2_Long"}))
    assert r[1]["raw_value"] is None                                    # Long NAN sentinel declared
    assert r[2]["raw_value"] is None                                    # NAN -> NULL_VALUE downstream
    assert r[3]["raw_value"] == 7.0
    assert _rows()[1]["raw_value"] == -2147483648.0                     # undeclared: never guessed


def test_offset_required_and_missing_mapping_raises():
    t = parse_text(DAM)
    with pytest.raises(tl.Toa5LandingError):
        tl.to_landing_rows(t, "CRD", None, {"PZ1_Freq": "S"})
    with pytest.raises(tl.Toa5LandingError):
        tl.to_landing_rows(t, "CRD", 0, {"renamed": "S"})


def test_csv_header():
    assert tl.to_landing_csv(_rows()).splitlines()[0] == ",".join(tl.LANDING_COLUMNS)


def test_dedupe_single_parser_and_provenance_note():
    """Steering 9 + Genie 2026-11-02: one TOA5 parser (common.toa5); the dam_safety.toa5 shim had no callers and was
    deleted; the landing layer uses common.toa5 directly; provenance stated."""
    import importlib.util
    import inspect
    import os
    import dam_safety.toa5_landing as land
    assert importlib.util.find_spec("dam_safety.toa5") is None
    src = inspect.getsource(land)
    assert "def parse_text" not in src and "from common.toa5 import" in src
    for mod in (land,):
        doc = mod.__doc__
        assert "clean-room" in doc and "Campbell" in doc and "PyTOA5" in doc and "GPL" in doc
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prov = open(os.path.join(root, "docs", "PROVENANCE.md"), encoding="utf-8").read()
    assert "clean-room" in prov and "PyTOA5" in prov and "igbpyutils" in prov
    for f in ("src/common/toa5.py", "src/dam_safety/toa5_landing.py"):
        body = open(os.path.join(root, f), encoding="utf-8").read().lower()
        assert "import pytoa5" not in body and "igbpyutils import" not in body and "import igbpyutils" not in body


# QA-018 landing residual: entirely synthetic; contract docs/TOA5_READER.md [CS-LN-TOA5].
def _synthetic(records, *, table_name="Synthetic"):
    header = (f'"TOA5","TEST","CR1000X","0000","TEST","TEST","0","{table_name}"\n'
              '"TIMESTAMP","RECORD","V","T"\n"TS","RN","mV","degC"\n'
              '"","","Smp","Smp"\n')
    return header + "".join(f'"{ts}",{record},{value},20\n' for ts, record, value in records)


def _land_synthetic(records, **kw):
    return tl.to_landing_rows(parse_text(_synthetic(records)), "TEST", 0, {"V": "SYN-V"}, **kw)


def test_exact_and_conflicting_duplicates_first_win_before_channel_expansion():
    from copy import deepcopy
    source = parse_text(_synthetic([
        ("2026-01-01 00:00:00", 0, 10),
        ("2026-01-01 00:00:00", 0, 10),
        ("2026-01-01 00:00:00", 0, 999),
        ("2026-01-01 00:01:00", 1, 11),
    ]))
    original = deepcopy(source)
    with pytest.warns(tl.Toa5LandingWarning) as emitted:
        result = tl.to_landing_rows(source, "TEST", 0, {"V": "SYN-V", "T": "SYN-T"})
    assert source == original and len(source.records) == 4  # raw append-only, never edited
    assert len(result) == 4  # two unique source records, two channels each
    assert [r["raw_value"] for r in result] == [10, 20, 11, 20]
    assert len(emitted) == len(result.diagnostics)
    suppressed = [d for d in result.diagnostics if d["code"] == "landing_duplicate_suppressed"]
    assert [d["line"] for d in suppressed] == [6, 7]
    assert all(d["disposition"] == "suppressed_duplicate" for d in suppressed)
    assert all("line 5" in d["message"] for d in suppressed)


def test_composite_key_does_not_dedup_on_record_or_timestamp_alone():
    with pytest.warns(tl.Toa5LandingWarning):
        result = _land_synthetic([
            ("2026-01-01 00:00:00", 1, 10),
            ("2026-01-01 00:01:00", 1, 11),  # same RECORD, different timestamp
            ("2026-01-01 00:01:00", 2, 12),  # same timestamp, different RECORD
        ])
    assert len(result) == 3
    assert {d["code"] for d in result.diagnostics} == {"record_duplicate", "timestamp_duplicate"}
    assert all(d["disposition"] == "retained_flagged" for d in result.diagnostics)


def test_non_monotonic_record_and_timestamp_retained_with_reader_diagnostics():
    with pytest.warns(tl.Toa5LandingWarning):
        result = _land_synthetic([
            ("2026-01-01 00:02:00", 2, 10),
            ("2026-01-01 00:01:00", 1, 11),
        ])
    assert [r["raw_value"] for r in result] == [10, 11]  # not sorted or silently dropped
    assert {d["code"] for d in result.diagnostics} == {"record_out_of_order", "timestamp_out_of_order"}
    assert all(d["line"] == 6 and d["record"] == 1 for d in result.diagnostics)


def test_gaps_flagged_with_explicit_cadence_only():
    records = [("2026-01-01 00:00:00", 0, 10), ("2026-01-01 00:03:00", 3, 11)]
    with pytest.warns(tl.Toa5LandingWarning):
        result = _land_synthetic(records, expected_interval_seconds=60)
    assert {d["code"] for d in result.diagnostics} == {"record_gap", "timestamp_gap"}
    assert len(result) == 2
    with pytest.warns(tl.Toa5LandingWarning):
        result = _land_synthetic(records)
    assert {d["code"] for d in result.diagnostics} == {"record_gap"}


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True, "60"])
def test_invalid_cadence_rejected(interval):
    with pytest.raises(tl.Toa5LandingError, match="finite and positive"):
        _land_synthetic([], expected_interval_seconds=interval)


@pytest.mark.parametrize("maximum", [0x7FFFFFFF, 0xFFFFFFFF])
def test_documented_rollovers_not_mistaken_for_gap_or_disorder(maximum):
    import warnings
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        result = _land_synthetic([
            ("2026-01-01 00:00:00", maximum, 10),
            ("2026-01-01 00:01:00", 0, 11),
        ], expected_interval_seconds=60)
    assert len(result) == 2 and not result.diagnostics and not emitted


def test_sidecar_roundtrip_preserves_reader_codes_line_message_and_identity(tmp_path):
    import json
    source = parse_text(_synthetic([
        ("2026-01-01 00:00:00", 0, 10), ("2026-01-01 00:00:00", 0, 11),
    ]))
    with pytest.warns(tl.Toa5LandingWarning):
        result = tl.to_landing_rows(source, "TEST", 0, {"V": "SYN-V"})
    sidecar = tmp_path / "synthetic.diagnostics.json"
    sidecar.write_text(tl.to_landing_diagnostics_json(result), encoding="utf-8")
    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 1
    diagnostics = persisted["diagnostics"]
    for warning in source.warnings:
        assert any((d["line"], d["code"], d["message"]) ==
                   (warning.line, warning.code, warning.message) for d in diagnostics)
    assert all(d["dam_id"] == "TEST" and d["logger_id"] == "CR1000X:0000" and
               d["table_name"] == "Synthetic" and d["timestamp"] == "2026-01-01T00:00:00"
               for d in diagnostics)
    csv_rows = list(__import__("csv").DictReader(tl.to_landing_csv(result).splitlines()))
    assert len(csv_rows) == 1 and tuple(csv_rows[0]) == tl.LANDING_COLUMNS


def test_dropped_tail_reader_warning_is_not_lost():
    text = _synthetic([("2026-01-01 00:00:00", 0, 10), ("2026-01-01 00:01:00", 1, 11)])
    table = parse_text(text.rstrip("\n"), on_unterminated="drop")
    with pytest.warns(tl.Toa5LandingWarning, match="unterminated_last_line_dropped"):
        result = tl.to_landing_rows(table, "TEST", 0, {"V": "SYN-V"})
    assert len(result) == 1
    assert result.diagnostics[0]["line"] == 6
    assert result.diagnostics[0]["disposition"] == "reader_warning"
    assert result.diagnostics[0]["record"] is None


def test_no_record_fails_closed_instead_of_guessing_dedup_key():
    text = ('"TOA5","TEST","CR1000X","0000","TEST","TEST","0","Synthetic"\n'
            '"TIMESTAMP","V"\n"TS","mV"\n"","Smp"\n"2026-01-01 00:00:00",10\n')
    with pytest.raises(tl.Toa5LandingError, match="requires RECORD"):
        tl.to_landing_rows(parse_text(text), "TEST", 0, {"V": "SYN-V"})


def test_empty_and_regular_output_have_serializable_empty_diagnostics():
    import json
    import warnings
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        empty = _land_synthetic([])
        regular = _land_synthetic([("2026-01-01 00:00:00", 0, 10)])
    assert not emitted and isinstance(regular, list)
    assert json.loads(tl.to_landing_diagnostics_json(empty))["diagnostics"] == []
    assert json.loads(tl.to_landing_diagnostics_json(regular))["diagnostics"] == []
