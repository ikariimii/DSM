"""Pure-Python core of the silver.incl_reduced job (src/jobs/reduce_inclinometers.py). No Spark imports.

Per instrument, from table rows (dicts):
  ref.incl_installation (current row)  -> Installation (+ depth_reference, raw_unit)
  silver.incl_survey rows              -> RawReading per (survey, depth, axis) via survey_import (sign once,
                                          BAD/MISSING checks); workbook 'from_bottom_label' depths re-keyed to the
                                          interval top from top of casing (procedure §4.0, REPORT D2), raw label kept
  ref.incl_correction rows             -> CorrectionRecord (invalid stored rows are skipped and flagged)
then engine.reduce_with_corrections (procedure §4-§5: only applied, reviewed, non-superseded records; spiral never),
checksum QC (qc.survey_qc, procedure §7) and between-set bias QC (qc.between_set_bias_qc, QA-020 + QA-021 rule, §5/§7.1; proposes only),
and emits rows with exactly the silver.incl_reduced columns (sql/ddl/06_silver.sql).

BIAS-RULE HOOK: the bias rule is injected (`bias_rule=`, default `DEFAULT_BIAS_RULE`, rule QA021-v2 = procedure §5
lead ruling 2026-11-02). On any rule change bump qc.BIAS_RULE; BIAS_RULE_VERSION goes into every row's qc_rule_version
(formula_version stays ENGINE_VERSION so it joins ref.formula) and the job re-reduces surveys whose latest row carries
another formula_version or qc_rule_version. Nothing here applies a bias proposal."""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import corrections as C
from . import qc
from .engine import reduce_with_corrections
from .installation import Installation, Probe, interval_top_from_label
from .survey_import import SIGN_MULT, make_reading

ENGINE_FORMULA_ID = "INCL_PROBE_2PASS"            # formulas/registry.py
ENGINE_VERSION = "1.0.0"
BIAS_RULE_VERSION = qc.BIAS_RULE                  # "QA021-v2" (§5 lead rulings); bump on any bias-rule change
DEFAULT_BIAS_RULE = qc.between_set_bias_qc        # signature (installation, axis, suspect_id, suspect, reference,
                                                  #            reference_survey_id=None) -> qc.BiasQC

REDUCED_COLUMNS = ("dam_id", "instrument_id", "survey_id", "survey_ts", "initial_survey_ts", "depth_m", "diff_a",
                   "diff_b", "checksum_a", "checksum_b", "dev_a", "dev_b", "inc_a", "inc_b", "cum_a", "cum_b",
                   "resultant", "fixed_end", "qa_flag", "qa_reason", "calib_id", "formula_id", "formula_version",
                   "recompute_batch_id", "reduced_ts", "depth_reference", "baseline_survey_id",
                   "correction_record_ids", "raw_depth_label", "qc_rule_version", "installation_version",
                   "probe_constants")
_SEV = {"OK": 0, "SUSPECT": 1, "MISSING": 2, "BAD": 3}
_PROFILE = {"from_bottom_label": "workbook", "top_of_casing": "gtilt"}   # gtilt == digipro: native from-top depths


@dataclass(frozen=True)
class InstallationRow:
    installation: Installation
    depth_reference: str
    raw_unit: str


def installation_from_row(r: dict) -> InstallationRow:
    if r["depth_reference"] not in _PROFILE:
        raise ValueError(f"unknown depth_reference {r['depth_reference']!r}")
    sysname = r.get("system") or "probe"
    for ax in ("A", "B"):                                 # DDL sign_mult_* must agree with the one sign table used
        v = r.get(f"sign_mult_{ax}")
        if v is not None and int(v) != int(SIGN_MULT[sysname][ax]):
            raise ValueError(f"{r['instrument_id']}: sign_mult_{ax}={v} disagrees with {sysname} sign {SIGN_MULT[sysname][ax]}")
    inst = Installation(r["instrument_id"], r["dam_id"], Probe(float(r["probe_L_m"]), float(r["probe_K"]), r["raw_unit"]),
                        float(r["casing_depth_m"]), float(r.get("a0_azimuth_deg") or 0.0), r.get("baseline_survey_id"),
                        r.get("system") or "probe", r.get("spiral_survey_id"),
                        None if r.get("stable_zone_top_m") is None else float(r["stable_zone_top_m"]))
    return InstallationRow(inst, r["depth_reference"], r["raw_unit"])


def _num_or_str(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def correction_from_row(r: dict) -> C.CorrectionRecord:
    rng = r.get("depth_range")
    if isinstance(rng, str) and rng:
        a, b = rng.split("..")
        rng = (float(a), float(b))
    vals = {f: r.get(f) for f in C.AUDIT_FIELDS}
    vals.update(depth_range=rng or None, override_value=_num_or_str(r.get("override_value")),
                applied=bool(r.get("applied")), timestamp_utc=str(r.get("timestamp_utc") or ""),
                reviewed_utc=None if r.get("reviewed_utc") is None else str(r["reviewed_utc"]),
                software_version=r.get("software_version") or "", input_file_sha256=r.get("input_file_sha256") or "")
    return C.CorrectionRecord(**vals)


def _readings(ir: InstallationRow, rows, label_unit_m: float = 1.0):
    """silver.incl_survey rows -> {survey_id: (survey_ts, [RawReading], {depth_top: raw label})}.
    label_unit_m: metres per unit of silver.incl_survey.depth_m. The column is named metres, so 1.0; UNVERIFIED for
    workbook files (labels are feet from bottom, REPORT D2) until the workbook landing path is confirmed to convert."""
    inst, out = ir.installation, {}
    prof = _PROFILE[ir.depth_reference]
    for r in rows:
        top, _ = interval_top_from_label(prof, r["depth_m"], label_unit_m, inst.casing_depth_m, inst.probe.L_m)
        ts, rd, labels = out.setdefault(r["survey_id"], (r["survey_ts"], [], {}))
        labels[top] = str(r["depth_m"])                     # label exactly as received (DDL 06 raw_depth_label)
        for ax, a, b in (("A", "a0", "a180"), ("B", "b0", "b180")):
            if r.get(a) is None and r.get(b) is None:
                continue
            rd.append(make_reading(inst.instrument_id, r["survey_id"], str(ts), top, ax, r.get(a), r.get(b),
                                   ir.raw_unit, system=inst.system))
    return out


def _pairs(readings, axis):
    return {r.depth_top_m: (r.r0, r.r180) for r in readings
            if r.axis == axis and r.qa_flag in ("OK", "SUSPECT")}


def reduce_instrument(inst_row: dict, survey_rows, correction_rows, batch_id: str, reduced_ts,
                      target_surveys=None, bias_rule=DEFAULT_BIAS_RULE, label_unit_m: float = 1.0):
    """Returns (reduced rows [dict with REDUCED_COLUMNS], bias proposals [qc.BiasQC, status PROPOSED or
    SUSPECT_CROSSCHECK; review-only, never applied]).
    target_surveys: survey_ids to emit (None = all); all surveys are still read for baseline/reference."""
    ir = installation_from_row(inst_row)
    inst = ir.installation
    probe_constants = (f"L_m={inst.probe.L_m!r};K={inst.probe.K!r};raw_unit={ir.raw_unit};system={inst.system};"
                       f"sign_mult_A={SIGN_MULT[inst.system]['A']};sign_mult_B={SIGN_MULT[inst.system]['B']}")
    surveys = _readings(ir, survey_rows, label_unit_m)
    if not surveys:
        return [], []
    recs, bad_recs = [], set()
    for r in correction_rows:
        try:
            recs.append(correction_from_row(r))
        except (ValueError, TypeError, KeyError):
            bad_recs.add(r.get("survey_id"))                       # invalid stored record: skipped, flagged
    order = sorted(surveys, key=lambda s: (str(surveys[s][0]), s))
    base_id = inst.baseline_survey_id if inst.baseline_survey_id in surveys else order[0]   # None = earliest
    base_ts, base_rd, _ = surveys[base_id]
    base_recs = C.active(recs, inst.instrument_id, base_id)
    info = C.info_flags(inst, recs)
    out_rows, proposals = [], []
    for sid in order:
        if target_surveys is not None and sid not in target_surveys:
            continue
        ts, rd, labels = surveys[sid]
        reasons, sev = [], 0
        try:
            rows, _audit = reduce_with_corrections(rd, base_rd, inst.probe, recs, inst.instrument_id, sid,
                                                   base_records=base_recs)
            applied_ids = [x.record_id for x in sorted(C.active(recs, inst.instrument_id, sid),
                                                       key=lambda x: (C.APPLY_ORDER.index(x.correction_type)
                                                                      if x.correction_type in C.APPLY_ORDER else 99,
                                                                      x.timestamp_utc, x.record_id))
                           if x.correction_type != "baseline"]
        except (ValueError, NotImplementedError) as e:          # e.g. depth shift not whole intervals
            rows, applied_ids = {}, []
            reasons.append(f"CORRECTION_ERROR:{type(e).__name__}")
            sev = 3
        if sid in bad_recs:
            reasons.append("CORRECTION_RECORD_INVALID")
            sev = max(sev, 1)
        for ax in ("A", "B"):
            cs = {r.depth_top_m: (None if r.r0 is None or r.r180 is None else r.r0 + r.r180) for r in rd if r.axis == ax}
            if not cs:
                continue
            sq = qc.survey_qc(sid, ax, cs)
            if sq.flag != "MISSING":                              # all-missing axis: rows carry MISSING already
                reasons += [f"{x}_{ax}" for x in sq.reasons if x != "MISSING_PASS"]
            sev = max(sev, _SEV["SUSPECT"] if sq.flag == "SUSPECT" else 0)
            if sid == base_id:
                continue
            has_bias = any(x.correction_type == "bias" and x.axis == ax for x in C.active(recs, inst.instrument_id, sid))
            bq = bias_rule(inst, ax, sid, _pairs(rd, ax), _pairs(base_rd, ax), base_id)
            if bq.suspect_flag:
                reasons.append(f"BIAS_CS_SHIFT_{ax}")
            if bq.stable_zone_assumed:
                reasons.append("STABLE_ZONE_ASSUMED")
            proposed = bq.status in ("PROPOSED", "SUSPECT_CROSSCHECK")
            if proposed and not has_bias:
                reasons.append(f"BIAS_PROPOSED_{ax}")
                if bq.status == "SUSPECT_CROSSCHECK":
                    reasons.append(f"BIAS_CROSSCHECK_{ax}")            # proposal ranked SUSPECT (review flag)
                proposals.append(bq)
            elif bq.status == "BAD_RESURVEY":
                reasons.append(f"BIAS_BAD_RESURVEY_{ax}")
            if not (has_bias and proposed):
                sev = max(sev, _SEV[bq.qa_flag])
        survey_reason = C.merge_qa_reason(",".join(dict.fromkeys(reasons)), info)
        by = {(r.depth_top_m, r.axis): r for ax_rows in rows.values() for r in ax_rows}
        rd_by = {(r.depth_top_m, r.axis): r for r in rd}
        for d in sorted({d for (d, _a) in rd_by}):
            A, B = by.get((d, "A")), by.get((d, "B"))
            ra, rb = rd_by.get((d, "A")), rd_by.get((d, "B"))
            rsev = sev
            row_reasons = [x for r in (ra, rb) if r is not None and r.qa_reason for x in r.qa_reason.split(",")]
            for r in (ra, rb):
                if r is not None and r.qa_flag != "OK":
                    rsev = max(rsev, _SEV[r.qa_flag])
            ca, cb = (A.cum_change_mm if A else None), (B.cum_change_mm if B else None)
            out_rows.append({
                "dam_id": inst.dam_id, "instrument_id": inst.instrument_id, "survey_id": sid, "survey_ts": ts,
                "initial_survey_ts": base_ts, "depth_m": d,
                "diff_a": A.D if A else None, "diff_b": B.D if B else None,
                "checksum_a": None if ra is None or ra.r0 is None or ra.r180 is None else ra.r0 + ra.r180,
                "checksum_b": None if rb is None or rb.r0 is None or rb.r180 is None else rb.r0 + rb.r180,
                "dev_a": A.dev_mm if A else None, "dev_b": B.dev_mm if B else None,
                "inc_a": A.change_mm if A else None, "inc_b": B.change_mm if B else None,
                "cum_a": ca, "cum_b": cb,
                "resultant": None if ca is None or cb is None else math.hypot(ca, cb),
                "fixed_end": "bottom",
                "qa_flag": [k for k, v in _SEV.items() if v == rsev][0],
                "qa_reason": C.merge_qa_reason(survey_reason, dict.fromkeys(row_reasons)) or None,
                "calib_id": None, "formula_id": ENGINE_FORMULA_ID,
                "formula_version": ENGINE_VERSION,              # joins ref.formula (INCL_PROBE_2PASS 1.0.0, verified)
                "recompute_batch_id": batch_id, "reduced_ts": reduced_ts,
                "depth_reference": ir.depth_reference, "baseline_survey_id": base_id,
                "correction_record_ids": applied_ids, "raw_depth_label": labels.get(d),
                "qc_rule_version": BIAS_RULE_VERSION, "installation_version": inst_row.get("effective_from"),
                "probe_constants": probe_constants,
            })
    return out_rows, proposals
