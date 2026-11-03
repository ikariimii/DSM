"""7_VELOCITY -> rates (REPORT §6.1 row 7; procedure §4.2): rate = (cum_n - cum_{n-1}) / days, per depth, in mm/day
and mm/yr (365.25 d). Both surveys must be reduced against the SAME baseline (the workbook's S1->S2 rate mixes S1
absolute with S2 change, REPORT verify T13f note); this function refuses mixed baselines."""
from __future__ import annotations

from datetime import date


def rates(cum_prev: dict, cum_cur: dict, d_prev: date, d_cur: date, base_prev: str, base_cur: str):
    if base_prev != base_cur:
        raise ValueError("rate needs both surveys against the same baseline")
    days = (d_cur - d_prev).days
    if days <= 0:
        raise ValueError("surveys must be in time order")
    out = {}
    for d, c in cum_cur.items():
        p = cum_prev.get(d)
        out[d] = None if (c is None or p is None) else {"mm_per_day": (c - p) / days, "mm_per_yr": (c - p) / days * 365.25}
    return out
