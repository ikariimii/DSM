"""Metadata-driven formula registry.

ref.formula rows (formula_id, formula_version, reading_type, output_unit, source_citation,
test_vectors) are seeded from ``REGISTRY`` (tools/formula_seed.py -> sql/seed/ref_formula_seed.sql). A calibration
row (ref.calibration) names the formula_id + formula_version that must reduce its readings,
so the engine is driven by metadata, not code branches. Each output row records
calib_id + formula_id + formula_version for lineage (recompute support).

All registered functions take a *canonical reading* dict (CONVENTIONS.md canonical fields:
raw_value, raw_unit, temp_c, temp_raw, baro_kpa, ...) and the calibration params dict, and
return a dict of engineering outputs. They are pure Python -> trivially unit-testable,
wrapped for Spark in dam_safety.spark.udfs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from . import core


@dataclass(frozen=True)
class Formula:
    formula_id: str
    version: str
    reading_type: str
    output_unit: str
    fn: Callable[[dict, dict], dict]
    sources: Tuple[str, ...]
    description: str
    verified: bool = True
    test_vectors: Tuple[dict, ...] = field(default_factory=tuple)


REGISTRY: Dict[Tuple[str, str], Formula] = {}


def register(f: Formula) -> Formula:
    key = (f.formula_id, f.version)
    if key in REGISTRY:
        raise ValueError(f"duplicate formula {key}")
    REGISTRY[key] = f
    return f


def get_formula(formula_id: str, version: Optional[str] = None) -> Formula:
    if version is not None:
        return REGISTRY[(formula_id, version)]
    cands = sorted((k for k in REGISTRY if k[0] == formula_id), key=lambda k: tuple(int(x) for x in k[1].split(".")))
    if not cands:
        raise KeyError(formula_id)
    return REGISTRY[cands[-1]]


def _f(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _temp_c(reading: dict, params: dict) -> Optional[float]:
    t = _f(reading.get("temp_c"))
    if t is not None:
        return t
    r = _f(reading.get("temp_raw"))  # thermistor resistance (ohm)
    if r is not None and r > 0:
        return core.thermistor_temp_c(r, str(params.get("thermistor", "3k")))
    return None


# -------------------------------------------------------------------------------------------
def _require_gauge_only(params: dict) -> None:
    """CONVENTIONS.md: 'G is ONLY the gauge factor; never overload G or A for sign or offset'.
    At the registry (calibration-table) level a negative linear G is rejected: store |G| and
    sign_mult = -1 (identical result to the manufacturer form P = G(R1-R0) with G < 0)."""
    if str(params.get("method", "linear")).lower() == "linear" and _f(params.get("G")) is not None and float(params["G"]) < 0:
        raise ValueError("calibration G < 0: G is the gauge factor only; store |G| and sign_mult = -1 (CONVENTIONS.md)")


def _no_sign_offset(params: dict, formula: str) -> None:
    if core.check_sign_mult(params.get("sign_mult", 1)) != 1 or (_f(params.get("offset")) or 0.0) != 0.0:
        raise ValueError(f"sign_mult/offset not defined for {formula} (UNVERIFIED; ask Geotech lead)")


def _vw_piezo(reading: dict, params: dict) -> dict:
    _require_gauge_only(params)
    raw = _f(reading.get("raw_value"))
    if raw is None:
        return {"eng_value": None, "eng_unit": "kPa", "corrected_value": None, "corrected_unit": "m"}
    unit = str(reading.get("raw_unit") or "digits").lower()
    baro = _f(reading.get("baro_kpa"))
    p = dict(params)
    if baro is not None:
        p["baro_unit"] = "kpa"   # logger barometer is ingested in kPa (canonical baro_kpa)
    res = core.vw_piezometer(raw, p, temp_c=_temp_c(reading, params), baro=baro, reading_unit=unit)
    return {
        "eng_value": res.pressure_kpa,
        "eng_unit": "kPa",
        "head_m": res.head_m,
        "corrected_value": res.piezometric_el_m,
        "corrected_unit": "m",
        "temp_corr": res.temp_corr,
        "baro_corr": res.baro_corr,
        "digits": res.digits,
    }


def _standpipe(reading: dict, params: dict) -> dict:
    _no_sign_offset(params, "STANDPIPE_DTW")
    raw = _f(reading.get("raw_value"))
    if raw is None:
        return {"eng_value": None, "eng_unit": "m", "corrected_value": None, "corrected_unit": "m"}
    res = core.standpipe_elevation(raw, params, unit=str(reading.get("raw_unit") or "m"))
    return {
        "eng_value": res.depth_m,
        "eng_unit": "m",
        "corrected_value": res.water_el_m,
        "corrected_unit": "m",
        "is_dry": res.is_dry,
    }


register(Formula(
    "VW_PIEZO", "1.0.0", "VW_PIEZOMETER", "m",
    _vw_piezo,
    (core.SOURCES["VW_DIGITS"], core.SOURCES["VW_PIEZO_LINEAR"], core.SOURCES["VW_PIEZO_POLY"],
     core.SOURCES["VW_TEMP_CORR"], core.SOURCES["VW_BARO_CORR"], core.SOURCES["PRESSURE_TO_HEAD"],
     core.SOURCES["PIEZO_ELEVATION"], core.SOURCES["THERMISTOR_3K"]),
    "VW piezometer: digits -> P (linear G or polynomial A,B,C) + K(T1-T0) - (S1-S0)F -> kPa -> head (m) -> piezometric elevation",
    test_vectors=(
        {"reading": {"raw_value": 6749, "raw_unit": "digits"}, "params": {"method": "linear", "G": 0.1795, "sign_mult": -1, "R0": 9082}, "expect": {"eng_value": 418.8}, "tol": 0.05, "source": core.SOURCES["VW_PIEZO_LINEAR"] + " (manual G = -0.1795 stored as |G| + sign_mult -1 per " + core.SOURCES["SIGN_OFFSET"] + ")"},
        {"reading": {"raw_value": 6749, "raw_unit": "digits"}, "params": {"method": "poly", "A": -7.113e-07, "B": -0.1694, "R0": 9082}, "expect": {"eng_value": 421.5}, "tol": 0.05, "source": core.SOURCES["VW_PIEZO_POLY"]},
    ),
))

register(Formula(
    "STANDPIPE_DTW", "1.0.0", "STANDPIPE", "m",
    _standpipe,
    (core.SOURCES["STANDPIPE_DTW"],),
    "Open standpipe / observation well: water elevation = top-of-pipe elevation - depth to water",
    test_vectors=(
        {"reading": {"raw_value": 12.34, "raw_unit": "m"}, "params": {"top_el_m": 250.00}, "expect": {"corrected_value": 237.66}, "tol": 1e-9, "source": core.SOURCES["STANDPIPE_DTW"] + " (constructed example; rule only, no numeric example in source)"},
    ),
))

def _settlement_rowwise(reading: dict, params: dict) -> dict:
    """Row-wise stage of SETTLEMENT_DELTA: validates the calibration only. The value needs the
    series baseline per datum_id, so eng/corrected are produced by dam_safety.datum.rebaseline_series
    in the per-sensor stage (spark.pandas_ops.qc_pdf)."""
    sm = core.check_sign_mult(params.get("sign_mult", 1))
    axis = str(params.get("raw_axis", "")).lower()
    if axis == "up" and sm != -1:
        raise ValueError("raw_axis=up (elevation/heave-positive source) requires sign_mult = -1 (settlement positive-down)")
    if axis == "down" and sm != 1:
        raise ValueError("raw_axis=down (settlement-positive source) requires sign_mult = +1")
    return {"eng_value": None, "eng_unit": "mm", "corrected_value": None, "corrected_unit": "mm", "needs_baseline": True}


register(Formula(
    "SETTLEMENT_DELTA", "1.0.0", "SETTLEMENT", "mm",
    _settlement_rowwise,
    (core.SOURCES["SETTLEMENT_SIGN"], core.SOURCES["DATUM_REBASELINE"], core.SOURCES["SIGN_OFFSET"]),
    "Settlement from an elevation/displacement series: eng = sign_mult*(value - value0[datum_id])*to_mm + carry; settlement positive-down; raw kept as received",
    test_vectors=(
        {"series": True, "raw": [0.0, -4.2, -6.9, 1.1], "raw_unit": "mm", "sign_mult": -1, "expect": [0.0, 4.2, 6.9, -1.1], "tol": 1e-12, "source": core.SOURCES["SETTLEMENT_SIGN"] + " Ex14"},
    ),
))

# Inclinometer reductions operate on a whole survey (profile), not a single reading, so they
# are registered for lineage/metadata and executed by dam_safety.spark.engine via
# applyInPandas -> core.incl_profile.
register(Formula(
    "INCL_PROBE_2PASS", "1.0.0", "INCLINOMETER", "mm",
    lambda reading, params: {},  # profile-level; see core.incl_profile
    (core.SOURCES["INCL_COMBINED"], core.SOURCES["INCL_CHECKSUM"], core.SOURCES["INCL_DEVIATION"], core.SOURCES["INCL_DISPLACEMENT"]),
    "Two-pass probe inclinometer: checksum, deviation L(R0-R180)/2K, incremental vs initial, cumulative from fixed bottom or fixed top",
    test_vectors=(
        {"profile": True, "a0": 359, "a180": -339, "L": 24, "K": 20000, "expect": {"dev": 0.4188, "checksum": 20}, "tol": 5e-5, "source": core.SOURCES["INCL_DEVIATION"]},
    ),
))


# -------------------------------------------------------------------------------------------
# Steering 6: engine formulas the app already displays (WEIR_VNOTCH, VW_DISP_LIN, LEVEL_DATUM).
REF_USBR_WMM = "USBR-WMM-2001: USBR Water Measurement Manual 3rd ed. (/workspace/dam-refs/USBR_Water_Measurement_Manual.pdf)"
REF_GEOKON_4420 = "GEOKON-4420: Geokon Model 4420 VW Crackmeter manual (/workspace/dam-refs/Geokon_4420_Crackmeter.pdf)"
REF_GEOKON_4450 = "GEOKON-4450: Geokon Model 4450 VW Displacement Transducer manual (/workspace/dam-refs/Geokon_4450_Displacement_Transducer.pdf)"
SOURCES_EXTRA = {
    "WEIR_V90": REF_USBR_WMM + " Ch.7 Eq.7-6 (Q = 2.49 h^2.48, ft3/s, ft) and Table A7-4; min head 0.2 ft (Ch.7 '90-degree V-notch weirs')",
    "WEIR_CIPOLLETTI": REF_USBR_WMM + " Ch.7 Eq.7-7 (Q = 3.367 L h^1.5, ft3/s, ft) and Table A7-5",
    "DISP_LINEAR": REF_GEOKON_4420 + " §6.1.1 Eq.2 D = G(R1-R0), example p.19; " + REF_GEOKON_4450 + " p.18",
    "DISP_POLY": REF_GEOKON_4420 + " §6.1.2 Eq.3-4, example p.20",
    "DISP_THERMAL": REF_GEOKON_4420 + " §6.3 Eq.5 T_corr = K(T1-T0) ADDED to Eq.2/3; Eq.6 K = (R1*M + B)G, Table 2, example p.21",
    "LEVEL_DATUM": "DAMREFS-CONVENTIONS 'Units and symbols': elevations in metres above the project datum (rule only). "
                   "No instrument-specific source in /workspace/dam-refs for reservoir/tailwater level sensors -> UNVERIFIED",
}
FT_PER_M = 1.0 / 0.3048            # exact (international foot)
LPS_PER_CFS = 28.316846592         # exact: 0.3048^3 m3 * 1000


def _weir(reading: dict, params: dict) -> dict:
    """Seepage weir: head over crest -> Q. Formulas are in ft / ft3/s (USBR-WMM-2001: 'convert to SI
    only after computing Q'); stored as L/s. raw_unit 'ft' | 'm' | 'mm'. Optional gauge_zero (same
    unit as raw) is subtracted to get head over crest. sign_mult/offset are not defined -> rejected."""
    _no_sign_offset(params, "WEIR_VNOTCH")
    raw = _f(reading.get("raw_value"))
    unit = str(reading.get("raw_unit") or "ft").lower()
    if raw is None:
        return {"eng_value": None, "eng_unit": "ft3/s", "corrected_value": None, "corrected_unit": "L/s"}
    h = raw - (_f(params.get("gauge_zero")) or 0.0)
    h_ft = {"ft": h, "m": h * FT_PER_M, "mm": h / 1000.0 * FT_PER_M}[unit]
    wtype = str(params.get("weir_type", "v_notch_90")).lower()
    if h_ft <= 0:
        q = 0.0
    elif wtype == "v_notch_90":
        q = 2.49 * h_ft ** 2.48
    elif wtype == "cipolletti":
        q = 3.367 * float(params["L_ft"]) * h_ft ** 1.5
    else:
        raise ValueError(f"unsupported weir_type {wtype!r} (v_notch_90 | cipolletti)")
    return {"eng_value": q, "eng_unit": "ft3/s", "corrected_value": q * LPS_PER_CFS, "corrected_unit": "L/s",
            "head_ft": h_ft, "low_head": 0 < h_ft < 0.2}


def _vw_disp_lin(reading: dict, params: dict) -> dict:
    """VW crackmeter / jointmeter / displacement transducer:
    D = sign_mult * [G(R1-R0)  |  A R1^2 + B R1 + C] + K(T1-T0) + offset   (mm).
    K is taken from params K, else computed as (R1*M_th + B_th)*G (Eq.6). Like VW_PIEZO, sign_mult
    multiplies the gauge term only; the thermal term keeps its own sign (CONVENTIONS.md, GEOKON-4420 Eq.5)."""
    _require_gauge_only(params)
    raw = _f(reading.get("raw_value"))
    if raw is None:
        return {"eng_value": None, "eng_unit": "mm", "corrected_value": None, "corrected_unit": "mm"}
    unit = str(reading.get("raw_unit") or "digits").lower()
    r1 = {"digits": raw, "hz": None}.get(unit, None)
    if unit == "hz":
        r1 = core.vw_digits_from_hz(raw)
    if r1 is None:
        raise ValueError(f"unsupported VW reading unit {unit!r}")
    r0 = float(params["R0"])
    method = str(params.get("method", "linear")).lower()
    if method == "linear":
        d = core.vw_pressure_linear(r1, r0, float(params["G"]))      # same algebra: G(R1-R0)
    elif method in ("poly", "polynomial"):
        a, b = float(params["A"]), float(params["B"])
        c = _f(params.get("C"))
        d = core.vw_pressure_poly(r1, a, b, core.vw_poly_c(a, b, r0) if c is None else c)
    else:
        raise ValueError(f"unsupported method {method!r}")
    d = core.check_sign_mult(params.get("sign_mult", 1)) * d
    t1, t0 = _temp_c(reading, params), _f(params.get("T0"))
    k = _f(params.get("K"))
    if k is None and _f(params.get("M_th")) is not None and _f(params.get("B_th")) is not None and _f(params.get("G")) is not None:
        k = (r1 * float(params["M_th"]) + float(params["B_th"])) * float(params["G"])
    tcorr = k * (t1 - t0) if (k is not None and t1 is not None and t0 is not None) else 0.0
    val = d + tcorr + (_f(params.get("offset")) or 0.0)
    return {"eng_value": val, "eng_unit": "mm", "corrected_value": val, "corrected_unit": "mm", "temp_corr": tcorr, "K_used": k}


def _level_datum(reading: dict, params: dict) -> dict:
    """Level sensor: EL (m above project datum) = datum_el_m + sign_mult * reading_m + offset.
    sign_mult = -1 for down-looking sensors that report distance to water (radar/ultrasonic).
    UNVERIFIED: rule from CONVENTIONS.md only; no instrument-specific source in dam-refs."""
    raw = _f(reading.get("raw_value"))
    unit = str(reading.get("raw_unit") or "m").lower()
    if raw is None:
        return {"eng_value": None, "eng_unit": "m", "corrected_value": None, "corrected_unit": "m"}
    from .units import length_to_m
    r_m = length_to_m(raw, unit)
    datum = _f(params.get("datum_el_m", params.get("datum_elev_m")))
    if datum is None:
        raise ValueError("LEVEL_DATUM needs params.datum_el_m")
    el = core.apply_sign_offset(r_m, params.get("sign_mult", 1), params.get("offset", 0.0)) + datum
    return {"eng_value": r_m, "eng_unit": "m", "corrected_value": el, "corrected_unit": "m"}


register(Formula(
    "WEIR_VNOTCH", "1.0.0", "SEEPAGE_WEIR", "L/s", _weir,
    (SOURCES_EXTRA["WEIR_V90"], SOURCES_EXTRA["WEIR_CIPOLLETTI"]),
    "Seepage weir head -> flow: 90-deg V-notch Q = 2.49 h^2.48 or Cipolletti Q = 3.367 L h^1.5 (ft, ft3/s) -> L/s",
    test_vectors=(
        {"reading": {"raw_value": 1.0, "raw_unit": "ft"}, "params": {}, "expect": {"eng_value": 2.49}, "tol": 0.005, "source": SOURCES_EXTRA["WEIR_V90"] + " Table A7-4 h=1.00 ft"},
        {"reading": {"raw_value": 0.5, "raw_unit": "ft"}, "params": {}, "expect": {"eng_value": 0.446}, "tol": 0.002, "source": SOURCES_EXTRA["WEIR_V90"] + " Table A7-4 h=0.50 ft"},
        {"reading": {"raw_value": 1.5, "raw_unit": "ft"}, "params": {}, "expect": {"eng_value": 6.81}, "tol": 0.02, "source": SOURCES_EXTRA["WEIR_V90"] + " Table A7-4 h=1.50 ft"},
        {"reading": {"raw_value": 0.3, "raw_unit": "ft"}, "params": {"weir_type": "cipolletti", "L_ft": 1.0}, "expect": {"eng_value": 0.553}, "tol": 0.002, "source": SOURCES_EXTRA["WEIR_CIPOLLETTI"] + " Table A7-5 L=1 h=0.30"},
        {"reading": {"raw_value": 0.5, "raw_unit": "ft"}, "params": {"weir_type": "cipolletti", "L_ft": 2.0}, "expect": {"eng_value": 2.38}, "tol": 0.005, "source": SOURCES_EXTRA["WEIR_CIPOLLETTI"] + " Table A7-5 L=2 h=0.50"},
    ),
))

register(Formula(
    "VW_DISP_LIN", "1.0.0", "VW_DISPLACEMENT", "mm", _vw_disp_lin,
    (SOURCES_EXTRA["DISP_LINEAR"], SOURCES_EXTRA["DISP_POLY"], SOURCES_EXTRA["DISP_THERMAL"], core.SOURCES["SIGN_OFFSET"]),
    "VW crackmeter/jointmeter/displacement transducer: D = sign_mult*G(R1-R0) (or poly) + K(T1-T0) + offset (mm)",
    test_vectors=(
        {"reading": {"raw_value": 6000, "raw_unit": "digits"}, "params": {"G": 0.01211, "R0": 4200}, "expect": {"eng_value": 21.79}, "tol": 0.01, "source": SOURCES_EXTRA["DISP_LINEAR"] + " (4420 example; manual truncates 21.798)"},
        {"reading": {"raw_value": 6000, "raw_unit": "digits"}, "params": {"method": "poly", "A": 4.1548e-08, "B": 0.01173, "R0": 4200}, "expect": {"eng_value": 21.88}, "tol": 0.01, "source": SOURCES_EXTRA["DISP_POLY"]},
        {"reading": {"raw_value": 6000, "raw_unit": "digits", "temp_c": 32.9}, "params": {"G": 0.01211, "R0": 4200, "T0": 20.3, "M_th": 0.000376, "B_th": 0.328}, "expect": {"temp_corr": 0.394}, "tol": 0.001, "source": SOURCES_EXTRA["DISP_THERMAL"]},
        {"reading": {"raw_value": 5875, "raw_unit": "digits", "temp_c": 20.0}, "params": {"G": 0.06152, "R0": 4250, "T0": 10.0, "M_th": 0.000369, "B_th": 0.572}, "expect": {"eng_value": 101.656}, "tol": 0.002, "source": SOURCES_EXTRA["DISP_LINEAR"] + " (4450 p.18; manual prints K=0.0168, its own Eq gives 0.1686 -> 101.656, dam-refs verify disp4450_K_recomputed)"},
    ),
))

register(Formula(
    "LEVEL_DATUM", "1.0.0", "WATER_LEVEL", "m", _level_datum,
    (SOURCES_EXTRA["LEVEL_DATUM"], core.SOURCES["SIGN_OFFSET"]),
    "Level sensor: EL = datum_el_m + sign_mult*reading_m + offset (UNVERIFIED: no instrument source)",
    verified=False,
    test_vectors=(
        {"reading": {"raw_value": 12.5, "raw_unit": "m"}, "params": {"datum_el_m": 300.0}, "expect": {"corrected_value": 312.5}, "tol": 1e-9, "source": "constructed (UNVERIFIED rule)"},
        {"reading": {"raw_value": 4.2, "raw_unit": "m"}, "params": {"datum_el_m": 320.0, "sign_mult": -1}, "expect": {"corrected_value": 315.8}, "tol": 1e-9, "source": "constructed (down-looking radar, UNVERIFIED rule)"},
    ),
))


# SCHEMA_CONTRACT.md §5: app seed spells elevations *_elev_m; data layer uses *_el_m.
PARAM_ALIASES = {"top_elev_m": "top_el_m", "tip_elev_m": "tip_el_m", "sensor_elev_m": "sensor_el_m"}


def normalize_params(params) -> dict:
    """Accept dict or list-of-(k, v) (Arrow MAP) and map alias keys to canonical names.
    A canonical key wins if both spellings are present."""
    d = dict(params)
    for alias, canon in PARAM_ALIASES.items():
        if alias in d and canon not in d:
            d[canon] = d[alias]
    return d


def apply_formula(reading: dict, calibration: dict) -> dict:
    """Reduce one canonical reading with the calibration row that is valid at its ts.

    Returns outputs + lineage (calib_id, cal_version, formula_id, formula_version).
    """
    f = get_formula(calibration["formula_id"], calibration.get("formula_version"))
    params = normalize_params(calibration.get("params") or {})
    # ref.calibration.sign_mult / offset are COLUMNS (CONVENTIONS.md); they win over params keys.
    for col in ("sign_mult", "offset"):
        if calibration.get(col) is not None and _f(calibration.get(col)) is not None:
            params[col] = _f(calibration[col])
    out = f.fn(reading, params)
    out.update({
        "calib_id": calibration.get("calib_id"),
        "cal_version": calibration.get("cal_version"),
        "formula_id": f.formula_id,
        "formula_version": f.version,
    })
    return out
