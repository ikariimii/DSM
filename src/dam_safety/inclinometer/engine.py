"""End-to-end reduction of one survey: raw readings -> D -> active corrections -> reduced rows + audit rows with
before/after top cumulative change (REPORT §6.3 before_value/after_value)."""
from __future__ import annotations

from dataclasses import replace

from . import corrections as C
from .reduction import reduce_survey, top_cum_change_mm


def d_map(readings):
    """RawReading list -> {(depth, axis): D or None}; BAD/MISSING readings give None."""
    out = {}
    for r in readings:
        ok = r.qa_flag in ("OK", "SUSPECT") and r.r0 is not None and r.r180 is not None
        out[(r.depth_top_m, r.axis)] = (r.r0 - r.r180) if ok else None
    return out


def reduce_with_corrections(readings, base_readings, probe, records, instrument_id, survey_id, fill="missing",
                            base_records=()):
    D = d_map(readings)
    Db = None if base_readings is None else C.apply(d_map(base_readings), list(base_records), fill)
    act = C.active(records, instrument_id, survey_id)
    Dc = C.apply(D, act, fill)
    rows = {ax: reduce_survey(Dc, Db, probe, ax) for ax in ("A", "B") if any(a == ax for (_, a) in Dc)}
    audit = []
    for rec in act:
        if rec.correction_type in ("baseline", "spiral"):
            audit.append(rec)
            continue
        before = top_cum_change_mm(reduce_survey(C.apply(D, [x for x in act if x is not rec], fill), Db, probe, rec.axis))
        after = top_cum_change_mm(rows[rec.axis])
        audit.append(replace(rec, before_value=before, after_value=after))
    return rows, audit
