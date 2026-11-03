"""3_QC_CHECKSUMS -> qc (REPORT §6.1 row 3; procedure §7).

Per survey and axis: mean / SD of CS = R0 + R180, individual CS outside +-10 (A) / +-20 (B) of the axis mean,
|mean CS| > 100, and the mean-checksum shift vs the previous survey. A shift is SUSPECT 'investigate' only; it is NOT
a bias correction by itself [MIKKELSEN-2003 PDF p4-5]. The workbook's ABS(mean CS) > 20 test (flags 8/8 surveys,
REPORT E8 / verify T13p) is deliberately not used. Tolerances are site settings; the defaults are the procedure's."""
from __future__ import annotations

import statistics as st
from dataclasses import dataclass

IND_TOL = {"A": 10.0, "B": 20.0}       # procedure §7 'Individual checksums' row
MEAN_ABS_MAX = 100.0                   # procedure §7 '|mean checksum| > 100 -> SUSPECT'


@dataclass(frozen=True)
class SurveyQC:
    survey_id: str
    axis: str
    n: int
    mean_cs: float | None
    sd_cs: float | None
    delta_mean_cs: float | None        # vs previous survey (None for the first)
    outlier_depths: tuple[float, ...]
    missing_depths: tuple[float, ...]
    flag: str                          # OK | SUSPECT | MISSING
    reasons: tuple[str, ...]


def survey_qc(survey_id, axis, cs_by_depth: dict, prev_mean_cs=None, delta_tol=None, sd_typical=None,
              sd_tol=5.0) -> SurveyQC:
    """cs_by_depth: {depth_top_m: checksum or None}. delta_tol: site threshold for the mean-CS shift (None = report
    the statistic, flag nothing; UNVERIFIED default, procedure gives none). sd_typical: probe's typical SD (optional)."""
    vals = {d: v for d, v in cs_by_depth.items() if v is not None}
    missing = tuple(sorted(d for d, v in cs_by_depth.items() if v is None))
    reasons = []
    if not vals:
        return SurveyQC(survey_id, axis, 0, None, None, None, (), missing, "MISSING", ("no checksums",))
    m = st.fmean(vals.values())
    sd = st.pstdev(vals.values()) if len(vals) > 1 else 0.0
    out = tuple(sorted(d for d, v in vals.items() if abs(v - m) > IND_TOL[axis]))
    dm = None if prev_mean_cs is None else m - prev_mean_cs
    if abs(m) > MEAN_ABS_MAX:
        reasons.append("MEAN_CS_GT_100")
    if out:
        reasons.append("INDIVIDUAL_CS")
    if sd_typical is not None and abs(sd - sd_typical) > sd_tol:
        reasons.append("CS_SD")
    if dm is not None and delta_tol is not None and abs(dm) > delta_tol:
        reasons.append("DELTA_CS_INVESTIGATE")
    if missing:
        reasons.append("MISSING_PASS")
    flag = "SUSPECT" if any(r != "MISSING_PASS" for r in reasons) else ("MISSING" if missing else "OK")
    return SurveyQC(survey_id, axis, len(vals), m, sd, dm, out, missing, flag, tuple(reasons))


# ---- QA-020/QA-021 between-set bias QC (procedure §7.1; §5 bias row governs) ------------------------------------------------
# Sits beside the checksum flag above. It PROPOSES a ZA/ZB bias-shift value and never applies it: the output can be
# turned into an applied=False ref.incl_correction row (bias_proposal_record) for a human to review and apply.
# Every numeric default below is configurable and is UNVERIFIED engineering judgment (no source number) except where
# noted; params_unverified=True travels with every result.
import math                                                    # noqa: E402

BIAS_STATUS = ("NO_PROPOSAL", "PROPOSED", "SUSPECT_CROSSCHECK", "BAD_RESURVEY")
BIAS_RULE = "QA021-v2"   # §5 lead ruling 2026-11-02 (QA-021 sign, QA-023 cross-check) + Inclinometer Specialist ruling
                          # 19:55 ET: magnitude-only cross-check, no sign gate. Replaces the QA-020 0.5 ratio.


@dataclass(frozen=True)
class BiasQCParams:
    cs_shift_suspect: tuple = (("A", 10.0), ("B", 20.0))   # |Δ mean CS| suspect vs reference; UNVERIFIED (mirrors DMM
                                                           # individual-checksum bands). Investigate only.
    min_stable_intervals: int = 5                          # gate (i) n_stable_min; 5 sourced as typical [MIKKELSEN-2003 p4;
                                                           # DIGIPRO2 p36]; as a rule UNVERIFIED
    significance_k: float = 3.0                            # gate (ii) bias_signif_k: |ΔD| > k·SE(ΔD); UNVERIFIED
    max_spread: tuple = (("A", 8.0), ("B", 16.0))          # gate (iii) SD(ΔD) <= value; UNVERIFIED (MIKKELSEN-2003 p6 σ bands)
    crosscheck_tol_abs: float = 2.0                        # gate (iv) bias_crosscheck_tol_abs, reading units; UNVERIFIED.
                                                           # Ranks proposals only (AGREE / SUSPECT), never applies or
                                                           # rejects: procedure §5 ruling, GTILT PDF p45 (<1% of datasets)
                                                           # -> not a production blocker (docs/VERIFICATION.md INC-QA021)
    crosscheck_tol_k: float = 3.0                          # gate (iv) bias_crosscheck_tol_k × SE(ΔD); UNVERIFIED, ranks only
    max_abs_c: float = 25.0                                # gate (v) |ZA| <= 25 else BAD / resurvey; UNVERIFIED as a rule
                                                           # ("Generally ... does not exceed 25 units" MIKKELSEN-2003 p7)
    fallback_bottom_intervals: int = 5                     # stable_zone_top missing -> bottom 5; UNVERIFIED fallback
    params_unverified: bool = True

    def per_axis(self, name, axis):
        return dict(getattr(self, name))[axis]


@dataclass(frozen=True)
class BiasQC:
    survey_id: str
    axis: str
    reference_survey_id: str
    delta_mean_cs: float | None        # mean CS(suspect) − mean CS(reference)
    suspect_flag: bool                 # |delta_mean_cs| > cs_shift_suspect: investigate only, never a correction
    n: int                             # stable intervals with both sets present
    mean_dD: float | None              # ΔD = mean(D_suspect − D_reference) over the stable zone, reading units (§5:
                                       # every Δ is suspect − reference; ΔD = mean(ΔA0 − ΔA180))
    sd: float | None                   # sample SD of the per-depth ΔD (gate iii)
    se: float | None                   # SD/√n (gates ii, iv)
    spread: float | None               # max − min of ΔD (evidence only)
    mean_cs_change: float | None       # bs = mean(ΔA0 + ΔA180) over the stable zone (§5 cross-check, full value)
    crosscheck_diff: float | None      # ||ΔD| − |bs|| (procedure §7.1 row: magnitudes)
    crosscheck_tol: float | None       # max(crosscheck_tol_abs, crosscheck_tol_k·SE)
    abs_dD: float | None               # |ΔD| (shown with the proposal, §5 lead ruling)
    abs_bs: float | None               # |Δ0 + Δ180| (shown with the proposal; cross-check only, never sets ZA)
    crosscheck_verdict: str | None     # 'AGREE' | 'SUSPECT' (tolerance verdict; ranks proposals, review flag only)
    bse_mm: float | None               # cumulative correction over the stable zone, mm: Σ(−ΔD_i)·C
    za_proposed_c: float | None        # ZA/ZB = −ΔD, applied to A0 (D' = D + ZA); set when status is PROPOSED or
                                       # SUSPECT_CROSSCHECK. Never applied here: a reviewer approves each record.
    status: str                        # BIAS_STATUS
    gates: tuple                       # ((gate, passed), ...) in order i..v
    stable_zone_top_m: float
    stable_zone_assumed: bool
    params_unverified: bool
    qa_flag: str                       # OK | SUSPECT | BAD (BAD only for |ZA| > max_abs_c)
    rule: str = BIAS_RULE
    requires_review: bool = True       # always: no bias correction is ever auto-applied (§5 automation ruling)


def stable_zone(installation, params: BiasQCParams = BiasQCParams()):
    """(stable interval tops, stable_zone_top_m, assumed). Intervals whose top is at/below stable_zone_top_m."""
    tops = installation.interval_tops_m()
    if installation.stable_zone_top_m is None:
        z = tops[-params.fallback_bottom_intervals:]
        return z, z[0], True
    return [t for t in tops if t >= installation.stable_zone_top_m - 1e-9], installation.stable_zone_top_m, False


def between_set_bias_qc(installation, axis, suspect_survey_id, suspect: dict, reference: dict,
                        reference_survey_id=None, params: BiasQCParams = BiasQCParams()) -> BiasQC:
    """Procedure §5 bias row (lead ruling 2026-11-02, QA-021/QA-023) + §7.1 gates. PROPOSES only.
    suspect / reference: {interval_top_m: (r0, r180)} for one axis (None for a missing pass).
    reference_survey_id defaults to installation.baseline_survey_id (user-overridable).
    Gates: (i) n >= n_stable_min; (ii) |ΔD| > k·SE; (iii) SD <= max; (iv) ||ΔD| − |bs|| <= max(tol_abs, tol_k·SE)
    (magnitudes only, §5/§7.1; no sign gate, Specialist/lead ruling 2026-11-02); (v) |ZA| <= 25.
    (i)-(iii) fail -> NO_PROPOSAL; (v) fail -> BAD_RESURVEY; else a proposal ZA = −ΔD (applied to A0) with
    status PROPOSED (iv agrees) or SUSPECT_CROSSCHECK (iv disagrees: a review flag that ranks the proposal, NOT a
    rejection). bs is a cross-check only and never sets ZA. Nothing is applied: every record needs reviewer approval."""
    ref_id = reference_survey_id or installation.baseline_survey_id
    if not ref_id:
        raise ValueError("no reference set: installation has no baseline_survey_id and none was given")
    if ref_id == suspect_survey_id:
        raise ValueError("reference set must differ from the suspect set")

    def ok(v):
        return v is not None and v[0] is not None and v[1] is not None

    def mean_cs(sv):
        cs = [a + b for a, b in (v for v in sv.values() if ok(v))]
        return st.fmean(cs) if cs else None

    ms, mr = mean_cs(suspect), mean_cs(reference)
    dcs = None if ms is None or mr is None else ms - mr
    suspect_flag = dcs is not None and abs(dcs) > params.per_axis("cs_shift_suspect", axis)

    zone, z_top, assumed = stable_zone(installation, params)
    keys = [d for d in zone if ok(suspect.get(d)) and ok(reference.get(d))]
    n = len(keys)
    d0 = [suspect[d][0] - reference[d][0] for d in keys]                   # ΔA0  (suspect − reference)
    d180 = [suspect[d][1] - reference[d][1] for d in keys]                 # ΔA180
    dD = [a - b for a, b in zip(d0, d180)]                                 # per-depth change in D
    m = st.fmean(dD) if n else None
    bs = st.fmean(a + b for a, b in zip(d0, d180)) if n else None
    sd = st.stdev(dD) if n > 1 else None
    se = None if sd is None else sd / math.sqrt(n)
    spread = (max(dD) - min(dD)) if n else None
    xdiff = None if m is None else abs(abs(m) - abs(bs))
    xtol = None if se is None else max(params.crosscheck_tol_abs, params.crosscheck_tol_k * se)
    C = installation.probe.L_m / (2.0 * installation.probe.K) * 1000.0       # mm per reading unit per interval
    za = None if m is None else -m                                           # ZA = −ΔD (§5, QA-021)
    bse = sum(-x * C for x in dD) if n else None

    g = [("n_stable>=min", n >= params.min_stable_intervals)]
    g.append(("significant", bool(se is not None and m is not None and abs(m) > params.significance_k * se
                                  and m != 0)))
    g.append(("spread<=max", bool(sd is not None and sd <= params.per_axis("max_spread", axis))))
    base = all(p for _, p in g)
    g.append(("crosscheck", bool(xdiff is not None and xtol is not None and xdiff <= xtol)))
    g.append(("|ZA|<=max", bool(za is not None and abs(za) <= params.max_abs_c)))
    verdict = None if xdiff is None or xtol is None else ("AGREE" if g[3][1] else "SUSPECT")
    if not base:
        status, out_za, flag = "NO_PROPOSAL", None, ("SUSPECT" if suspect_flag else "OK")
    elif not g[4][1]:
        status, out_za, flag = "BAD_RESURVEY", None, "BAD"
    elif verdict != "AGREE":
        status, out_za, flag = "SUSPECT_CROSSCHECK", za, "SUSPECT"        # proposal ranked SUSPECT, for review
    else:
        status, out_za, flag = "PROPOSED", za, "SUSPECT"
    return BiasQC(suspect_survey_id, axis, ref_id, dcs, suspect_flag, n, m, sd, se, spread, bs, xdiff, xtol,
                  None if m is None else abs(m), None if bs is None else abs(bs), verdict, bse, out_za, status, tuple(g), z_top, assumed, params.params_unverified, flag)


def bias_proposal_record(q: BiasQC, record_id, dam_id, instrument_id, user, timestamp_utc):
    """A PROPOSED or SUSPECT_CROSSCHECK result as an UNAPPLIED ref.incl_correction row (applied=False). Applying it
    is a separate reviewer-approved row (CorrectionRecord refuses applied bias without review); never applied here."""
    from .corrections import CorrectionRecord
    if q.status not in ("PROPOSED", "SUSPECT_CROSSCHECK") or q.za_proposed_c is None:
        raise ValueError(f"no proposal to record (status {q.status})")
    stat = (f"rule={q.rule};n={q.n};mean_dD={q.mean_dD:.4f};bs={q.mean_cs_change:.4f};sd={q.sd:.4f};se={q.se:.4f};"
            f"spread={q.spread:.4f};ZA={q.za_proposed_c:.4f};abs_dD={q.abs_dD:.4f};abs_bs={q.abs_bs:.4f};"
            f"crosscheck_verdict={q.crosscheck_verdict};status={q.status};requires_review=True;crosscheck_diff={q.crosscheck_diff:.4f};crosscheck_tol={q.crosscheck_tol:.4f};"
            f"reference_survey_id={q.reference_survey_id};stable_zone_top_m={q.stable_zone_top_m};"
            f"stable_zone_assumed={q.stable_zone_assumed};params_unverified={q.params_unverified}")
    return CorrectionRecord(record_id, dam_id, instrument_id, q.survey_id, q.axis, "bias", "c", q.za_proposed_c,
                            "between_set_bias_qc", stat, None, False, None,
                            None,                     # bias is a whole-survey ZA/ZB (straight line from the toe)
                            "QA-021 proposal (review-only): ZA/ZB = -dD applied to A0 (suspect - reference); cross-check "
                            f"{q.crosscheck_verdict}; reviewer approval required; check D-profile signature (§5)",
                            "procedure §5 bias row (lead ruling 2026-11-02), §7.1; MIKKELSEN-2003-FMGM PDF p4-7",
                            user, timestamp_utc)


# ---- v1.5 (App Developer mismatch 4): API field naming -------------------------------------------------------
# Engine notation (and the stored ref.incl_correction.detection_statistic key) is the procedure's `ZA`/`ZB`. The
# contract and every API field use lowercase snake_case `za`/`zb`. The mapping happens HERE, at the boundary; no
# API payload may carry `ZA`. (Stored statistic keys are unchanged so existing records stay parseable.)
API_FIELD_MAP = {"ZA": "za", "ZB": "zb"}
ZA_UNIT = "reading units (za/zb; D' = D + za)"


def parse_detection_statistic(stat) -> dict:
    """'k=v;k=v' (ref.incl_correction.detection_statistic) -> dict with API field names (ZA -> za)."""
    out = {}
    for part in str(stat or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[API_FIELD_MAP.get(k.strip(), k.strip())] = v.strip()
    return out


def bias_qc_api(q: BiasQC) -> dict:
    """BiasQC -> API dict (api_contract §4 bias fields), `za` not `ZA`."""
    return {"survey_id": q.survey_id, "axis": q.axis, "status": q.status, "rule": q.rule, "qa_flag": q.qa_flag,
            "za": q.za_proposed_c, "za_unit": ZA_UNIT, "abs_dD": q.abs_dD, "abs_bs": q.abs_bs,
            "crosscheck_verdict": q.crosscheck_verdict, "crosscheck_diff": q.crosscheck_diff,
            "crosscheck_tol": q.crosscheck_tol, "reference_survey_id": q.reference_survey_id,
            "requires_review": q.requires_review, "applied": False}
