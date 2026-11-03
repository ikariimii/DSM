"""silver.incl_reduced job core (dam_safety.inclinometer.batch): columns = DDL 06, corrections from stored records,
QA-020 flags (propose only), QA-021 bias-rule hook, workbook depth re-key, spiral info flag. All data SYNTHETIC."""
import ast
import os
import re

import pytest

from dam_safety.inclinometer import batch, qc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TS = "2026-11-02T00:00:00"


def inst_row(**kw):
    r = dict(instrument_id="I1", dam_id="D1", probe_L_m=0.5, probe_K=25000.0, raw_unit="sin*25000",
             casing_depth_m=10.0, a0_azimuth_deg=0.0, baseline_survey_id="S0", system="probe",
             spiral_survey_id=None, stable_zone_top_m=7.0, depth_reference="top_of_casing")
    r.update(kw)
    return r


def survey(sid, ts, a0_shift=0.0, noise=0.0, label=lambda d: d):
    rows = []
    for i in range(20):
        d = round(i * 0.5, 9)
        e = noise if i % 2 else -noise
        a, b = 100.0 + i, -(100.0 + i) + 12.0
        sh = a0_shift + e if d >= 7.0 else 0.0
        rows.append(dict(survey_id=sid, survey_ts=ts, depth_m=label(d), a0=a + sh, a180=b, b0=50.0, b180=-38.0))
    return rows


def corr(rid="R1", **kw):
    r = dict(record_id=rid, dam_id="D1", instrument_id="I1", survey_id="S5", axis="A", correction_type="bias",
             parameter_name="c", auto_value=6.0, detection_method="between_set_bias_qc", detection_statistic=None,
             override_value=None, applied=True, sign_convention=None, depth_range=None, reason="QA-020 proposal reviewed",
             source_citation="procedure §7.1", user="eng", timestamp_utc=TS, superseded_by=None,
             reviewed_by="eng2", reviewed_utc=TS)
    r.update(kw)
    return r


def run(surveys, corrs=(), **kw):
    rows = [r for s in surveys for r in s]
    return batch.reduce_instrument(kw.pop("inst", inst_row()), rows, list(corrs), "B1", TS, **kw)


def by(rows, sid):
    return {r["depth_m"]: r for r in rows if r["survey_id"] == sid}


def test_columns_match_ddl_06():
    ddl = open(os.path.join(ROOT, "sql/ddl/06_silver.sql")).read()
    body = ddl.split("silver.incl_reduced (", 1)[1].split("\n)", 1)[0]
    body = re.sub(r"--[^\n]*", "", re.sub(r"COMMENT '[^']*'", "", body))
    cols = tuple(m.group(1) for m in re.finditer(r"(\w+)\s+(?:ARRAY<STRING>|STRING\b|DOUBLE\b|TIMESTAMP\b)", body))
    assert cols == batch.REDUCED_COLUMNS
    out, _ = run([survey("S0", "2026-01-01")])
    assert out and all(tuple(r) == batch.REDUCED_COLUMNS for r in out)


def test_baseline_rows_zero_change_and_lineage():
    out, props = run([survey("S0", "2026-01-01"), survey("S1", "2026-02-01")])
    s0 = by(out, "S0")
    assert len(s0) == 20 and not props
    r = s0[0.0]
    assert r["inc_a"] == pytest.approx(0) and r["cum_a"] == pytest.approx(0) and r["qa_flag"] == "OK"
    assert r["diff_a"] == pytest.approx(100 - (-88)) and r["checksum_a"] == pytest.approx(12)
    assert r["baseline_survey_id"] == "S0" and r["initial_survey_ts"] == "2026-01-01" and r["fixed_end"] == "bottom"
    assert r["formula_version"] == batch.ENGINE_VERSION == "1.0.0" and r["recompute_batch_id"] == "B1"
    assert r["qc_rule_version"] == batch.BIAS_RULE_VERSION and r["probe_constants"].startswith("L_m=0.5;K=25000.0;")
    assert r["correction_record_ids"] == [] and r["raw_depth_label"] == "0.0" and r["depth_reference"] == "top_of_casing"


def test_baseline_none_uses_earliest_survey():
    out, _ = run([survey("S1", "2026-02-01"), survey("S0", "2026-01-01")], inst=inst_row(baseline_survey_id=None))
    assert {r["baseline_survey_id"] for r in out} == {"S0"}


def test_qa020_bias_is_flagged_and_proposed_never_applied():
    out, props = run([survey("S0", "2026-01-01"), survey("S5", "2026-03-01", a0_shift=-6.0, noise=0.5)])
    s5 = by(out, "S5")
    assert len(props) == 1 and props[0].status == "PROPOSED" and props[0].za_proposed_c == pytest.approx(6.0)
    assert all("BIAS_PROPOSED_A" in r["qa_reason"] and r["qa_flag"] == "SUSPECT" for r in s5.values())
    assert s5[9.5]["diff_a"] == pytest.approx(by(out, "S0")[9.5]["diff_a"] - 5.5)   # uncorrected (shift -6 + noise +0.5)
    assert all(r["correction_record_ids"] == [] for r in s5.values())


def test_crosscheck_suspect_is_a_ranked_proposal_not_a_rejection():
    rows = survey("S5", "2026-03-01")
    for r in rows:
        if r["depth_m"] >= 7.0:
            r["a0"] += 3.0
            r["a180"] -= 3.0                                  # movement-like: ΔD = 6, bs = 0 -> verdict SUSPECT
    out, props = run([survey("S0", "2026-01-01"), rows])
    s5 = by(out, "S5")
    assert [p.status for p in props] == ["SUSPECT_CROSSCHECK"] and props[0].za_proposed_c == pytest.approx(-6.0)
    assert all({"BIAS_PROPOSED_A", "BIAS_CROSSCHECK_A"} <= set(r["qa_reason"].split(",")) and r["qa_flag"] == "SUSPECT"
               for r in s5.values())


def test_a180_only_shift_proposed_in_batch():
    rows = survey("S5", "2026-03-01")
    for r in rows:
        if r["depth_m"] >= 7.0:
            r["a180"] += 6.0                                  # ΔD = −6, bs = +6 -> PROPOSED ZA = +6 (no sign gate)
    _out, props = run([survey("S0", "2026-01-01"), rows])
    assert [p.status for p in props] == ["PROPOSED"] and props[0].za_proposed_c == pytest.approx(6.0)


def test_applied_reviewed_bias_record_is_used_and_suppresses_proposal():
    out, props = run([survey("S0", "2026-01-01"), survey("S5", "2026-03-01", a0_shift=-6.0)], [corr()])
    s5, s0 = by(out, "S5"), by(out, "S0")
    assert not props
    assert all(r["correction_record_ids"] == ["R1"] for r in s5.values())
    assert s5[9.5]["diff_a"] == pytest.approx(s0[9.5]["diff_a"]) and "BIAS_PROPOSED" not in (s5[9.5]["qa_reason"] or "")
    # unapplied / superseded records are ignored
    out2, props2 = run([survey("S0", "2026-01-01"), survey("S5", "2026-03-01", a0_shift=-6.0)],
                       [corr(applied=False), corr("R2", superseded_by="R3")])
    assert all(r["correction_record_ids"] == [] for r in by(out2, "S5").values()) and len(props2) == 1


def test_qa021_hook_bias_rule_is_injectable():
    seen = []

    def rule(installation, axis, sid, suspect, reference, ref_id):
        seen.append((axis, sid, ref_id))
        return qc.between_set_bias_qc(installation, axis, sid, suspect, reference, ref_id,
                                      params=qc.BiasQCParams(min_stable_intervals=99))
    out, props = run([survey("S0", "2026-01-01"), survey("S5", "2026-03-01", a0_shift=-6.0, noise=0.5)], bias_rule=rule)
    assert seen == [("A", "S5", "S0"), ("B", "S5", "S0")] and not props
    assert batch.DEFAULT_BIAS_RULE is qc.between_set_bias_qc


def test_workbook_profile_rekeys_from_bottom_label_and_keeps_label():
    lab = lambda d: round(10.0 - d - 0.5, 9)                 # label from bottom: top = casing - d - L
    out, _ = run([survey("S0", "2026-01-01", label=lab)], inst=inst_row(depth_reference="from_bottom_label"))
    s0 = by(out, "S0")
    assert sorted(s0) == [round(i * 0.5, 9) for i in range(20)]
    assert s0[0.0]["raw_depth_label"] == "9.5" and s0[0.0]["depth_reference"] == "from_bottom_label"
    assert s0[0.0]["diff_a"] == pytest.approx(188.0)


def test_spiral_info_flag_and_invalid_record_flag():
    out, _ = run([survey("S0", "2026-01-01")], inst=inst_row(spiral_survey_id="SP1"))
    assert all(r["qa_reason"].endswith("SPIRAL_NOT_APPLIED") and r["qa_flag"] == "OK" for r in out)
    bad = corr(correction_type="rotation", sign_convention="MIKKELSEN2003_EQ9", reviewed_by=None, reviewed_utc=None,
               survey_id="S0")
    out, _ = run([survey("S0", "2026-01-01")], [bad])
    assert all("CORRECTION_RECORD_INVALID" in r["qa_reason"] and r["qa_flag"] == "SUSPECT" for r in out)
    assert all(r["correction_record_ids"] == [] for r in out)


def test_missing_and_bad_readings_flag_rows():
    s = survey("S0", "2026-01-01")
    s[3]["a180"] = None
    s[4]["a0"] = "ERR"
    r = by(run([s])[0], "S0")
    assert r[1.5]["qa_flag"] == "MISSING" and r[1.5]["diff_a"] is None
    assert r[2.0]["qa_flag"] == "BAD" and "BAD:TEXT" in r[2.0]["qa_reason"]


def test_installation_sign_mult_must_match_system():
    with pytest.raises(ValueError, match="sign_mult_B"):
        run([survey("S0", "2026-01-01")], inst=inst_row(system="saa", sign_mult_A=1, sign_mult_B=1))
    out, _ = run([survey("S0", "2026-01-01")], inst=inst_row(system="saa", sign_mult_A=1, sign_mult_B=-1))
    assert out


def test_target_surveys_filter():
    out, _ = run([survey("S0", "2026-01-01"), survey("S1", "2026-02-01")], target_surveys={"S1"})
    assert {r["survey_id"] for r in out} == {"S1"} and {r["baseline_survey_id"] for r in out} == {"S0"}


def test_spark_wrapper_wired_and_parses():
    src = open(os.path.join(ROOT, "src/jobs/reduce_inclinometers.py")).read()
    ast.parse(src)
    assert "batch.reduce_instrument" in src and "silver.incl_reduced" in src and 'mode("append")' in src
    jobs = open(os.path.join(ROOT, "resources/jobs.yml")).read()
    assert "../src/jobs/reduce_inclinometers.py" in jobs
    assert "last_version" in src and "BIAS_RULE_VERSION" in src           # version bump -> re-reduce
    assert batch.BIAS_RULE_VERSION == qc.BIAS_RULE == "QA021-v2"


def test_spark_wrapper_schema_matches_core():
    pytest.importorskip("pyspark")
    import importlib.util
    spec = importlib.util.spec_from_file_location("ri", os.path.join(ROOT, "src/jobs/reduce_inclinometers.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert tuple(f.name for f in m.OUT_SCHEMA.fields) == batch.REDUCED_COLUMNS


def test_inclinometer_lineage_joins_a_verified_formula_row_without_calibration():
    # SCHEMA_CONTRACT v1.4 §5.5: calib_id is NULL for inclinometers; approval = ref.formula verified row for
    # (formula_id, formula_version) + the ref.incl_installation SCD2 row named by installation_version.
    seed = open(os.path.join(ROOT, "sql", "seed", "ref_formula_seed.sql"), encoding="utf-8").read()
    assert re.search(r"\('INCL_PROBE_2PASS', '1\.0\.0', .*, true, 'dam_safety\.formulas\.registry#sha256=", seed)
    out, _ = run([survey("S0", "2026-01-01"), survey("S5", "2026-03-01")], inst=inst_row(effective_from="2026-01-01T00:00:00"))
    r = out[0]
    assert r["calib_id"] is None and (r["formula_id"], r["formula_version"]) == ("INCL_PROBE_2PASS", "1.0.0")
    assert r["installation_version"] == "2026-01-01T00:00:00"

