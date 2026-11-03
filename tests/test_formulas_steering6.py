"""Steering 6: engine formulas the app displays. Each case cites its /workspace/dam-refs source.
Values are the published examples (also checked by /workspace/dam-refs/verify_examples.py)."""
import pytest

from dam_safety.formulas import registry as R


def run(fid, reading, params, **cal):
    return R.apply_formula(reading, {"formula_id": fid, "formula_version": "1.0.0", "params": params, **cal})


# USBR_Water_Measurement_Manual.pdf Ch.7 Eq.7-6 and Table A7-4 (verify_examples weir_cone_90V_*)
@pytest.mark.parametrize("h_ft,q_cfs,tol", [(1.0, 2.49, 0.005), (0.5, 0.446, 0.002), (1.5, 6.81, 0.02)])
def test_weir_vnotch_usbr_table(h_ft, q_cfs, tol):
    out = run("WEIR_VNOTCH", {"raw_value": h_ft, "raw_unit": "ft"}, {})
    assert out["eng_value"] == pytest.approx(q_cfs, abs=tol)
    assert out["corrected_value"] == pytest.approx(out["eng_value"] * 28.316846592)
    assert out["corrected_unit"] == "L/s"


def test_weir_si_input_and_low_head():
    # same head given in metres must give the same Q (WMM: compute in ft, convert after)
    a = run("WEIR_VNOTCH", {"raw_value": 1.0, "raw_unit": "ft"}, {})
    b = run("WEIR_VNOTCH", {"raw_value": 0.3048, "raw_unit": "m"}, {})
    assert a["eng_value"] == pytest.approx(b["eng_value"], rel=1e-12)
    # USBR WMM Ch.7: 'h1 should be at least 0.2 ft' -> flagged
    assert run("WEIR_VNOTCH", {"raw_value": 0.1, "raw_unit": "ft"}, {})["low_head"] is True
    assert run("WEIR_VNOTCH", {"raw_value": -0.01, "raw_unit": "ft"}, {})["eng_value"] == 0.0
    with pytest.raises(ValueError):
        run("WEIR_VNOTCH", {"raw_value": 1.0, "raw_unit": "ft"}, {}, sign_mult=-1)


# USBR_Water_Measurement_Manual.pdf Eq.7-7, Table A7-5 (verify_examples weir_cipolletti_*)
def test_weir_cipolletti():
    assert run("WEIR_VNOTCH", {"raw_value": 0.3, "raw_unit": "ft"}, {"weir_type": "cipolletti", "L_ft": 1})["eng_value"] == pytest.approx(0.553, abs=0.002)
    assert run("WEIR_VNOTCH", {"raw_value": 0.5, "raw_unit": "ft"}, {"weir_type": "cipolletti", "L_ft": 2})["eng_value"] == pytest.approx(2.38, abs=0.005)


# Geokon_4420_Crackmeter.pdf §6.1.1 Eq.2 p.19, §6.1.2 p.20, §6.3 Eq.5-6 p.21
def test_vw_disp_lin_geokon_4420():
    assert run("VW_DISP_LIN", {"raw_value": 6000}, {"G": 0.01211, "R0": 4200})["eng_value"] == pytest.approx(21.79, abs=0.01)
    assert run("VW_DISP_LIN", {"raw_value": 6000}, {"method": "poly", "A": 4.1548e-08, "B": 0.01173, "R0": 4200})["eng_value"] == pytest.approx(21.88, abs=0.01)
    o = run("VW_DISP_LIN", {"raw_value": 6000, "temp_c": 32.9}, {"G": 0.01211, "R0": 4200, "T0": 20.3, "M_th": 0.000376, "B_th": 0.328})
    assert o["K_used"] == pytest.approx(0.031292, abs=5e-6)
    assert o["temp_corr"] == pytest.approx(0.394, abs=0.001)


# Geokon_4450_Displacement_Transducer.pdf p.18; dam-refs formula_library.md §1.5 manual-error note
def test_vw_disp_lin_geokon_4450_corrected_k():
    o = run("VW_DISP_LIN", {"raw_value": 5875, "temp_c": 20.0}, {"G": 0.06152, "R0": 4250, "T0": 10.0, "M_th": 0.000369, "B_th": 0.572})
    assert o["eng_value"] == pytest.approx(101.656, abs=0.002)        # NOT the printed 100.138


def test_vw_disp_sign_mult_and_offset_conventions():
    # CONVENTIONS.md: G gauge factor only; sign_mult multiplies gauge term; offset added after
    base = run("VW_DISP_LIN", {"raw_value": 6000}, {"G": 0.01211, "R0": 4200})["eng_value"]
    flipped = run("VW_DISP_LIN", {"raw_value": 6000}, {"G": 0.01211, "R0": 4200}, sign_mult=-1, offset=1.5)["eng_value"]
    assert flipped == pytest.approx(-base + 1.5)
    with pytest.raises(ValueError):
        run("VW_DISP_LIN", {"raw_value": 6000}, {"G": -0.01211, "R0": 4200})


def test_level_datum_flagged_unverified():
    f = R.get_formula("LEVEL_DATUM")
    assert f.verified is False and "UNVERIFIED" in f.sources[0]
    assert run("LEVEL_DATUM", {"raw_value": 12.5, "raw_unit": "m"}, {"datum_el_m": 300.0})["corrected_value"] == pytest.approx(312.5)
    assert run("LEVEL_DATUM", {"raw_value": 4200, "raw_unit": "mm"}, {"datum_elev_m": 320.0}, sign_mult=-1)["corrected_value"] == pytest.approx(315.8)
