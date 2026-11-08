"""QA-042 synthetic cases; no vendor files, manual examples or private scalars embedded.

Rules: procedures/inclinometer_depth_semantics.md §§3–7 and inclinometers.md §3.1.
"""
import ast
import json
from pathlib import Path

import pytest

from dam_safety.inclinometer.depth_semantics import CONTEXT_FIELDS
from dam_safety.inclinometer.installation import interval_top_from_label, interval_tops_m
from dam_safety.inclinometer.survey_import import import_reading
from dam_safety.inclinometer.vendor_import import REQUIRED_METADATA

L, CASING = 0.8, 4.8
BASE = dict(depth_datum="index", depth_label_ref="midpoint", ground_shift=0.0,
            index_above_toc=0.2, auto_adjust=False, export_type="raw_survey", depth_mode="depth",
            output_point="midpoint", placement_verified=True, placement_evidence="synthetic declared placement",
            settings_verified=True, additional_depth_corrections="none", probe_type="classic",
            apply_stickup=False, stickup_value_m=0.4)


def convert(label, metadata=None, profile="gtilt", **kwargs):
    return interval_top_from_label(profile, label, 1.0, CASING, L,
                                   depth_metadata=dict(BASE) if metadata is None else metadata, **kwargs)[0]


@pytest.mark.parametrize("profile", ["gtilt", "digipro"])
@pytest.mark.parametrize("field", sorted(REQUIRED_METADATA | CONTEXT_FIELDS))
@pytest.mark.parametrize("missing", ["absent", "null"])
def test_no_missing_metadata_is_inferred(profile, field, missing):
    m = dict(BASE)
    if missing == "absent":
        del m[field]
    else:
        m[field] = None
    with pytest.raises(ValueError, match="metadata"):
        convert(0.6, m, profile)


@pytest.mark.parametrize("profile", ["gtilt", "digipro"])
def test_no_silent_top_of_casing_assumption(profile):
    with pytest.raises(ValueError, match="metadata"):
        interval_top_from_label(profile, 0.0, 1.0, CASING, L)
    # A perfect grid fit is not evidence for GTILT plot placement.
    with pytest.raises(ValueError, match="UNVERIFIED"):
        convert(0.0, dict(BASE, export_type="plot", placement_verified=False), profile)


@pytest.mark.parametrize("profile", ["gtilt", "digipro"])
@pytest.mark.parametrize("adjusted", [False, True])
@pytest.mark.parametrize("stickup_applied", [False, True])
def test_all_explicit_stickup_auto_adjust_states(profile, adjusted, stickup_applied):
    g = 0.4 if stickup_applied else 0.0
    q = 0.0 if adjusted else L / 2
    m = dict(BASE, depth_datum="ground" if stickup_applied else "index",
             auto_adjust=adjusted, output_point="top" if adjusted else "midpoint",
             apply_stickup=stickup_applied, ground_shift=g)
    for top in interval_tops_m(CASING, L):
        raw = top - g + m["index_above_toc"] + q
        assert convert(raw, m, profile) == pytest.approx(top)


def test_header_stickup_is_not_applied_when_setting_is_off():
    # BASE explicitly has a nonzero header stickup but Apply Stickup OFF.
    assert convert(0.6) == pytest.approx(0.0)
    with pytest.raises(ValueError, match="stickup state"):
        convert(0.6, dict(BASE, ground_shift=0.4))


@pytest.mark.parametrize("change", [dict(placement_verified=False), dict(settings_verified=False),
    dict(placement_evidence="UNVERIFIED"), dict(additional_depth_corrections="compression"),
    dict(output_point="UNVERIFIED"), dict(depth_label_ref="UNVERIFIED"), dict(probe_type="UNVERIFIED"),
    dict(depth_mode="UNVERIFIED"), dict(export_type="UNVERIFIED"), dict(apply_stickup="false"),
    dict(probe_type="at"), dict(auto_adjust=True, output_point="midpoint")])
def test_unknown_or_unsupported_vendor_interpretations_stop_conversion(change):
    with pytest.raises(ValueError):
        convert(0.6, dict(BASE, **change))


def test_at_uses_verified_top_marks_without_classic_adjustment():
    m = dict(BASE, probe_type="at", depth_label_ref="top", output_point="top")
    assert convert(1.0, m) == pytest.approx(0.8)
    with pytest.raises(ValueError):
        convert(1.0, dict(m, auto_adjust=True))


def test_bottom_output_and_toe_anchor_are_not_extra_intervals():
    m = dict(BASE, auto_adjust=True, output_point="bottom")
    assert convert(1.0, m) == pytest.approx(0.0)
    # An unadjusted top-wheel label at the toe is a boundary, not a new interval.
    with pytest.raises(ValueError, match="BELOW_CASING"):
        convert(CASING + 0.2, dict(m, depth_label_ref="top", auto_adjust=False, output_point="top"))


def test_elevation_uses_matching_datums_without_reapplying_stickup_or_index():
    m = dict(BASE, depth_mode="elevation", label_datum="synthetic project", toc_datum="synthetic project",
             toc_elevation_m=40.0, apply_stickup=True, ground_shift=0.4)
    assert convert(38.8, m) == pytest.approx(0.8)  # 40 - 38.8 - L/2; neither g nor h again
    for bad in (dict(m, toc_datum="other"), {k: v for k, v in m.items() if k != "toc_elevation_m"}):
        with pytest.raises(ValueError):
            convert(38.8, bad)


@pytest.mark.parametrize("label,reason", [(0.7, "OFF_GRID"), (-0.2, "OFF_GRID"),
                                        (6.2, "BELOW_CASING"), (float("inf"), "finite")])
def test_invalid_converted_labels_are_rejected_not_snapped(label, reason):
    with pytest.raises(ValueError, match=reason):
        convert(label)


def test_negative_ground_referenced_label_is_valid_after_explicit_conversion():
    m = dict(BASE, depth_datum="ground", ground_shift=0.4, apply_stickup=True, auto_adjust=True, output_point="top")
    assert convert(-0.2, m) == pytest.approx(0.0)


def test_raw_label_and_declared_metadata_are_immutable_provenance():
    m = dict(BASE)
    r = import_reading("digipro", "0.6", 1.0, "I", "S", "t", "A", "10", "-8", "digits",
                       CASING, L, depth_metadata=m)
    assert r.raw_depth_label == "0.6" and r.depth_top_m == pytest.approx(0.0)
    assert r.raw_r0 == "10" and r.raw_r180 == "-8"
    m["ground_shift"] = 99
    assert json.loads(r.depth_metadata_json) == BASE


def test_synthetic_conversion_matches_specialist_function_by_path():
    path = Path("/workspace/dam-refs/inclinometers/verify.py")
    if not path.exists():
        pytest.skip("specialist reference absent")
    fn = next(n for n in ast.parse(path.read_text()).body
              if isinstance(n, ast.FunctionDef) and n.name == "to_toc_interval_top")
    namespace = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), namespace)
    for top in interval_tops_m(CASING, L):
        label = top + BASE["index_above_toc"] + L / 2
        expected = namespace[fn.name](label, ground_shift=0, index_above_toc=0.2, mark_shift=L / 2)
        assert convert(label) == pytest.approx(expected)


@pytest.mark.parametrize("change", [dict(depth_datum="toc"),
    dict(ground_shift=0.4, apply_stickup=True)])
def test_named_depth_datum_must_agree_with_declared_offsets(change):
    with pytest.raises(ValueError, match="datum contradicts"):
        convert(0.6, dict(BASE, **change))


def test_vendor_profile_requires_an_explicit_physical_casing_grid():
    with pytest.raises(ValueError, match="casing_depth_m"):
        interval_top_from_label("gtilt", 0.6, 1.0, L_m=L, depth_metadata=BASE)


@pytest.mark.parametrize("export_type", ["processed", "plot"])
def test_display_only_exports_cannot_become_raw_readings(export_type):
    m = dict(BASE, export_type=export_type)
    assert convert(0.6, m) == pytest.approx(0.0)  # coordinate-only cross-check is permitted
    with pytest.raises(ValueError, match="never re-reduced"):
        import_reading("gtilt", 0.6, 1.0, "I", "S", "t", "A", 10, -8, "digits",
                       CASING, L, depth_metadata=m)
