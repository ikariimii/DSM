"""11_PRINT_REPORT -> dssmr_appendix (REPORT §6.1 row 12, §6.2). Builds the appendix table as data; values are
computed, never typed: sign-safe max |change| with depth from top in mm and in (REPORT E6: MAX() misses negative
movement), survey count computed (E5), correction table straight from CorrectionRecords, software version and input
hashes for reproducibility."""
from __future__ import annotations

from .corrections import CorrectionRecord, info_flags
from .installation import IN, FT


def max_abs_change(rows):
    vals = [r for r in rows if r.cum_change_mm is not None]
    if not vals:
        return None
    r = max(vals, key=lambda x: abs(x.cum_change_mm))
    return {"axis": r.axis, "depth_top_m": r.depth_top_m, "depth_top_ft": r.depth_top_m / FT,
            "value_mm": r.cum_change_mm, "value_in": r.cum_change_mm / (IN * 1000.0)}


def appendix(installation, surveys_qc, rows_by_axis, records: list[CorrectionRecord], alert_level, rate,
             software_version, input_hashes, run_ts_utc):
    peaks = [p for p in (max_abs_change(v) for v in rows_by_axis.values()) if p]
    return {
        "identification": {"instrument_id": installation.instrument_id, "dam_id": installation.dam_id,
                           "system": installation.system, "L_m": installation.probe.L_m, "K": installation.probe.K,
                           "a0_azimuth_deg": installation.a0_azimuth_deg},
        "survey_count": len({q.survey_id for q in surveys_qc}),
        "surveys": [{"survey_id": q.survey_id, "axis": q.axis, "qc": q.flag, "reasons": list(q.reasons)} for q in surveys_qc],
        "corrections": [r.as_row() for r in records],
        "info_flags": list(info_flags(installation, records)),   # e.g. SPIRAL_NOT_APPLIED (never "Applied", REPORT E2)
        "max_change": max(peaks, key=lambda p: abs(p["value_mm"])) if peaks else None,
        "alert_level": alert_level,
        "rate": rate,
        "software_version": software_version,
        "input_sha256": dict(input_hashes),
        "generated_utc": run_ts_utc,
    }
