"""VW piezometer worked examples.

Every expected value comes from a manual worked example, re-derived independently in
/workspace/dam-refs/verify_examples.py (output: /workspace/dam-refs/verify_output.txt).
PDF page numbers follow /workspace/dam-refs/pg.py (form-feed page split).
"""
import math

import pytest

from dam_safety.formulas import core
from dam_safety.formulas.units import to_kpa

GEOKON = "/workspace/dam-refs/4500_Piezometer.pdf"
G, R0, R1 = -0.1795, 9082.0, 6749.0          # GEOKON-4500 §6.1.1 worked example


def test_digits_from_hz():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Eq.1 PDF p.22 (2828 Hz ≈ 8000 digits)
    assert core.vw_digits_from_hz(2828) == pytest.approx(8000, abs=5)


def test_digits_from_period():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Eq.1 PDF p.22 (354 µs ≈ 8000 digits)
    assert core.vw_digits_from_period_us(354) == pytest.approx(8000, abs=40)


def test_linear_pressure_418_8_kpa():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Eq.2 PDF p.22 -> 418.8 kPa
    assert core.vw_pressure_linear(R1, R0, G) == pytest.approx(418.8, abs=0.05)


def test_usbr_sign_convention_equivalent():
    # source: /workspace/dam-refs/USBR_Embankment_Dam_Instrumentation_Manual_1987.pdf PDF p.68-69
    # P = G'(L0 - L1) with positive G' is identical to GEOKON's G(R1 - R0) with negative G
    assert 0.1795 * (R0 - R1) == pytest.approx(core.vw_pressure_linear(R1, R0, G), abs=1e-9)


def test_head_42_7_m():
    # source: /workspace/dam-refs/4500_Piezometer.pdf PDF p.22 + Table 2 PDF p.26 (kPa x 0.10197)
    p = core.vw_pressure_linear(R1, R0, G)
    assert core.pressure_kpa_to_head_m(p) == pytest.approx(42.7, abs=0.05)


def test_poly_c_and_pressure():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Eq.3/Eq.4 PDF p.23: C = 1597.2, P = 421.5 kPa
    a, b = -7.113e-07, -0.1694
    c = core.vw_poly_c(a, b, R0)
    assert c == pytest.approx(1597.2, abs=0.05)
    assert core.vw_pressure_poly(R1, a, b, c) == pytest.approx(421.5, abs=0.05)


def test_thermal_correction():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Eq.6 PDF p.25: K=0.1319, T1=15, T0=22 -> -0.92 kPa
    assert core.vw_temp_correction(0.1319, 15, 22) == pytest.approx(-0.92, abs=0.005)


def test_baro_29_to_31_inhg_is_about_1_psi():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Eq.5 PDF p.23-24 (29 -> 31 inHg ≈ 1 psi ≈ 2.3 ft)
    kpa = to_kpa(31 - 29, "inhg")
    psi = kpa / 6.8947
    assert psi == pytest.approx(1.0, abs=0.05)
    assert core.vw_baro_correction(31, 29, 0.491) == pytest.approx(1.0, abs=0.05)


def test_combined_vw_piezometer_end_to_end():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Eq.2 + Eq.6 - Eq.5 (verify_output: 417.85, want 417.88 tol 0.06)
    params = {"method": "linear", "G": G, "R0": R0, "K": 0.1319, "T0": 22, "S0": 100.0,
              "baro_correct": "true", "baro_unit": "kpa", "pressure_unit": "kpa", "sensor_el_m": 100.0}
    r = core.vw_piezometer(R1, params, temp_c=15, baro=100.0)
    assert r.pressure_kpa == pytest.approx(417.88, abs=0.06)
    assert r.baro_corr == 0
    assert r.head_m == pytest.approx(r.pressure_kpa * 0.10197, rel=1e-9)
    assert r.piezometric_el_m == pytest.approx(100.0 + r.head_m, rel=1e-12)


def test_vw_piezometer_poly_method_matches_eq3():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Eq.3 PDF p.23 (C derived from Eq.4 when absent)
    r = core.vw_piezometer(R1, {"method": "poly", "A": -7.113e-07, "B": -0.1694, "R0": R0})
    assert r.pressure_kpa == pytest.approx(421.5, abs=0.05)


def test_vw_piezometer_psi_calibration_converts_to_kpa():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Table 2 PDF p.26 (1 psi = 6.895 kPa)
    g_psi = G / 6.8947
    r = core.vw_piezometer(R1, {"method": "linear", "G": g_psi, "R0": R0, "pressure_unit": "psi"})
    assert r.pressure_kpa == pytest.approx(418.8, abs=0.05)


def test_vw_baro_string_false_is_not_applied():
    # MAP<STRING,STRING> params: 'false' must not enable correction (engine robustness)
    r = core.vw_piezometer(R1, {"method": "linear", "G": G, "R0": R0, "S0": 100, "baro_correct": "false"},
                           baro=103.0)
    assert r.baro_corr == 0


@pytest.mark.parametrize("ohm,degc", [(2221, 32), (9796, 0), (5971, 10), (2130, 33)])
def test_thermistor_3k(ohm, degc):
    # source: /workspace/dam-refs/4500_Piezometer.pdf App.B Eq.7 + Table 8 PDF p.31
    assert core.thermistor_temp_c(ohm, "3k") == pytest.approx(degc, abs=0.1)


@pytest.mark.parametrize("ohm,degc", [(32650, 0), (7402, 32), (2157, 64)])
def test_thermistor_10k(ohm, degc):
    # source: /workspace/dam-refs/4500_Piezometer.pdf App.B Eq.8 + Table 9 PDF p.32
    assert core.thermistor_temp_c(ohm, "10k") == pytest.approx(degc, abs=0.15)


def test_kpa_to_m_water_factor():
    # source: /workspace/dam-refs/4500_Piezometer.pdf Table 2 PDF p.26 (1 kPa = 0.10197 m H2O)
    assert core.pressure_kpa_to_head_m(1.0) == pytest.approx(0.10197, abs=2e-5)
    assert 1 / (0.999972 * 9.80665) == pytest.approx(0.10197, abs=2e-5)
