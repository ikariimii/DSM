"""QA-020 between-set bias QC (procedure §7.1): suspect flag, ZA proposal gates (i)-(v), reference set, stable zone.
All data SYNTHETIC. Parameters are configurable UNVERIFIED engineering judgment (params_unverified)."""
import os
import re

import pytest

from dam_safety.inclinometer import qc
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
    assert q.n == 6 and q.za_proposed_c == pytest.approx(6.0) and q.mean_dD == pytest.approx(6.0)
    # c = BSE/(N·C) and sign Δ = reference − suspect: D_suspect + c recovers D_reference
    C = P.L_m / (2 * P.K) * 1000
    assert q.bse_mm == pytest.approx(6.0 * 6 * C) and q.za_proposed_c == pytest.approx(q.bse_mm / (q.n * C))
    s, r = shifted(-6.0, noise=0.5), ref_set()
    assert sum((s[d][0] - s[d][1] + q.za_proposed_c) - (r[d][0] - r[d][1]) for d in s) == pytest.approx(0.0)
    assert q.params_unverified and q.reference_survey_id == "S0" and not q.stable_zone_assumed
    rec = qc.bias_proposal_record(q, "R1", "D1", "I1", "eng", "2026-11-02T00:00:00Z")
    assert rec.applied is False and rec.correction_type == "bias" and rec.auto_value == pytest.approx(6.0)
    assert "params_unverified=True" in rec.detection_statistic and "reference_survey_id=S0" in rec.detection_statistic


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


def test_gate_iv_equal_and_opposite_is_movement_not_bias():
    q = run(shifted(move=3.0))
    assert q.equal_opposite is True and q.mean_cs_change == pytest.approx(0.0)
    assert not gate(q, "not_equal_opposite") and q.status == "NO_PROPOSAL" and not q.suspect_flag


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
    fields = ("suspect_flag", "za_proposed_c", "n", "mean_dD", "sd", "spread", "equal_opposite",
              "reference_survey_id", "stable_zone_assumed", "params_unverified")
    assert set(fields) <= set(qc.BiasQC.__dataclass_fields__)
    for f in ("SCHEMA_CONTRACT.md", os.path.join("docs", "api_contract.md")):
        s = open(os.path.join(ROOT, f), encoding="utf-8").read()
        assert "QA-020" in s and all(f"`{x}`" in s for x in fields), f
    ddl = open(os.path.join(ROOT, "sql", "ddl", "02_ref_calibration_threshold.sql"), encoding="utf-8").read()
    block = ddl.split("CREATE TABLE IF NOT EXISTS ref.incl_installation (")[1].split("\n);")[0]
    assert re.search(r"^\s*stable_zone_top_m\s+DOUBLE", block, re.M)
