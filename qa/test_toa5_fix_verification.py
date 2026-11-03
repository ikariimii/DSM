"""QA independent verification of App Dev's QA-017/018/019 fixes (2026-11-02 19:4x). Hand-written fixtures."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from common.toa5 import Toa5Error, parse_text, read_toa5  # noqa: E402

ENV = '"TOA5","QA_Stn","CR1000X","77","CR1000X.Std.07","CPU:qa.CR1X","999","T1"'
HDR = [ENV, '"TIMESTAMP","RECORD","V1"', '"TS","RN",""', '"","","Smp"']


def doc(*rows, final_eol=True, eol="\n"):
    s = eol.join(HDR + list(rows))
    return s + eol if final_eol else s


def r(ts, rec, v="1.5"):
    return f'"2026-11-02 {ts}",{rec},{v}'


def codes(t):
    return [w.code for w in t.warnings]


# QA-017
@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_unterminated_last_line_rejected_any_eol(eol):
    with pytest.raises(Toa5Error):
        parse_text(doc(r("10:00:00", 1), r("10:15:00", 2, "2"), final_eol=False, eol=eol))


def test_drop_policy_drops_only_last_line_and_warns_with_line_number():
    t = parse_text(doc(r("10:00:00", 1), r("10:15:00", 2, "2"), final_eol=False), on_unterminated="drop")
    assert t.column("RECORD") == [1] and [(w.line, w.code) for w in t.warnings] == [(6, "unterminated_last_line_dropped")]


def test_terminated_file_unaffected_and_no_warnings():
    t = parse_text(doc(r("10:00:00", 1), r("10:15:00", 2, "2.5")))
    assert t.column("V1") == [1.5, 2.5] and t.warnings == ()


def test_unterminated_header_only_file_is_error():
    with pytest.raises(Toa5Error):
        parse_text("\n".join(HDR), on_unterminated="drop")


def test_read_toa5_file_default_rejects(tmp_path):
    p = tmp_path / "x.dat"
    p.write_bytes(doc(r("10:00:00", 1), final_eol=False).encode())
    with pytest.raises(Toa5Error):
        read_toa5(p)


def test_bad_policy_value():
    with pytest.raises(ValueError):
        parse_text(doc(r("10:00:00", 1)), on_unterminated="keep")


# QA-018
def test_duplicate_record_and_timestamp_warned_data_kept():
    t = parse_text(doc(r("10:00:00", 1), r("10:00:00", 1)))
    assert t.column("RECORD") == [1, 1] and set(codes(t)) == {"record_duplicate", "timestamp_duplicate"}


def test_out_of_order_warned():
    t = parse_text(doc(r("10:15:00", 2), r("10:00:00", 1)))
    assert set(codes(t)) == {"record_out_of_order", "timestamp_out_of_order"}


@pytest.mark.parametrize("top", [0xFFFFFFFF, 0x7FFFFFFF])
def test_documented_rollover_not_warned(top):
    t = parse_text(doc(r("10:00:00", top), r("10:15:00", 0), r("10:30:00", 1)))
    assert t.warnings == ()


def test_reset_to_zero_not_at_max_is_warned():
    t = parse_text(doc(r("10:00:00", 500), r("10:15:00", 0)))
    assert codes(t) == ["record_out_of_order"]


def test_gap_is_not_warned():
    t = parse_text(doc(r("10:00:00", 1), r("10:15:00", 5)))
    assert t.warnings == ()


# QA-019
@pytest.mark.parametrize("bad", ["+5", "-0", "\u0661", "\u0035\u0301"])
def test_record_sign_and_unicode_rejected(bad):
    with pytest.raises(Toa5Error):
        parse_text(doc(f'"2026-11-02 10:00:00",{bad},1'))


@pytest.mark.parametrize("val", ["\u0661.5", "1.\u0665", "1e\u0662", "\u2212" "1"])
def test_value_unicode_digits_rejected(val):
    with pytest.raises(Toa5Error):
        parse_text(doc(f'"2026-11-02 10:00:00",1,{val}'))


def test_signed_values_still_accepted():
    t = parse_text(doc(r("10:00:00", 1, "-2.5"), r("10:15:00", 2, "+3")))
    assert t.column("V1") == [-2.5, 3]
