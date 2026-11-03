"""Inclinometer module (src/dam_safety/inclinometer), REPORT §6 template. Only public worked values and synthetic
numbers here; the user's workbook values are tested in testonly_restricted/ (gitignored, sync-excluded).
Sources: /workspace/dam-refs/procedures/inclinometers.md (§3, §4.0, §4.4, §5, §7, §6) and
/workspace/dam-refs/inclinometers/user_tool/REPORT.md (§5 D1-D10, §6.1, §6.3)."""
import math
import os
import re
from datetime import date

import pytest

from dam_safety.inclinometer import alerts, corrections as C, diagnostics, dssmr_appendix, qc, rates
from dam_safety.inclinometer.engine import d_map, reduce_with_corrections
from dam_safety.inclinometer.installation import ENGLISH_DIGITILT, FT, IN, Installation, Probe, rekey_from_bottom_label
from dam_safety.inclinometer.reduction import deviation_mm, reduce_survey
from dam_safety.inclinometer.survey_import import make_reading, renormalise

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = Probe(L_m=0.5, K=25000.0, raw_unit="sin*25000")


def rec(**kw):
    base = dict(record_id="r1", dam_id="D1", instrument_id="I1", survey_id="S2", axis="A", correction_type="bias",
                parameter_name="c", auto_value=None, detection_method=None, detection_statistic=None,
                override_value=None, applied=True, sign_convention=None, depth_range=None, reason="test",
                source_citation="procedure §5", user="eng", timestamp_utc="2026-11-02T00:00:00Z",
                reviewed_by="reviewer", reviewed_utc="2026-11-02T01:00:00Z")   # applied bias needs approval (§5)
    base.update(kw)
    return C.CorrectionRecord(**base)


def test_dgsi_worked_example_english_probe():
    # procedure §4.1 / SI-DR-2001 p.2-3 (DGSI-DATAREDUCTION): A0 359, A180 -339 -> C 349, CS 20, dev 0.4188 in
    D = 359 - (-339)
    assert D / 2 == 349 and 359 + (-339) == 20
    assert abs(deviation_mm(D, ENGLISH_DIGITILT) / (IN * 1000) - 0.4188) < 1e-12


def test_mikkelsen_2003_bias_cancels_in_D():
    # MIKKELSEN-2003 PDF p4 eq (1)-(4) (verify T14a): 1 deg, K 25000, bias 10 -> A0 446, A180 -426, D 872, CS 20
    a0 = round(25000 * math.sin(math.radians(1))) + 10
    a180 = round(25000 * math.sin(math.radians(-1))) + 10
    r = make_reading("I1", "S1", "t", 0.0, "A", a0, a180, "sin*25000")
    assert (r.r0 - r.r180, r.r0 + r.r180) == (872, 20)


def test_depth_rekey_from_bottom_label_to_interval_top():
    # REPORT D2 / verify T13d-e: label d ft (from bottom) -> interval top at 28-d ft from top (30 ft casing, L 2 ft)
    assert abs(rekey_from_bottom_label(26 * FT, 30 * FT, 2 * FT) - 2 * FT) < 1e-9
    assert abs(rekey_from_bottom_label(0.0, 30 * FT, 2 * FT) - 28 * FT) < 1e-9
    with pytest.raises(ValueError):
        rekey_from_bottom_label(29 * FT, 30 * FT, 2 * FT)
    inst = Installation("I1", "D1", P, 2.0, 0.0, None)
    assert inst.interval_tops_m() == [0.0, 0.5, 1.0, 1.5]


def test_saa_b_sign_flip_once_raw_kept():
    # procedure §4.4: stored B = -Y, applied once, flag sign_normalized; raw kept
    r = make_reading("I1", "S1", "t", 0.0, "B", "12", 5, "mm", system="saa")
    assert (r.r0, r.r180, r.raw_r0, r.sign_mult, r.sign_normalized) == (-12.0, -5.0, "12", -1, True)
    assert renormalise(r, "saa") is r
    a = make_reading("I1", "S1", "t", 0.0, "A", 12, 5, "mm", system="saa")
    assert a.r0 == 12.0


def test_validation_replaces_iferror_zero():
    # REPORT E9: bad input is MISSING/BAD, never 0
    assert make_reading("I", "S", "t", 0, "A", "", 3, "u").qa_flag == "MISSING"
    assert make_reading("I", "S", "t", 0, "A", "x", 3, "u").qa_flag == "BAD"
    assert make_reading("I", "S", "t", 0, "A", 1e6, 3, "u", max_abs=5e4).qa_flag == "BAD"
    D = d_map([make_reading("I", "S", "t", 0.0, "A", "", 3, "u"), make_reading("I", "S", "t", 0.5, "A", 10, 0, "u")])
    rows = reduce_survey(D, D, P, "A")
    assert rows[0].cum_change_mm is None and rows[1].cum_change_mm == 0.0     # missing breaks cum ABOVE it


def test_cumulative_from_bottom_includes_own_interval_top_keyed():
    # procedure §4.0: cum at the interval top includes that interval
    D = {(0.0, "A"): 100.0, (0.5, "A"): 50.0, (1.0, "A"): 10.0}
    B = {k: 0.0 for k in D}
    rows = reduce_survey(D, B, P, "A")
    f = P.L_m * 1000 / (2 * P.K)
    assert [round(r.cum_change_mm / f, 9) for r in rows] == [160.0, 60.0, 10.0]


def test_bias_full_c_and_half_on_both_faces_is_noop():
    # procedure §5.1: add c (full) to A0 -> D + c. REPORT E1: c/2 on both faces cancels.
    D = {(0.0, "A"): 100.0, (0.5, "A"): 40.0}
    out = C.apply(D, [rec(auto_value=-90.0)])
    assert out == {(0.0, "A"): 10.0, (0.5, "A"): -50.0} and D[(0.0, "A")] == 100.0      # input untouched
    assert ((359 - 45) - (-339 - 45)) == 359 - (-339)
    assert C.apply(D, [rec(auto_value=5.0, depth_range=(0.5, 0.5))])[(0.0, "A")] == 100.0


def test_checksum_shift_alone_creates_no_correction():
    # MIKKELSEN-2003 p4-5: a between-set CS shift is SUSPECT/investigate, never an automatic bias record
    q = qc.survey_qc("S3", "A", {0.0: 114.0, 0.5: 114.0}, prev_mean_cs=24.0, delta_tol=20.0)
    assert q.flag == "SUSPECT" and "DELTA_CS_INVESTIGATE" in q.reasons and q.delta_mean_cs == 90.0
    assert "MEAN_CS_GT_100" in q.reasons
    q2 = qc.survey_qc("S1", "A", {0.0: 24.0, 0.5: 30.0, 1.0: 24.0})
    assert q2.flag == "OK"                                # the workbook's ABS(mean)>20 rule is not used (E8)
    q3 = qc.survey_qc("S1", "B", {0.0: 0.0, 0.5: 45.0, 1.0: 0.0, 1.5: 0.0})
    assert q3.outlier_depths == (0.5,)                    # +-20 for B (procedure §7)


def test_rotation_correction_and_fit():
    # MIKKELSEN-2007 p17-18: dev_A_corr = dev_A - f*dev_B; factor normally < sin 1 deg
    DB = [100.0, 300.0, -50.0, 200.0]
    true = [10.0, 20.0, 5.0, 0.0]
    f = 0.01
    D = {}
    for i, (t, b) in enumerate(zip(true, DB)):
        D[(i * 0.5, "A")] = t + f * b
        D[(i * 0.5, "B")] = b
    out = C.apply(D, [rec(correction_type="rotation", parameter_name="f", auto_value=f, sign_convention="A_corr=A-f*B",
                          reviewed_by="geotech", reviewed_utc="2026-11-02T01:00:00Z")])
    assert all(abs(out[(i * 0.5, "A")] - t) < 1e-12 for i, t in enumerate(true))
    with pytest.raises(ValueError):
        C.apply(D, [rec(correction_type="rotation", parameter_name="f", auto_value=f, sign_convention="gtilt")])
    fit = diagnostics.rotation_fit([f * b for b in DB], DB)
    assert abs(fit["factor"] - f) < 1e-12 and abs(fit["r"] - 1) < 1e-12 and fit["plausible"]
    assert not diagnostics.rotation_fit([133.4 * b for b in DB], DB)["plausible"]


def test_depth_shift_correction_and_search():
    # verify T13l convention: corrected[i] = observed[i+n], bottom->top
    D = {(0.0, "A"): 4.0, (0.5, "A"): 3.0, (1.0, "A"): 2.0, (1.5, "A"): 1.0}    # bottom->top: 1,2,3,4
    out = C.apply(D, [rec(correction_type="depth", parameter_name="n_intervals", auto_value=1.0)])
    assert [out[(d, "A")] for d in (1.5, 1.0, 0.5, 0.0)] == [2.0, 3.0, 4.0, None]
    assert C.apply(D, [rec(correction_type="depth", parameter_name="n", auto_value=1.0)], fill="zero")[(0.0, "A")] == 0.0
    s = diagnostics.depth_shift_search([1, 5, 9, 2, 0], [5, 9, 2, 0, 0])
    assert s["best_shift"] == 1


def test_records_superseded_unapplied_and_unimplemented():
    recs = [rec(record_id="a", auto_value=10.0, superseded_by="b"), rec(record_id="b", auto_value=20.0),
            rec(record_id="c", auto_value=99.0, applied=False, reason="", user="")]
    act = C.active(recs, "I1", "S2")
    assert [r.record_id for r in act] == ["b"]
    with pytest.raises(ValueError):
        rec(applied=True, reason="")                       # D10: applied needs reason/citation/user
    with pytest.raises(ValueError):                        # spiral deferred: never applied (REPORT E2)
        rec(correction_type="spiral", auto_value=1.0)
    assert rec(override_value=3.0, auto_value=1.0).value == 3.0


def test_engine_audit_before_after():
    mk = lambda s, vals: [make_reading("I1", s, "t", d, "A", v, 0, "u") for d, v in vals]   # noqa: E731
    base, cur = mk("S1", [(0.0, 0), (0.5, 0)]), mk("S2", [(0.0, 100), (0.5, 100)])
    rows, audit = reduce_with_corrections(cur, base, P, [rec(auto_value=-100.0)], "I1", "S2")
    f = P.L_m * 1000 / (2 * P.K)
    assert abs(audit[0].before_value - 200 * f) < 1e-12 and abs(audit[0].after_value) < 1e-12
    assert rows["A"][0].cum_change_mm == pytest.approx(0.0)


def test_rates_same_baseline_and_usace_rules():
    r = rates.rates({0.0: 1.0}, {0.0: 3.0}, date(2026, 1, 1), date(2026, 1, 11), "S1", "S1")
    assert r[0.0]["mm_per_day"] == pytest.approx(0.2)
    with pytest.raises(ValueError):
        rates.rates({}, {}, date(2026, 1, 1), date(2026, 1, 2), "S1", "S2")
    assert abs(alerts.USACE_SIGNIFICANT_MM - 0.25 * 25.4) < 1e-12                 # procedure §6
    assert alerts.three_consecutive_same_direction([0, 1, 2, 3])
    assert not alerts.three_consecutive_same_direction([0, 1, 0.5, 3])
    assert alerts.level(-7.0, [("WATCH", 6.35), ("ALERT", 25.0)]) == "WATCH"


def test_appendix_sign_safe_max_and_computed_count():
    # REPORT E5/E6: computed survey count; MAX() must not miss negative movement
    rows = reduce_survey({(0.0, "A"): -500.0, (0.5, "A"): 10.0}, {(0.0, "A"): 0.0, (0.5, "A"): 0.0}, P, "A")
    m = dssmr_appendix.max_abs_change(rows)
    assert m["value_mm"] < 0 and m["depth_top_m"] == 0.0
    inst = Installation("I1", "D1", P, 1.0, 0.0, None)
    qs = [qc.survey_qc(s, ax, {0.0: 0.0}) for s in ("S1", "S2") for ax in "AB"]
    a = dssmr_appendix.appendix(inst, qs, {"A": rows}, [rec(auto_value=1.0)], "NORMAL", None, "x", {"f": "h"}, "t")
    assert a["survey_count"] == 2 and a["corrections"][0]["record_id"] == "r1"


def test_ddl_carries_report_6_3_audit_fields_and_row_filter():
    s = open(os.path.join(ROOT, "sql", "ddl", "02_ref_calibration_threshold.sql"), encoding="utf-8").read()
    body = s.split("CREATE TABLE IF NOT EXISTS ref.incl_correction (")[1].split(");")[0]
    cols = [re.match(r"\s*(\w+)", ln).group(1) for ln in body.strip().splitlines() if ln.strip()]
    assert cols == list(C.AUDIT_FIELDS)
    sec = open(os.path.join(ROOT, "sql", "ddl", "10a_row_filters_table.sql"), encoding="utf-8").read()
    for t in ("ref.incl_correction", "ref.incl_installation"):
        assert re.search(rf"ALTER TABLE {re.escape(t)}\s+SET ROW FILTER ref.dam_row_filter ON \(dam_id\)", sec)


def test_user_tool_paths_excluded_from_git_and_sync():
    gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    yml = open(os.path.join(ROOT, "databricks.yml"), encoding="utf-8").read()
    for p in ("**/user_tool/", "*user_tool_surveys*", "*libreoffice_recalc_compare*", "testonly_restricted/"):
        assert p in gi and p in yml, p
