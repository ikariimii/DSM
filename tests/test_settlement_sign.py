"""Settlement sign convention: stored positive-down, raw up-positive kept in raw_value.
Sources: /workspace/dam-refs/CONVENTIONS.md 'Sign conventions'; /workspace/dam-refs/procedures/
settlement-survey-gnss.md §0; /workspace/dam-refs/settlement-survey/verify.py Ex14
(raw_up_mm [0, -4.2, -6.9, 1.1], sign_mult -1 -> +6.9 mm; raw round-trips) and Ex1
(GEOKON-4660 §4.1 Eq1 PDF p20: E0 541.62, R0 9030, R1 8800, G -0.001765, dE_res -0.010 ->
E 541.204 m, settlement +0.416 m)."""
import pytest

from dam_safety.datum import rebaseline_series
from dam_safety.formulas import registry

RAW_UP = [0.0, -4.2, -6.9, 1.1]


def _rows(vals, d="D1"):
    return [{"ts_utc": i, "raw_value": v, "datum_id": d} for i, v in enumerate(vals)]


def test_ex14_up_positive_source_stored_settlement_positive():
    out, _ = rebaseline_series(_rows(RAW_UP), sign_mult=-1, raw_unit="mm")
    stored = [o["eng_value"] for o in out]
    assert stored[2] == pytest.approx(6.9, abs=1e-12)
    assert stored[3] == pytest.approx(-1.1, abs=1e-12)          # heave negative
    assert [-1 * s + RAW_UP[0] for s in stored] == pytest.approx(RAW_UP, abs=1e-12)   # round-trip


def test_ex1_geokon_4660_elevation_to_settlement():
    # manual Eq1 with manufacturer G<0 gives elevation; SETTLEMENT_DELTA of an elevation (up) series
    E0, R0, R1, G, dE = 541.62, 9030, 8800, -0.001765, -0.010
    E = E0 - ((R1 - R0) * G) + dE
    assert E == pytest.approx(541.204, abs=0.0005)
    out, _ = rebaseline_series(_rows([E0, E]), sign_mult=-1, raw_unit="m")
    assert out[1]["eng_value"] == pytest.approx(415.95, abs=0.01)    # +down, mm
    assert out[1]["eng_value"] / 1000 == pytest.approx(0.416, abs=0.0005)


def test_settlement_positive_source_keeps_sign():
    out, _ = rebaseline_series(_rows([0.0, 4.2]), sign_mult=1, raw_unit="mm")
    assert out[1]["eng_value"] == pytest.approx(4.2)


@pytest.mark.parametrize("axis,sm", [("up", 1), ("down", -1)])
def test_inconsistent_axis_and_sign_rejected(axis, sm):
    with pytest.raises(ValueError):
        registry.apply_formula({"raw_value": 1.0, "raw_unit": "mm"},
                               {"formula_id": "SETTLEMENT_DELTA", "formula_version": "1.0.0",
                                "params": {"raw_axis": axis}, "sign_mult": sm})
