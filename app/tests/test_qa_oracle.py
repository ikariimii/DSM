"""Every QA oracle case in qa/reference_examples.py is reproduced through the app's own reduction
code (backend/formulas.py), not through the oracle's helper functions.

The oracle module is loaded by path and never modified. Restricted cases (IN-06..IN-09) are read
by path at test time from $DAM_QA_PRIVATE; nothing from them is embedded here.

Each oracle case maps to exactly one of:
  * an app computation that must land within the oracle's published tolerance, or
  * NOT_DISPLAYED: the app has no reduction for that quantity and never shows it (asserted below).
A new oracle case with no mapping fails test_every_case_is_mapped, so coverage cannot drift."""
from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path

import pytest

from backend import formulas as fx

ROOT = Path(__file__).resolve().parents[2]
QA_PATH = ROOT / "qa" / "reference_examples.py"


def _qa():
    if not QA_PATH.exists():
        pytest.skip("qa/reference_examples.py not present")
    spec = importlib.util.spec_from_file_location("qa_reference_examples", QA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


QA = _qa()
CASES = {c[0]: c for c in QA.CASES}           # id -> (id, source, oracle_computed, published, tol)


def _piezo(**p):
    base = {"method": "linear", "G": 0.0, "R0": 9082, "sign_mult": 1, "vented": True}
    return fx.vw_piezo({**base, **{k: v for k, v in p.items() if k not in ("R1", "T1", "S1")}},
                       p.get("R1", 9082), p.get("T1"), p.get("S1"))


PZ_LIN = {"method": "linear", "G": 0.1795, "sign_mult": -1, "R0": 9082}      # |G| + sign_mult (CONVENTIONS.md)
PZ_POLY = {"method": "poly", "A": -7.113e-7, "B": -0.1694, "R0": 9082, "sign_mult": 1}

# id -> callable returning the app's value for that case
APP: dict[str, callable] = {
    "PZ-01 digits 8000 -> period 354us": lambda: fx.digits_from_period_us(354),
    "PZ-02 digits 8000 -> 2828 Hz": lambda: fx.digits_from_hz(2828),
    "PZ-03 linear P=G(R1-R0)": lambda: fx.vw_piezo(PZ_LIN, 6749)["pressure_kpa"],
    "PZ-04 linear head m H2O": lambda: fx.vw_piezo(PZ_LIN, 6749)["pressure_head_m"],
    "PZ-05 polynomial C": lambda: fx.vw_poly_C(PZ_POLY["A"], PZ_POLY["B"], PZ_POLY["R0"]),
    "PZ-06 polynomial P": lambda: fx.vw_piezo(PZ_POLY, 6749)["pressure_kpa"],
    "PZ-07 poly head m H2O": lambda: fx.vw_piezo(PZ_POLY, 6749)["pressure_head_m"],
    # temperature correction alone: R1 = R0 so the gauge term is 0
    "PZ-08 temp corr K(T1-T0)": lambda: _piezo(K=0.1319, T0=22, T1=15)["pressure_kpa"],
    # barometric correction alone (unvented): app subtracts F(S1-S0); oracle states the correction magnitude
    "PZ-09 baro inHg->kPa F=3.386 (29->31 inHg)": lambda: -_piezo(vented=False, F=3.386, S0=29, S1=31)["pressure_kpa"],
    "PZ-10 baro 2 inHg ~ 1 psi": lambda: -_piezo(vented=False, F=0.491, S0=29, S1=31)["pressure_kpa"],
    "PZ-11 thermistor 3k @3000 ohm ~25C": lambda: fx.thermistor_3k_c(3000),
    "PZ-12 thermistor table 9796 ohm = 0C": lambda: fx.thermistor_3k_c(9796),
    "PZ-13 thermistor table 1598 ohm = 40C": lambda: fx.thermistor_3k_c(1598),
    "PZ-14 thermistor table 201.1k = -50C": lambda: fx.thermistor_3k_c(201100),
    # reading = K sin(theta): app deviation with L=1 over one face pair gives sin(theta); scale back by K
    "IN-01 sin(1deg)*20000": lambda: 20000 * math.sin(math.asin(fx.inc_deviation(1, 349, -349, 20000))),
    "IN-02 combined (A0-A180)/2": lambda: fx.inc_deviation(1, 359, -339, 1),
    "IN-03 checksum A0+A180 (=2x bias)": lambda: fx.inc_checksum(359, -339),
    "IN-04 deviation 24in English": lambda: fx.reduce_inclinometer_profile(
        [{"depth_m": 0.0, "a0": 359, "a180": -339, "b0": 0, "b180": 0}], {0.0: (0.0, 0.0)}, 24, 20000)[0]["a_cum_mm"],
    # displacement: baseline sum 698 (349/-349) vs current sum 700 (350/-350), through the production profile code
    "IN-05 displacement 24in (700-698)": lambda: fx.reduce_inclinometer_profile(
        [{"depth_m": 0.0, "a0": 350, "a180": -350, "b0": 0, "b180": 0}],
        {0.0: (fx.inc_deviation(24, 349, -349, 20000), 0.0)}, 24, 20000)[0]["a_inc_mm"],
    "CK-01 linear D=(R1-R0)G": lambda: fx.vw_disp({"G": 0.01211, "R0": 4200}, 6000),
    "CK-02 polynomial C": lambda: fx.vw_poly_C(4.1548e-8, 0.01173, 4200),
    "CK-03 polynomial D": lambda: fx.vw_disp({"method": "poly", "A": 4.1548e-8, "B": 0.01173, "R0": 4200}, 6000),
    # CK-05: thermal correction through vw_disp with K supplied as the calibration parameter (see CK-04 below)
    "CK-05 thermal corr": lambda: fx.vw_disp({"G": 0.0, "R0": 6000, "K": (6000 * 0.000376 + 0.328) * 0.01211,
                                              "T0": 20.3}, 6000, 32.9),
    # load cell via the same generic VW gauge path; manufacturer negative G -> |G| with sign_mult -1
    "LC-01 linear L=(R1-R0)G": lambda: fx.vw_gauge({"G": 397, "sign_mult": -1, "R0": 7309}, 5497),
    "LC-02 polynomial C": lambda: fx.vw_poly_C(-0.00247, -367, 7305),
    "LC-03 polynomial L": lambda: fx.vw_gauge({"method": "poly", "A": -0.00247, "B": -367, "R0": 7305}, 6409),
}

DIFFERENT_INPUTS = {"PZ-04 linear head m H2O", "PZ-07 poly head m H2O", "IN-01 sin(1deg)*20000"}

# Quantities the app does not reduce and therefore never displays (asserted in test_not_displayed_really_absent).
NOT_DISPLAYED = {
    "CK-04 thermal K=(R1*M+B)G": "app takes the thermal factor K as an approved calibration parameter; "
                                 "it does not derive K from (M, B). CK-05 covers the correction it applies.",
    "SG-01 avg axial strain": "strain-gauge rosettes are not an onboarding instrument type",
    "SG-02 tan(theta)=80/60 -> 53 deg": "strain-gauge rosettes are not an onboarding instrument type",
    "SG-03 principal max 60*.6+80*.8+100": "strain-gauge rosettes are not an onboarding instrument type",
}

PRIVATE_FILE = Path(os.environ.get("DAM_QA_PRIVATE", "/workspace/dam-refs/qa_private")) / "inclinometer_expected.json"
PRIVATE_NOT_DISPLAYED_KINDS = {"asin_deg": "probe rotation (asin) is not reduced or displayed by the app"}


def test_every_case_is_mapped():
    unmapped = set(CASES) - set(APP) - set(NOT_DISPLAYED)
    unmapped = {c for c in unmapped if not c.startswith(("IN-06", "IN-07", "IN-08", "IN-09"))}
    assert not unmapped, f"QA oracle cases with no app mapping: {sorted(unmapped)}"
    assert not (set(APP) & set(NOT_DISPLAYED))
    assert set(APP) | set(NOT_DISPLAYED) <= set(CASES), "mapping refers to cases the oracle no longer has"


@pytest.mark.parametrize("case_id", sorted(APP))
def test_app_reproduces_oracle(case_id):
    _id, source, oracle, published, tol = CASES[case_id]
    got = APP[case_id]()
    assert abs(got - published) <= tol, f"{case_id} [{source}]: app={got!r} published={published} tol={tol}"
    # Where the app and the oracle start from the same inputs they must agree to rounding error.
    # (PZ-04/07 oracle starts from the rounded published pressure; IN-01 from the exact angle.)
    if case_id not in DIFFERENT_INPUTS:
        assert got == pytest.approx(oracle, rel=1e-9, abs=1e-9)


def test_not_displayed_really_absent():
    types = set().union(*[set(k.split("_")) for k in fx_types()])
    assert "strain" not in types and "rosette" not in types
    outputs = {f.output for f in fx.REGISTRY.values()}
    assert not any("rotation" in o or "strain" in o or "thermal_k" in o.lower() for o in outputs)


def fx_types():
    # types the app can compute: §5.4 types with a non-empty engine_formulas list (others are listed, never computed)
    from backend import onboarding as ob
    from backend.repos import contract as ct
    return [t for t in ob.TYPE_UNITS if ct.type_is_computable(t)]


def test_private_inclinometer_cases_by_path():
    if not PRIVATE_FILE.exists():
        pytest.skip("restricted QA cases not present (read by path only)")
    rows = json.loads(PRIVATE_FILE.read_text())["reference_examples"]
    ran = 0
    for case_id, _src, kind, args, published, tol in rows:
        if kind == "factor":                  # L/(2K): app deviation per unit of (A0-A180)
            L, K = args
            got = fx.inc_deviation(L, 1, 0, K)
            assert abs(got - published) <= tol, case_id
            ran += 1
        else:
            assert kind in PRIVATE_NOT_DISPLAYED_KINDS, f"unmapped restricted case kind {kind} ({case_id})"
    assert ran >= 1


def test_displayed_formulas_have_oracle_or_are_flagged():
    """Which engine formulas the QA oracle covers; the rest are listed as gaps in SCHEMA_CONTRACT.md §6."""
    covered = {"VW_PIEZO": "PZ-", "VW_DISP_LIN": "CK-", "INCL_PROBE_2PASS": "IN-"}
    no_qa_oracle = {"STANDPIPE_DTW", "WEIR_VNOTCH", "LEVEL_DATUM", "SETTLEMENT_DELTA", "SETTLEMENT"}
    ids = {f.formula_id for f in fx.REGISTRY.values()}
    assert ids == set(covered) | no_qa_oracle, "new formula: add QA oracle mapping or list it as a gap"
    for fid, prefix in covered.items():
        assert any(c.startswith(prefix) for c in APP), fid
