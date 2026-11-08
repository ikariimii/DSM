"""QA-044 synthetic structural cases only. No vendor example or restricted file is read.

These tables are invented for parser contracts, not assertions about real vendor spelling,
layout, values, depth semantics or parity. Explicit descriptors are always supplied.
"""
from dataclasses import FrozenInstanceError

import pytest

from dam_safety.inclinometer import vendor_import as v

META = dict(depth_datum="ground", depth_label_ref="midpoint", ground_shift=1.2,
            index_above_toc=0.4, auto_adjust=False)
RAW_MAP = dict(depth="Zlabel", a0="Pplus", a180="Pminus", b0="Qplus", b180="Qminus")
RAW_HEADER = "Zlabel,Pplus,Pminus,Qplus,Qminus"
RAW_ROW = "1.6,11,-9,21,-19"


def table(kind="raw", section=None, axes=("A", "B"), **kw):
    if kind == "raw":
        columns = dict(RAW_MAP)
        units = {k: "m" if k == "depth" else "digits" for k in columns}
        quantity = None
    else:
        prefix = "checksum_" if kind == "checksums" else "value_"
        columns = {"depth": "Zlabel", **{prefix + a.lower(): a + "display" for a in axes}}
        units = {k: "m" if k == "depth" else "declared-unit" for k in columns}
        quantity = "synthetic-quantity"
    return v.TableLayout(kind=kind, columns=columns, units=units, axes=axes,
                         survey_id="synthetic-survey", section=section, quantity=quantity, **kw)


def layout(*tables, delimiter=",", decimal=".", encoding="utf-8", **kw):
    return v.ExportLayout(tables=tuple(tables) or (table(),), delimiter=delimiter,
                          decimal=decimal, encoding=encoding, **kw)


def parse(content=None, profile="digipro2", filename=None, spec=None, **kw):
    if content is None:
        content = (RAW_HEADER + "\n" + RAW_ROW + "\n").encode()
    if filename is None:
        filename = "synthetic.txt" if profile == "gtilt" else "synthetic.csv"
    options = dict(processed_only=False, enabled=True, layout=layout() if spec is None else spec)
    options.update(kw)
    return v.parse_export(profile, filename, content, META, 0.8, **options)


@pytest.mark.parametrize("profile", ["digipro", "digipro2"])
@pytest.mark.parametrize("separator", [",", ";", "\t"])
def test_declared_csv_separator_and_raw_headers(profile, separator):
    content = (RAW_HEADER.replace(",", separator) + "\r\n" + RAW_ROW.replace(",", separator) + "\r\n").encode()
    result = parse(content, profile=profile, spec=layout(delimiter=separator))
    assert result.status == "UNVERIFIED" and result.mode == "raw_unverified"
    assert len(result.raw_rows) == 1 and not result.display_tables
    row = result.raw_rows[0]
    assert row.values["depth"] == 1.6 and row.values["a0"] == 11
    assert row.original_cells == ("1.6", "11", "-9", "21", "-19")
    assert row.qa_flag == "UNVERIFIED" and result.request.raw_content is content
    assert result.request.metadata == v.DepthMetadata(**META)
    with pytest.raises(v.ImportRejected, match="parity"):
        result.require_reduction()


@pytest.mark.parametrize("extension", [".txt", ".out"])
@pytest.mark.parametrize("delimiter", ["whitespace", "\t"])
def test_gtilt_declared_text_layout(extension, delimiter):
    separator = "   " if delimiter == "whitespace" else "\t"
    content = (RAW_HEADER.replace(",", separator) + "\n" + RAW_ROW.replace(",", separator) + "\n").encode()
    result = parse(content, profile="gtilt", filename="synthetic" + extension, spec=layout(delimiter=delimiter))
    assert result.raw_rows[0].values["a180"] == -9


def test_gtilt_fixed_width_explicit_spans():
    spans = tuple((i * 12, (i + 1) * 12) for i in range(5))
    render = lambda cells: "".join(c.ljust(12) for c in cells)
    content = (render(RAW_HEADER.split(",")) + "\n" + render(RAW_ROW.split(",")) + "\n").encode()
    result = parse(content, profile="gtilt", spec=layout(table(spans=spans), delimiter="fixed"))
    assert result.raw_rows[0].values["b0"] == 21
    with pytest.raises(v.ImportRejected, match="outside declared"):
        parse(content + b"unmapped text beyond span" * 4,
              profile="gtilt", spec=layout(table(spans=spans), delimiter="fixed"))


def test_regional_decimal_is_explicit_not_guessed():
    content = (RAW_HEADER.replace(",", ";") + "\n1,6;11,0;-9,0;21,0;-19,0\n").encode()
    result = parse(content, spec=layout(delimiter=";", decimal=","))
    assert result.raw_rows[0].values["depth"] == 1.6
    assert result.raw_rows[0].original_cells[0] == "1,6"
    with pytest.raises(v.ImportRejected, match="Non-numeric"):
        parse(content, spec=layout(delimiter=";", decimal="."))
    with pytest.raises(v.ImportRejected, match="Wrong decimal"):
        parse((RAW_HEADER.replace(",", ";") + "\n" + RAW_ROW.replace(",", ";") + "\n").encode(),
              spec=layout(delimiter=";", decimal=","))


@pytest.mark.parametrize("kind", ["processed", "plot", "checksums"])
def test_processed_only_tables_stay_display_only(kind):
    spec = layout(table(kind=kind))
    content = b"Zlabel,Adisplay,Bdisplay\n1.6,2.4,3.2\n"
    result = parse(content, spec=spec, processed_only=True)
    assert not result.raw_rows and len(result.display_tables) == 1
    assert result.mode == "display_cross_check_only"
    with pytest.raises(v.ImportRejected, match="never re-reduced"):
        result.require_reduction()
    with pytest.raises(v.ImportRejected, match="Processed-only"):
        parse(content, spec=spec, processed_only=False)


def test_mixed_raw_and_processed_sections_never_fill_missing_passes():
    content = ("SYNTH RAW\n" + RAW_HEADER + "\n1.6,,-9,21,-19\n"
               "SYNTH DISPLAY\nZlabel,Adisplay,Bdisplay\n1.6,2.4,3.2\n").encode()
    result = parse(content, spec=layout(table(section="SYNTH RAW"), table("processed", section="SYNTH DISPLAY")))
    assert len(result.raw_rows) == 1 and result.raw_rows[0].values["a0"] is None
    assert result.raw_rows[0].qa_flag == "MISSING" and "MISSING_A0" in result.raw_rows[0].qa_reasons
    assert result.display_tables[0].rows[0].values["value_a"] == 2.4
    assert "value_a" not in result.raw_rows[0].values
    with pytest.raises(v.ImportRejected, match="parity"):
        result.require_reduction()


def test_unmapped_processed_column_is_preserved_not_used_as_raw():
    content = (RAW_HEADER + ",OpaqueProcessed\n1.6,,-9,21,-19,2.4\n").encode()
    row = parse(content).raw_rows[0]
    assert row.values["a0"] is None and row.original_cells[-1] == "2.4"
    assert "OpaqueProcessed" not in row.values


@pytest.mark.parametrize("field", sorted(RAW_MAP.values()))
def test_missing_required_source_header_is_rejected(field):
    header = RAW_HEADER.replace(field, "SyntheticOtherHeader")
    with pytest.raises(v.ImportRejected, match="Missing required source columns"):
        parse((header + "\n" + RAW_ROW + "\n").encode())


def test_required_raw_pairs_cannot_be_mapped_from_processed_names_implicitly():
    with pytest.raises(v.ImportRejected, match="canonical columns"):
        v.TableLayout("raw", {"depth": "Zlabel", "value_a": "Adisplay"},
                      {"depth": "m", "value_a": "mm"}, ("A",), "synthetic")
    with pytest.raises(v.ImportRejected, match="one-to-one"):
        v.TableLayout("raw", dict(RAW_MAP, a0="Pminus"),
                      {k: "m" if k == "depth" else "digits" for k in RAW_MAP}, ("A", "B"), "synthetic")


@pytest.mark.parametrize("content,reason", [
    (b"Zlabel,Pplus,Pplus,Qplus,Qminus\n1.6,11,-9,21,-19\n", "duplicate"),
    (b"Zlabel,Pplus,Pminus,Qplus,\n1.6,11,-9,21,-19\n", "Blank"),
    ((RAW_HEADER + "\n1.6,11,-9\n").encode(), "column count"),
    ((RAW_HEADER + "\n,11,-9,21,-19\n").encode(), "Missing depth"),
    ((RAW_HEADER + "\n1.6,NAN,-9,21,-19\n").encode(), "Non-numeric"),
    ((RAW_HEADER + "\n1.6,1e999,-9,21,-19\n").encode(), "Nonfinite"),
    ((RAW_HEADER + '\n1.6,"unterminated\n').encode(), "Malformed"),
    (RAW_HEADER.encode(), "at least one"),
])
def test_malformed_structures_are_rejected(content, reason):
    with pytest.raises(v.ImportRejected, match=reason):
        parse(content)


@pytest.mark.parametrize("filename", ["file.GTL", "file.dux", "file.bin", "PC-SLIN", "file.xlsx", "Inclinalysis.dat"])
def test_native_and_other_binary_filenames_require_text_export(filename):
    with pytest.raises(v.ImportRejected, match="export to text/CSV"):
        parse(filename=filename)


@pytest.mark.parametrize("content", [b"a\x00b", b"%PDF-binary", b"PK\x03\x04payload", b"GIF89a",
                                     b"\x89PNG\r\n", b"\x01\x02payload"])
def test_renamed_binary_content_requires_text_export(content):
    with pytest.raises(v.ImportRejected, match="export to text/CSV"):
        parse(content)


@pytest.mark.parametrize("profile", ["gtilt", "digipro", "digipro2"])
@pytest.mark.parametrize("field", sorted(v.REQUIRED_METADATA))
def test_parser_never_infers_missing_metadata(profile, field):
    metadata = dict(META)
    del metadata[field]
    with pytest.raises(v.ImportRejected, match=field):
        v.parse_export(profile, "synthetic.txt" if profile == "gtilt" else "synthetic.csv",
                       b"synthetic", metadata, 0.8, processed_only=True, enabled=True, layout=layout())


def test_off_by_default_and_no_layout_sniffing(monkeypatch):
    monkeypatch.delenv(v.FEATURE_FLAG, raising=False)
    with pytest.raises(v.ImportRejected, match="UNVERIFIED.*disabled"):
        parse(enabled=None)
    with pytest.raises(v.ImportRejected, match="layout required"):
        v.parse_export("digipro2", "synthetic.csv", b"text", META, 0.8,
                       processed_only=False, enabled=True)


def test_explicit_header_names_do_not_trigger_datum_conversion():
    # Parsing keeps vendor depth labels: QA-042 conversion belongs to a separate gated step.
    result = parse()
    assert result.raw_rows[0].values["depth"] == 1.6
    assert result.request.metadata.ground_shift == 1.2
    assert not hasattr(result.raw_rows[0], "depth_top_m")


def test_no_silent_metadata_or_grammar_mutation():
    columns = dict(RAW_MAP)
    t = v.TableLayout("raw", columns, {k: "m" if k == "depth" else "digits" for k in columns},
                      ("A", "B"), "synthetic")
    columns["a0"] = "ChangedOutside"
    result = parse(spec=layout(t))
    with pytest.raises(TypeError):
        result.raw_rows[0].values["a0"] = 99
    with pytest.raises(FrozenInstanceError):
        result.status = "VERIFIED"
    assert result.tables[0].layout.columns["a0"] == "Pplus"


def test_section_markers_preamble_and_order_are_explicit():
    content = ("SYNTH TITLE\nSYNTH RAW\n" + RAW_HEADER + "\n" + RAW_ROW + "\n").encode()
    result = parse(content, spec=layout(table(section="SYNTH RAW"), preamble_lines=1))
    assert result.request.raw_content == content
    with pytest.raises(v.ImportRejected, match="declared section"):
        parse(content, spec=layout(table(section="SYNTH RAW")))
    with pytest.raises(v.ImportRejected, match="section markers"):
        layout(table(), table("processed"))


def test_explicit_single_axis_pair_is_structural_not_a_biaxial_default():
    t = v.TableLayout("raw", {"depth": "Zlabel", "a0": "Pplus", "a180": "Pminus"},
                      {"depth": "m", "a0": "digits", "a180": "digits"}, ("A",), "synthetic")
    result = parse(b"Zlabel,Pplus,Pminus\n1.6,11,-9\n", spec=layout(t))
    assert set(result.raw_rows[0].values) == {"depth", "a0", "a180"}


def test_unsupported_format_layouts_stop_without_guessing():
    with pytest.raises(v.ImportRejected, match="separator"):
        parse(spec=layout(delimiter="whitespace"))
    with pytest.raises(v.ImportRejected, match="GTILT text"):
        parse(profile="gtilt")
    with pytest.raises(v.ImportRejected, match="deferred"):
        parse(profile="rst")
    with pytest.raises(v.ImportRejected, match="explicit column spans"):
        layout(delimiter="fixed")
    with pytest.raises(v.ImportRejected, match="cannot coincide"):
        layout(delimiter=",", decimal=",")



def test_quoted_csv_header_and_explicit_bom_encoding():
    columns = dict(RAW_MAP, depth="Synthetic depth label")
    t = v.TableLayout("raw", columns, {k: "m" if k == "depth" else "digits" for k in columns},
                      ("A", "B"), "synthetic")
    content = ('"Synthetic depth label",Pplus,Pminus,Qplus,Qminus\n' + RAW_ROW + "\n").encode("utf-8-sig")
    result = parse(content, spec=layout(t, encoding="utf-8-sig"))
    assert result.tables[0].original_header[0] == "Synthetic depth label"
    assert result.request.raw_content == content
    with pytest.raises(v.ImportRejected, match="Missing required"):
        parse(content, spec=layout(t, encoding="utf-8"))


def test_empty_raw_passes_are_not_replaced_by_display_values():
    content = ("SYNTH RAW\n" + RAW_HEADER + "\n1.6,,,,\n"
               "SYNTH DISPLAY\nZlabel,Adisplay,Bdisplay\n1.6,2.4,3.2\n").encode()
    result = parse(content, spec=layout(table(section="SYNTH RAW"), table("plot", section="SYNTH DISPLAY")))
    assert result.raw_rows[0].qa_flag == "MISSING"
    assert all(result.raw_rows[0].values[k] is None for k in ("a0", "a180", "b0", "b180"))
    assert len(result.display_tables) == 1


def test_invalid_encoding_and_missing_metadata_never_fall_back():
    with pytest.raises(v.ImportRejected, match="export to text/CSV"):
        parse(b"\xff\xffnot-utf8")
    with pytest.raises(v.ImportRejected, match="Missing required depth metadata"):
        v.parse_export("gtilt", "synthetic.out", b"text", None, 0.8,
                       processed_only=True, enabled=True, layout=layout(delimiter="whitespace"))


def test_fixed_columns_never_guess_units_or_optional_missing_pair():
    with pytest.raises(v.ImportRejected, match="units required"):
        v.TableLayout("raw", RAW_MAP, {}, ("A", "B"), "synthetic")
    with pytest.raises(v.ImportRejected, match="metre depth"):
        v.TableLayout("raw", RAW_MAP, {k: "ft" if k == "depth" else "digits" for k in RAW_MAP},
                      ("A", "B"), "synthetic")
    with pytest.raises(v.ImportRejected, match="nonoverlapping"):
        table(spans=((0, 12), (8, 20)))
