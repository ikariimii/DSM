"""Synthetic QA-044 structure/semantics only; no VDV data, headers or scalars.

Evidence: procedures/inclinometer_export_evidence.md §§2, 5. These invented layouts
are not native vendor grammars and cannot establish parity or complete depth settings.
"""
from dataclasses import FrozenInstanceError
import math

import pytest

from dam_safety.inclinometer import vendor_import as v

META = dict(depth_datum="ground", depth_label_ref="midpoint", ground_shift=1.2,
            index_above_toc=0.4, auto_adjust=False)
HEADER = "Zlabel,Adisplay,Bdisplay"
ROW = "1.6,2.4,3.2"
RAW_MAP = dict(depth="Zlabel", a0="Pplus", a180="Pminus", b0="Qplus", b180="Qminus")
RAW_HEADER = "Zlabel,Pplus,Pminus,Qplus,Qminus"
RAW_ROW = "1.6,11,-9,21,-19"
# Explicit test cases, not production defaults or sample-derived layouts.
VARIANTS = [
    ("gtilt", "synthetic.txt", "vdv_gtilt_plot_coordinates", "whitespace", "Plot_Coordinates"),
    ("digipro", "synthetic.txt", "digipro_tilt_change_text", "\t", "Tilt_Change"),
    ("digipro", "synthetic.csv", "digipro_profile_change_csv", ",", "Profile_Change"),
    ("digipro2", "synthetic.txt", "digipro2_text", "\t", "Tilt_Change"),
    ("digipro2", "synthetic.csv", "digipro2_spreadsheet", ";", "Profile_Change"),
]


def table(kind="processed", quantity="Profile_Change", axes=("A", "B"), section=None, **kw):
    if kind == "raw":
        columns = {k: h for k, h in RAW_MAP.items() if k == "depth" or k[0].upper() in axes}
        quantity = None
    else:
        prefix = "checksum_" if kind == "checksums" else "value_"
        columns = {"depth": "Zlabel", **{prefix + a.lower(): a + "display" for a in axes}}
    units = {k: "m" if k == "depth" else "declared-unit" for k in columns}
    return v.TableLayout(kind, columns, units, axes, "synthetic-survey",
                         section=section, quantity=quantity, **kw)


def layout(*tables, variant="digipro2_spreadsheet", delimiter=",", decimal=".", encoding="utf-8", **kw):
    return v.ExportLayout(variant=variant, tables=tuple(tables) or (table(),),
                          delimiter=delimiter, decimal=decimal, encoding=encoding, **kw)


def content(header=HEADER, row=ROW, delimiter=","):
    separator = "   " if delimiter == "whitespace" else delimiter
    return (header.replace(",", separator) + "\r\n" + row.replace(",", separator) + "\r\n").encode()


def parse(data=None, profile="digipro2", filename="synthetic.csv", spec=None, **kw):
    options = dict(processed_only=True, enabled=True, layout=layout() if spec is None else spec)
    options.update(kw)
    return v.parse_export(profile, filename, content() if data is None else data, META, 0.8, **options)


def parse_raw(data=None, spec=None, **kw):
    spec = spec or layout(table("raw"), variant="gtilt_file_tabulate", delimiter="\t")
    return parse(content(RAW_HEADER, RAW_ROW, "\t") if data is None else data,
                 profile="gtilt", filename="synthetic.out", spec=spec, processed_only=False, **kw)


@pytest.mark.parametrize("profile,filename,variant,separator,quantity", VARIANTS)
def test_processed_variants_are_display_only_with_exact_quantity(profile, filename, variant, separator, quantity):
    data = content(delimiter=separator)
    result = parse(data, profile, filename, layout(table(quantity=quantity), variant=variant, delimiter=separator))
    assert result.status == "UNVERIFIED" and result.variant == variant
    assert result.mode == "display_cross_check_only" and result.raw_rows == ()
    assert result.display_tables[0].layout.quantity == quantity
    row = result.display_tables[0].rows[0]
    assert row.values["depth"] == 1.6 and row.values["value_a"] == 2.4
    assert row.qa_flag == "UNVERIFIED"
    assert row.original_cells == ("1.6", "2.4", "3.2")
    assert "UNVERIFIED_DEPTH_METADATA" in row.qa_reasons and "UNVERIFIED_MISSING_RULES" in row.qa_reasons
    assert result.request.raw_content is data and result.request.metadata == v.DepthMetadata(**META)
    with pytest.raises(v.ImportRejected, match="never re-reduced"):
        result.require_reduction()


@pytest.mark.parametrize("profile,filename,variant,separator,quantity", VARIANTS)
@pytest.mark.parametrize("wrong_mode", ["raw_table", "raw_mode"])
def test_processed_variants_cannot_be_declared_raw(profile, filename, variant, separator, quantity, wrong_mode):
    t = table("raw") if wrong_mode == "raw_table" else table(quantity=quantity)
    with pytest.raises(v.ImportRejected, match="processed-only"):
        parse(content(delimiter=separator), profile, filename,
              layout(t, variant=variant, delimiter=separator), processed_only=wrong_mode == "raw_table")


@pytest.mark.parametrize("quantity", ["Profile", "Tilt", "Tilt_Change"])
def test_profile_change_csv_is_not_an_absolute_profile_or_tilt(quantity):
    with pytest.raises(v.ImportRejected, match="Profile_Change"):
        parse(spec=layout(table(quantity=quantity), variant="digipro_profile_change_csv"), profile="digipro")


def test_filename_never_turns_a_change_profile_into_absolute_profile():
    result = parse(filename="synthetic-absolute-Profile.csv", profile="digipro",
                   spec=layout(variant="digipro_profile_change_csv"))
    assert result.display_tables[0].layout.quantity == "Profile_Change"


@pytest.mark.parametrize("separator,decimal,row", [
    (",", ".", "1.6,2.4,3.2"), (";", ".", "1.6;2.4;3.2"),
    ("\t", ".", "1.6\t2.4\t3.2"), (";", ",", "1,6;2,4;3,2"),
])
def test_digipro2_spreadsheet_separator_and_decimal_are_caller_declared(separator, decimal, row):
    data = (HEADER.replace(",", separator) + "\n" + row + "\n").encode()
    result = parse(data, spec=layout(delimiter=separator, decimal=decimal))
    assert result.display_tables[0].rows[0].values["value_b"] == 3.2
    if decimal == ",":
        with pytest.raises(v.ImportRejected, match="Non-numeric"):
            parse(data, spec=layout(delimiter=separator, decimal="."))


@pytest.mark.parametrize("quantity", sorted(v.PROCESSED_QUANTITIES))
def test_digipro2_keeps_processed_quantities_distinct(quantity):
    kind = "checksums" if quantity in {"Checksum", "Difference_Checksum"} else "processed"
    result = parse(spec=layout(table(kind, quantity)))
    assert result.raw_rows == () and result.display_tables[0].layout.quantity == quantity


@pytest.mark.parametrize("profile,filename,variant,separator,quantity", VARIANTS)
def test_variant_quantity_is_not_guessed(profile, filename, variant, separator, quantity):
    with pytest.raises(v.ImportRejected, match="quantity"):
        parse(content(delimiter=separator), profile, filename,
              layout(table(quantity="synthetic-unknown-quantity"), variant=variant, delimiter=separator))


@pytest.mark.parametrize("variant,profile,filename,delimiter", [
    ("digipro_tilt_change_text", "digipro", "synthetic.txt", ","),
    ("digipro_profile_change_csv", "digipro", "synthetic.csv", ";"),
    ("digipro2_text", "digipro2", "synthetic.txt", ";"),
    ("vdv_gtilt_plot_coordinates", "gtilt", "synthetic.txt", "\t"),
    ("digipro2_spreadsheet", "digipro2", "synthetic.csv", "whitespace"),
])
def test_variant_separator_cannot_be_silently_coerced(variant, profile, filename, delimiter):
    with pytest.raises(v.ImportRejected, match="separator"):
        parse(profile=profile, filename=filename, spec=layout(variant=variant, delimiter=delimiter))


def test_variant_profile_extension_and_checksum_role_must_agree():
    with pytest.raises(v.ImportRejected, match="mismatch"):
        parse(profile="digipro", spec=layout(variant="digipro2_spreadsheet"))
    with pytest.raises(v.ImportRejected, match="mismatch"):
        parse(filename="synthetic.txt")
    with pytest.raises(v.ImportRejected, match="diagnostic"):
        parse(spec=layout(table(quantity="Checksum")))
    with pytest.raises(v.ImportRejected, match="variant"):
        layout(variant=None)


@pytest.mark.parametrize("extension", [".txt", ".out"])
@pytest.mark.parametrize("delimiter", ["whitespace", "\t"])
def test_gtilt_file_tabulate_raw_is_separate_from_vdv_plots(extension, delimiter):
    result = parse(content(RAW_HEADER, RAW_ROW, delimiter), profile="gtilt", filename="synthetic" + extension,
                   spec=layout(table("raw"), variant="gtilt_file_tabulate", delimiter=delimiter), processed_only=False)
    assert result.raw_rows[0].values["a180"] == -9 and not result.display_tables
    with pytest.raises(v.ImportRejected, match="parity"):
        result.require_reduction()


def test_gtilt_fixed_width_requires_explicit_spans_and_no_truncation():
    spans = tuple((i * 12, (i + 1) * 12) for i in range(5))
    render = lambda cells: "".join(c.ljust(12) for c in cells)
    data = (render(RAW_HEADER.split(",")) + "\n" + render(RAW_ROW.split(",")) + "\n").encode()
    spec = layout(table("raw", spans=spans), variant="gtilt_file_tabulate", delimiter="fixed")
    assert parse_raw(data, spec).raw_rows[0].values["b0"] == 21
    with pytest.raises(v.ImportRejected, match="outside declared"):
        parse_raw(data + b"synthetic text outside declared spans" * 3, spec)


@pytest.mark.parametrize("passes", ["1.6,,-9,21,-19", "1.6,,,,"])
def test_raw_gaps_are_never_filled_from_processed_tables(passes):
    data = ("SYNTH RAW\n" + RAW_HEADER.replace(",", "\t") + "\n" + passes.replace(",", "\t") +
            "\nSYNTH DISPLAY\n" + HEADER.replace(",", "\t") + "\n" + ROW.replace(",", "\t") + "\n").encode()
    spec = layout(table("raw", section="SYNTH RAW"), table(section="SYNTH DISPLAY"),
                  variant="gtilt_file_tabulate", delimiter="\t")
    result = parse_raw(data, spec)
    assert result.raw_rows[0].values["a0"] is None and result.raw_rows[0].qa_flag == "MISSING"
    assert "value_a" not in result.raw_rows[0].values
    assert result.display_tables[0].rows[0].values["value_a"] == 2.4


def test_processed_values_do_not_create_passes_even_with_raw_named_extra_columns():
    result = parse((HEADER + ",Pplus,Pminus\n1.6,,3.2,11,-9\n").encode())
    assert not result.raw_rows
    row = result.display_tables[0].rows[0]
    assert row.values["value_a"] is None and row.original_cells[-2:] == ("11", "-9")
    assert "a0" not in row.values and row.qa_flag == "MISSING"


@pytest.mark.parametrize("field", ["Zlabel", "Adisplay", "Bdisplay"])
def test_required_headers_are_explicit(field):
    with pytest.raises(v.ImportRejected, match="Missing required source columns"):
        parse((HEADER.replace(field, "SyntheticOtherHeader") + "\n" + ROW + "\n").encode())


@pytest.mark.parametrize("data,reason", [
    (b"Zlabel,Adisplay,Adisplay\n1.6,2.4,3.2\n", "duplicate"),
    (b"Zlabel,Adisplay,\n1.6,2.4,3.2\n", "Blank"),
    ((HEADER + "\n1.6,2.4\n").encode(), "column count"),
    ((HEADER + "\n,2.4,3.2\n").encode(), "Missing depth"),
    ((HEADER + "\n1.6,NAN,3.2\n").encode(), "Non-numeric"),
    ((HEADER + "\n1.6,*,3.2\n").encode(), "Non-numeric"),
    ((HEADER + "\n1.6,1e999,3.2\n").encode(), "Nonfinite"),
    ((HEADER + '\n1.6,"unterminated\n').encode(), "Malformed"),
    (HEADER.encode(), "at least one"),
])
def test_malformed_structure_and_unknown_nonnumeric_sentinels_fail(data, reason):
    with pytest.raises(v.ImportRejected, match=reason):
        parse(data)


def test_zeros_are_numeric_not_guessed_missing_or_boundary_anchors():
    row = parse((HEADER + "\n1.6,-0.0,0.0\n").encode()).display_tables[0].rows[0]
    assert row.values["value_a"] == 0 and row.values["value_b"] == 0
    assert math.copysign(1, row.values["value_a"]) == -1 and row.qa_flag == "UNVERIFIED"


@pytest.mark.parametrize("filename", ["file.GTL", "file.dux", "file.bin", "PC-SLIN", "file.xlsx", "Inclinalysis.dat"])
def test_native_and_other_binary_filenames_require_text_export(filename):
    with pytest.raises(v.ImportRejected, match="export to text/CSV"):
        parse(filename=filename)


@pytest.mark.parametrize("data", [b"a\x00b", b"%PDF-binary", b"PK\x03\x04payload", b"GIF89a",
                                  b"\x89PNG\r\n", b"\x01\x02payload", b"\xff\xffnot-utf8"])
def test_renamed_binary_and_invalid_encoding_require_text_export(data):
    with pytest.raises(v.ImportRejected, match="export to text/CSV"):
        parse(data)


@pytest.mark.parametrize("profile,filename,variant,separator,quantity", VARIANTS)
@pytest.mark.parametrize("field", sorted(v.REQUIRED_METADATA))
def test_five_depth_fields_are_never_inferred(profile, filename, variant, separator, quantity, field):
    metadata = dict(META)
    del metadata[field]
    with pytest.raises(v.ImportRejected, match=field):
        v.parse_export(profile, filename, b"synthetic", metadata, 0.8, processed_only=True, enabled=True,
                       layout=layout(table(quantity=quantity), variant=variant, delimiter=separator))


def test_off_by_default_no_layout_sniffing_or_depth_conversion(monkeypatch):
    monkeypatch.delenv(v.FEATURE_FLAG, raising=False)
    with pytest.raises(v.ImportRejected, match="UNVERIFIED.*disabled"):
        parse(enabled=None)
    with pytest.raises(v.ImportRejected, match="layout required"):
        v.parse_export("digipro2", "synthetic.csv", b"text", META, 0.8, processed_only=True, enabled=True)
    row = parse().display_tables[0].rows[0]
    assert row.values["depth"] == 1.6 and not hasattr(row, "depth_top_m")


def test_original_bytes_cells_grammar_and_metadata_stay_immutable():
    columns = {"depth": "Zlabel", "value_a": "Adisplay", "value_b": "Bdisplay"}
    t = v.TableLayout("processed", columns, {k: "m" for k in columns}, ("A", "B"), "synthetic",
                      quantity="Profile_Change")
    columns["value_a"] = "ChangedOutside"
    result = parse(spec=layout(t))
    with pytest.raises(TypeError):
        result.display_tables[0].rows[0].values["value_a"] = 99
    with pytest.raises(FrozenInstanceError):
        result.status = "VERIFIED"
    assert result.display_tables[0].layout.columns["value_a"] == "Adisplay"


def test_explicit_sections_preamble_bom_and_quoted_headers():
    spec = layout(table(section="SYNTH DISPLAY"), preamble_lines=1, encoding="utf-8-sig")
    data = ('SYNTH TITLE\nSYNTH DISPLAY\n"Zlabel",Adisplay,Bdisplay\n' + ROW + "\n").encode("utf-8-sig")
    assert parse(data, spec=spec).request.raw_content is data
    with pytest.raises(v.ImportRejected, match="declared section"):
        parse(data, spec=layout(table(section="SYNTH DISPLAY"), encoding="utf-8-sig"))
    with pytest.raises(v.ImportRejected, match="section markers"):
        layout(table(), table())


def test_explicit_single_axis_and_deferred_profiles():
    result = parse(b"Zlabel,Adisplay\n1.6,2.4\n", spec=layout(table(axes=("A",))))
    assert set(result.display_tables[0].rows[0].values) == {"depth", "value_a"}
    with pytest.raises(v.ImportRejected, match="deferred"):
        parse(profile="rst")


def test_bad_units_spans_missing_metadata_and_ambiguous_decimal_are_rejected():
    with pytest.raises(v.ImportRejected, match="units required"):
        v.TableLayout("raw", RAW_MAP, {}, ("A", "B"), "synthetic")
    with pytest.raises(v.ImportRejected, match="metre depth"):
        v.TableLayout("raw", RAW_MAP, {k: "ft" for k in RAW_MAP}, ("A", "B"), "synthetic")
    with pytest.raises(v.ImportRejected, match="nonoverlapping"):
        table(spans=((0, 12), (8, 20)))
    with pytest.raises(v.ImportRejected, match="column spans"):
        layout(variant="gtilt_file_tabulate", delimiter="fixed")
    with pytest.raises(v.ImportRejected, match="cannot coincide"):
        layout(decimal=",")
    with pytest.raises(v.ImportRejected, match="Missing required depth metadata"):
        v.parse_export("digipro2", "synthetic.csv", b"text", None, 0.8,
                       processed_only=True, enabled=True, layout=layout())


@pytest.mark.parametrize("quantity", ["Tilt", "Profile", "Profile_Change"])
def test_digipro_text_tilt_change_does_not_become_absolute_tilt(quantity):
    with pytest.raises(v.ImportRejected, match="Tilt_Change"):
        parse(content(delimiter="\t"), "digipro", "synthetic.txt",
              layout(table(quantity=quantity), variant="digipro_tilt_change_text", delimiter="\t"))


def test_file_tabulate_processed_option_cannot_enter_raw_mode():
    with pytest.raises(v.ImportRejected, match="Processed-only"):
        parse(content(delimiter="\t"), "gtilt", "synthetic.out",
              layout(variant="gtilt_file_tabulate", delimiter="\t"), processed_only=False)


def test_raw_canonical_pairs_cannot_be_replaced_by_processed_columns():
    with pytest.raises(v.ImportRejected, match="canonical columns"):
        v.TableLayout("raw", {"depth": "Zlabel", "value_a": "Adisplay"},
                      {"depth": "m", "value_a": "mm"}, ("A",), "synthetic")
    with pytest.raises(v.ImportRejected, match="one-to-one"):
        v.TableLayout("raw", dict(RAW_MAP, a0="Pminus"),
                      {k: "m" for k in RAW_MAP}, ("A", "B"), "synthetic")


def test_wrong_decimal_convention_and_undeclared_bom_are_not_normalized():
    with pytest.raises(v.ImportRejected, match="Wrong decimal"):
        parse(content(delimiter=";"), spec=layout(delimiter=";", decimal=","))
    with pytest.raises(v.ImportRejected, match="Missing required"):
        parse((HEADER + "\n" + ROW + "\n").encode("utf-8-sig"))


def test_vdv_text_is_not_selected_as_file_tabulate_out_by_extension():
    with pytest.raises(v.ImportRejected, match="mismatch"):
        parse(content(delimiter="whitespace"), "gtilt", "synthetic.out",
              layout(table(quantity="Plot_Coordinates"), variant="vdv_gtilt_plot_coordinates",
                     delimiter="whitespace"))
