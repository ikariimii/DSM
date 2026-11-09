"""QA adversarial checks for the clean-room TOA5 reader (src/common/toa5.py). Written by QA, 2026-11-02.

Independent of App Dev's tests/test_toa5.py. Fixtures are hand-written by QA from the Campbell spec text:
  [CS-CR1000] CR1000 operator's manual rev 2/18 (s.campbellsci.com/documents/us/manuals/cr1000.pdf,
              sha256 9b91e875...730528a, downloaded by QA 2026-11-02): Table 12-13 PDF p146-147 (4-line header,
              8-field environment line, name(i,j) array names, "first field timestamp, second record number");
              PDF p437 TOA5 example; p438-439 one descriptor per field, empty string when none; RECORD max
              &hffffffff then restarts at 0; Table 114 p494 NAN/INF storage per data type.
  [CS-LN-TOA5] LoggerNet manual "TOA5" page (fetched 2026-11-02).
  [CS-NANINF]  CR1000X help "Understanding NAN and INF occurrences" (fetched 2026-11-02).
  [GEOKON-4500] /workspace/dam-refs/4500_Piezometer.pdf PDF p22 worked example (public manual).
Known defects are xfail(strict=True) with their QA id, so they turn into failures once fixed and the marker
must be removed. Run: app/.venv/bin/python -m pytest -q qa/test_toa5_adversarial.py -p no:cacheprovider
"""
import ast
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from common.toa5 import Toa5Error, parse_lines, parse_text, read_toa5  # noqa: E402

ENV = '"TOA5","QA_Stn","CR1000X","77","CR1000X.Std.07","CPU:qa.CR1X","999","T1"'
HDR = [ENV, '"TIMESTAMP","RECORD","V1","S1"', '"TS","RN","kPa",""', '"","","Smp","Smp"']
ROW1 = '"2026-11-02 10:00:00",1,1.5,"ok"'
ROW2 = '"2026-11-02 10:15:00",2,2.5,"ok"'


def doc(*rows, eol="\n", hdr=HDR, final_eol=True):
    s = eol.join(list(hdr) + list(rows))
    return s + (eol if final_eol else "")


# ------------------------------------------------------------- 4 header lines [CS-CR1000 Table 12-13]
def test_env_line_fields_map_to_table13_order():
    t = parse_text(doc(ROW1))
    e = t.environment
    assert (e.station_name, e.logger_model, e.serial_number, e.os_version, e.program_name,
            e.program_signature, e.table_name) == ("QA_Stn", "CR1000X", "77", "CR1000X.Std.07", "CPU:qa.CR1X", "999", "T1")


@pytest.mark.parametrize("env", [
    '"TOA5","a","b","c","d","e","f"',                 # 7 fields
    '"TOA5","a","b","c","d","e","f","g","h"',         # 9 fields
    '"TOB1","a","b","c","d","e","f","g"',             # other Campbell format
    '"TOACI1","a","b","c","d","e","f","g"',
    '"toa5","a","b","c","d","e","f","g"',             # case
])
def test_env_line_rejected(env):
    with pytest.raises(Toa5Error) as ei:
        parse_text(doc(ROW1, hdr=[env] + HDR[1:]))
    assert ei.value.line == 1


def test_array_names_with_embedded_commas_are_quoted_and_kept():
    hdr = [ENV, '"TIMESTAMP","RECORD","Values(1,1)","Values(1,2)"', '"TS","RN","",""', '"","","Avg","Avg"']
    t = parse_text(doc('"2026-11-02 10:00:00",0,1,2', hdr=hdr))
    assert t.names == ("TIMESTAMP", "RECORD", "Values(1,1)", "Values(1,2)")
    assert t.records[0]["Values(1,2)"] == 2


def test_station_name_with_comma_and_doubled_quote():
    env = '"TOA5","Dam ""A"", left","CR1000","1","os","p","sig","t"'
    t = parse_text(doc(ROW1, hdr=[env] + HDR[1:]))
    assert t.environment.station_name == 'Dam "A", left'


@pytest.mark.parametrize("bad, line", [
    ((ENV, '"TIMESTAMP","RECORD","V1","S1"', '"TS","RN","kPa"', '"","","Smp","Smp"'), 3),   # short units
    ((ENV, '"TIMESTAMP","RECORD","V1","S1"', '"TS","RN","kPa",""', '"","","Smp"'), 4),       # short processing
    ((ENV, '"TIMESTAMP","RECORD","V1","V1"', '"TS","RN","",""', '"","","",""'), 2),           # duplicate names
    ((ENV, '"RECORD","TIMESTAMP","V1","S1"', '"RN","TS","",""', '"","","",""'), 2),           # order
    ((ENV, '"TIMESTAMP","RECORD","V1","S1"', '"TS","RN","RN",""', '"","","",""'), 3),         # RN on data field
    ((ENV, '"TIMESTAMP","RECORD","V1","S1"', '', '"","","",""'), 3),                          # blank units line
])
def test_header_errors_with_line_numbers(bad, line):
    with pytest.raises(Toa5Error) as ei:
        parse_text(doc(ROW1, hdr=bad))
    assert ei.value.line == line


def test_header_only_and_short_file():
    assert parse_text(doc()).records == ()
    with pytest.raises(Toa5Error) as ei:
        parse_text("\n".join(HDR[:3]) + "\n")
    assert ei.value.line == 4
    with pytest.raises(Toa5Error):
        parse_text("")


def test_units_and_processing_rows_preserved_including_empty():
    t = parse_text(doc(ROW1))
    assert [(f.units, f.processing) for f in t.fields] == [("TS", ""), ("RN", ""), ("kPa", "Smp"), ("", "Smp")]


# ------------------------------------------------------------- BOM / encodings / line endings
def test_bom_utf8_file(tmp_path):
    p = tmp_path / "bom.dat"
    p.write_bytes(b"\xef\xbb\xbf" + doc(ROW1, eol="\r\n").encode())
    t = read_toa5(p)
    assert t.environment.station_name == "QA_Stn" and len(t.records) == 1


def test_bom_in_parse_text_and_utf8_sig(tmp_path):
    assert parse_text("\ufeff" + doc(ROW1)).records[0]["RECORD"] == 1
    p = tmp_path / "bom2.dat"
    p.write_bytes(b"\xef\xbb\xbf" + doc(ROW1).encode())
    assert read_toa5(p, encoding="utf-8-sig").records[0]["V1"] == 1.5


def test_double_bom_is_rejected():
    with pytest.raises(Toa5Error):
        parse_text("\ufeff\ufeff" + doc(ROW1))


def test_utf16_file_is_rejected_not_misread(tmp_path):
    p = tmp_path / "u16.dat"
    p.write_bytes(doc(ROW1).encode("utf-16"))
    with pytest.raises((UnicodeDecodeError, Toa5Error)):
        read_toa5(p)


def test_latin1_byte_rejected_strict(tmp_path):
    p = tmp_path / "l1.dat"
    p.write_bytes(doc('"2026-11-02 10:00:00",1,1.5,"caf\xe9"').encode("latin-1"))
    with pytest.raises(UnicodeDecodeError):
        read_toa5(p)
    assert read_toa5(p, encoding="latin-1").records[0]["S1"] == "caf\u00e9"


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_line_endings_file(tmp_path, eol):
    p = tmp_path / "e.dat"
    p.write_bytes(doc(ROW1, ROW2, eol=eol).encode())
    t = read_toa5(p)
    assert t.column("RECORD") == [1, 2]
    assert t.column("S1") == ["ok", "ok"]


def test_mixed_line_endings():
    s = HDR[0] + "\r\n" + HDR[1] + "\n" + HDR[2] + "\r\n" + HDR[3] + "\n" + ROW1 + "\r\n" + ROW2 + "\n"
    assert parse_text(s).column("V1") == [1.5, 2.5]


def test_parse_lines_with_raw_crlf_lines():
    assert parse_lines([h + "\r\n" for h in HDR] + [ROW1 + "\r\n"]).records[0]["S1"] == "ok"


# ------------------------------------------------------------- truncated last line
def test_truncated_last_line_fewer_fields_is_error_with_line():
    with pytest.raises(Toa5Error) as ei:
        parse_text(doc(ROW1, '"2026-11-02 10:15:00",2,2.5', final_eol=False))
    assert ei.value.line == 6


def test_truncated_inside_quote_is_error():
    with pytest.raises(Toa5Error) as ei:
        parse_text(doc(ROW1, '"2026-11-02 10:15:00",2,2.5,"o', final_eol=False))
    assert ei.value.line == 6


def test_truncated_inside_timestamp_is_error():
    with pytest.raises(Toa5Error):
        parse_text(doc(ROW1, '"2026-11-02 10:1', final_eol=False))


# QA-017 fixed (App Dev 2026-11-02 19:3x): strict xfail marker removed, test body unchanged.
def test_truncated_numeric_last_field_without_eol_is_detected():
    hdr = [ENV, '"TIMESTAMP","RECORD","V1","V2"', '"TS","RN","",""', '"","","",""']
    with pytest.raises(Toa5Error):
        parse_text(doc('"2026-11-02 10:00:00",1,1.5,2.5', '"2026-11-02 10:15:00",2,1.5,2', hdr=hdr, final_eol=False))


# QA-017 fixed (App Dev 2026-11-02 19:3x): strict xfail marker removed, test body unchanged.
def test_truncated_after_comma_without_eol_is_detected():
    hdr = [ENV, '"TIMESTAMP","RECORD","V1","V2"', '"TS","RN","",""', '"","","",""']
    with pytest.raises(Toa5Error):
        parse_text(doc('"2026-11-02 10:00:00",1,1.5,2.5', '"2026-11-02 10:15:00",2,1.5,', hdr=hdr, final_eol=False))


def test_complete_file_without_final_eol_value_is_correct():
    # App Dev 2026-11-02 19:3x, per owner decision on QA-017: an unterminated last line is rejected by default
    # (a complete line cannot be told from a truncated one). Opt-in on_unterminated="drop" discards it with a
    # warning. Original assertion: parse_text(doc(ROW1, ROW2, final_eol=False)).column("V1") == [1.5, 2.5]
    with pytest.raises(Toa5Error, match="no line terminator"):
        parse_text(doc(ROW1, ROW2, final_eol=False))
    t = parse_text(doc(ROW1, ROW2, final_eol=False), on_unterminated="drop")
    assert t.column("V1") == [1.5] and [w.code for w in t.warnings] == ["unterminated_last_line_dropped"]


# ------------------------------------------------------------- duplicates / sequence
def test_duplicate_record_lines_are_kept_not_silently_dropped():
    t = parse_text(doc(ROW1, ROW1, ROW2))
    assert t.column("RECORD") == [1, 1, 2]     # reader keeps both; dedup is ingest's job (QA-018)


def test_record_and_timestamp_regression_accepted():
    t = parse_text(doc(ROW2, ROW1))
    assert t.column("RECORD") == [2, 1]        # no sequence diagnostics (QA-018)


def test_record_bounds():
    ok = parse_text(doc('"2026-11-02 10:00:00",4294967295,1,"a"', '"2026-11-02 10:15:00",0,1,"a"'))
    assert ok.column("RECORD") == [0xFFFFFFFF, 0]
    for bad in ("4294967296", "-1", "1.0", "", "NAN", "0x10", "1e3"):
        with pytest.raises(Toa5Error):
            parse_text(doc(f'"2026-11-02 10:00:00",{bad},1,"a"'))


# ------------------------------------------------------------- embedded commas / empty fields / quoting
def test_embedded_comma_in_quoted_string_value():
    t = parse_text(doc('"2026-11-02 10:00:00",1,1.5,"a,b,,c"'))
    assert t.records[0]["S1"] == "a,b,,c"


def test_unquoted_thousands_separator_is_field_count_error():
    with pytest.raises(Toa5Error) as ei:
        parse_text(doc('"2026-11-02 10:00:00",1,1,234,"a"'))
    assert ei.value.line == 5


def test_empty_fields():
    t = parse_text(doc('"2026-11-02 10:00:00",1,,""', '"2026-11-02 10:15:00",2,   ,"   "'))
    assert t.records[0]["V1"] is None and t.records[0]["S1"] == ""
    assert t.records[1]["V1"] is None and t.records[1]["S1"] == "   "


@pytest.mark.parametrize("row", [
    '"",1,1.5,"ok"',                              # empty timestamp
    ',1,1.5,"ok"',                                # unquoted empty timestamp
    '"2026-11-02 10:00:00",1,1.5,"ok",',          # trailing comma -> 5 fields
    '"2026-11-02 10:00:00",1,1.5,"ok"x',          # text after quote
    '"2026-11-02 10:00:00",1,1"5,"ok"',           # stray quote
    '"2026-11-02 10:00:00",1,abc,"ok"',           # unquoted text
    '"2026-11-02 10:00:00",1,1.5, "ok"',          # space before quote
])
def test_bad_data_rows(row):
    with pytest.raises(Toa5Error) as ei:
        parse_text(doc(row))
    assert ei.value.line == 5


def test_blank_line_inside_data_is_error_trailing_blank_ok():
    with pytest.raises(Toa5Error) as ei:
        parse_text(doc(ROW1, "", ROW2))
    assert ei.value.line == 6
    assert len(parse_text(doc(ROW1) + "\n  \n\r\n").records) == 1


# ------------------------------------------------------------- NAN / INF [CS-NANINF; CS-CR1000 Table 114]
@pytest.mark.parametrize("tok, chk", [
    ("NAN", math.isnan), ('"NAN"', math.isnan), ("nan", math.isnan),
    ("INF", lambda v: v == math.inf), ("+INF", lambda v: v == math.inf),
    ('"-INF"', lambda v: v == -math.inf), ("-inf", lambda v: v == -math.inf),
])
def test_nan_inf_tokens(tok, chk):
    assert chk(parse_text(doc(f'"2026-11-02 10:00:00",1,{tok},"x"')).records[0]["V1"])


@pytest.mark.parametrize("tok", ["Infinity", "1.#INF", "-NAN", "NaN%", "inf1"])
def test_non_campbell_special_tokens_rejected(tok):
    with pytest.raises(Toa5Error):
        parse_text(doc(f'"2026-11-02 10:00:00",1,{tok},"x"'))


def test_integer_sentinels_left_as_ints():
    t = parse_text(doc('"2026-11-02 10:00:00",1,-7999,"x"', '"2026-11-02 10:15:00",2,-2147483648,"x"'))
    assert t.column("V1") == [-7999, -2147483648]


def test_quoted_string_field_whose_text_is_nan_becomes_float_info():
    # INFO (not a defect by itself): a STRING field holding the text "INF" or "nan" is coerced to a float.
    v = parse_text(doc('"2026-11-02 10:00:00",1,1,"inf"')).records[0]["S1"]
    assert v == math.inf


# ------------------------------------------------------------- TIMESTAMP
@pytest.mark.parametrize("ts, exp", [
    ("2026-11-02 10:00:00", datetime(2026, 11, 2, 10, 0, 0)),
    ("2026-11-02 10:00:00.25", datetime(2026, 11, 2, 10, 0, 0, 250000)),
    ("2024-02-29 23:59:59.123456000", datetime(2024, 2, 29, 23, 59, 59, 123456)),
])
def test_timestamp_ok(ts, exp):
    assert parse_text(doc(f'"{ts}",1,1,"x"')).records[0]["TIMESTAMP"] == exp


@pytest.mark.parametrize("ts", ["2023-02-29 00:00:00", "2026-11-02 24:00:00", "2026-11-02 10:00:60",
                                "2026-11-02T10:00:00", "2026-11-02 10:00", "11/02/2026 10:00:00",
                                "2026-11-02 10:00:00 ", "2026-11-02 10:00:00.1234567", "2026-11-02 10:00:00Z"])
def test_timestamp_bad(ts):
    with pytest.raises(Toa5Error):
        parse_text(doc(f'"{ts}",1,1,"x"'))


# ------------------------------------------------------------- ASCII digits only
# QA-019 fixed (App Dev 2026-11-02 19:3x): strict xfail marker removed, test body unchanged.
@pytest.mark.parametrize("row", [
    '"2026-11-02 10:00:00",\u0661,1,"x"',                                  # Arabic-Indic RECORD
    '"2026-11-02 10:00:00",1,\uff11\uff12,"x"',                            # full-width value
    '"\uff12\uff10\uff12\uff16-11-02 10:00:00",1,1,"x"',                   # full-width year
])
def test_non_ascii_digits_rejected(row):
    with pytest.raises(Toa5Error):
        parse_text(doc(row))


# ------------------------------------------------------------- spec examples, retyped by QA
def test_cr1000_manual_p437_example_as_printed():
    # PDF p437 prints the field name as "battfivoltfiMin" (typesetting artefact); QA keeps it as printed.
    s = ('"TOA5","11467","CR1000","11467","CR1000.Std.20","CPU:file format.CR1","26243","Test"\r\n'
         '"TIMESTAMP","RECORD","battfivoltfiMin","PTemp"\r\n"TS","RN","",""\r\n"","","Min","Smp"\r\n'
         '"2010-12-20 11:31:30",7,13.29,20.77\r\n"2010-12-20 11:31:45",8,13.26,20.77\r\n'
         '"2010-12-20 11:32:00",9,13.29,20.8\r\n')
    t = parse_text(s)
    assert t.environment.program_signature == "26243" and t.environment.table_name == "Test"
    assert t.column("RECORD") == [7, 8, 9] and t.column("PTemp") == [20.77, 20.77, 20.8]
    assert [f.units for f in t.fields] == ["TS", "RN", "", ""]


# ------------------------------------------------------------- end to end: TOA5 Hz -> VW_PIEZO 1.0.0 -> kPa
def _pz03_expected():
    """Expected value of reference_examples PZ-03 (parsed from qa/reference_examples.py, not imported)."""
    tree = ast.parse((ROOT / "qa" / "reference_examples.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and node.elts and isinstance(node.elts[0], ast.Constant) \
                and str(node.elts[0].value).startswith("PZ-03"):
            return ast.literal_eval(node.elts[3]), ast.literal_eval(node.elts[4])
    raise AssertionError("PZ-03 not found in qa/reference_examples.py")


def test_end_to_end_geokon4500_through_vw_piezo_matches_pz03():
    from dam_safety.formulas import core
    from dam_safety.formulas.registry import apply_formula
    exp, tol = _pz03_expected()
    priv = Path(os.environ.get("DAM_QA_PRIVATE", "/workspace/dam-refs/qa_private")) / "piezometer_expected.json"
    if priv.exists():
        assert json.loads(priv.read_text())["p_lin"] == exp
    # QA-chosen frequencies: Hz = sqrt(digits*1000) for R0 = 9082 and R1 = 6749 digits [GEOKON-4500 Eq.1]
    f0, f1 = math.sqrt(9082 * 1000), math.sqrt(6749 * 1000)
    hdr = ['"TOA5","DamQA","CR1000X","1","os","p","1","VW"', '"TIMESTAMP","RECORD","PZ_Hz"', '"TS","RN","Hz"',
           '"","","Smp"']
    t = parse_text(doc(f'"2026-11-02 00:00:00",0,{f0:.6f}', f'"2026-11-02 01:00:00",1,{f1:.6f}', hdr=hdr, eol="\r\n"))
    r0 = core.vw_digits_from_hz(t.records[0]["PZ_Hz"])
    cal = {"formula_id": "VW_PIEZO", "formula_version": "1.0.0", "calib_id": "CAL-QA", "cal_version": 1,
           "params": {"method": "linear", "G": 0.1795, "sign_mult": -1, "R0": r0}}
    out = apply_formula({"raw_value": t.records[1]["PZ_Hz"], "raw_unit": "hz"}, cal)
    assert out["eng_value"] == pytest.approx(exp, abs=tol)
    assert out["eng_value"] == pytest.approx(-0.1795 * (6749 - 9082), abs=1e-4)   # 418.7735, manual p22
    assert out["eng_unit"] == "kPa"
