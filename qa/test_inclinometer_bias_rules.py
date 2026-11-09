"""QA RULE tests for the inclinometer bias-shift rules: procedures/inclinometers.md §5 bias row (sign convention,
lead rewrite 2026-11-02 19:44), §7 mean-checksum row, §7.1 between-set QC (QA-020). Written by QA 2026-11-02.

ONE sign convention, as §5 states it: every Δ = SUSPECT-set reading − REFERENCE-set reading.
    ΔD = mean(Δ0 − Δ180) over the stable zone (change in D; DMM 'Shift'),   bs = mean(Δ0 + Δ180) (cross-check),
    ZA = −ΔD ALWAYS (GTILT adds ZA to A0; for an A180-only shift δ: ΔD = −δ, bs = +δ, ZA = +δ).
    The cross-check compares MAGNITUDES only (no sign gate): ||ΔD| − |bs|| <= max(2.0, 3·SE), SE = SD(ΔD)/√n.
    Lead ruling 2026-11-02 (§5 / §7.1 row): bias corrections are NEVER auto-applied. The app PROPOSES ZA = −ΔD with both
    magnitudes and the tolerance verdict (agree / SUSPECT); the UNVERIFIED tolerance only RANKS proposals; a reviewer
    must approve each correction record.
§7.1 gates (ALL UNVERIFIED engineering judgment): n >= 5; |mean ΔD| > 3·SD/√n (strict, as §7.1); SD(ΔD) <= 8 (A) / 16 (B);
|ZA| <= 25 else BAD/resurvey; between-set SUSPECT if |Δ mean checksum| > 10 (A) / 20 (B); reference = baseline.

Data are read BY PATH; restricted values are never copied here:
  (a) user workbook S1/S3  datasets/inclinometers/user_tool/user_tool_surveys.csv  (DS-INC-USERTOOL, internal-test-only)
      CONVENTIONS.md: the workbook is AI-made, a LAYOUT reference only. Here it is used only as REAL INPUT READINGS
      (regression input); no expected value below comes from a workbook formula or workbook result column.
  (b) synthetic +6 on A0   datasets/inclinometers/synthetic/synthetic_biaxial_metric.csv (DS-INC-SYNTHETIC-01)
  (c) Mikkelsen 2007 DMM   datasets/inclinometers/vendor_manual_samples/mikkelsen2007_dmm_biasshift_table.csv
                           (DS-INC-M2007-DMM-BIASSHIFT, internal-test-only; checked by QA against PDF p16 image)
  (d) Mikkelsen 2003 p7    qa_private/inclinometer_bias_rules.json (internal reference)
Expected values DERIVED from (a), (c) and (d) live in qa_private/inclinometer_bias_expected.json and
qa_private/inclinometer_bias_rules.json (read by path; tests skip without them). Only synthetic (b) and
procedure-worked-example numbers appear as literals here.
QA-020 is checked against the engine (qc.between_set_bias_qc) and the app path (batch qa_reason -> contract.bias_qc).
QA-030 closed: contract.bias_qc shows proposed_c / record / evidence of a SUSPECT_CROSSCHECK proposal (marker removed). QA-023 is closed (engine has gate iv; its xfail XPASSed 20:00).
Run: app/.venv/bin/python -m pytest -q qa/test_inclinometer_bias_rules.py -p no:cacheprovider
"""
import csv
import json
import math
import os
import statistics as st
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "backend"))
sys.path.insert(0, str(ROOT / "src"))
REFS = Path(os.environ.get("DAM_REFS", "/workspace/dam-refs"))
PRIV = Path(os.environ.get("DAM_QA_PRIVATE", str(REFS / "qa_private")))
USER = REFS / "datasets/inclinometers/user_tool/user_tool_surveys.csv"
SYN = REFS / "datasets/inclinometers/synthetic/synthetic_biaxial_metric.csv"
DMM = REFS / "datasets/inclinometers/vendor_manual_samples/mikkelsen2007_dmm_biasshift_table.csv"

# §7.1 / §5 parameters (UNVERIFIED engineering judgment, copied from the procedure, not from any dataset)
CHK_TOL = {"A": 10, "B": 20}
MAX_SD = {"A": 8, "B": 16}
N_MIN, SIG_K, MAX_ZA = 5, 3.0, 25
XTOL_ABS, XTOL_K = 2.0, 3.0


def _exp():
    p = PRIV / "inclinometer_bias_expected.json"
    if not p.exists():
        pytest.skip("qa_private expected values not present")
    return json.loads(p.read_text())


# ---------------------------------------------------------------- procedure logic (QA reference implementation)
def deltas(sus, ref, ax="A"):
    """Δ = suspect − reference, per pass, per matched interval."""
    return [s[ax + "0"] - r[ax + "0"] for s, r in zip(sus, ref)], [s[ax + "180"] - r[ax + "180"] for s, r in zip(sus, ref)]


def checksum_shift(sus, ref, ax="A"):
    return st.mean(r[ax + "0"] + r[ax + "180"] for r in sus) - st.mean(r[ax + "0"] + r[ax + "180"] for r in ref)


def between_set_flag(sus, ref, ax="A"):
    """§7: a mean-checksum shift vs the reference is a POSSIBLE bias -> SUSPECT (investigate); never a correction."""
    return "SUSPECT" if abs(checksum_shift(sus, ref, ax)) > CHK_TOL[ax] else "OK"


def bias_decision(d0, d180, ax="A", xtol_abs=XTOL_ABS, xtol_k=XTOL_K):
    """§5 + §7.1 on stable-zone deltas. Returns dict(status, za, dD, bs, sd, tol). Proposes only: status is
    PROPOSE_AGREE / PROPOSE_SUSPECT (tolerance verdict = rank), NO_PROPOSAL or BAD_RESURVEY. No sign gate.
    QA-032: significance is STRICT |ΔD| > 3·SE as §7.1 states (this helper used >= before 2026-11-02 20:05)."""
    dD = [a - b for a, b in zip(d0, d180)]
    n = len(dD)
    m, bs = st.mean(dD), st.mean(a + b for a, b in zip(d0, d180))
    sd = st.stdev(dD) if n > 1 else float("inf")
    se = sd / math.sqrt(n)
    tol = max(xtol_abs, xtol_k * se)
    out = dict(dD=m, bs=bs, sd=sd, tol=tol, za=None)
    if n < N_MIN or m == 0 or not abs(m) > SIG_K * se or sd > MAX_SD[ax]:
        return {**out, "status": "NO_PROPOSAL"}
    if abs(m) > MAX_ZA:
        return {**out, "status": "BAD_RESURVEY"}
    agree = abs(abs(m) - abs(bs)) <= tol                        # magnitudes only; verdict ranks, never gates
    return {**out, "status": "PROPOSE_AGREE" if agree else "PROPOSE_SUSPECT", "za": -m}   # ZA = −ΔD, proposed only


# ---------------------------------------------------------------- loaders
def _user():
    if not USER.exists():
        pytest.skip("DS-INC-USERTOOL not present")
    out = {}
    for r in csv.DictReader(l for l in USER.open() if not l.startswith("#")):   # readings only, no wb_* columns
        out.setdefault(r["survey"], []).append({"dfb": float(r["depth_from_bottom_ft"]),
                                                **{k: int(r[k]) for k in ("A0", "A180", "B0", "B180")}})
    return {s: sorted(v, key=lambda x: x["dfb"]) for s, v in out.items()}         # bottom -> top


# DS-INC-SYNTHETIC-01 depth design (dataset README + manifest, declared 2026-11-08 EST; generator unchanged):
# depth_m = interval TOP below top of casing (identity, no depth-L), L = 0.5 m, casing_depth_m = max(depth_m) + L.
# Interval [0, 0.5] is MISSING in every survey and is never filled.
def _syn_casing(S):
    return max(r["depth_m"] for r in S["0"]) + S["0"][0]["L"]


def _syn_key(r):
    return round(r["depth_m"], 9)


def _syn():
    out = {}
    for r in csv.DictReader(SYN.open()):
        out.setdefault(r["survey"], []).append({"depth_m": float(r["depth_m"]), **{k: int(r[k]) for k in
                                                ("A0", "A180", "B0", "B180")}, "K": float(r["K"]), "L": float(r["L_m"])})
    return {s: sorted(v, key=lambda x: x["depth_m"]) for s, v in out.items()}


def _dmm():
    if not DMM.exists():
        pytest.skip("DS-INC-M2007-DMM-BIASSHIFT not present")
    return [{k: float(v) for k, v in r.items()} for r in csv.DictReader(l for l in DMM.open() if not l.startswith("#"))]


def _D(r):
    return r["A0"] - r["A180"]


# ---------------------------------------------------------------- (a) real pair: baseline S1 (reference) vs S3 (suspect)
def test_a_s3_vs_baseline_is_suspect_but_never_corrected():
    S = _user()
    ref, sus = S["S1"], S["S3"]
    assert between_set_flag(sus, ref, "A") == "SUSPECT"           # |Δ mean CS| > 10
    assert abs(checksum_shift(sus, ref, "A")) == pytest.approx(_exp()["usertool_s3_abs_cs_shift"])
    assert all(between_set_flag(S[s], ref, "A") == "OK" for s in ("S2", "S4"))
    d0, d180 = deltas(sus[:5], ref[:5], "A")                        # §7.1 fallback stable zone: bottom 5 (assumed)
    dec = bias_decision(d0, d180, "A")
    assert dec["bs"] == pytest.approx(checksum_shift(sus, ref, "A"))  # bs is the checksum change (same sign)
    assert dec["sd"] > MAX_SD["A"] and dec["status"] == "NO_PROPOSAL" and dec["za"] is None   # real movement
    # D of S3 lies on the S2-S4 trend (whole hole): the shift is not in D
    assert max(abs((_D(a) + _D(c)) / 2 - _D(b)) for a, b, c in zip(S["S2"], sus, S["S4"])) <= 1.0


def _engine_pair(sus_id, ref_id="S1", ax="A"):
    from dam_safety.inclinometer import qc
    from dam_safety.inclinometer.installation import ENGLISH_DIGITILT as P, FT, Installation, rekey_from_bottom_label
    S = _user()
    inst = Installation("WB", "D", P, 30 * FT, 0.0, ref_id, stable_zone_top_m=None)   # bottom-5 fallback
    key = lambda r: rekey_from_bottom_label(r["dfb"] * FT, 30 * FT, 2 * FT)            # noqa: E731
    sv = lambda s: {round(key(r), 9): (r[ax + "0"], r[ax + "180"]) for r in S[s]}      # noqa: E731
    return qc.between_set_bias_qc(inst, ax, sus_id, sv(sus_id), sv(ref_id))


def test_a_s3_engine_flags_suspect_without_proposal():
    q = _engine_pair("S3")
    assert q.reference_survey_id == "S1" and q.stable_zone_assumed
    assert q.suspect_flag and q.qa_flag == "SUSPECT"
    assert q.status == "NO_PROPOSAL" and q.za_proposed_c is None


def _s3_vs_s1_app_inputs():
    """Real reference/suspect pair for the app: S1 = baseline (reference, §7.1), S3 = suspect."""
    from formulas import inc_deviation
    S = _user()
    L_mm, K = 24 * 25.4, 20000.0
    top = max(r["dfb"] for r in S["S3"])
    rows = lambda s: [{"depth_m": (top - r["dfb"]) * 0.3048, "a0": r["A0"], "a180": r["A180"], "b0": r["B0"],  # noqa
                       "b180": r["B180"]} for r in S[s]]
    ref, sus = rows("S1"), rows("S3")
    base = {r["depth_m"]: (inc_deviation(L_mm, r["a0"], r["a180"], K), inc_deviation(L_mm, r["b0"], r["b180"], K))
            for r in ref}
    return ref, sus, base, L_mm, K


def test_a_s3_real_pair_is_a_valid_qa020_case():
    """Positive control for the QA-020 xfail: every suspect depth has a baseline deviation, the app reduces
    against it, and the pair carries the checksum shift §7 requires to be flagged."""
    from formulas import reduce_inclinometer_profile
    ref, sus, base, L_mm, K = _s3_vs_s1_app_inputs()
    assert base and {r["depth_m"] for r in sus} == set(base)
    out = reduce_inclinometer_profile(sus, base, L_mm, K, chk_tol=CHK_TOL["A"])
    assert max(abs(r["a_cum_mm"]) for r in out) > 0
    assert abs(st.mean(r["a0"] + r["a180"] for r in sus) - st.mean(r["a0"] + r["a180"] for r in ref)) > CHK_TOL["A"]


def test_qa020_engine_defaults_are_the_7_1_parameters():
    p = __import__("dam_safety.inclinometer.qc", fromlist=["qc"]).BiasQCParams()
    assert dict(p.cs_shift_suspect) == CHK_TOL and dict(p.max_spread) == MAX_SD
    assert (p.min_stable_intervals, p.significance_k, p.max_abs_c) == (N_MIN, SIG_K, MAX_ZA)
    assert (p.crosscheck_tol_abs, p.crosscheck_tol_k) == (XTOL_ABS, XTOL_K) and p.params_unverified
    assert getattr(p, "require_sign_agreement", False) is False      # §5 ruling: no sign gate (removed or False)


def test_qa020_engine_real_pair_both_directions_no_sign_gate():
    """Real baseline/suspect pair S1/S3 through the engine with §7.1 defaults, both ways round. No sign gate (§5):
    swapping the pair only negates the statistics; the decision is identical."""
    S = _user()
    fwd, rev = _engine_pair("S3", "S1"), _engine_pair("S1", "S3")
    assert fwd.delta_mean_cs == pytest.approx(checksum_shift(S["S3"], S["S1"]))          # suspect − reference
    assert rev.delta_mean_cs == pytest.approx(-fwd.delta_mean_cs) and abs(fwd.delta_mean_cs) > CHK_TOL["A"]
    for q in (fwd, rev):
        assert q.suspect_flag and q.qa_flag == "SUSPECT" and q.status == "NO_PROPOSAL" and q.za_proposed_c is None
    assert rev.mean_dD == pytest.approx(-fwd.mean_dD) and rev.mean_cs_change == pytest.approx(-fwd.mean_cs_change)


def _syn_rows(base, sus, a180_bias=0, common=0):
    rows = []
    for sid, src, b, c in (("REF", base, 0, 0), ("SUS", sus, a180_bias, common)):
        for r in _syn()[src]:
            rows.append({"survey_id": sid, "survey_ts": "2020-01-01" if sid == "REF" else "2020-02-01", "depth_m": r["depth_m"],
                         "a0": r["A0"] + c, "a180": r["A180"] + b + c, "b0": r["B0"], "b180": r["B180"]})
    return rows


def _batch(base, sus, a180_bias=0, rule=None, common=0):
    """App path: batch.reduce_instrument (silver qa_reason + proposals) -> contract.bias_qc (API)."""
    import dataclasses
    from dam_safety.inclinometer import batch, qc
    from repos import contract
    S = _syn()
    ir = {"instrument_id": "SYN", "dam_id": "D", "depth_reference": "top_of_casing", "probe_L_m": S["0"][0]["L"],
          "probe_K": S["0"][0]["K"], "raw_unit": "digits", "casing_depth_m": _syn_casing(S),
          "baseline_survey_id": "REF", "stable_zone_top_m": 7.0}
    out, props = batch.reduce_instrument(ir, _syn_rows(base, sus, a180_bias, common), [], "qa", "2026-11-02T00:00:00Z",
                                         target_surveys=["SUS"], bias_rule=rule or batch.DEFAULT_BIAS_RULE)
    recs = [dataclasses.asdict(qc.bias_proposal_record(p, f"r{i}", "D", "SYN", "qa", "2026-11-02T00:00:00Z"))
            for i, p in enumerate(props)]
    return out, props, recs, contract.bias_qc(out, recs)["axes"]["A"]


@pytest.mark.parametrize("base,sus,a180,za", [("0", "2", 0, -6.0),     # +6 on A0: ΔD = bs = +6
                                               ("2", "0", 0, +6.0),     # pair swapped: ΔD = bs = −6 (no sign gate)
                                               ("0", "0", 6, +6.0)])    # A180-only +6: ΔD = −6, bs = +6 (no sign gate)
def test_qa020_app_path_proposes_and_never_applies(base, sus, a180, za):
    out, props, recs, api = _batch(base, sus, a180)
    assert [(p.status, p.za_proposed_c) for p in props] == [("PROPOSED", pytest.approx(za))]
    assert all(r["applied"] is False for r in recs)
    assert api["status"] == "PROPOSED" and api["proposed_c"] == pytest.approx(za) and api["applied"] is False
    assert api["display_status"] == "UNVERIFIED"


def test_qa020_bias_qc_never_changes_reduced_data():
    """Same reduced numbers whether or not the engine proposes (max_abs_c=0 forces no proposal), and the engine
    leaves its inputs untouched; the reduced top displacement stays the RAW 5.20 mm (T6e raw)."""
    import copy
    from dam_safety.inclinometer import qc
    nop = lambda *a, **k: qc.between_set_bias_qc(*a, **k, params=qc.BiasQCParams(max_abs_c=0.0))   # noqa: E731
    with_p, props, _, _ = _batch("0", "2")
    without, none, _, _ = _batch("0", "2", rule=nop)
    assert len(props) == 1 and none == []
    cols = ("depth_m", "diff_a", "diff_b", "checksum_a", "checksum_b", "dev_a", "dev_b", "cum_a", "cum_b", "resultant")
    num = lambda o: [tuple(r[c] for c in cols) for r in o]                                          # noqa: E731
    assert num(with_p) == num(without)
    # DS-INC-SYNTHETIC-01: interval [0,0.5] is MISSING; T6e raw 5.20 mm is at the shallowest observed top 0.5.
    row0 = next(r for r in with_p if r["depth_m"] == 0.0)
    assert row0["qa_flag"] == "MISSING" and row0["cum_a"] is None
    observed = [r for r in with_p if r["cum_a"] is not None]
    top = max(observed, key=lambda r: abs(r["cum_a"]))
    assert abs(top["cum_a"]) == pytest.approx(5.20, abs=0.005) and top["depth_m"] == 0.5
    assert not any(r["correction_record_ids"] for r in with_p)
    q_in = _engine_inputs()
    snap = copy.deepcopy(q_in)
    qc.between_set_bias_qc(*q_in[:3], q_in[3], q_in[4])
    assert q_in == snap


def _engine_inputs():
    from dam_safety.inclinometer.installation import METRIC_DIGITILT as P, Installation
    S = _syn()
    inst = Installation("SYN", "D", P, _syn_casing(S), 0.0, "0", stable_zone_top_m=7.0)
    sv = lambda s: {_syn_key(r): (r["A0"], r["A180"]) for r in S[s]}   # noqa: E731
    return [inst, "A", "2", sv("2"), sv("0")]


def test_a180_only_bias_gives_za_plus_6_restores_zero_and_is_not_rejected():
    """Lead ruling 2026-11-02 / verify T16d: δ = +6 on A180 only -> ΔD = −6, bs = +6, ZA = +6 on A0, 0.0 mm."""
    from formulas import inc_deviation
    from dam_safety.inclinometer import qc
    S = _syn()
    ref = [r for r in S["0"] if r["depth_m"] >= 7.0]
    sus = [{**r, "A180": r["A180"] + 6} for r in ref]
    d0, d180 = deltas(sus, ref)
    dec = bias_decision(d0, d180)
    assert dec["dD"] == -6 and dec["bs"] == 6 and dec["status"] == "PROPOSE_AGREE" and dec["za"] == 6
    L_mm, K = ref[0]["L"] * 1000, ref[0]["K"]
    disp = lambda za: sum(inc_deviation(L_mm, s["A0"] + za, s["A180"], K) - inc_deviation(L_mm, r["A0"], r["A180"], K)  # noqa
                          for s, r in zip(sus, ref))
    assert disp(0) == pytest.approx(-len(ref) * 6 * L_mm / (2 * K))   # uncorrected false displacement
    assert disp(dec["za"]) == pytest.approx(0.0, abs=1e-9)
    assert disp(-dec["za"]) == pytest.approx(2 * disp(0))             # ZA = −bs would double it
    inst, *_ = _engine_inputs()
    from dam_safety.inclinometer.installation import METRIC_DIGITILT as P
    sv = lambda rows: {_syn_key(r): (r["A0"], r["A180"]) for r in rows}   # noqa: E731
    q = qc.between_set_bias_qc(inst, "A", "SUS", sv(sus), sv(ref), reference_survey_id="0")
    assert q.status == "PROPOSED" and q.za_proposed_c == pytest.approx(6.0)
    assert q.mean_dD == pytest.approx(-6) and q.mean_cs_change == pytest.approx(6)        # ΔD, bs differ in sign: no sign gate
    assert q.crosscheck_verdict == "AGREE" and (q.abs_dD, q.abs_bs) == (pytest.approx(6), pytest.approx(6))
    assert not [g for g, ok in q.gates if not ok]                                            # no gate rejects it


# ---------------------------------------------------------------- (b) synthetic +6 on A0, baseline "0" vs suspect "2"
def test_b_synthetic_one_pass_bias_gives_za_minus_6():
    S = _syn()
    ref, sus = S["0"], S["2"]
    z = [i for i, r in enumerate(ref) if r["depth_m"] >= 7.0]        # below the 6.0-6.5 m shear zone
    d0, d180 = deltas([sus[i] for i in z], [ref[i] for i in z])
    dec = bias_decision(d0, d180)
    assert dec["dD"] == 6 and dec["bs"] == 6                         # Δ = suspect − reference: +6 bias -> +6
    assert dec["status"] == "PROPOSE_AGREE" and dec["za"] == -6      # ZA = −ΔD


def test_b_engine_same_sign():
    from dam_safety.inclinometer import qc
    from dam_safety.inclinometer.installation import METRIC_DIGITILT as P, Installation
    S = _syn()
    inst = Installation("SYN", "D", P, _syn_casing(S), 0.0, "0", stable_zone_top_m=7.0)
    sv = lambda s: {_syn_key(r): (r["A0"], r["A180"]) for r in S[s]}   # noqa: E731 depth = interval top
    q = qc.between_set_bias_qc(inst, "A", "2", sv("2"), sv("0"))
    assert q.status == "PROPOSED" and q.za_proposed_c == pytest.approx(-6)


def test_b_procedure_worked_example_3_0_6():
    """§5 worked example (synthetic): ref A0 +500/A180 −480, suspect A0 +506, 50 stable intervals, metric probe."""
    from formulas import inc_deviation
    L, K, N = 500.0, 25000.0, 50
    top = lambda a0: N * (inc_deviation(L, a0, -480, K) - inc_deviation(L, 500, -480, K))   # noqa: E731
    d = bias_decision([6] * N, [0] * N)
    assert d["za"] == -6
    assert top(506) == pytest.approx(3.0) and top(506 + d["za"]) == pytest.approx(0.0)
    assert top(506 + 6) == pytest.approx(6.0)                          # wrong sign doubles


def test_b_synthetic_app_regression_t6e():
    """verify.py T6e is a DIFFERENT case from the §5 worked example: this dataset, 4.00 mm corrected, raw 5.20."""
    from formulas import inc_deviation, reduce_inclinometer_profile
    S = _syn()
    L_mm, K = S["0"][0]["L"] * 1000, S["0"][0]["K"]
    rows = lambda s, za=0: [{"depth_m": r["depth_m"], "a0": r["A0"] + za, "a180": r["A180"], "b0": r["B0"],  # noqa
                             "b180": r["B180"]} for r in S[s]]
    base = {r["depth_m"]: (inc_deviation(L_mm, r["a0"], r["a180"], K), inc_deviation(L_mm, r["b0"], r["b180"], K))
            for r in rows("0")}
    raw = reduce_inclinometer_profile(rows("2"), base, L_mm, K, stable_from_m=7.0)
    fixed = reduce_inclinometer_profile(rows("2", za=-6), base, L_mm, K, stable_from_m=7.0)
    assert raw[0]["a_cum_mm"] == pytest.approx(5.20, abs=0.005) and fixed[0]["a_cum_mm"] == pytest.approx(4.00, abs=0.005)
    doubled = reduce_inclinometer_profile(rows("2", za=+6), base, L_mm, K)
    assert doubled[0]["a_cum_mm"] - raw[0]["a_cum_mm"] == pytest.approx(len(raw) * 0.06)


def test_bs_is_the_checksum_change_not_independent_evidence():
    """With Δ = suspect − reference, mean(Δ0+Δ180) IS the mean-checksum shift (same sign): it can only cross-check ΔD."""
    S = _user()
    d0, d180 = deltas(S["S3"], S["S1"])
    assert st.mean(a + b for a, b in zip(d0, d180)) == pytest.approx(checksum_shift(S["S3"], S["S1"]))


# ---------------------------------------------------------------- (c) Mikkelsen 2007 DMM (T16b)
def _dmm_A():
    rows = _dmm()
    return [r["diff_A0"] for r in rows], [r["diff_A180"] for r in rows], rows


def test_c_dmm_is_suspect_and_verdict_depends_on_unverified_tolerance():
    d0, d180, rows = _dmm_A()
    E = _exp()
    dec = bias_decision(d0, d180)
    assert len(rows) == E["dmm_n"]
    assert dec["dD"] == pytest.approx(E["dmm_dD"]) and dec["bs"] == pytest.approx(E["dmm_bs"])
    assert dec["tol"] == pytest.approx(E["dmm_tol"], abs=1e-4)
    assert dec["status"] == "PROPOSE_SUSPECT" and dec["za"] == pytest.approx(E["dmm_za"])    # proposed, ranked SUSPECT
    assert dec["za"] == pytest.approx(-dec["dD"])                                             # ZA = −ΔD
    # only the RANK flips on the UNVERIFIED tolerance (values in qa_private); ZA is the same
    assert bias_decision(d0, d180, xtol_abs=E["dmm_rank_flip_tol_abs"])["status"] == "PROPOSE_AGREE"
    assert bias_decision(d0, d180, xtol_k=E["dmm_rank_flip_tol_k_above"])["status"] == "PROPOSE_AGREE"
    assert bias_decision(d0, d180, xtol_k=E["dmm_rank_flip_tol_k_below"])["status"] == "PROPOSE_SUSPECT"
    assert all(bias_decision(d0, d180, xtol_abs=t)["za"] == pytest.approx(E["dmm_za"])
               for t in (XTOL_ABS, E["dmm_rank_flip_tol_abs"], 4 * E["dmm_rank_flip_tol_abs"]))


def _dmm_engine():
    from dam_safety.inclinometer import qc
    from dam_safety.inclinometer.installation import ENGLISH_DIGITILT as P, FT, Installation
    d0, d180, rows = _dmm_A()
    inst = Installation("DMM", "D", P, 62 * FT, 0.0, "REF", stable_zone_top_m=30 * FT)
    keys = [t for t in inst.interval_tops_m() if t >= 30 * FT - 1e-9]
    assert len(keys) == len(d0)
    return qc.between_set_bias_qc(inst, "A", "SUS", {k: (a, b) for k, a, b in zip(keys, d0, d180)},
                                  {k: (0.0, 0.0) for k in keys})


def test_c_dmm_engine_ranks_suspect_and_never_auto_corrects():   # QA-023 closed (was a strict xfail; XPASS 20:00)
    q, E = _dmm_engine(), _exp()
    assert q.mean_dD == pytest.approx(E["dmm_dD"]) and q.mean_cs_change == pytest.approx(E["dmm_bs"])
    assert q.crosscheck_diff == pytest.approx(E["dmm_crosscheck_diff"])
    assert q.crosscheck_tol == pytest.approx(E["dmm_tol"], abs=1e-4)
    assert q.status == "SUSPECT_CROSSCHECK" and q.qa_flag == "SUSPECT" and q.crosscheck_verdict == "SUSPECT"
    assert q.za_proposed_c == pytest.approx(E["dmm_za"])            # proposed (ranked SUSPECT), = verify T16b
    assert (q.abs_dD, q.abs_bs) == (pytest.approx(abs(E["dmm_dD"])), pytest.approx(abs(E["dmm_bs"])))


def _crosscheck_case():
    """Synthetic +6 on A0 plus a harmless common +2 on both passes: ΔD = 6, bs = 10 -> verdict SUSPECT."""
    return _batch("0", "2", common=2)


def test_crosscheck_suspect_is_proposed_unapplied_through_batch():
    out, props, recs, api = _crosscheck_case()
    assert [(p.status, p.crosscheck_verdict, p.za_proposed_c) for p in props] == [("SUSPECT_CROSSCHECK", "SUSPECT",
                                                                                    pytest.approx(-6.0))]
    assert recs and all(r["applied"] is False for r in recs)
    assert api["status"] == "SUSPECT_CROSSCHECK" and api["applied"] is False
    assert any("BIAS_CROSSCHECK_A" in (r["qa_reason"] or "") for r in out)


# QA-030 closed (was a strict xfail; XPASS 2026-11-02 20:02 EST): contract.bias_qc now passes proposed_c /
# proposal_record_id / evidence through for SUSPECT_CROSSCHECK proposals (UNVERIFIED, not applied).
# QA-040 closed (App Dev aab97bd, XPASS verified 2026-11-02 20:5x EST in a clean 853ca80 worktree): strict xfail removed, body unchanged.
def test_contract_shows_suspect_ranked_proposal_to_reviewer():
    _, _, recs, api = _crosscheck_case()
    assert api["proposed_c"] == pytest.approx(-6.0) and api["proposal_record_id"] == recs[0]["record_id"]
    ev = api["evidence"]                                   # QA 20:10: assert content, not just truthiness
    assert float(ev["za"]) == pytest.approx(-6.0)          # v1.5: API field `za` (SCHEMA_CONTRACT §5.5, api_contract)
    assert (float(ev["abs_dD"]), float(ev["abs_bs"])) == (pytest.approx(6.0), pytest.approx(10.0))
    assert ev["crosscheck_verdict"] == "SUSPECT" and ev["status"] == "SUSPECT_CROSSCHECK"
    assert api["ranking"] == "SUSPECT" and api["display_status"] == "UNVERIFIED" and api["applied"] is False
    assert all(r["applied"] is False and not r["reviewed_by"] for r in recs)


# ---------------------------------------------------------------- (d) Mikkelsen 2003 p7
def test_d_mikkelsen_p7_values():
    p = PRIV / "inclinometer_bias_rules.json"
    if not p.exists():
        pytest.skip("qa_private not present")
    d = json.loads(p.read_text())
    assert d["p7_delta_0"] + d["p7_delta_180"] == d["p7_c"]           # key name 'p7_c' = Mikkelsen's bs
    assert abs(d["p7_c"]) < d["p7_typical_max_units"]                 # 'generally less than 25 units'
    dD = d["p7_delta_0"] - d["p7_delta_180"]                            # ΔD from the same two changes
    assert abs(abs(dD) - abs(d["p7_c"])) > XTOL_ABS                     # |ΔD| != |bs|: p7 is not a clean one-pass case


# QA-040 (v1.5 rename, SCHEMA_CONTRACT §5.5 "No API payload carries `ZA`"): neither the per-survey bias_qc summary nor
# the bias_proposal row may expose `ZA` (key or unit text); the value is `za`.
# QA-040 closed (App Dev aab97bd, XPASS verified 2026-11-02 20:5x EST): strict xfail removed, body unchanged.
def test_v15_no_api_payload_carries_uppercase_ZA():
    _, _, recs, api = _crosscheck_case()
    sys.path.insert(0, str(ROOT / "app" / "backend"))
    from repos import contract as ct
    prop = ct.bias_proposal(recs[0])
    for payload in (api, prop):
        assert "ZA" not in payload.get("evidence", {}), payload.get("evidence")
        assert "ZA" not in json.dumps({k: v for k, v in payload.items()}, default=str), payload
    assert prop["za"] == pytest.approx(-6.0)
