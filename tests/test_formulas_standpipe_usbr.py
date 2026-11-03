"""Standpipe and USBR piezometric-elevation examples.
source files: /workspace/dam-refs/USBR_Embankment_Dam_Instrumentation_Manual_1987.pdf"""
import pytest

from dam_safety.formulas import core
from dam_safety.formulas.registry import apply_formula
from dam_safety.formulas.units import FT_TO_M

USBR = "/workspace/dam-refs/USBR_Embankment_Dam_Instrumentation_Manual_1987.pdf"


def test_usbr_piezometric_elevation_x231():
    # source: USBR-EDIM-1987 PDF p.62 method (elevation = tip el + psi x 2.31 ft/psi):
    # tip 1000 ft, 50 psi -> 1115.5 ft. Our engine converts psi->kPa->m with GEOKON Table 2
    # factors (2.3066 ft/psi); USBR rounds to 2.31, so agreement is within 0.25 ft (documented
    # in docs/VERIFICATION.md as a rounding difference, not an error).
    g_psi = 1.0  # P(psi) = 1.0 * (R1 - R0)
    params = {"method": "linear", "G": g_psi, "R0": 0.0, "pressure_unit": "psi",
              "sensor_el_m": 1000.0 * FT_TO_M}
    r = core.vw_piezometer(50.0, params)
    el_ft = r.piezometric_el_m / FT_TO_M
    assert el_ft == pytest.approx(1115.5, abs=0.25)
    assert (el_ft - 1000.0) / 50.0 == pytest.approx(2.31, abs=0.005)


def test_standpipe_depth_to_water_to_elevation():
    # source: USBR-EDIM-1987 PDF p.83/86/87 'Data Processing': water el = top-of-pipe el - depth
    # (constructed numbers; formula from the manual)
    r = core.standpipe_elevation(12.34, {"top_el_m": 1000.0, "tip_el_m": 970.0})
    assert r.water_el_m == pytest.approx(987.66, abs=1e-9)
    assert not r.is_dry


def test_standpipe_feet_input():
    # source: USBR-EDIM-1987 p.83 (field readings in feet) ; 1 ft = 0.3048 m exact
    r = core.standpipe_elevation(10.0, {"top_el_m": 100.0}, unit="ft")
    assert r.water_el_m == pytest.approx(100.0 - 3.048, abs=1e-12)


def test_standpipe_dry_reading():
    # source: USBR-EDIM-1987 p.86: a depth reaching the tip means no water above the tip
    r = core.standpipe_elevation(30.0, {"top_el_m": 1000.0, "tip_el_m": 970.0})
    assert r.is_dry and r.water_el_m is None


def test_registry_accepts_app_param_spelling():
    # SCHEMA_CONTRACT.md §5: app seed uses top_elev_m/tip_elev_m
    out = apply_formula({"raw_value": 2.0, "raw_unit": "m"},
                        {"formula_id": "STANDPIPE_DTW", "formula_version": "1.0.0", "calib_id": "c1",
                         "cal_version": 1, "params": {"top_elev_m": "50.0", "tip_elev_m": "40.0"}})
    assert out["corrected_value"] == pytest.approx(48.0)
    assert out["calib_id"] == "c1" and out["formula_id"] == "STANDPIPE_DTW"
