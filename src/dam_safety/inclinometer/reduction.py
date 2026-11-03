"""5_CALCULATIONS -> reduction (REPORT §6.1 row 5; procedure §4.0-§4.2).

  dev_i   = L * D_i / (2K)                       (m -> reported mm)       [SI-DR-2001 p.3; procedure §4.1]
  change  = dev(current, corrected) - dev(baseline, corrected)            baseline selectable (REPORT D9)
  cum(d)  = sum of change_i over intervals whose TOP is at or below d     summed from the fixed bottom, INCLUDING the
                                                                          interval whose top is d (procedure §4.0)
A missing interval makes every cumulative value above it MISSING (no IFERROR->0, REPORT E9)."""
from __future__ import annotations

from dataclasses import dataclass

from .installation import Probe


@dataclass(frozen=True)
class ReducedRow:
    depth_top_m: float
    axis: str
    D: float | None
    dev_mm: float | None
    change_mm: float | None
    cum_change_mm: float | None
    cum_dev_mm: float | None      # absolute profile, from bottom


def deviation_mm(D, probe: Probe):
    return None if D is None else probe.L_m * D / (2.0 * probe.K) * 1000.0


def reduce_survey(D: dict, D_base: dict | None, probe: Probe, axis: str) -> list[ReducedRow]:
    """D, D_base: {(depth_top_m, axis): value} (already corrected). Rows returned top-down."""
    depths = sorted(d for (d, a) in D if a == axis)
    dev = {d: deviation_mm(D[(d, axis)], probe) for d in depths}
    if D_base is None:
        chg = {d: None for d in depths}
    else:
        chg = {}
        for d in depths:
            b = D_base.get((d, axis))
            chg[d] = None if (dev[d] is None or b is None) else dev[d] - deviation_mm(b, probe)

    def cum(vals):
        acc, out, broken = 0.0, {}, False
        for d in reversed(depths):                     # bottom -> top
            v = vals[d]
            if v is None:
                broken = True
            if broken:
                out[d] = None
                continue
            acc += v
            out[d] = acc
        return out

    cc = cum(chg) if D_base is not None else {d: None for d in depths}
    cd = cum(dev)
    return [ReducedRow(d, axis, D[(d, axis)], dev[d], chg[d], cc[d], cd[d]) for d in depths]


def top_cum_change_mm(rows):
    return rows[0].cum_change_mm if rows else None
