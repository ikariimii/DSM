"""Reference reduction formulas used by the app.

The production reduction engine is the Lakeflow pipeline (silver.reduced_readings).
The app uses these pure-Python versions for three things only:
  1. the onboarding wizard's formula test-vector check (a formula version is
     'verified' only when every one of its test vectors reproduces the
     manufacturer's published value within tolerance),
  2. demo-mode seed generation, and
  3. parity tests against the independent QA oracle (../qa/reference_examples.py).

Conventions follow /workspace/dam-refs/CONVENTIONS.md: change = current - initial,
temperature term +K*(T1-T0), barometric term -(S1-S0)*F, SI units internally.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

GAMMA_W_KPA_PER_M = 9.80665  # kPa per metre of fresh water
KPA_TO_M_H2O = 0.10197


def digits_from_hz(hz: float) -> float:
    """Linear digits (B units) = Hz^2 / 1000."""
    return hz * hz / 1000.0


def digits_from_period_us(period_us: float) -> float:
    return (1.0 / (period_us * 1e-6)) ** 2 * 1e-3


def vw_linear(G: float, R1: float, R0: float) -> float:
    return G * (R1 - R0)


def vw_poly_C(A: float, B: float, R0: float) -> float:
    return -(A * R0 * R0 + B * R0)


def vw_poly(A: float, B: float, R1: float, R0: float) -> float:
    return A * R1 * R1 + B * R1 + vw_poly_C(A, B, R0)


def check_sign_mult(v) -> int:
    """sign_mult must be exactly +1 or -1 (CONVENTIONS.md calibration table; mirrors engine core.check_sign_mult)."""
    try:
        sm = int(float(v))
    except (TypeError, ValueError):
        raise ValueError(f"sign_mult must be +1 or -1, got {v!r}") from None
    if sm not in (1, -1) or float(v) != sm:
        raise ValueError(f"sign_mult must be +1 or -1, got {v!r}")
    return sm


def vw_gauge(params: dict, R1: float) -> float:
    """sign_mult * [G(R1-R0) | A R1^2 + B R1 + C]. G is the gauge factor only; the manufacturer's
    negative G is stored as |G| with sign_mult -1 (CONVENTIONS.md). C defaults to -(A R0^2 + B R0)."""
    R0 = params["R0"]
    if params.get("method") in ("poly", "polynomial") or params.get("use_poly"):
        C = params.get("C")
        C = vw_poly_C(params["A"], params["B"], R0) if C is None or (isinstance(C, float) and math.isnan(C)) else C
        p = params["A"] * R1 * R1 + params["B"] * R1 + C
    else:
        p = vw_linear(params["G"], R1, R0)
    return check_sign_mult(params.get("sign_mult", 1)) * p


def thermistor_3k_c(ohms: float, A: float = 1.4051e-3, B: float = 2.369e-4, C: float = 1.019e-7) -> float:
    """Steinhart-Hart for Geokon 3 kOhm thermistor, returns degC."""
    L = math.log(ohms)
    return 1.0 / (A + B * L + C * L**3) - 273.15


def vw_piezo(params: dict, R1: float, T1: float | None = None, S1: float | None = None) -> dict:
    """VW piezometer -> pressure (kPa), head (m), piezometric elevation (m).

    params: G or (A,B) with use_poly, R0, K, T0, vented, F, S0, tip_elev_m.
    Returns qa_flag NO_BARO when unvented and barometer missing (value still computed
    without the correction but flagged SUSPECT)."""
    p = vw_gauge(params, R1)
    flags: list[str] = []
    if params.get("K") is not None and T1 is not None and params.get("T0") is not None:
        p += params["K"] * (T1 - params["T0"])
    if not params.get("vented", True):
        if S1 is None:
            flags.append("NO_BARO")
        else:
            p -= params.get("F", 1.0) * (S1 - params["S0"])
    p += float(params.get("offset") or 0.0)   # kPa, added after sign_mult and corrections (engine order)
    head = p / GAMMA_W_KPA_PER_M
    out = {"pressure_kpa": p, "pressure_head_m": head}
    if params.get("tip_elev_m") is not None:
        out["piezometric_elev_m"] = params["tip_elev_m"] + head
    if head < 0:
        flags.append("BELOW_TIP")
    out["qa_reason"] = ",".join(flags) or None
    out["qa_flag"] = "SUSPECT" if flags else "OK"
    return out


def standpipe_dtw(params: dict, depth_to_water_m: float) -> dict:
    el_w = params["top_elev_m"] - depth_to_water_m
    dry = el_w <= params["tip_elev_m"]
    return {
        "piezometric_elev_m": el_w,
        "pressure_head_m": el_w - params["tip_elev_m"],
        "qa_flag": "SUSPECT" if dry or depth_to_water_m < 0 else "OK",
        "qa_reason": "DRY" if dry else ("NEG_DEPTH" if depth_to_water_m < 0 else None),
    }


def weir_vnotch_90_cfs(head_ft: float) -> float:
    """USBR 90-degree V-notch: Q = 2.49 h^2.48 (cfs, ft)."""
    return 2.49 * head_ft**2.48 if head_ft > 0 else 0.0


def weir_vnotch_90_lps(head_m: float) -> float:
    head_ft = head_m / 0.3048
    return weir_vnotch_90_cfs(head_ft) * 28.316846592


def vw_disp(params: dict, R1: float, T1: float | None = None) -> float:
    """VW displacement (crackmeter/jointmeter/extensometer), mm; opening/extension positive."""
    d = vw_gauge(params, R1)
    if params.get("K") is not None and T1 is not None and params.get("T0") is not None:
        d += params["K"] * (T1 - params["T0"])
    return d


# ---------------------------------------------------------------- inclinometer
def inc_checksum(a0: float, a180: float) -> float:
    return a0 + a180


def inc_deviation(L: float, a0: float, a180: float, K: float) -> float:
    """Deviation per interval in units of L: L*(A0-A180)/(2K)."""
    return L * (a0 - a180) / (2.0 * K)


def reduce_inclinometer_profile(
    rows: list[dict], baseline: dict[float, tuple[float, float]], L_mm: float, K: float,
    stable_from_m: float | None = None, chk_tol: float | None = None,
) -> list[dict]:
    """rows: [{depth_m, a0, a180, b0, b180}] for one survey.
    baseline: depth_m -> (a_dev0_mm, b_dev0_mm).
    Returns rows sorted top-down with incremental and cumulative (summed from the
    bottom, fixed toe) displacement in mm, checksums and a checksum QC flag.
    Bias-shift correction is NOT applied automatically.

    INTERNAL ONLY: when stable_from_m is given, `proposed_bias` (the mean incremental change in the stable zone)
    is computed solely for QA's reference tests (qa/test_inclinometer_bias_rules.py). It does NOT meet the
    inclinometer procedure §5 rule (D-profile AND within-set evidence must agree), so the app never calls this
    with stable_from_m and never shows or returns proposed_bias (owner decision 2026-11-02, QA-020). It stays
    suppressed until the QA-020 spec exists and the engine provides a verified proposal."""
    out = []
    for r in rows:
        a_dev = inc_deviation(L_mm, r["a0"], r["a180"], K)
        b_dev = inc_deviation(L_mm, r["b0"], r["b180"], K)
        a0dev, b0dev = baseline.get(r["depth_m"], (a_dev, b_dev))
        out.append({
            "depth_m": r["depth_m"],
            "a_chk": inc_checksum(r["a0"], r["a180"]),
            "b_chk": inc_checksum(r["b0"], r["b180"]),
            "a_inc_mm": a_dev - a0dev,
            "b_inc_mm": b_dev - b0dev,
        })
    out.sort(key=lambda x: x["depth_m"], reverse=True)  # bottom first
    ca = cb = 0.0
    for r in out:
        ca += r["a_inc_mm"]
        cb += r["b_inc_mm"]
        r["a_cum_mm"] = ca
        r["b_cum_mm"] = cb
        r["resultant_mm"] = math.hypot(ca, cb)
    if out:
        ma = _median([r["a_chk"] for r in out])
        mb = _median([r["b_chk"] for r in out])
        for r in out:
            r["a_chk_dev"] = r["a_chk"] - ma
            r["b_chk_dev"] = r["b_chk"] - mb
            r["qc_checksum_ok"] = (
                True if chk_tol is None else abs(r["a_chk_dev"]) <= chk_tol and abs(r["b_chk_dev"]) <= chk_tol
            )
    bias = None
    if stable_from_m is not None:
        st = [r for r in out if r["depth_m"] >= stable_from_m]
        if st:
            bias = {
                "a_bias_mm_per_interval": sum(r["a_inc_mm"] for r in st) / len(st),
                "b_bias_mm_per_interval": sum(r["b_inc_mm"] for r in st) / len(st),
            }
    out.sort(key=lambda x: x["depth_m"])
    for r in out:
        r["proposed_bias"] = bias
    return out


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# ---------------------------------------------------------------- registry
@dataclass
class TestVector:
    name: str
    source: str
    inputs: dict
    expected: float
    tol: float


@dataclass
class FormulaDef:
    formula_id: str
    version: str
    description: str
    inputs: list[str]
    output: str
    unit: str
    fn: Callable[[dict], float]
    vectors: list[TestVector] = field(default_factory=list)
    required_params: list[str] = field(default_factory=list)


def _lin(i):
    return vw_linear(i["G"], i["R1"], i["R0"])


def _vwp(i):
    return vw_gauge(i, i["R1"])


REGISTRY: dict[tuple[str, str], FormulaDef] = {}


def _reg(f: FormulaDef):
    REGISTRY[(f.formula_id, f.version)] = f


# IDs and versions mirror the engine registry (src/dam_safety/formulas/registry.py):
# VW_PIEZO, STANDPIPE_DTW, INCL_PROBE_2PASS at "1.0.0". The remaining IDs are app-side
# definitions the engine has not registered yet (see SCHEMA_CONTRACT.md open items).
_reg(FormulaDef(
    "VW_PIEZO", "1.0.0", "VW piezometer: P = G(R1-R0) [linear] or A R1^2 + B R1 + C [poly] + K(T1-T0) - F(S1-S0)",
    ["R1 (digits)", "T1 (degC)", "S1 (kPa)"], "pressure_kpa", "kPa", _vwp,
    [TestVector("Geokon 4500 linear example", "GEOKON-4500 §6.1.1",
                {"method": "linear", "G": 0.1795, "sign_mult": -1, "R1": 6749, "R0": 9082}, 418.8, 0.05),
     TestVector("Geokon 4500 polynomial example", "GEOKON-4500 §6.1.2",
                {"method": "poly", "A": -7.113e-7, "B": -0.1694, "R1": 6749, "R0": 9082}, 421.5, 0.05)],
    ["R0", "T0", "K", "tip_elev_m"],
))
_reg(FormulaDef(
    "STANDPIPE_DTW", "1.0.0", "Open standpipe: EL_w = EL_top - depth to water",
    ["depth_to_water_m"], "piezometric_elev_m", "m",
    lambda i: standpipe_dtw(i, i["depth_to_water_m"])["piezometric_elev_m"],
    [TestVector("Arithmetic check", "definition", {"top_elev_m": 100.0, "tip_elev_m": 80.0, "depth_to_water_m": 7.5}, 92.5, 1e-9)],
    ["top_elev_m", "tip_elev_m"],
))
_reg(FormulaDef(
    "WEIR_VNOTCH", "1.0.0", "USBR 90-degree V-notch weir Q = 2.49 h^2.48 (Q in ft3/s, h in ft); app stores L/s",
    ["head_ft"], "flow_cfs", "ft3/s", lambda i: weir_vnotch_90_cfs(i["head_ft"]),
    # USBR Water Measurement Manual, eq. 7-6 and Table A7-4 (0.50 ft -> 0.446 cfs; 1.00 ft -> 2.49 cfs)
    [TestVector("USBR WMM Table A7-4, h=0.50 ft", "USBR-WMM Table A7-4", {"head_ft": 0.5}, 0.446, 0.0005),
     TestVector("USBR WMM Table A7-4, h=1.00 ft", "USBR-WMM Table A7-4", {"head_ft": 1.0}, 2.49, 0.005)],
    [],
))
_reg(FormulaDef(
    "VW_DISP_LIN", "1.0.0", "VW displacement (crackmeter/jointmeter), linear: D = G(R1-R0) + K(T1-T0)",
    ["R1 (digits)", "T1 (degC)"], "displacement_mm", "mm", _lin,
    [TestVector("Geokon 4420 linear example", "GEOKON-4420 §6.1.1", {"G": 0.01211, "R1": 6000, "R0": 4200}, 21.79, 0.01)],
    ["G", "R0"],
))
_reg(FormulaDef(
    "INCL_PROBE_2PASS", "1.0.0", "Inclinometer probe deviation L(A0-A180)/(2K), cumulative from fixed toe",
    ["a0", "a180", "b0", "b180"], "a_cum_mm", "mm",
    lambda i: inc_deviation(i["L"], i["a0"], i["a180"], i["K"]),
    [TestVector("DGSI data reduction, 24 in English", "DGSI data_reduction p3",
                {"L": 24, "a0": 359, "a180": -339, "K": 20000}, 0.4188, 0.00005)],
    ["L_mm", "K", "stable_from_m"],
))
_reg(FormulaDef(
    "LEVEL_DATUM", "1.0.0", "Level sensor: EL = datum_elev_m + reading_m", ["reading_m"], "elevation_m", "m",
    lambda i: i["datum_elev_m"] + i["reading_m"],
    # No published source exists for level sensors (engine registry: LEVEL_DATUM verified=False). A
    # self-made arithmetic vector must not mark it verified, so it is registered without vectors -> unverified.
    [],
    ["datum_elev_m"],
))
# Deliberately registered without test vectors: shows the 'unverified' path in the UI.
_reg(FormulaDef(
    "SETTLEMENT", "1.0.0", "Settlement = EL0 - EL_t (positive downward)", ["elev_m"], "settlement_mm", "mm",
    lambda i: (i["EL0"] - i["ELt"]) * 1000.0, [], ["EL0"],
))


# Mirrors engine SETTLEMENT_DELTA 1.0.0 (CONVENTIONS.md sign conventions; settlement-survey-gnss.md Ex14):
# eng = sign_mult * (value - value0) * to_mm, settlement positive-down. The engine re-baselines per
# datum_id; the app only needs the row-level rule for verification parity (it never recomputes gold).
_reg(FormulaDef(
    "SETTLEMENT_DELTA", "1.0.0", "Settlement = sign_mult x (value - value0[datum]) (positive downward)",
    ["value_mm"], "settlement_mm", "mm",
    lambda i: check_sign_mult(i.get("sign_mult", 1)) * (i["value"] - i["value0"]),
    [TestVector(f"Ex14 point {n}", "DAMREFS-SETTLEMENT verify.py Ex14",
                {"value": v, "value0": 0.0, "sign_mult": -1}, e, 1e-12)
     for n, (v, e) in enumerate(((-4.2, 4.2), (-6.9, 6.9), (1.1, -1.1)), start=1)],
    ["sign_mult"],
))


def get_formula(formula_id: str, version: str) -> FormulaDef | None:
    return REGISTRY.get((formula_id, str(version)))


def run_vectors(formula_id: str, version: str, extra_vectors: list[dict] | None = None) -> dict:
    """Run every registered (plus user-supplied) test vector. verified = at least one
    vector and all pass. A formula with no vectors is never verified."""
    f = get_formula(formula_id, version)
    if f is None:
        return {"formula_id": formula_id, "version": version, "known": False, "verified": False, "results": []}
    vectors = list(f.vectors)
    for v in extra_vectors or []:
        vectors.append(TestVector(v.get("name", "user vector"), v.get("source", "user"), v["inputs"],
                                  float(v["expected"]), float(v.get("tol", 1e-6))))
    results = []
    for v in vectors:
        try:
            got = float(f.fn(v.inputs))
            ok = abs(got - v.expected) <= v.tol
            err = None
        except Exception as e:  # noqa: BLE001 - surface any failure as a failed vector
            got, ok, err = None, False, f"{type(e).__name__}: {e}"
        results.append({"name": v.name, "source": v.source, "expected": v.expected, "tol": v.tol,
                        "computed": got, "pass": ok, "error": err})
    return {"formula_id": formula_id, "version": version, "known": True,
            "verified": bool(results) and all(r["pass"] for r in results), "results": results,
            "unit": f.unit, "output": f.output, "description": f.description,
            "required_params": f.required_params}


def verified_formulas() -> set[tuple[str, str]]:
    return {k for k in REGISTRY if run_vectors(*k)["verified"]}
