"""QA-044 boundary tests: all numeric fixtures are synthetic, not vendor examples."""
import ast
from pathlib import Path

import pytest

from dam_safety.inclinometer import vendor_import as v

META = dict(depth_datum="ground", depth_label_ref="midpoint", ground_shift=1.2,
            index_above_toc=0.4, auto_adjust=False)
PROFILES = [(p, "sample" + sorted(suffixes)[0]) for p, suffixes in v.PROFILES.items()]


def request(profile="gtilt", filename="sample.txt", metadata=None, **kwargs):
    return v.prepare_import(profile, filename, b"synthetic text\n", META if metadata is None else metadata,
                            0.8, **dict(processed_only=False, enabled=True, **kwargs))


@pytest.mark.parametrize("profile,filename", PROFILES)
@pytest.mark.parametrize("field", sorted(v.REQUIRED_METADATA))
@pytest.mark.parametrize("invalid", ["absent", "null"])
def test_every_profile_requires_every_depth_field(profile, filename, field, invalid):
    meta = dict(META)
    if invalid == "absent":
        del meta[field]
    else:
        meta[field] = None
    with pytest.raises(v.ImportRejected, match=field):
        request(profile, filename, meta)


def test_never_silently_assumes_top_of_casing():
    with pytest.raises(v.ImportRejected, match="Missing required depth metadata"):
        request(metadata={})
    m = v.DepthMetadata.from_mapping(META)
    assert m.interval_top_m(6.4, 0.8) == pytest.approx(6.8)
    assert m.interval_top_m(6.4, 0.8) != 6.4


@pytest.mark.parametrize("label_ref,adjust,shift", [("midpoint", False, 0.4),
                                                   ("midpoint", True, 0), ("top", False, 0)])
def test_depth_formula_and_no_double_auto_adjust(label_ref, adjust, shift):
    m = v.DepthMetadata.from_mapping(dict(META, depth_label_ref=label_ref, auto_adjust=adjust))
    assert m.interval_top_m(6.4, 0.8) == pytest.approx(6.4 + 1.2 - 0.4 - shift)


@pytest.mark.parametrize("change", [dict(depth_datum="UNVERIFIED"), dict(depth_label_ref="unknown"),
    dict(auto_adjust="false"), dict(auto_adjust=0), dict(ground_shift=True),
    dict(ground_shift=float("nan")), dict(index_above_toc=-1), dict(index_above_toc=float("inf")),
    dict(depth_label_ref="top", auto_adjust=True), dict(guessed_field=0)])
def test_ambiguous_or_invalid_metadata_is_rejected(change):
    with pytest.raises(v.ImportRejected):
        request(metadata=dict(META, **change))


@pytest.mark.parametrize("L", [0, -1, True, float("nan")])
def test_invalid_probe_length_is_rejected(L):
    with pytest.raises(v.ImportRejected):
        v.prepare_import("rst", "file.csv", b"header\n", META, L, processed_only=False, enabled=True)


@pytest.mark.parametrize("filename", ["file.GTL", "file.dux", "PC-SLIN", "Inclinalysis.bin"])
def test_native_formats_offer_export_to_text(filename):
    with pytest.raises(v.ImportRejected, match="export to text"):
        request(filename=filename)


def test_renamed_binary_and_unknown_profile_are_rejected():
    with pytest.raises(v.ImportRejected, match="binary"):
        v.prepare_import("gtilt", "renamed.txt", b"a\x00b", META, 0.8,
                         processed_only=False, enabled=True)
    with pytest.raises(v.ImportRejected, match="Unsupported"):
        request(profile="unknown")


@pytest.mark.parametrize("profile,filename", PROFILES)
def test_flag_defaults_off_for_all_profiles(monkeypatch, profile, filename):
    monkeypatch.delenv(v.FEATURE_FLAG, raising=False)
    with pytest.raises(v.ImportRejected, match="UNVERIFIED.*disabled"):
        v.prepare_import(profile, filename, b"text\n", META, 0.8, processed_only=False)
    monkeypatch.setenv(v.FEATURE_FLAG, "true")
    r = v.prepare_import(profile, filename, b"text\n", META, 0.8, processed_only=False)
    assert r.status == "UNVERIFIED" and r.mode == "raw_unverified"
    with pytest.raises(v.ImportRejected, match="parity"):
        r.require_reduction()


def test_preserves_raw_input_and_processed_is_never_reduced():
    raw = b"synthetic unmodified text\n"
    r = v.prepare_import("rst", "file.csv", raw, META, 0.8, processed_only=True, enabled=True)
    assert r.raw_content is raw and r.metadata == v.DepthMetadata(**META)
    assert r.mode == "display_cross_check_only"
    with pytest.raises(v.ImportRejected, match="never re-reduced"):
        r.require_reduction()
    with pytest.raises(v.ImportRejected, match="processed_only"):
        v.prepare_import("rst", "file.csv", raw, META, 0.8, processed_only=None, enabled=True)


@pytest.mark.parametrize("profile,filename", PROFILES)
def test_parser_never_infers_layout_even_after_opt_in(profile, filename):
    with pytest.raises(v.ImportRejected, match="UNVERIFIED"):
        v.parse_export(profile, filename, b"text\n", META, 0.8, processed_only=False, enabled=True)


def test_matches_specialist_reference_by_path():
    # Load only the conversion function: importing verify.py would execute unrelated restricted examples.
    path = Path("/workspace/dam-refs/inclinometers/verify.py")
    if not path.exists():
        pytest.skip("specialist reference not mounted")
    tree = ast.parse(path.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "to_toc_interval_top")
    namespace = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), namespace)
    reference = namespace[fn.name]
    # Synthetic labels span every conversion branch; no restricted T17 inputs/results copied.
    for ref, adjusted in (("midpoint", False), ("midpoint", True), ("top", False)):
        m = v.DepthMetadata.from_mapping(dict(META, depth_label_ref=ref, auto_adjust=adjusted))
        for label in (2.4, 7.2, 9.6):
            shift = 0.4 if ref == "midpoint" and not adjusted else 0
            assert m.interval_top_m(label, 0.8) == pytest.approx(reference(
                label, ground_shift=m.ground_shift, index_above_toc=m.index_above_toc, mark_shift=shift))
