"""Unit conversions used by the formula engine.

SI internally (kPa, m, degC) per /workspace/dam-refs/CONVENTIONS.md "Units and symbols".

Sources
-------
* Geokon 4500 manual, Table 2 "Engineering Units Multiplication Factors", §6.4.2, p.20
  (/workspace/dam-refs/piezometers/txt/4500_Piezometer.txt, PDF page 26):
  1 kPa = 0.10197 m H2O, 1 psi = 6.8947 kPa, 1 bar = 100 kPa, 1 MPa = 1000 kPa,
  1 inHg = 3.3863 kPa (also §6.2: "inches of mercury to kPa is 3.386", to psi 0.491).
* 1 ft = 0.3048 m exactly (international foot definition).
"""
from __future__ import annotations

# multiply a value in <unit> by this factor to get kPa   [GEOKON-4500 Table 2]
TO_KPA = {
    "kpa": 1.0,
    "mpa": 1000.0,
    "psi": 6.8947,
    "bar": 100.0,
    "mbar": 0.1,
    "inhg": 3.3863,
    "mmhg": 0.13332,
    "m_h2o": 9.8068,
    "ft_h2o": 2.98896,
}

# kPa -> metres of fresh water   [GEOKON-4500 Table 2: "m H20" row, "kPa" column = .10197]
KPA_TO_M_H2O = 0.10197

FT_TO_M = 0.3048


def to_kpa(value: float, unit: str) -> float:
    try:
        return value * TO_KPA[unit.lower()]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"unsupported pressure unit {unit!r}") from exc


def length_to_m(value: float, unit: str) -> float:
    u = unit.lower()
    if u in ("m", "metre", "meter"):
        return value
    if u in ("ft", "feet", "foot"):
        return value * FT_TO_M
    if u in ("mm",):
        return value / 1000.0
    if u in ("in", "inch", "inches"):
        return value * 0.0254
    raise ValueError(f"unsupported length unit {unit!r}")
