"""QA-044 UNVERIFIED text-export interface; no vendor parser is enabled for production.

Depth rule: procedures/inclinometers.md §3.1; reference verify.py T17a–g.
Only metadata declared by the caller is used; vendor defaults are never inferred.
"""
from __future__ import annotations

import math
import csv
import io
import re
import os
from dataclasses import dataclass
from pathlib import PurePath
from types import MappingProxyType
from typing import Mapping

STATUS = "UNVERIFIED"
FEATURE_FLAG = "INCL_VENDOR_IMPORTERS_UNVERIFIED"
PROFILES = MappingProxyType({
    "gtilt": frozenset({".txt", ".out"}),
    "digipro": frozenset({".csv"}),
    "digipro2": frozenset({".csv"}),
    "rst": frozenset({".csv"}),
    "geosense": frozenset({".csv"}),
})
REQUIRED_METADATA = frozenset({"depth_datum", "depth_label_ref", "ground_shift",
                               "index_above_toc", "auto_adjust"})


class ImportRejected(ValueError):
    pass


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ImportRejected(f"{name} must be a finite number in metres")
    return float(value)


@dataclass(frozen=True)
class DepthMetadata:
    depth_datum: str
    depth_label_ref: str
    ground_shift: float
    index_above_toc: float
    auto_adjust: bool

    def __post_init__(self):
        if self.depth_datum not in {"toc", "index", "ground"}:
            raise ImportRejected("depth_datum must explicitly be toc, index or ground")
        if self.depth_label_ref not in {"midpoint", "top"}:
            raise ImportRejected("depth_label_ref must explicitly be midpoint or top")
        _finite(self.ground_shift, "ground_shift")
        if _finite(self.index_above_toc, "index_above_toc") < 0:
            raise ImportRejected("index_above_toc cannot be negative")
        if type(self.auto_adjust) is not bool:
            raise ImportRejected("auto_adjust must be an explicit boolean")
        if self.depth_label_ref == "top" and self.auto_adjust:
            raise ImportRejected("top labels must declare auto_adjust=false (avoid double adjustment)")

    @classmethod
    def from_mapping(cls, values: Mapping):
        missing = REQUIRED_METADATA - values.keys()
        if missing or any(values.get(k) is None for k in REQUIRED_METADATA):
            raise ImportRejected("Missing required depth metadata: " + ", ".join(
                sorted(k for k in REQUIRED_METADATA if k not in values or values[k] is None)))
        extra = values.keys() - REQUIRED_METADATA
        if extra:
            raise ImportRejected("Unknown depth metadata: " + ", ".join(sorted(extra)))
        return cls(**values)

    def interval_top_m(self, label_m, probe_L_m):
        label = _finite(label_m, "label")
        L = _finite(probe_L_m, "probe_L_m")
        if L <= 0:
            raise ImportRejected("probe_L_m must be positive")
        # auto_adjust explicitly declares that the exporter ALREADY shifted midpoint labels.
        mark_shift = L / 2 if self.depth_label_ref == "midpoint" and not self.auto_adjust else 0.0
        return label + self.ground_shift - self.index_above_toc - mark_shift


@dataclass(frozen=True)
class ImportRequest:
    profile: str
    filename: str
    raw_content: bytes                   # unchanged input; caller must not persist restricted fixtures
    metadata: DepthMetadata
    probe_L_m: float
    processed_only: bool
    status: str = STATUS

    @property
    def mode(self):
        return "display_cross_check_only" if self.processed_only else "raw_unverified"

    def require_reduction(self):
        if self.processed_only:
            raise ImportRejected("Processed-only exports are display/cross-check only; never re-reduced")
        raise ImportRejected("UNVERIFIED: reduction blocked until real vendor-file parity is approved")


def prepare_import(profile, filename, raw_content, depth_metadata, probe_L_m, *,
                   processed_only, enabled=None):
    """Validate the staged parser boundary; this function does not parse content.

    Units must already be explicitly declared/converted to metres by the future adapter.
    A filename suffix is a routing guard, not a guarantee of file content or vendor parity.
    """
    suffix = PurePath(filename).suffix.lower()
    lowered = filename.lower()
    if suffix in {".gtl", ".dux"} or any(x in lowered for x in ("pc-slin", "inclinalysis")):
        raise ImportRejected("Native binary format unsupported; export to text/CSV first")
    if profile not in PROFILES or suffix not in PROFILES[profile]:
        raise ImportRejected("Unsupported text/CSV import profile or extension; export to text/CSV")
    if not isinstance(raw_content, bytes) or b"\x00" in raw_content:
        raise ImportRejected("Text/CSV bytes required; binary files must export to text/CSV first")
    # Reject common binary signatures even if the file has been renamed .txt/.csv.
    if raw_content.startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"GIF8", b"\xff\xd8", b"\xd0\xcf\x11\xe0")):
        raise ImportRejected("Binary file unsupported; export to text/CSV first")
    if any(b < 32 and b not in (9, 10, 13) for b in raw_content):
        raise ImportRejected("Binary/control bytes unsupported; export to text/CSV first")
    if not isinstance(depth_metadata, Mapping):
        raise ImportRejected("Missing required depth metadata; no vendor defaults")
    metadata = DepthMetadata.from_mapping(depth_metadata)
    L = _finite(probe_L_m, "probe_L_m")
    if L <= 0:
        raise ImportRejected("probe_L_m must be positive")
    if type(processed_only) is not bool:
        raise ImportRejected("Declare processed_only explicitly; never infer raw versus processed")
    if enabled is None:
        enabled = os.environ.get(FEATURE_FLAG, "").lower() == "true"
    if enabled is not True:
        raise ImportRejected(f"UNVERIFIED vendor importers are disabled; {FEATURE_FLAG} defaults off")
    return ImportRequest(profile, filename, raw_content, metadata, L, processed_only)


# Sources: GTILT PLUS v3 File/Tabulate, PDF p26 [printed 4-2]; DigiPro2
# Manual 2025/5/29 Exporting Plots, PDF p33 [printed 31]. These format descriptions
# establish output kinds and separator policies, NOT a universal column grammar.
# Layouts below must be supplied explicitly; no vendor sample or default is embedded.
STAGED_PARSERS = frozenset({"gtilt", "digipro", "digipro2"})
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


@dataclass(frozen=True)
class TableLayout:
    """Caller-declared single-survey table grammar, pending specialist review.

    columns maps canonical field -> exact source header. All cells and original file
    bytes survive parsing, including unmapped columns. No field names are guessed.
    For fixed-width GTILT text, spans are explicit (start, exclusive end) character
    offsets, one per source column. Processed quantities/units are declarations only.
    """
    kind: str                          # raw | processed | plot | checksums
    columns: Mapping[str, str]
    units: Mapping[str, str]
    axes: tuple[str, ...]
    survey_id: str
    section: str | None = None          # exact standalone marker; required for multiple tables
    quantity: str | None = None         # explicit for nonraw tables, no Tilt/Profile inference
    spans: tuple[tuple[int, int], ...] | None = None

    def __post_init__(self):
        if self.kind not in {"raw", "processed", "plot", "checksums"}:
            raise ImportRejected("Explicit table kind must be raw, processed, plot or checksums")
        axes = tuple(self.axes)
        if not axes or len(axes) != len(set(axes)) or not set(axes) <= {"A", "B"}:
            raise ImportRejected("Explicit axes must contain A and/or B without duplicates")
        if not isinstance(self.survey_id, str) or not self.survey_id.strip():
            raise ImportRejected("Explicit survey_id required; survey dates are never guessed")
        if not isinstance(self.columns, Mapping) or not isinstance(self.units, Mapping):
            raise ImportRejected("Explicit column and unit mappings required")
        columns, units = dict(self.columns), dict(self.units)
        required = {"depth"}
        if self.kind == "raw":
            required |= {ax.lower() + p for ax in axes for p in ("0", "180")}
            if self.quantity is not None:
                raise ImportRejected("Raw table cannot masquerade as a processed quantity")
        else:
            prefix = "checksum_" if self.kind == "checksums" else "value_"
            required |= {prefix + ax.lower() for ax in axes}
            if not isinstance(self.quantity, str) or not self.quantity.strip():
                raise ImportRejected("Explicit processed/checksum quantity required")
        if set(columns) != required:
            raise ImportRejected("Required canonical columns are " + ", ".join(sorted(required)))
        if (any(not isinstance(h, str) or not h.strip() for h in columns.values())
                or len(set(columns.values())) != len(columns)):
            raise ImportRejected("Source headers must be nonblank and map one-to-one")
        if set(units) != required or any(not isinstance(u, str) or not u.strip() for u in units.values()):
            raise ImportRejected("Explicit units required for every mapped column")
        if units["depth"] != "m":
            raise ImportRejected("This stage requires explicitly declared metre depth labels; no unit conversion")
        if self.section is not None and (not isinstance(self.section, str) or not self.section.strip()
                                         or "\n" in self.section or "\r" in self.section):
            raise ImportRejected("Section marker must be one explicit nonblank line")
        if self.spans is not None:
            spans = tuple(tuple(span) for span in self.spans)
            previous = 0
            for start, end in spans:
                if type(start) is not int or type(end) is not int or start < previous or end <= start:
                    raise ImportRejected("Fixed-width spans must be nonoverlapping increasing integer ranges")
                previous = end
            if not spans:
                raise ImportRejected("Explicit fixed-width spans required")
            object.__setattr__(self, "spans", spans)
        object.__setattr__(self, "axes", axes)
        object.__setattr__(self, "columns", MappingProxyType(columns))
        object.__setattr__(self, "units", MappingProxyType(units))


@dataclass(frozen=True)
class ExportLayout:
    tables: tuple[TableLayout, ...]
    delimiter: str                     # comma/semicolon/tab, or GTILT whitespace/fixed
    decimal: str                       # '.' | ','; no locale sniffing or thousands separator
    encoding: str                      # explicit utf-8, utf-8-sig, cp1252 or latin-1
    preamble_lines: int = 0             # explicit count; preamble retained in raw_content

    def __post_init__(self):
        tables = tuple(self.tables)
        if not tables or any(not isinstance(t, TableLayout) for t in tables):
            raise ImportRejected("At least one explicit TableLayout required")
        if self.delimiter not in {",", ";", "\t", "whitespace", "fixed"} or self.decimal not in {".", ","}:
            raise ImportRejected("Explicit supported delimiter and decimal required")
        if self.delimiter == self.decimal:
            raise ImportRejected("Delimiter and decimal cannot coincide in this staged grammar")
        if self.encoding not in {"utf-8", "utf-8-sig", "cp1252", "latin-1"}:
            raise ImportRejected("Explicit supported text encoding required; export to text/CSV")
        if type(self.preamble_lines) is not int or self.preamble_lines < 0:
            raise ImportRejected("preamble_lines must be an explicit nonnegative count")
        markers = [t.section for t in tables]
        if len(tables) > 1 and (any(m is None for m in markers) or len(set(markers)) != len(markers)):
            raise ImportRejected("Multiple tables require distinct explicit section markers")
        if self.delimiter == "fixed" and any(t.spans is None for t in tables):
            raise ImportRejected("Fixed-width GTILT text requires explicit column spans")
        if self.delimiter != "fixed" and any(t.spans is not None for t in tables):
            raise ImportRejected("Column spans apply only to fixed-width text")
        object.__setattr__(self, "tables", tables)


@dataclass(frozen=True)
class ExportRow:
    source_record: int
    original_cells: tuple[str, ...]
    values: Mapping[str, float | None]  # source depth label, NOT a converted TOC interval top
    qa_flag: str                       # UNVERIFIED or MISSING, never OK
    qa_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ParsedTable:
    layout: TableLayout
    original_header: tuple[str, ...]
    rows: tuple[ExportRow, ...]


@dataclass(frozen=True)
class ParsedExport:
    request: ImportRequest
    tables: tuple[ParsedTable, ...]
    status: str = STATUS

    @property
    def raw_rows(self):
        # Never select a value from a processed table, even when raw passes are missing.
        return tuple(r for t in self.tables if t.layout.kind == "raw" for r in t.rows)

    @property
    def display_tables(self):
        return tuple(t for t in self.tables if t.layout.kind != "raw")

    @property
    def mode(self):
        return self.request.mode

    def require_reduction(self):
        self.request.require_reduction()  # UNVERIFIED even for structurally complete raw exports


def _table_records(lines, layout, table):
    if layout.delimiter == "whitespace":
        return [tuple(line.split()) for line in lines if line.strip()]
    if layout.delimiter == "fixed":
        records = []
        for line in lines:
            if not line.strip():
                continue
            # No silent truncation of data beyond/between declared source columns.
            covered = [False] * len(line)
            for start, end in table.spans:
                for i in range(start, min(end, len(line))):
                    covered[i] = True
            if any(not cover and not c.isspace() for c, cover in zip(line, covered)):
                raise ImportRejected("Text outside declared fixed-width columns")
            records.append(tuple(line[start:end].strip() for start, end in table.spans))
        return records
    try:
        return [tuple(r) for r in csv.reader(io.StringIO("\n".join(lines)),
                                            delimiter=layout.delimiter, strict=True) if r and any(c.strip() for c in r)]
    except csv.Error as e:
        raise ImportRejected("Malformed delimited export") from e


def _number_cell(cell, decimal, column, record):
    text = cell.strip()
    if not text:
        return None
    if decimal == ",":
        if "." in text:
            raise ImportRejected(f"Wrong decimal convention in {column}, record {record}")
        text = text.replace(",", ".")
    if not _NUMBER.fullmatch(text):
        raise ImportRejected(f"Non-numeric/ambiguous {column} in record {record}; no thousands or sentinel guessing")
    value = float(text)
    if not math.isfinite(value):
        raise ImportRejected(f"Nonfinite {column} in record {record}")
    return value


def _parse_table(lines, layout, table):
    records = _table_records(lines, layout, table)
    if len(records) < 2:
        raise ImportRejected("Header and at least one data record required")
    header = records[0]
    if any(not h.strip() for h in header) or len(set(header)) != len(header):
        raise ImportRejected("Blank or duplicate source column header")
    missing = set(table.columns.values()) - set(header)
    if missing:
        raise ImportRejected("Missing required source columns: " + ", ".join(sorted(missing)))
    indices = {key: header.index(source) for key, source in table.columns.items()}
    rows = []
    for i, cells in enumerate(records[1:], 2):
        if len(cells) != len(header):
            raise ImportRejected(f"Wrong column count in source record {i}")
        values = {key: _number_cell(cells[j], layout.decimal, key, i) for key, j in indices.items()}
        if values["depth"] is None:
            raise ImportRejected(f"Missing depth label in source record {i}")
        missing_values = tuple(key for key, value in values.items() if value is None)
        reasons = ("UNVERIFIED_VENDOR_FORMAT",) + tuple("MISSING_" + key.upper() for key in missing_values)
        rows.append(ExportRow(i, cells, MappingProxyType(values), "MISSING" if missing_values else STATUS, reasons))
    return ParsedTable(table, header, tuple(rows))


def parse_export(profile, filename, raw_content, depth_metadata, probe_L_m, *,
                 processed_only, enabled=None, layout: ExportLayout | None = None):
    """Stage GTILT File/Tabulate text or DigiPro CSV/TSV with caller-declared grammar.

    Parse structure only: keep raw labels/units/values, never convert depths or produce
    RawReading objects. QA-042/043 gates remain unchanged and reduction remains blocked.
    Regional CSV separators/decimals, monospaced boundaries, headers, table roles and
    survey identity must be supplied, not inferred. No vendor-parity claim is made.
    """
    request = prepare_import(profile, filename, raw_content, depth_metadata, probe_L_m,
                             processed_only=processed_only, enabled=enabled)
    if profile not in STAGED_PARSERS:
        raise ImportRejected(f"UNVERIFIED: {profile} parser deferred beyond this stage")
    if not isinstance(layout, ExportLayout):
        raise ImportRejected("Explicit UNVERIFIED export layout required; no vendor header/delimiter defaults")
    if profile in {"digipro", "digipro2"} and layout.delimiter not in {",", ";", "\t"}:
        raise ImportRejected("DigiPro CSV/tab export requires an explicit comma, semicolon or tab separator")
    if profile == "gtilt" and layout.delimiter not in {"whitespace", "fixed", "\t"}:
        raise ImportRejected("GTILT text requires explicit whitespace, fixed-width or tab layout")
    try:
        text = raw_content.decode(layout.encoding)
    except UnicodeDecodeError as e:
        raise ImportRejected("Invalid declared text encoding or binary file; export to text/CSV") from e
    if any((ord(c) < 32 and c not in "\t\r\n") or 127 <= ord(c) <= 159 for c in text):
        raise ImportRejected("Binary/control characters unsupported; export to text/CSV")
    lines = text.splitlines()[layout.preamble_lines:]
    parsed = []
    for i, table in enumerate(layout.tables):
        while lines and not lines[0].strip():
            lines = lines[1:]
        if table.section is not None:
            if not lines or lines[0] != table.section:
                raise ImportRejected("Missing/out-of-order declared section: " + table.section)
            lines = lines[1:]
        if i + 1 < len(layout.tables):
            next_marker = layout.tables[i + 1].section
            if next_marker not in lines:
                raise ImportRejected("Missing declared section: " + next_marker)
            end = lines.index(next_marker)
            block, lines = lines[:end], lines[end:]
        else:
            block, lines = lines, []
        parsed.append(_parse_table(block, layout, table))
    has_raw = any(t.layout.kind == "raw" for t in parsed)
    if not has_raw and not processed_only:
        raise ImportRejected("Processed-only exports are display/cross-check only; declare processed_only=true")
    return ParsedExport(request, tuple(parsed))
