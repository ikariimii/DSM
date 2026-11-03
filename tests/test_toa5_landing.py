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
