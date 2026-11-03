"""Geotech lead answers (2026-11-02): workbook depth-label profile, A-axis rotation conventions with review gating,
spiral deferred. Public/synthetic numbers only. Sources: /workspace/dam-refs/inclinometers/user_tool/REPORT.md (D2, E2),
verify T13d/T13e/T14f/T7d, MIKKELSEN-2003 PDF p9 eq 9-10 + Fig 7, TRB-EC129 PDF p44."""
import math
import os
import re

import pytest

from dam_safety.inclinometer import corrections as C, dssmr_appendix, qc
from dam_safety.inclinometer.installation import FT, IMPORT_PROFILES, Installation, Probe, interval_top_from_label
from dam_safety.inclinometer.reduction import reduce_survey
from dam_safety.inclinometer.survey_import import import_reading, make_reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRIC = Probe(L_m=0.5, K=25000.0, raw_unit="sin*25000")
REV = dict(reviewed_by="geotech-lead", reviewed_utc="2026-11-02T20:00:00Z")


def rot(**kw):
    base = dict(record_id="rot1", dam_id="D1", instrument_id="I1", survey_id="S2", axis="A",
                correction_type="rotation", parameter_name="sin_alpha", auto_value=None, detection_method=None,
                detection_statistic=None, override_value=None, applied=True, sign_convention="MIKKELSEN2003_EQ9",
                depth_range=None, reason="test", source_citation="MIKKELSEN-2003 p9 eq 9", user="eng",
                timestamp_utc="2026-11-02T00:00:00Z")
    base.update(kw)
    return C.CorrectionRecord(**base)


# ---- (1) depth-label import profiles -------------------------------------------------------------------------------
def test_workbook_profile_from_bottom_label_rekeyed_raw_label_kept():
    # REPORT D2 / verify T13d-e: 30 ft casing, L 2 ft -> top = 28 - d ft
    for d in (0, 2, 26):
        top, ref = interval_top_from_label("workbook", d, FT, 30 * FT, 2 * FT)
        assert ref == "from_bottom_label" and top == pytest.approx((28 - d) * FT, abs=1e-9)
    r = import_reading("workbook", "26", FT, "I1", "S1", "t", "A", 10, -8, "sin*20000", 30 * FT, 2 * FT)
    assert r.depth_top_m == pytest.approx(2 * FT, abs=1e-9)
    assert (r.raw_depth_label, r.depth_reference) == ("26", "from_bottom_label")
    with pytest.raises(ValueError):
        interval_top_from_label("workbook", 26, FT)                # needs casing depth and L, no guessing


@pytest.mark.parametrize("profile", ["gtilt", "digipro"])
def test_gtilt_digipro_keep_native_from_top_depths(profile):
    top, ref = interval_top_from_label(profile, 2, FT, 30 * FT, 2 * FT)
    assert ref == "top_of_casing" and top == pytest.approx(2 * FT, abs=1e-9)     # NOT re-keyed to 26 ft
    r = import_reading(profile, 2.0, FT, "I1", "S1", "t", "B", 1, 1, "u", 30 * FT, 2 * FT)
    assert r.depth_reference == "top_of_casing" and r.raw_depth_label == 2.0
    assert make_reading("I1", "S1", "t", 0.0, "A", 1, 1, "u").depth_reference == "top_of_casing"
    with pytest.raises(ValueError):
        interval_top_from_label("excel_guess", 2, FT)
    assert set(IMPORT_PROFILES) == {"workbook", "gtilt", "digipro"}


# ---- (2) rotation, axis A ------------------------------------------------------------------------------------------
def _profile(n, dB):
    return [i * METRIC.L_m for i in range(n)], dB


def test_mikkelsen_2003_eq9_fig7_example():
    # PDF p9: 35 mm induced on A by a 4500 mm B profile -> sin a = 35/4500 = 0.0078 (a = 0.45 deg)
    s = 35 / 4500
    assert round(s, 4) == 0.0078 and round(math.degrees(math.asin(s)), 2) == 0.45
    n = 90
    dB = 2 * METRIC.K * 4500 / (n * METRIC.L_m * 1000)             # D per interval: B profile sums to 4500 mm
    depths = [i * METRIC.L_m for i in range(n)]
    D = {}
    for d in depths:
        D[(d, "B")] = dB
        D[(d, "A")] = 0.0 - s * dB                                  # eq 9: A' - A = -B' sin a_A, true A = 0
    rows = reduce_survey(D, {k: 0.0 for k in D}, METRIC, "A")
    assert rows[0].cum_change_mm == pytest.approx(-35.0)
    out = C.apply(D, [rot(auto_value=s, **REV)])
    assert max(abs(out[(d, "A")]) for d in depths) < 1e-9
    wrong = C.apply(D, [rot(auto_value=s, sign_convention="A_corr=A-f*B", **REV)])     # wrong sign doubles error
    assert reduce_survey(wrong, {k: 0.0 for k in D}, METRIC, "A")[0].cum_change_mm == pytest.approx(-70.0)


def test_gtilt_plus_sin_convention_t7d():
    # verify T7d / INFO T14f: GTILT e = +sin(rot)*B0 -> top cum error sin0.5*sin4*50000 = 30.4 mm, removed by k=-1
    r = math.sin(math.radians(0.5))
    b0 = METRIC.K * math.sin(math.radians(4))
    depths = [i * METRIC.L_m for i in range(100)]
    D = {}
    for d in depths:
        e = r * b0
        D[(d, "B")] = b0 - (-b0)
        D[(d, "A")] = (0 + e) - (0 - e)
    base = {k: 0.0 for k in D}
    assert round(reduce_survey(D, base, METRIC, "A")[0].cum_change_mm, 1) == 30.4
    out = C.apply(D, [rot(sign_convention="GTILT_PLUS_SIN", auto_value=r, **REV)])
    assert abs(reduce_survey(out, base, METRIC, "A")[0].cum_change_mm) < 1e-9


def test_rotation_sign_convention_required_and_axis_b_unverified():
    for sc in (None, "", "gtilt", "+sin"):
        with pytest.raises(ValueError):
            rot(sign_convention=sc, applied=False, reason="", user="")
    with pytest.raises(ValueError):
        rot(axis="B", auto_value=0.01, **REV)                       # B-axis UNVERIFIED (eq 10)
    b = rot(axis="B", auto_value=0.01, applied=False, **REV)        # may be stored, not applied
    assert b.sign_convention == "MIKKELSEN2003_EQ9" and C.active([b], "I1", "S2") == []
    assert "axis" in C.AUDIT_FIELDS and "sign_convention" in C.AUDIT_FIELDS


def test_rotation_off_by_default_until_reviewed():
    # TRB-EC129 PDF p44: rotation correction is not routinely performed
    with pytest.raises(ValueError):
        rot(auto_value=0.01)                                        # applied but no review
    with pytest.raises(ValueError):
        rot(auto_value=0.01, reviewed_by="eng", reviewed_utc="2026-11-02T20:00:00Z")   # self-review is not a review
    proposed = rot(auto_value=0.01, applied=False, reason="", user="")
    assert C.active([proposed], "I1", "S2") == []
    D = {(0.0, "A"): 5.0, (0.0, "B"): 100.0}
    with pytest.raises(ValueError):
        C.apply(D, [proposed])                                      # never applied even if passed directly
    assert C.apply(D, [rot(auto_value=0.01, **REV)])[(0.0, "A")] == pytest.approx(6.0)


# ---- (3) spiral deferred -------------------------------------------------------------------------------------------
def test_spiral_kept_in_enum_never_applied_info_flag():
    assert "spiral" in C.CORRECTION_TYPES
    sp = dict(record_id="sp", dam_id="D1", instrument_id="I1", survey_id="SP1", axis="A", correction_type="spiral",
              parameter_name="twist_deg", auto_value=3.0, detection_method=None, detection_statistic=None,
              override_value=None, sign_convention=None, depth_range=None, reason="", source_citation="", user="",
              timestamp_utc="2026-11-02T00:00:00Z")
    with pytest.raises(ValueError):                                 # REPORT E2: never mark as corrected
        C.CorrectionRecord(applied=True, **{**sp, "reason": "x", "source_citation": "x", "user": "x"})
    stored = C.CorrectionRecord(applied=False, **sp)
    inst = Installation("I1", "D1", METRIC, 1.0, 0.0, None, spiral_survey_id="SP1")
    plain = Installation("I1", "D1", METRIC, 1.0, 0.0, None)
    assert C.info_flags(inst) == ("SPIRAL_NOT_APPLIED",)
    assert C.info_flags(plain) == () and C.info_flags(plain, [stored]) == ("SPIRAL_NOT_APPLIED",)
    q = qc.survey_qc("S2", "A", {0.0: 0.0, 0.5: 0.0})
    reason = C.merge_qa_reason(";".join(q.reasons), C.info_flags(inst))
    assert reason.endswith("SPIRAL_NOT_APPLIED") and q.flag == "OK"        # info only, severity unchanged
    assert C.merge_qa_reason(reason, C.info_flags(inst)) == reason
    rows = reduce_survey({(0.0, "A"): 1.0}, {(0.0, "A"): 0.0}, METRIC, "A")
    a = dssmr_appendix.appendix(inst, [q], {"A": rows}, [stored], "NORMAL", None, "x", {}, "t")
    assert a["info_flags"] == ["SPIRAL_NOT_APPLIED"]
    assert all(c["applied"] is False for c in a["corrections"] if c["correction_type"] == "spiral")


def test_ddl_and_docs_carry_the_rulings():
    s = open(os.path.join(ROOT, "sql", "ddl", "02_ref_calibration_threshold.sql"), encoding="utf-8").read()
    inst = s.split("CREATE TABLE IF NOT EXISTS ref.incl_installation (")[1].split("\n);")[0]
    assert "spiral_survey_id" in inst and "from_bottom_label" in inst
    silver = open(os.path.join(ROOT, "sql", "ddl", "06_silver.sql"), encoding="utf-8").read()
    assert re.search(r"raw_depth_label\s+STRING", silver) and "from_bottom_label" in silver
    v = open(os.path.join(ROOT, "docs", "VERIFICATION.md"), encoding="utf-8").read()
    for key in ("INC-LABEL | **UNVERIFIED", "INC-ROTB | **UNVERIFIED", "SPIRAL_NOT_APPLIED"):
        assert key in v
