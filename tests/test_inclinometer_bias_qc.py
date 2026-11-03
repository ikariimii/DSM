"""QA-020/QA-021 between-set bias QC (procedure §5 lead ruling 2026-11-02 + §7.1): suspect flag, ZA = −ΔD proposal
gates (i)-(v) incl. the magnitude-only §5 cross-check (no sign gate; it ranks proposals AGREE/SUSPECT), reference
set, stable zone, review-only proposals.
All data SYNTHETIC. Parameters are configurable UNVERIFIED engineering judgment (params_unverified)."""
import os
import re

import pytest

from dam_safety.inclinometer import corrections as C, qc
from dam_safety.inclinometer.reduction import reduce_survey, top_cum_change_mm
from dam_safety.inclinometer.installation import Installation, Probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = Probe(L_m=0.5, K=25000.0, raw_unit="sin*25000")


def inst(stable=7.0, baseline="S0"):
    return Installation("I1", "D1", P, 10.0, 0.0, baseline, stable_zone_top_m=stable)


def ref_set():
    return {round(i * 0.5, 9): (100.0 + i, -(100.0 + i) + 12.0) for i in range(20)}


def shifted(a0_shift=0.0, noise=0.0, move=0.0, zone_from=0.0):
    """A0 += a0_shift (+ alternating ±noise) [one-pass bias]; real movement: A0 += move, A180 -= move."""
    out = {}
    for k, (d, (a, b)) in enumerate(ref_set().items()):
        e = noise if k % 2 else -noise
        out[d] = (a + a0_shift + e + move, b - move) if d >= zone_from else (a, b)
    return out


def run(s, i=None, axis="A", **kw):
    return qc.between_set_bias_qc(i or inst(), axis, "S5", s, ref_set(), **kw)


def gate(q, name):
    return dict(q.gates)[name]


def test_clean_one_pass_bias_is_proposed_never_applied():
    q = run(shifted(-6.0, noise=0.5))
    assert q.status == "PROPOSED" and q.qa_flag == "SUSPECT" and all(p for _, p in q.gates)
    assert q.n == 6 and q.za_proposed_c == pytest.approx(6.0) and q.mean_dD == pytest.approx(-6.0)
    assert q.mean_cs_change == pytest.approx(-6.0) and q.crosscheck_diff == pytest.approx(0.0)
    assert q.abs_dD == pytest.approx(6.0) and q.abs_bs == pytest.approx(6.0) and q.crosscheck_verdict == "AGREE"
    assert q.rule == "QA021-v2" and q.requires_review
    # §5: Δ = suspect − reference, ZA = −ΔD; D_suspect + ZA recovers D_reference; BSE = Σ(−ΔD)·C = n·ZA·C
    C = P.L_m / (2 * P.K) * 1000
    assert q.bse_mm == pytest.approx(6.0 * 6 * C) and q.za_proposed_c == pytest.approx(q.bse_mm / (q.n * C))
    s, r = shifted(-6.0, noise=0.5), ref_set()
    assert sum((s[d][0] - s[d][1] + q.za_proposed_c) - (r[d][0] - r[d][1]) for d in s) == pytest.approx(0.0)
    assert q.params_unverified and q.reference_survey_id == "S0" and not q.stable_zone_assumed
    rec = qc.bias_proposal_record(q, "R1", "D1", "I1", "eng", "2026-11-02T00:00:00Z")
    assert rec.applied is False and rec.correction_type == "bias" and rec.auto_value == pytest.approx(6.0)
    assert "params_unverified=True" in rec.detection_statistic and "reference_survey_id=S0" in rec.detection_statistic
    for x in ("ZA=6.0000", "abs_dD=6.0000", "abs_bs=6.0000", "crosscheck_verdict=AGREE", "requires_review=True"):
        assert x in rec.detection_statistic, x


def test_suspect_flag_threshold_per_axis_investigate_only():
    a = run(shifted(-11.0))
    assert a.suspect_flag and a.delta_mean_cs == pytest.approx(-11.0)
    b = run(shifted(-11.0), axis="B")
    assert not b.suspect_flag                       # B band 20
    # flag alone never proposes: shift confined above the stable zone -> no ΔD in zone
    q = run(shifted(-30.0, zone_from=0.0) | {d: v for d, v in ref_set().items() if d >= 7.0})
    assert q.suspect_flag and q.status == "NO_PROPOSAL" and q.za_proposed_c is None and q.qa_flag == "SUSPECT"


def test_gate_i_min_stable_intervals():
    q = run(shifted(-6.0, noise=0.5), i=inst(stable=8.0))
    assert q.n == 4 and not gate(q, "n_stable>=min") and q.status == "NO_PROPOSAL" and q.za_proposed_c is None


def test_gate_ii_significance():
    q = run(shifted(-1.0, noise=3.0))
    assert not gate(q, "significant") and q.status == "NO_PROPOSAL"


def test_gate_iii_spread():
    q = run(shifted(-20.0, noise=12.0))
    assert gate(q, "significant") and not gate(q, "spread<=max") and q.sd > 8 and q.status == "NO_PROPOSAL"
    assert qc.between_set_bias_qc(inst(), "B", "S5", shifted(-20.0, noise=12.0), ref_set()).status == "PROPOSED"


def passes(d0=0.0, d180=0.0, noise=0.0):
    """Stable zone (>= 7.0): ΔA0 = d0 (± alternating noise), ΔA180 = d180."""
    out = {}
    for k, (d, (a, b)) in enumerate(ref_set().items()):
        e = noise if k % 2 else -noise
        out[d] = (a + d0 + e, b + d180) if d >= 7.0 else (a, b)
    return out


def test_gate_iv_movement_in_stable_zone_ranks_suspect():
    # real movement r: ΔA0 = +r, ΔA180 = −r -> ΔD = 2r, bs = 0 -> ||ΔD|−|bs|| = 6 > 2 -> SUSPECT verdict (§5).
    # SUSPECT is a review flag that ranks the proposal, not a rejection: ZA = −ΔD is still shown, never applied.
    q = run(shifted(move=3.0))
    assert q.mean_dD == pytest.approx(6.0) and q.mean_cs_change == pytest.approx(0.0)
    assert not gate(q, "crosscheck") and q.crosscheck_verdict == "SUSPECT"
    assert q.status == "SUSPECT_CROSSCHECK" and q.qa_flag == "SUSPECT" and not q.suspect_flag
    assert q.za_proposed_c == pytest.approx(-6.0) and (q.abs_dD, q.abs_bs) == (pytest.approx(6.0), pytest.approx(0.0))
    rec = qc.bias_proposal_record(q, "R2", "D1", "I1", "eng", "2026-11-02T00:00:00Z")
    assert rec.applied is False and "crosscheck_verdict=SUSPECT" in rec.detection_statistic


def test_gate_iv_dmm_like_disagreement_is_suspect_and_tolerance_configurable():
    # synthetic DMM-shaped case: both passes moved, ΔD = −5, bs = −8 -> diff 3 > max(2.0, 3·SE=0) -> SUSPECT
    q = run(passes(d0=-6.5, d180=-1.5))
    assert q.mean_dD == pytest.approx(-5.0) and q.mean_cs_change == pytest.approx(-8.0)
    assert q.crosscheck_diff == pytest.approx(3.0) and q.crosscheck_tol == pytest.approx(2.0)
    assert q.status == "SUSPECT_CROSSCHECK" and q.crosscheck_verdict == "SUSPECT" and q.za_proposed_c == pytest.approx(5.0)
    assert run(passes(d0=-6.5, d180=-1.5), params=qc.BiasQCParams(crosscheck_tol_abs=3.0)).status == "PROPOSED"
    # tolerance = max(abs, k·SE): with scatter the SE term governs
    q2 = run(passes(d0=-6.5, d180=-1.5, noise=4.0))
    assert q2.crosscheck_tol == pytest.approx(3 * q2.se) and q2.crosscheck_tol > 2.0


def test_a180_only_shift_is_proposed_and_corrects_to_zero():
    # Lead/Specialist ruling 2026-11-02 (§5): ZA = −ΔD always, applied to A0; never −bs; magnitude-only cross-check.
    # A180-only shift δ = +6 (ΔA0 = 0, ΔA180 = +6): ΔD = −6, bs = +6, ZA = +6, corrected result 0.0 mm.
    s = passes(d180=6.0)
    q = run(s)
    assert q.mean_dD == pytest.approx(-6.0) and q.mean_cs_change == pytest.approx(6.0)
    assert gate(q, "crosscheck") and q.crosscheck_diff == pytest.approx(0.0) and q.crosscheck_verdict == "AGREE"
    assert q.status == "PROPOSED" and q.za_proposed_c == pytest.approx(6.0) == pytest.approx(-q.mean_dD)
    assert "sign_agree" not in dict(q.gates) and not hasattr(qc.BiasQCParams(), "require_sign_agreement")
    # apply ZA to A0 over the stable zone via a reviewer-approved record -> 0.0 mm change vs the reference
    zone = [d for d in s if d >= 7.0]
    D = {(d, "A"): s[d][0] - s[d][1] for d in zone}
    D_ref = {(d, "A"): ref_set()[d][0] - ref_set()[d][1] for d in zone}
    before = top_cum_change_mm(reduce_survey(D, D_ref, P, "A"))
    assert before == pytest.approx(-6 * len(zone) * P.L_m / (2 * P.K) * 1000)
    prop = qc.bias_proposal_record(q, "R3", "D1", "I1", "eng", "2026-11-02T00:00:00Z")
    with pytest.raises(ValueError):                                    # never applied without reviewer approval
        C.CorrectionRecord(**{**prop.__dict__, "applied": True})
    ok = C.CorrectionRecord(**{**prop.__dict__, "record_id": "R4", "applied": True,
                               "reviewed_by": "reviewer", "reviewed_utc": "2026-11-02T01:00:00Z"})
    after = top_cum_change_mm(reduce_survey(C.apply(D, C.active([ok], "I1", "S5")), D_ref, P, "A"))
    assert after == pytest.approx(0.0, abs=1e-12)


def test_gate_v_large_c_is_bad_resurvey_and_params_configurable():
    q = run(shifted(-30.0, noise=0.5))
    assert q.status == "BAD_RESURVEY" and q.qa_flag == "BAD" and q.za_proposed_c is None
    with pytest.raises(ValueError):
        qc.bias_proposal_record(q, "R1", "D1", "I1", "eng", "t")
    q2 = run(shifted(-30.0, noise=0.5), params=qc.BiasQCParams(max_abs_c=40.0))
    assert q2.status == "PROPOSED" and q2.za_proposed_c == pytest.approx(30.0)


def test_stable_zone_fallback_bottom_5_flagged():
    q = run(shifted(-6.0, noise=0.5), i=inst(stable=None))
    assert q.stable_zone_assumed and q.n == 5 and q.stable_zone_top_m == pytest.approx(7.5)


def test_reference_set_default_baseline_and_override():
    assert run(shifted(-6.0, noise=0.5), reference_survey_id="S3").reference_survey_id == "S3"
    with pytest.raises(ValueError):
        run(shifted(), i=inst(baseline=None))
    with pytest.raises(ValueError):
        run(shifted(), reference_survey_id="S5")


def test_contract_docs_and_ddl_expose_fields():
    fields = ("suspect_flag", "za_proposed_c", "n", "mean_dD", "sd", "se", "spread", "mean_cs_change",
              "crosscheck_diff", "crosscheck_tol", "abs_dD", "abs_bs", "crosscheck_verdict", "requires_review",
              "reference_survey_id", "stable_zone_assumed", "params_unverified")
    assert set(fields) <= set(qc.BiasQC.__dataclass_fields__)
    for f in ("SCHEMA_CONTRACT.md", os.path.join("docs", "api_contract.md")):
        s = open(os.path.join(ROOT, f), encoding="utf-8").read()
        assert "QA-020" in s and "QA-021" in s and all(f"`{x}`" in s for x in fields), f
    ddl = open(os.path.join(ROOT, "sql", "ddl", "02_ref_calibration_threshold.sql"), encoding="utf-8").read()
    block = ddl.split("CREATE TABLE IF NOT EXISTS ref.incl_installation (")[1].split("\n);")[0]
    assert re.search(r"^\s*stable_zone_top_m\s+DOUBLE", block, re.M)


def test_v15_api_uses_lowercase_za_mapped_at_the_boundary():
    """App Developer mismatch 4: engine/storage key `ZA`, API/contract field `za` (no `ZA` in any API payload)."""
    q = run(shifted(a0_shift=6.0))
    api = qc.bias_qc_api(q)
    assert api["za"] == pytest.approx(q.za_proposed_c) and "ZA" not in api and api["applied"] is False
    rec = qc.bias_proposal_record(q, "R1", "D1", "I1", "eng", "2026-11-02T00:00:00Z")
    st = qc.parse_detection_statistic(rec.detection_statistic)
    assert float(st["za"]) == pytest.approx(q.za_proposed_c) and "ZA" not in st
    assert "ZA=" in rec.detection_statistic                  # stored key unchanged (existing records parse)
    for doc in ("docs/api_contract.md",):
        txt = open(os.path.join(os.path.dirname(__file__), "..", doc)).read()
        api_rows = [l for l in txt.splitlines() if l.startswith("| `/api/")]
        assert api_rows and not any(re.search(r"`ZA`", l) for l in api_rows)
