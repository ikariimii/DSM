"""Pure-Python reduction formulas (no Spark, no numpy required).

Every public function cites its source. ``SOURCES`` maps formula id -> citation and
is used by docs/VERIFICATION.md and the ref.formula seed.

Conventions follow /workspace/dam-refs/CONVENTIONS.md:
  * subscript 0 = initial/zero reading, 1 = current; change = current - initial
  * temperature correction written K*(T1-T0) (added); barometric -(S1-S0) (subtracted)
  * SI internally (kPa, m, degC); elevations in metres above project datum
  * inclinometer: A0 positive (downstream), cumulative summed from fixed bottom
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .units import KPA_TO_M_H2O, length_to_m, to_kpa

REF_GEOKON_4500 = "GEOKON-4500: Geokon Model 4500 Series VW Piezometer manual (/workspace/dam-refs/4500_Piezometer.pdf)"
REF_USBR_EDIM = "USBR-EDIM-1987: USBR Embankment Dam Instrumentation Manual (/workspace/dam-refs/USBR_Embankment_Dam_Instrumentation_Manual_1987.pdf)"
REF_SI_DATAREDUCTION = "SI-DR-2001: Slope Indicator 'Inclinometers: Data Reduction and Error Correction' 2001-01-29 (/workspace/dam-refs/data_reduction.pdf)"
REF_GEOKON_6000 = "GEOKON-6000: Geokon Model 6000 Inclinometer Probe manual (/workspace/dam-refs/6000_Inclinometer_Probe.pdf)"

# Page numbers are PDF pages (form-feed index of the /workspace/dam-refs/txt/*.txt extracts,
# as printed by /workspace/dam-refs/pg.py), matching /workspace/dam-refs/verify_output.txt.
REF_CONVENTIONS = "DAMREFS-CONVENTIONS: /workspace/dam-refs/CONVENTIONS.md (team authority, owner Geotech Instrumentation Expert)"
REF_SETTLE_PROC = "DAMREFS-SETTLEMENT: /workspace/dam-refs/procedures/settlement-survey-gnss.md"
REF_DGSI_PROBE = "DGSI-PROBE: DGSI/Slope Indicator Digitilt Inclinometer Probe manual (/workspace/dam-refs/DGSI_Digitilt_Probe.pdf)"

SOURCES = {
    "VW_DIGITS": REF_GEOKON_4500 + " §6.1 Eq.1, PDF p.22",
    "VW_PIEZO_LINEAR": REF_GEOKON_4500 + " §6.1.1 Eq.2, PDF p.22",
    "VW_PIEZO_POLY": REF_GEOKON_4500 + " §6.1.2 Eq.3 (p.22) and Eq.4 (p.23)",
    "VW_TEMP_CORR": REF_GEOKON_4500 + " §6.4.1 Eq.6, PDF p.25",
    "VW_BARO_CORR": REF_GEOKON_4500 + " §6.2 Eq.5, PDF p.23-24",
    "THERMISTOR_3K": REF_GEOKON_4500 + " App.B Eq.7 + Table 8, PDF p.31",
    "THERMISTOR_10K": REF_GEOKON_4500 + " App.B Eq.8 + Table 9, PDF p.32",
    "PRESSURE_TO_HEAD": REF_GEOKON_4500 + " §6.4.2 Table 2, PDF p.26",
    "PIEZO_ELEVATION": REF_USBR_EDIM + " PDF p.62 and p.69 (pressure head 'algebraically added to the tip elevation')",
    "STANDPIPE_DTW": REF_USBR_EDIM + " 'Data Processing', PDF p.83, p.86, p.87 (depth to water subtracted from top-of-standpipe elevation)",
    "SIGN_OFFSET": REF_CONVENTIONS + " 'Calibration table additionally has: sign_mult INT (+1|-1), offset (value in eng units added after sign_mult). G is ONLY the gauge factor'",
    "SETTLEMENT_SIGN": REF_CONVENTIONS + " 'Sign conventions' (settlement positive, heave negative; change = current - initial); " + REF_SETTLE_PROC + " §0 (corrected = sign_mult x (value - value0)), verify.py Ex14; USBR-EDIM-1987 §58 PDF p.162 (cumulative settlement unsigned, heave minus)",
    "DATUM_REBASELINE": REF_CONVENTIONS + " 'Schema additions (lead, 2026-11-02)'; " + REF_SETTLE_PROC + " §0 and §7 DATUM_CHANGE, verify.py Ex15; DWR-OROVILLE-12D-2021 PDF p.8 R-27b, p.18 R-9b",
    "INCL_COMBINED": REF_SI_DATAREDUCTION + " 'Combining Readings' PDF p.2; " + REF_DGSI_PROBE + " PDF p.12",
    "INCL_CHECKSUM": REF_SI_DATAREDUCTION + " 'Checksums' PDF p.2",
    "INCL_DEVIATION": REF_SI_DATAREDUCTION + " 'Calculating Deviation' PDF p.3; " + REF_DGSI_PROBE + " PDF p.12",
    "INCL_DISPLACEMENT": REF_SI_DATAREDUCTION + " 'Calculating Displacement' PDF p.3; " + REF_GEOKON_6000 + " (offsets vs initial survey, accumulated from fixed end)",
}


# --------------------------------------------------------------------------------------
# Vibrating-wire piezometer
# --------------------------------------------------------------------------------------
def vw_digits_from_hz(freq_hz: float) -> float:
    """digits = Hz^2 / 1000   [GEOKON-4500 §6.1 Eq.1]."""
    return freq_hz * freq_hz / 1000.0


def vw_digits_from_period_us(period_us: float) -> float:
    """digits = (1/Period[s])^2 * 10^-3   [GEOKON-4500 §6.1 Eq.1]."""
    p = period_us * 1e-6
    return (1.0 / p) ** 2 * 1e-3


def vw_pressure_linear(r1: float, r0: float, g: float) -> float:
    """P = G (R1 - R0)   [GEOKON-4500 §6.1.1 Eq.2]. Units of P are those of G."""
    return g * (r1 - r0)


def vw_poly_c(a: float, b: float, r0: float) -> float:
    """C from 0 = A R0^2 + B R0 + C   [GEOKON-4500 §6.1.2 Eq.4]."""
    return -(a * r0 * r0 + b * r0)


def vw_pressure_poly(r1: float, a: float, b: float, c: float) -> float:
    """P = A R1^2 + B R1 + C   [GEOKON-4500 §6.1.2 Eq.3]."""
    return a * r1 * r1 + b * r1 + c


def vw_temp_correction(k: float, t1: float, t0: float) -> float:
    """T_correction = K (T1 - T0); ADDED to P   [GEOKON-4500 §6.4.1 Eq.6]."""
    return k * (t1 - t0)


def vw_baro_correction(s1: float, s0: float, f: float = 1.0) -> float:
    """P_B = (S1 - S0) F; SUBTRACTED from P   [GEOKON-4500 §6.2 Eq.5].

    Apply only to unvented transducers hydraulically connected to atmosphere (open
    well / standpipe); GEOKON-4500 §6.2 warns that sealed/buried tips may see attenuated
    barometric effects. Controlled by calibration param ``baro_correct``.
    """
    return (s1 - s0) * f


def pressure_kpa_to_head_m(p_kpa: float) -> float:
    """Fresh-water head (m) = P(kPa) * 0.10197   [GEOKON-4500 §6.4.2 Table 2]."""
    return p_kpa * KPA_TO_M_H2O


@dataclass(frozen=True)
class VWPiezoResult:
    digits: float
    pressure_raw: float          # in calibration pressure unit, before corrections
    temp_corr: float             # same unit
    baro_corr: float             # same unit
    pressure_kpa: float          # corrected, kPa
    head_m: float                # m of fresh water
    piezometric_el_m: Optional[float]  # m above project datum


def _truthy(v) -> bool:
    """Params arrive as MAP<STRING,STRING> from UC: accept bools, numbers and strings."""
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes", "y", "1.0")
    return bool(v)


def check_sign_mult(sign_mult) -> int:
    """sign_mult must be exactly +1 or -1 [CONVENTIONS.md calibration table]."""
    try:
        sm = int(float(sign_mult))
    except (TypeError, ValueError):
        raise ValueError(f"sign_mult must be +1 or -1, got {sign_mult!r}")
    if sm not in (1, -1) or float(sign_mult) != sm:
        raise ValueError(f"sign_mult must be +1 or -1, got {sign_mult!r}")
    return sm


def apply_sign_offset(term: float, sign_mult=1, offset=0.0) -> float:
    """eng = sign_mult * term + offset  [CONVENTIONS.md: offset is in eng units, added after sign_mult]."""
    return check_sign_mult(sign_mult) * term + float(offset or 0.0)


def vw_piezometer(
    reading: float,
    params: dict,
    temp_c: Optional[float] = None,
    baro: Optional[float] = None,
    reading_unit: str = "digits",
) -> VWPiezoResult:
    """Full VW piezometer reduction driven by calibration ``params``.

    params keys (ref.calibration.params MAP<STRING,DOUBLE|STRING>):
      method: 'linear' | 'poly'
      G (linear) | A, B [, C] (poly); R0 = site zero (digits)
      K (thermal factor, pressure-unit/degC), T0 (site zero temp)
      S0 (site zero barometer, baro_unit), baro_correct (0/1), baro_unit ('kpa','inhg',...)
      pressure_unit ('kpa','psi','mpa','bar') -> unit of G/A/B/K
      sensor_el_m (sensing elevation, m above datum)
      sign_mult (+1|-1, default +1) multiplies the gauge term; offset (kPa, default 0) is
      added after sign_mult [CONVENTIONS.md]. With sign_mult given, G is the gauge factor
      only (manufacturer P = G(R1-R0) with negative G == sign_mult -1, |G|). Corrections
      K(T1-T0) and -(S1-S0) keep their own signs (not multiplied by sign_mult).
    """
    if reading_unit == "digits":
        r1 = float(reading)
    elif reading_unit == "hz":
        r1 = vw_digits_from_hz(reading)
    elif reading_unit == "period_us":
        r1 = vw_digits_from_period_us(reading)
    else:
        raise ValueError(f"unsupported VW reading unit {reading_unit!r}")

    method = str(params.get("method", "linear")).lower()
    r0 = float(params["R0"])
    if method == "linear":
        p = vw_pressure_linear(r1, r0, float(params["G"]))
    elif method in ("poly", "polynomial"):
        a, b = float(params["A"]), float(params["B"])
        c = params.get("C")
        c = vw_poly_c(a, b, r0) if c is None or (isinstance(c, float) and math.isnan(c)) else float(c)
        p = vw_pressure_poly(r1, a, b, c)
    else:
        raise ValueError(f"unsupported VW method {method!r}")
    p = check_sign_mult(params.get("sign_mult", 1)) * p

    punit = str(params.get("pressure_unit", "kpa"))
    tcorr = 0.0
    if temp_c is not None and params.get("K") not in (None, 0) and params.get("T0") is not None:
        tcorr = vw_temp_correction(float(params["K"]), float(temp_c), float(params["T0"]))
    bcorr = 0.0
    if baro is not None and _truthy(params.get("baro_correct")) and params.get("S0") is not None:
        # barometer in baro_unit -> convert the *difference* to the pressure unit
        bunit = str(params.get("baro_unit", "kpa"))
        diff_kpa = to_kpa(float(baro) - float(params["S0"]), bunit)
        bcorr = diff_kpa / to_kpa(1.0, punit)
    p_corr = p + tcorr - bcorr
    p_kpa = to_kpa(p_corr, punit) + float(params.get("offset") or 0.0)
    head = pressure_kpa_to_head_m(p_kpa)
    el = params.get("sensor_el_m", params.get("tip_el_m"))
    piezo_el = (float(el) + head) if el is not None else None
    return VWPiezoResult(r1, p, tcorr, bcorr, p_kpa, head, piezo_el)


def thermistor_temp_c(resistance_ohm: float, kind: str = "3k") -> float:
    """Steinhart-Hart thermistor temperature (degC).

    3k : T = 1/(A + B ln R + C (ln R)^3) - 273.15, A=1.4051E-3 B=2.369E-4 C=1.019E-7
         [GEOKON-4500 App.B Eq.7, PDF p.31]
    10k: T = 1/(A + B ln R + C (ln R)^3 + D (ln R)^5) - 273.15,
         A=1.127670E-3 B=2.344442E-4 C=8.476921E-8 D=1.175122E-11 [App.B Eq.8, PDF p.32]
    """
    lr = math.log(resistance_ohm)
    if kind == "3k":
        inv = 1.4051e-3 + 2.369e-4 * lr + 1.019e-7 * lr ** 3
    elif kind == "10k":
        inv = 1.127670e-3 + 2.344442e-4 * lr + 8.476921e-8 * lr ** 3 + 1.175122e-11 * lr ** 5
    else:
        raise ValueError(kind)
    return 1.0 / inv - 273.15


# --------------------------------------------------------------------------------------
# Open standpipe / observation well
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class StandpipeResult:
    water_el_m: Optional[float]
    depth_m: float
    is_dry: bool


def standpipe_elevation(depth_to_water: float, params: dict, unit: str = "m") -> StandpipeResult:
    """Piezometric level = elevation of top of standpipe - depth to water
    [USBR-EDIM-1987 pp.83,86,87 'Data Processing'].

    params: top_el_m (top-of-pipe/measuring-point elevation), optional tip_el_m
    (bottom of standpipe). A depth that reaches the tip means a DRY reading: the level
    is at or below the tip and the elevation is not reported (None).
    """
    d = length_to_m(float(depth_to_water), unit)
    top = float(params["top_el_m"])
    el = top - d
    tip = params.get("tip_el_m")
    if tip is not None and el <= float(tip) + 1e-9:
        return StandpipeResult(None, d, True)
    return StandpipeResult(el, d, False)


# --------------------------------------------------------------------------------------
# Probe inclinometer (two-pass survey)
# --------------------------------------------------------------------------------------
def incl_combined(r0: float, r180: float) -> float:
    """Combined reading = (R0 - R180)/2 ; cancels bias   [SI-DR-2001 PDF p.2].

    NOTE (QA.md 'Gotcha'): the same note also calls the un-halved difference 698 a
    "combined reading". This codebase stores the UN-HALVED difference ``diff = R0 - R180``
    in silver.incl_reduced.diff_a/diff_b (as GTILT/DigiPro do) and always divides by 2K
    in incl_deviation; ``incl_combined`` is the halved value, for display only.
    """
    return (r0 - r180) / 2.0


def incl_checksum(r0: float, r180: float) -> float:
    """Checksum = R0 + R180 (≈ 2 x bias)   [SI-DR-2001 p.2]."""
    return r0 + r180


def incl_deviation(r0: float, r180: float, interval: float, k: float) -> float:
    """Lateral deviation = L (R0 - R180) / (2K), in units of L   [SI-DR-2001 p.3]."""
    return interval * (r0 - r180) / (2.0 * k)


@dataclass(frozen=True)
class InclDepthReading:
    depth: float
    a0: float
    a180: float
    b0: Optional[float] = None
    b180: Optional[float] = None


@dataclass(frozen=True)
class InclProfileRow:
    depth: float
    checksum_a: float
    checksum_b: Optional[float]
    dev_a: float
    dev_b: Optional[float]
    inc_a: float              # incremental displacement vs initial survey
    inc_b: Optional[float]
    cum_a: float              # cumulative displacement
    cum_b: Optional[float]
    resultant: Optional[float]


def _axis(r: InclDepthReading, axis: str):
    if axis == "A":
        return r.a0, r.a180
    return r.b0, r.b180


def incl_profile(
    current: Sequence[InclDepthReading],
    initial: Sequence[InclDepthReading],
    interval: float,
    k: float,
    fixed: str = "bottom",
) -> list[InclProfileRow]:
    """Incremental and cumulative displacement profile.

    incremental_i = L * ((R0-R180)_cur - (R0-R180)_init) / (2K)  [SI-DR-2001 p.3]
    cumulative: summed from the fixed end  [GEOKON-6000 §2 p.5; dam-refs CONVENTIONS
    'cumulative displacement summed from the bottom (fixed toe)'].

    Depth is to the probe's upper wheels; reading i spans depth_i .. depth_i + L, so
      fixed='bottom': cum(d) = sum of inc_i for depth_i >= d   (zero below deepest interval)
      fixed='top'   : cum(d) = -sum of inc_i for depth_i <  d  (zero at the top reading)
    Depths present in only one survey are dropped (caller should QC that separately).
    """
    if fixed not in ("bottom", "top"):
        raise ValueError("fixed must be 'bottom' or 'top'")
    init = {round(r.depth, 6): r for r in initial}
    rows = []
    for r in sorted(current, key=lambda x: x.depth):
        i = init.get(round(r.depth, 6))
        if i is None:
            continue
        rows.append((r, i))
    has_b = all(r.b0 is not None and r.b180 is not None and i.b0 is not None and i.b180 is not None for r, i in rows)

    inc_a, inc_b, out = [], [], []
    for r, i in rows:
        inc_a.append(incl_deviation(r.a0, r.a180, interval, k) - incl_deviation(i.a0, i.a180, interval, k))
        if has_b:
            inc_b.append(incl_deviation(r.b0, r.b180, interval, k) - incl_deviation(i.b0, i.b180, interval, k))

    n = len(rows)

    def cumulate(inc: list[float]) -> list[float]:
        if fixed == "bottom":
            acc, cum = 0.0, [0.0] * n
            for j in range(n - 1, -1, -1):
                acc += inc[j]
                cum[j] = acc
            return cum
        acc, cum = 0.0, [0.0] * n
        for j in range(n):
            cum[j] = -acc
            acc += inc[j]
        return cum

    cum_a = cumulate(inc_a)
    cum_b = cumulate(inc_b) if has_b else [None] * n
    for j, (r, _) in enumerate(rows):
        res = math.hypot(cum_a[j], cum_b[j]) if has_b else None
        out.append(
            InclProfileRow(
                depth=r.depth,
                checksum_a=incl_checksum(r.a0, r.a180),
                checksum_b=incl_checksum(r.b0, r.b180) if has_b else None,
                dev_a=incl_deviation(r.a0, r.a180, interval, k),
                dev_b=incl_deviation(r.b0, r.b180, interval, k) if has_b else None,
                inc_a=inc_a[j],
                inc_b=inc_b[j] if has_b else None,
                cum_a=cum_a[j],
                cum_b=cum_b[j],
                resultant=res,
            )
        )
    return out


def incl_checksum_flags(checksums: Iterable[float], tolerance: float) -> list[bool]:
    """Flag depths whose checksum departs from the survey median by more than
    ``tolerance`` reading units. Checksums should be ~constant (2 x bias) through a
    survey [SI-DR-2001 p.2]; the tolerance itself is a site/probe setting (UNVERIFIED
    default, see docs/VERIFICATION.md)."""
    cs = list(checksums)
    if not cs:
        return []
    s = sorted(cs)
    m = s[len(s) // 2] if len(s) % 2 else 0.5 * (s[len(s) // 2 - 1] + s[len(s) // 2])
    return [abs(c - m) > tolerance for c in cs]
