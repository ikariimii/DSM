"""sign_mult / offset calibration columns.
Sources: /workspace/dam-refs/CONVENTIONS.md ('Calibration table additionally has: sign_mult INT
(+1|-1), offset ... G is ONLY the gauge factor'); /workspace/dam-refs/GEOKON-4500 manual §6.1.1
worked example (G = -0.1795 psi/digit, R0 9082, R1 6749 -> 418.8 psi... as used in test_formulas_vw)."""
import pytest

from dam_safety.calibration import payload_hash
from dam_safety.formulas import core, registry

CAL = {"formula_id": "VW_PIEZO", "formula_version": "1.0.0", "calib_id": "c1", "cal_version": 1}
RD = {"raw_value": 6749, "raw_unit": "digits"}


def _p(**kw):
    return {"method": "linear", "R0": 9082, **kw}


def test_manufacturer_negative_G_equals_abs_G_with_sign_mult_minus_one():
    # source: GEOKON-4500 §6.1.1 (manufacturer form, G<0) vs CONVENTIONS.md (|G|, sign_mult -1)
    manuf = core.vw_piezometer(6749, {"method": "linear", "G": -0.1795, "R0": 9082})
    conv = registry.apply_formula(RD, {**CAL, "params": _p(G=0.1795), "sign_mult": -1, "offset": 0.0})
    assert conv["eng_value"] == pytest.approx(manuf.pressure_kpa, abs=1e-9)
    assert conv["eng_value"] == pytest.approx(418.8, abs=0.05)   # registry vector (GEOKON-4500 §6.1.1: 2333 digits x 0.1795)


def test_negative_G_rejected_at_calibration_level():
    # source: CONVENTIONS.md 'never overload G or A for sign or offset'
    with pytest.raises(ValueError, match="gauge factor only"):
        registry.apply_formula(RD, {**CAL, "params": _p(G=-0.1795), "sign_mult": 1})


@pytest.mark.parametrize("bad", [0, 2, -2, 0.5, "x"])
def test_sign_mult_must_be_plus_or_minus_one(bad):
    # source: CONVENTIONS.md 'sign_mult INT (+1|-1)'
    with pytest.raises(ValueError):
        core.check_sign_mult(bad)


def test_offset_added_after_sign_mult_in_eng_units():
    # source: CONVENTIONS.md 'offset (value in eng units added after sign_mult)'
    base = registry.apply_formula(RD, {**CAL, "params": _p(G=0.1795), "sign_mult": -1, "offset": 0.0})
    off = registry.apply_formula(RD, {**CAL, "params": _p(G=0.1795), "sign_mult": -1, "offset": 2.5})
    assert off["eng_value"] - base["eng_value"] == pytest.approx(2.5, abs=1e-9)
    assert core.apply_sign_offset(10.0, -1, 2.5) == pytest.approx(-7.5)   # -1*10 + 2.5, not -(10+2.5)


def test_column_wins_over_params_key():
    # source: CONVENTIONS.md (sign_mult/offset are calibration-table columns)
    out = registry.apply_formula(RD, {**CAL, "params": _p(G=0.1795, sign_mult=1), "sign_mult": -1})
    assert out["eng_value"] > 0


def test_payload_hash_changes_with_sign_or_offset():
    # source: CONVENTIONS.md; a sign/offset correction must create a new SCD2 version -> recompute
    h = payload_hash({"G": 0.1795}, "VW_PIEZO", "1.0.0", 1, 0.0)
    assert h != payload_hash({"G": 0.1795}, "VW_PIEZO", "1.0.0", -1, 0.0)
    assert h != payload_hash({"G": 0.1795}, "VW_PIEZO", "1.0.0", 1, 0.1)


def test_standpipe_rejects_undefined_sign_offset():
    # source: CONVENTIONS.md defines sign_mult for gauge terms only; standpipe use UNVERIFIED
    with pytest.raises(ValueError):
        registry.apply_formula({"raw_value": 3.0, "raw_unit": "m"},
                               {"formula_id": "STANDPIPE_DTW", "formula_version": None,
                                "params": {"top_el_m": 100.0}, "sign_mult": -1})
