"""Tests for the clean-room TOA5 reader (src/common/toa5.py).

Spec sources: see the module docstring and docs/TOA5_READER.md. The fixtures are hand-written from the
Campbell documentation examples. No third-party parser output is used."""
import math
from datetime import datetime

import pytest

from common import toa5
from common.toa5 import Toa5Error, parse_text, read_toa5

# Verbatim example from the LoggerNet manual "TOA5" page [CS-LN-TOA5].
LN_EXAMPLE = (
    '"TOA5","CR1000","CR1000","1031","CR1000.Std.00.60","CPU:Test.CR1","4062","Test"\n'
    '"TIMESTAMP","RECORD","batt_volt_Min","PTemp"\n'
    '"TS","RN","Volts","C"\n'
    '"","","Min","Smp"\n'
    '"2004-11-11 15:03:45",0,13.7,24.92\n'
    '"2004-11-11 15:04:00",1,13.7,24.95\n'
    '"2004-11-11 15:04:15",2,13.7,24.98\n'
)
# Example from the CR1000 manual §8 "TOA5" (PDF p.437) [CS-CR1000]: the units line has empty strings.
# QA-016: NORMALISED, not verbatim. The PDF prints the third field name as "battfivoltfiMin" (a typesetting
# artefact for "batt_volt_Min"), and this fixture uses "batt_volt_Min". The as-printed form is tested in
# qa/test_toa5_adversarial.py::test_cr1000_manual_p437_example_as_printed. Values and other fields are as printed.
CR1000_EXAMPLE = (
    '"TOA5","11467","CR1000","11467","CR1000.Std.20","CPU:file format.CR1","26243","Test"\r\n'
    '"TIMESTAMP","RECORD","batt_volt_Min","PTemp"\r\n'
    '"TS","RN","",""\r\n'
    '"","","Min","Smp"\r\n'
    '"2010-12-20 11:31:30",7,13.29,20.77\r\n'
    '"2010-12-20 11:31:45",8,13.26,20.77\r\n'
    '"2010-12-20 11:32:00",9,13.29,20.8\r\n'
)
ENV = '"TOA5","Dam1","CR1000X","1234","CR1000X.Std.07","CPU:dam.CR1X","555","Piezo"\n'


def mk(names, units, procs, *rows, env=ENV):
    q = lambda xs: ",".join(f'"{x}"' for x in xs)
    return env + q(names) + "\n" + q(units) + "\n" + q(procs) + "\n" + "".join(r + "\n" for r in rows)


# ---------------------------------------------------------------- 4 header lines
def test_loggernet_example_header_and_records():
    t = parse_text(LN_EXAMPLE)
    e = t.environment
    assert (e.station_name, e.logger_model, e.serial_number, e.os_version, e.program_name,
            e.program_signature, e.table_name) == (
        "CR1000", "CR1000", "1031", "CR1000.Std.00.60", "CPU:Test.CR1", "4062", "Test")
    assert t.names == ("TIMESTAMP", "RECORD", "batt_volt_Min", "PTemp")
    assert [f.units for f in t.fields] == ["TS", "RN", "Volts", "C"]
    assert [f.processing for f in t.fields] == ["", "", "Min", "Smp"]
    assert t.field("PTemp") == toa5.Toa5Field("PTemp", "C", "Smp")
    assert len(t.records) == 3
    assert t.records[0] == {"TIMESTAMP": datetime(2004, 11, 11, 15, 3, 45), "RECORD": 0,
                            "batt_volt_Min": 13.7, "PTemp": 24.92}
    assert t.column("RECORD") == [0, 1, 2]
    assert t.column("PTemp") == [24.92, 24.95, 24.98]


def test_cr1000_example_crlf_and_empty_units():
    t = parse_text(CR1000_EXAMPLE)
    assert t.environment.logger_model == "CR1000"
    assert t.environment.program_signature == "26243"
    assert [f.units for f in t.fields] == ["TS", "RN", "", ""]
    assert t.column("RECORD") == [7, 8, 9]
    assert t.records[2]["TIMESTAMP"] == datetime(2010, 12, 20, 11, 32, 0)


def test_read_file_with_bom(tmp_path):
    p = tmp_path / "x.dat"
    p.write_bytes(b"\xef\xbb\xbf" + LN_EXAMPLE.encode())
    t = read_toa5(p)
    assert t.environment.station_name == "CR1000" and len(t.records) == 3
    t2 = read_toa5(str(p), encoding="utf-8-sig")
    assert t2 == t


def test_read_file_strict_decoding(tmp_path):
    p = tmp_path / "bad.dat"
    p.write_bytes(LN_EXAMPLE.encode() + b'"2004-11-11 15:04:30",3,13.7,"\xff"\n')
    with pytest.raises(UnicodeDecodeError):
        read_toa5(p)


def test_array_field_names_and_header_only_file():
    t = parse_text(mk(["TIMESTAMP", "RECORD", "Values(1,1)", "Values(1,2)"], ["TS", "RN", "", ""],
                      ["", "", "Smp", "Smp"]))
    assert t.names[2:] == ("Values(1,1)", "Values(1,2)")
    assert t.records == ()


def test_optional_timestamp_and_record():
    t = parse_text(mk(["A", "B"], ["V", "C"], ["Avg", "Smp"], "1.5,2"))
    assert t.records == ({"A": 1.5, "B": 2},)
    t = parse_text(mk(["RECORD", "A"], ["RN", "V"], ["", "Smp"], "4,1.0"))
    assert t.records[0] == {"RECORD": 4, "A": 1.0}
    t = parse_text(mk(["TIMESTAMP", "A"], ["TS", "V"], ["", "Smp"], '"2026-01-02 03:04:05",1'))
    assert t.records[0]["TIMESTAMP"] == datetime(2026, 1, 2, 3, 4, 5)


def test_field_and_column_unknown_name():
    t = parse_text(LN_EXAMPLE)
    with pytest.raises(KeyError):
        t.field("nope")
    with pytest.raises(KeyError):
        t.column("nope")


# ---------------------------------------------------------------- strict header errors
@pytest.mark.parametrize("text,line,msg", [
    ("", 1, "inside the 4-line header (0"),
    ('"TOA5","a","b","c","d","e","f","g"\n"A"\n"V"\n', 4, "(3 line(s)"),
    ('"TOA5","a","b","c","d","e","f"\n"A"\n"V"\n"Smp"\n', 1, "8 fields, found 7"),
    ('"TOA5","a","b","c","d","e","f","g",""\n"A"\n"V"\n"Smp"\n', 1, "8 fields, found 9"),
    ('"TOB1","a","b","c","d","e","f","g"\n"A"\n"V"\n"Smp"\n', 1, "expected 'TOA5'"),
    ('"toa5","a","b","c","d","e","f","g"\n"A"\n"V"\n"Smp"\n', 1, "expected 'TOA5'"),
    (ENV + '\n"V"\n"Smp"\n', 2, "empty field name"),
    (ENV + '"A",""\n"V","V"\n"Smp","Smp"\n', 2, "empty field name"),
    (ENV + '"A","B","A"\n"V","V","V"\n"Smp","Smp","Smp"\n', 2, "duplicate field names ['A']"),
    (ENV + '"A","B"\n"V"\n"Smp","Smp"\n', 3, "units line has 1 fields"),
    (ENV + '"A","B"\n"V","V"\n"Smp"\n', 4, "processing line has 1 fields"),
    (ENV + '"A","TIMESTAMP"\n"V","TS"\n"Smp",""\n', 2, "TIMESTAMP must be the first"),
    (ENV + '"TIMESTAMP","A","RECORD"\n"TS","V","RN"\n"","Smp",""\n', 2, "RECORD must directly follow"),
    (ENV + '"A","RECORD"\n"V","RN"\n"Smp",""\n', 2, "RECORD must directly follow"),
    (ENV + '"TIMESTAMP","A"\n"TS","TS"\n"","Smp"\n', 3, "'TS' on non-TIMESTAMP field 'A'"),
    (ENV + '"A","B"\n"RN","V"\n"Smp","Smp"\n', 3, "'RN' on non-RECORD field 'A'"),
    ('"TOA5","a,b","c""d"x,"e","f","g","h","i"\n"A"\n"V"\n"Smp"\n', 1, "unexpected text after closing quote"),
    ('"TOA5","a\n"A"\n"V"\n"Smp"\n', 1, "unterminated quoted field"),
    ('"TOA5",a"b,"c","d","e","f","g","h"\n"A"\n"V"\n"Smp"\n', 1, "stray quote"),
])
def test_malformed_header_raises(text, line, msg):
    with pytest.raises(Toa5Error) as ei:
        parse_text(text)
    assert ei.value.line == line
    assert msg in str(ei.value)
    assert str(ei.value).startswith(f"TOA5 line {line}: ")


# ---------------------------------------------------------------- quoting / splitting
def test_split_line_quotes_commas_escapes_and_trailing_empty():
    s = toa5.split_line
    assert s('"a,b","say ""hi""",3,,"",4', 9) == [("a,b", True), ('say "hi"', True), ("3", False),
                                                ("", False), ("", True), ("4", False)]
    assert s("1,2,", 9) == [("1", False), ("2", False), ("", False)]
    assert s("", 9) == [("", False)]
    assert s(' 1 , 2 ', 9) == [("1", False), ("2", False)]
    assert s('"x",', 9) == [("x", True), ("", False)]


def test_quoted_string_values_preserved():
    t = parse_text(mk(["A", "S"], ["", ""], ["Smp", "Smp"], '1,"hello, world"', '2,"12.5"', '3,""'))
    assert t.column("S") == ["hello, world", "12.5", ""]


# ---------------------------------------------------------------- NAN / INF / coercion
def test_nan_inf_and_type_coercion():
    t = parse_text(mk(["A", "B", "C", "D", "E", "F", "G"], [""] * 7, ["Smp"] * 7,
                      'NAN,INF,-INF,"NAN",+INF,nan,-7999',
                      '1,-2,+3,1.5e3,.5,-0.25,'))
    r0, r1 = t.records
    assert math.isnan(r0["A"]) and r0["B"] == math.inf and r0["C"] == -math.inf
    assert math.isnan(r0["D"]) and r0["E"] == math.inf and math.isnan(r0["F"])
    assert r0["G"] == -7999 and isinstance(r0["G"], int)       # FP2 sentinel NOT converted (documented)
    assert r1 == {"A": 1, "B": -2, "C": 3, "D": 1500.0, "E": 0.5, "F": -0.25, "G": None}
    assert all(isinstance(r1[k], int) for k in "ABC")
    assert toa5.FP2_NAN_SENTINEL == -7999 and toa5.LONG_NAN_SENTINEL == -2147483648


def test_coerce_value_direct():
    c = toa5.coerce_value
    assert c("12", True) == "12"
    assert c("12", False) == 12
    assert c("1.", False) == 1.0
    assert math.isnan(c(" NaN ", False))
    with pytest.raises(Toa5Error, match="unquoted non-numeric value 'abc'"):
        c("abc", False, 7)
    for bad in ("1.2.3", "e5", "--1", "0x10"):
        with pytest.raises(Toa5Error):
            c(bad, False)


# ---------------------------------------------------------------- timestamps
@pytest.mark.parametrize("text,expect", [
    ("2004-11-11 15:03:45", datetime(2004, 11, 11, 15, 3, 45)),
    ("2004-11-11 15:03:45.5", datetime(2004, 11, 11, 15, 3, 45, 500000)),
    ("2004-11-11 15:03:45.125", datetime(2004, 11, 11, 15, 3, 45, 125000)),
    ("2004-11-11 15:03:45.123456", datetime(2004, 11, 11, 15, 3, 45, 123456)),
    ("2004-11-11 15:03:45.123456000", datetime(2004, 11, 11, 15, 3, 45, 123456)),
    ("2024-02-29 00:00:00", datetime(2024, 2, 29)),
])
def test_timestamp_ok(text, expect):
    got = toa5.parse_timestamp(text)
    assert got == expect and got.tzinfo is None


@pytest.mark.parametrize("text,msg", [
    ("2004-11-11T15:03:45", "is not"),
    ("11/11/2004 15:03", "is not"),
    ("2004-11-11 15:03", "is not"),
    ("2004-11-11 15:03:45.1234567", "sub-microsecond"),
    ("2023-02-29 00:00:00", "invalid timestamp"),
    ("2004-11-11 24:00:00", "invalid timestamp"),
    ("", "is not"),
])
def test_timestamp_bad(text, msg):
    with pytest.raises(Toa5Error, match=msg):
        toa5.parse_timestamp(text, 5)


# ---------------------------------------------------------------- data-line errors
@pytest.mark.parametrize("row,line,msg", [
    ('"2004-11-11 15:03:45",0,13.7', 5, "data line has 3 fields, expected 4"),
    ('"2004-11-11 15:03:45",0,13.7,1,2', 5, "has 5 fields"),
    ('"2004/11/11 15:03:45",0,13.7,1', 5, "is not 'YYYY-MM-DD"),
    ('"2004-11-11 15:03:45",-1,13.7,1', 5, "RECORD '-1'"),
    ('"2004-11-11 15:03:45",1.0,13.7,1', 5, "RECORD '1.0'"),
    ('"2004-11-11 15:03:45",4294967296,13.7,1', 5, "RECORD '4294967296'"),
    ('"2004-11-11 15:03:45",NAN,13.7,1', 5, "RECORD 'NAN'"),
    ('"2004-11-11 15:03:45",0,volts,1', 5, "unquoted non-numeric"),
    ('"2004-11-11 15:03:45",0,13.7,"1', 5, "unterminated"),
])
def test_bad_data_lines(row, line, msg):
    with pytest.raises(Toa5Error, match=msg) as ei:
        parse_text(LN_EXAMPLE.split('"2004-11-11 15:03:45",0')[0] + row + "\n")
    assert ei.value.line == line


def test_record_max_and_rollover_accepted():
    hdr = LN_EXAMPLE.split('"2004-11-11 15:03:45",0')[0]
    t = parse_text(hdr + '"2004-11-11 15:03:45",4294967295,1,1\n"2004-11-11 15:04:00",0,1,1\n')
    assert t.column("RECORD") == [toa5.RECORD_MAX, 0]


def test_trailing_blank_lines_ok_but_inner_blank_line_is_error():
    assert len(parse_text(LN_EXAMPLE + "\n  \n\r\n").records) == 3
    lines = LN_EXAMPLE.splitlines(keepends=True)
    with pytest.raises(Toa5Error, match="blank line inside data") as ei:
        parse_text("".join(lines[:5]) + "\n" + "".join(lines[5:]))
    assert ei.value.line == 6


# ---------------------------------------------------------------- end-to-end (open-source review checks)
# Geokon 4500 manual (ref GEOKON-4500, /workspace/dam-refs/4500_Piezometer.pdf), §6.1 Eq.1
# digits = Hz^2/1000 and §6.1.1 Eq.2 P = G(R1 - R0). The worked example (PDF p.22) uses R0 = 9082 digits,
# R1 = 6749 digits and G = -0.1795 kPa/digit, giving P = 418.8 kPa. Frequencies are chosen so that Eq.1 gives
# those readings: sqrt(9082.15*1000) = 3013.66 Hz and sqrt(6749*1000) = 2597.88 Hz.
VW_TOA5 = (
    '"TOA5","DamA_PZ","CR1000X","4321","CR1000X.Std.07.01","CPU:piezo.CR1X","12345","VW_Hourly"\n'
    '"TIMESTAMP","RECORD","PZ1_Freq","PZ1_Therm"\n'
    '"TS","RN","Hz","Deg C"\n'
    '"","","Smp","Smp"\n'
    '"2026-10-01 00:00:00",0,3013.66,12.1\n'
    '"2026-10-01 01:00:00",1,2597.88,12.1\n'
    '"2026-10-01 02:00:00",2,NAN,12.1\n'
)


def test_review_check_parse_and_logger_model():
    t = parse_text(VW_TOA5)
    assert t.environment.logger_model == "CR1000X"
    assert t.environment.table_name == "VW_Hourly"
    assert t.field("PZ1_Freq").units == "Hz"
    assert t.column("RECORD") == [0, 1, 2]
    assert math.isnan(t.records[2]["PZ1_Freq"])


def test_review_check_digits_and_kpa_eq1_eq2_418_8():
    from dam_safety.formulas import core
    t = parse_text(VW_TOA5)
    hz = t.column("PZ1_Freq")
    r0 = core.vw_digits_from_hz(hz[0])                     # Eq.1
    r1 = core.vw_digits_from_hz(hz[1])
    assert r0 == pytest.approx(9082.15, abs=0.01)
    assert r0 == pytest.approx(hz[0] ** 2 / 1000, rel=1e-12)
    assert r1 == pytest.approx(6749.0, abs=0.05)
    p = core.vw_pressure_linear(r1, r0, -0.1795)          # Eq.2, manual's G
    assert p == pytest.approx(418.8, abs=0.05)
    assert round(p, 1) == 418.8
    # the manual's own worked example, digits in directly
    assert core.vw_pressure_linear(6749, 9082, -0.1795) == pytest.approx(418.7735, abs=1e-9)


def test_review_check_end_to_end_through_engine_formula():
    """TOA5 Hz column -> engine VW_PIEZO 1.0.0 (raw_unit hz) -> kPa. R0 is from the baseline row's digits."""
    from dam_safety.formulas import core
    from dam_safety.formulas.registry import apply_formula
    t = parse_text(VW_TOA5)
    r0 = core.vw_digits_from_hz(t.records[0]["PZ1_Freq"])
    cal = {"formula_id": "VW_PIEZO", "formula_version": "1.0.0", "calib_id": "CAL-T", "cal_version": 1,
           "params": {"method": "linear", "G": 0.1795, "sign_mult": -1, "R0": r0}}
    out = apply_formula({"raw_value": t.records[1]["PZ1_Freq"], "raw_unit": "hz"}, cal)
    assert out["eng_value"] == pytest.approx(418.8, abs=0.05)
    assert out["eng_unit"] == "kPa"
    assert (out["formula_id"], out["cal_version"]) == ("VW_PIEZO", 1)


# ---------------------------------------------------------------- QA-017 unterminated last line
TRUNC_HDR = mk(["TIMESTAMP", "RECORD", "V1", "V2"], ["TS", "RN", "", ""], ["", "", "Smp", "Smp"],
               '"2026-11-02 10:00:00",1,1.5,2.5')


@pytest.mark.parametrize("last", ['"2026-11-02 10:15:00",2,1.5,2', '"2026-11-02 10:15:00",2,1.5,'])
def test_unterminated_last_line_rejected_by_default(last):
    with pytest.raises(Toa5Error, match="no line terminator") as ei:
        parse_text(TRUNC_HDR + last)
    assert ei.value.line == 6


@pytest.mark.parametrize("last", ['"2026-11-02 10:15:00",2,1.5,2', '"2026-11-02 10:15:00",2,1.5,'])
def test_unterminated_last_line_dropped_with_warning(last):
    t = parse_text(TRUNC_HDR + last, on_unterminated="drop")
    assert t.column("V2") == [2.5]
    assert t.warnings == (toa5.Toa5Warning(6, "unterminated_last_line_dropped",
                                           "last data line had no line terminator and was dropped"),)


def test_unterminated_policy_via_read_toa5_and_crlf(tmp_path):
    p = tmp_path / "t.dat"
    p.write_bytes((TRUNC_HDR.replace("\n", "\r\n") + '"2026-11-02 10:15:00",2,1.5,2').encode())
    with pytest.raises(Toa5Error, match="no line terminator"):
        read_toa5(p)
    assert read_toa5(p, on_unterminated="drop").column("RECORD") == [1]


def test_terminated_files_and_unterminated_trailing_blank_have_no_warnings():
    assert parse_text(TRUNC_HDR).warnings == ()
    assert parse_text(TRUNC_HDR + "   ").column("RECORD") == [1]
    assert parse_text(CR1000_EXAMPLE).warnings == ()      # CRLF counts as terminated


@pytest.mark.parametrize("text", [ENV.rstrip("\n"), ENV + '"A"\n"V"\n"Sm', ENV + '"A"\n"V"\n"Smp"'])
def test_unterminated_header_line_always_error(text):
    for pol in ("error", "drop"):
        with pytest.raises(Toa5Error, match="header line has no line terminator"):
            parse_text(text, on_unterminated=pol)


def test_unterminated_policy_validated():
    with pytest.raises(ValueError, match="on_unterminated must be one of"):
        parse_text(LN_EXAMPLE, on_unterminated="keep")


# ---------------------------------------------------------------- QA-018 sequence warnings
def _seq(*pairs, names=("TIMESTAMP", "RECORD", "V"), units=("TS", "RN", "")):
    rows = []
    for p in pairs:
        cells = []
        for n, v in zip(names, p):
            cells.append(f'"2026-11-02 10:{v:02d}:00"' if n == "TIMESTAMP" else str(v))
        rows.append(",".join(cells + ["1"] * (len(names) - len(p))))
    return parse_text(mk(list(names), list(units), ["Smp"] * len(names), *rows))


def _codes(t):
    return [(w.line, w.code) for w in t.warnings]


def test_clean_sequence_no_warnings():
    assert _seq((0, 0), (1, 1), (2, 5)).warnings == ()      # gaps are not warned


def test_duplicate_record_and_timestamp_kept_and_warned():
    t = _seq((0, 0), (1, 1), (1, 1), (2, 2))
    assert t.column("RECORD") == [0, 1, 1, 2]                 # data kept as read
    assert _codes(t) == [(7, "record_duplicate"), (7, "timestamp_duplicate")]
    assert "RECORD 1 already seen" in t.warnings[0].message
    assert "TIMESTAMP 2026-11-02 10:01:00 already seen" in t.warnings[1].message


def test_out_of_order_record_and_timestamp_warned():
    t = _seq((0, 5), (1, 7), (2, 6), (3, 8))                   # (minute, RECORD)
    assert _codes(t) == [(7, "record_out_of_order")]
    assert t.warnings[0].message == "RECORD 6 after 7 (not a rollover)"
    t = _seq((0, 0), (5, 1), (3, 2))
    assert _codes(t) == [(7, "timestamp_out_of_order")]
    assert "TIMESTAMP 2026-11-02 10:03:00 after 2026-11-02 10:05:00" == t.warnings[0].message


def test_older_duplicate_reported_as_duplicate_not_out_of_order():
    t = _seq((0, 0), (1, 1), (2, 2), (3, 0))
    assert _codes(t) == [(8, "record_duplicate")]


@pytest.mark.parametrize("top", [toa5.RECORD_MAX, toa5.RECORD_MAX_REPORTED])
def test_record_rollover_to_zero_not_warned_and_restarts_sequence(top):
    t = _seq((0, top - 1), (1, top), (2, 0), (3, 1))
    assert t.column("RECORD") == [top - 1, top, 0, 1]
    assert t.warnings == ()


def test_reset_to_zero_not_from_max_is_warned():
    t = _seq((0, 41), (1, 42), (2, 0))
    assert _codes(t) == [(7, "record_out_of_order")]


def test_sequence_without_record_or_timestamp_columns():
    t = _seq((0,), (0,), names=("TIMESTAMP", "V"), units=("TS", ""))
    assert _codes(t) == [(6, "timestamp_duplicate")]
    t = parse_text(mk(["RECORD", "V"], ["RN", ""], ["", "Smp"], "3,1", "2,1"))
    assert _codes(t) == [(6, "record_out_of_order")]
    assert parse_text(mk(["A"], [""], ["Smp"], "1", "1")).warnings == ()


def test_warnings_sorted_by_line_with_drop():
    t = parse_text(TRUNC_HDR + '"2026-11-02 10:00:00",1,1,1\n"2026-11-02 10:30:00",3,1,1', on_unterminated="drop")
    assert _codes(t) == [(6, "record_duplicate"), (6, "timestamp_duplicate"), (7, "unterminated_last_line_dropped")]


# ---------------------------------------------------------------- QA-019 ASCII digits, unsigned RECORD
@pytest.mark.parametrize("row,msg", [
    ('"2026-11-02 10:00:00",\u0661,1', "RECORD"),
    ('"2026-11-02 10:00:00",+5,1', "RECORD '\\+5'"),
    ('"2026-11-02 10:00:00",-0,1', "RECORD '-0'"),
    ('"2026-11-02 10:00:00",1,\uff11\uff12', "unquoted non-numeric"),
    ('"2026-11-02 10:00:00",1,1.\u0665', "unquoted non-numeric"),
    ('"2026-11-02 10:00:00",1,1e\u0663', "unquoted non-numeric"),
    ('"\uff12\uff10\uff12\uff16-11-02 10:00:00",1,1', "is not 'YYYY-MM-DD"),
    ('"2026-11-02 10:00:00.\u0661",1,1', "is not 'YYYY-MM-DD"),
])
def test_non_ascii_digits_and_signed_record_rejected(row, msg):
    with pytest.raises(Toa5Error, match=msg):
        parse_text(mk(["TIMESTAMP", "RECORD", "V"], ["TS", "RN", ""], ["", "", "Smp"], row))


def test_ascii_signed_values_still_ok_and_record_leading_zero():
    t = parse_text(mk(["TIMESTAMP", "RECORD", "V", "W"], ["TS", "RN", "", ""], ["", "", "Smp", "Smp"],
                      '"2026-11-02 10:00:00",007,+5,-0'))
    assert t.records[0]["RECORD"] == 7 and t.records[0]["V"] == 5 and t.records[0]["W"] == 0
