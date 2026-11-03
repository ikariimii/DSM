"""6_THRESHOLDS -> alerts (REPORT §6.1 row 6, D6; procedure §6). Thresholds are Engineer-of-Record values per
instrument and depth zone, stored as SCD2 rows in ref.threshold; nothing here invents a level (no '3-sigma'/FERC
claims, REPORT E10). Built-in rule from the procedure: USACE EM 1110-2-1908 significant movement 0.25 in (6.4 mm)
and the trend rule 'same direction in 3 consecutive surveys' [procedure §6]."""
from __future__ import annotations

USACE_SIGNIFICANT_MM = 6.35    # 0.25 in; procedure §6 quotes '0.25 in (6.4 mm)'


def level(value_mm, zone_thresholds):
    """zone_thresholds: ordered [(name, limit_mm)] ascending; returns highest exceeded name or 'NORMAL'."""
    if value_mm is None:
        return "MISSING"
    lvl = "NORMAL"
    for name, lim in zone_thresholds:
        if abs(value_mm) >= lim:
            lvl = name
    return lvl


def three_consecutive_same_direction(changes_mm, min_step_mm=0.0):
    """True if the last 3 survey-to-survey increments have the same sign (each |step| > min_step_mm)."""
    if len(changes_mm) < 4 or any(c is None for c in changes_mm[-4:]):
        return False
    s = [b - a for a, b in zip(changes_mm[-4:], changes_mm[-3:])]
    return all(x > min_step_mm for x in s) or all(x < -min_step_mm for x in s)
