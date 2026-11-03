"""Clean-room reader for Campbell Scientific TOA5 ASCII table-data files.

This was written ONLY from Campbell Scientific's published file-format documentation. No third-party
TOA5 parser code was consulted (owner decision 2026-11-02: PyTOA5 dropped because its runtime
dependency igbpyutils is GPL-3.0-or-later; see docs/DEPENDENCY_PINS.md and docs/TOA5_READER.md).

Specification sources:
  [CS-LN-TOA5]  Campbell Scientific, LoggerNet manual, "TOA5",
                https://help.campbellsci.com/loggernet-manual/ln_manual/campbell_scientific_file_formats/toa5.htm
                (header contents: file type, station, logger type, serial, OS, program (DLD) name, program
                signature, table name; field names; units; processing. Timestamp and record number are
                optional; the timestamp has sub-second resolution. Comma-separated. One table per file.)
  [CS-LN-SUFFIX] Campbell Scientific, LoggerNet manual, "Field Name Suffixes",
                https://help.campbellsci.com/loggernet-manual/ln_manual/campbell_scientific_file_formats/field_name_suffixes.htm
  [CS-CR1000]   Campbell Scientific, "CR1000 Measurement and Control System" operator's manual (rev. 2018),
                https://s.campbellsci.com/documents/us/manuals/cr1000.pdf
                - §7 Table 12 "Typical Data Table" and Table 13 "TOA5 Environment Line", p.146-147:
                  the environment line has exactly 8 fields and field 1 is "TOA5". The file has >= 5 lines
                  and the first 4 are the header.
                - p.147-148: line 2 is field names (array elements written as name(i,j)), line 3 is units,
                  line 4 is data-process abbreviations, then data. The first field is the timestamp, the second the record number.
                - §8 "Data File Formats", TOA5 example and "Data File Format Elements", p.437-439: empty
                  string when no units or no recognised processing; one descriptor per field name; units
                  "TS"/"RN" mark the timestamp/record columns; the record number is a 32-bit unsigned counter
                  (max &hffffffff) that restarts at 0.
  [CS-NANINF]   Campbell Scientific, CR1000X help, "Understanding NAN and INF occurrences",
                https://help.campbellsci.com/CR1000X/Content/shared/Maintain/Troubleshooting/NAN_and_INF.htm
                (NAN = invalid measurement; INF = infinite/undefined result. Integer types have no NAN:
                FP2 NAN is stored as -7999 and Long NAN as -2147483648.)

Behaviour (strict: any deviation from the documented layout raises Toa5Error with a line number):
  * Header line 1 must have exactly 8 fields, and field 1 must be "TOA5". Lines 2-4 must have the same, non-zero
    number of fields. Field names must be non-empty and unique.
  * Every data line must have exactly as many fields as line 2. Trailing blank lines are ignored. A blank
    line followed by more data is an error.
  * Unterminated last line (QA-017): a final data line without a line terminator may have been cut mid-write
    (logger or collection still writing). A cut "2.5" reads as "2", and a cut after a comma reads as an empty field. The
    field count still matches, so this cannot be detected syntactically. Default on_unterminated="error"
    raises; on_unterminated="drop" discards that line and adds an `unterminated_last_line_dropped` warning.
    An unterminated header line is always an error. parse_lines() therefore expects lines WITH their
    terminators, as yielded by a file object or io.StringIO.
  * Sequence diagnostics (QA-018), returned in Toa5Table.warnings (data are kept as read; dedup is ingest's job):
    record_duplicate / record_out_of_order, timestamp_duplicate / timestamp_out_of_order. A RECORD restart
    at 0 straight after &h7fffffff or &hffffffff is the documented rollover [CS-CR1000 p.439] and is NOT
    warned. Any other decrease (including a reset to 0 after a program change) is warned as out of order.
  * Quoting: double-quoted fields may contain commas, and "" stands for a literal quote. A quote inside an unquoted
    field, an unterminated quote, or text after a closing quote is an error.
  * TIMESTAMP (if present; it must be column 1): "YYYY-MM-DD HH:MM:SS[.fraction]" -> naive datetime
    in logger time (TOA5 carries no time zone; the caller must attach the station's zone). Fractions
    beyond microseconds must be zeros (no silent loss of precision).
  * RECORD (if present; it must follow TIMESTAMP, or be column 1 if there is no TIMESTAMP): unsigned 32-bit
    integer written with ASCII digits only. No sign is allowed (QA-019).
  * All numeric syntax (values, RECORD, TIMESTAMP) is ASCII-only, [0-9]. Non-ASCII digits are rejected (QA-019).
  * If line 3 marks a column "TS" or "RN", that column must be TIMESTAMP or RECORD respectively.
  * Values: NAN, INF, +INF, -INF (any case, quoted or not) -> float nan/±inf. Unquoted integers -> int;
    other unquoted numbers -> float. An unquoted empty field -> None. A quoted field -> str (TOA5 quotes
    strings). Any other unquoted text is an error.
  * Integer NAN sentinels (-7999 FP2, -2147483648 Long) are NOT converted. TOA5 does not record the
    stored data type, so the ingest must map them per field type (constants below).
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Union

ENV_FIELDS = ("file_type", "station_name", "logger_model", "serial_number", "os_version",
              "program_name", "program_signature", "table_name")              # [CS-CR1000] Table 13
FP2_NAN_SENTINEL = -7999                                                      # [CS-NANINF]
LONG_NAN_SENTINEL = -2147483648                                               # [CS-NANINF]
RECORD_MAX = 0xFFFFFFFF                                                       # [CS-CR1000] p.439
RECORD_MAX_REPORTED = 0x7FFFFFFF   # CR1000 reports 31 bits to the support software  [CS-CR1000] p.439
UNTERMINATED_POLICIES = ("error", "drop")
_SPECIAL = {"NAN": math.nan, "INF": math.inf, "+INF": math.inf, "-INF": -math.inf}
_INT = re.compile(r"[+-]?[0-9]+\Z", re.ASCII)
_RECORD = re.compile(r"[0-9]+\Z", re.ASCII)
_FLOAT = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?\Z", re.ASCII)
_TS = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?\Z", re.ASCII)

Value = Union[int, float, str, datetime, None]


class Toa5Error(ValueError):
    """Malformed TOA5 content. `line` is the 1-based physical line number."""

    def __init__(self, line: int, msg: str):
        super().__init__(f"TOA5 line {line}: {msg}")
        self.line = line


@dataclass(frozen=True)
class Toa5Warning:
    """A non-fatal diagnostic. `line` is the 1-based physical line; `code` is machine-readable."""
    line: int
    code: str
    message: str


@dataclass(frozen=True)
class Toa5Environment:
    station_name: str
    logger_model: str
    serial_number: str
    os_version: str
    program_name: str
    program_signature: str
    table_name: str


@dataclass(frozen=True)
class Toa5Field:
    name: str
    units: str
    processing: str


@dataclass(frozen=True)
class Toa5Table:
    environment: Toa5Environment
    fields: tuple[Toa5Field, ...]
    records: tuple[dict[str, Value], ...]
    warnings: tuple[Toa5Warning, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def field(self, name: str) -> Toa5Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def column(self, name: str) -> list[Value]:
        self.field(name)
        return [r[name] for r in self.records]


def split_line(line: str, lineno: int) -> list[tuple[str, bool]]:
    """Split one TOA5 line into (text, was_quoted) tokens."""
    out: list[tuple[str, bool]] = []
    i, n = 0, len(line)
    while True:
        if i < n and line[i] == '"':
            buf, i = [], i + 1
            while True:
                if i >= n:
                    raise Toa5Error(lineno, "unterminated quoted field")
                if line[i] == '"':
                    if i + 1 < n and line[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(line[i])
                i += 1
            out.append(("".join(buf), True))
            if i < n and line[i] != ",":
                raise Toa5Error(lineno, f"unexpected text after closing quote at column {i + 1}")
        else:
            j = line.find(",", i)
            tok = line[i:] if j < 0 else line[i:j]
            if '"' in tok:
                raise Toa5Error(lineno, f"stray quote in unquoted field {tok!r}")
            out.append((tok.strip(), False))
            i = n if j < 0 else j
        if i >= n:
            return out
        i += 1  # skip comma
        if i == n:
            out.append(("", False))
            return out


def parse_timestamp(text: str, lineno: int = 0) -> datetime:
    m = _TS.match(text)
    if not m:
        raise Toa5Error(lineno, f"timestamp {text!r} is not 'YYYY-MM-DD HH:MM:SS[.fraction]'")
    y, mo, d, h, mi, s, frac = m.groups()
    us = 0
    if frac:
        if len(frac) > 6 and frac[6:].strip("0"):
            raise Toa5Error(lineno, f"timestamp {text!r} has sub-microsecond precision")
        us = int(frac[:6].ljust(6, "0"))
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), us)
    except ValueError as e:
        raise Toa5Error(lineno, f"invalid timestamp {text!r}: {e}") from None


def coerce_value(text: str, quoted: bool, lineno: int = 0) -> Value:
    special = _SPECIAL.get(text.strip().upper())
    if special is not None:
        return special
    if quoted:
        return text
    if text == "":
        return None
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    raise Toa5Error(lineno, f"unquoted non-numeric value {text!r}")


def _texts(tokens: list[tuple[str, bool]]) -> list[str]:
    return [t for t, _ in tokens]


def _sequence_warnings(records: list[tuple[int, dict[str, Value]]], ts_name: str | None,
                       rec_name: str | None) -> list[Toa5Warning]:
    out: list[Toa5Warning] = []
    prev_rec = prev_ts = None
    seen_rec: set = set()
    seen_ts: set = set()
    for lineno, rec in records:
        if rec_name is not None:
            r = rec[rec_name]
            if r == 0 and prev_rec in (RECORD_MAX, RECORD_MAX_REPORTED):
                seen_rec = set()                       # documented rollover: new sequence, no warning
            elif r in seen_rec:
                out.append(Toa5Warning(lineno, "record_duplicate", f"RECORD {r} already seen"))
            elif prev_rec is not None and r < prev_rec:
                out.append(Toa5Warning(lineno, "record_out_of_order", f"RECORD {r} after {prev_rec} (not a rollover)"))
            seen_rec.add(r)
            prev_rec = r
        if ts_name is not None:
            t = rec[ts_name]
            if t in seen_ts:
                out.append(Toa5Warning(lineno, "timestamp_duplicate", f"TIMESTAMP {t.isoformat(' ')} already seen"))
            elif prev_ts is not None and t < prev_ts:
                out.append(Toa5Warning(lineno, "timestamp_out_of_order",
                                       f"TIMESTAMP {t.isoformat(' ')} after {prev_ts.isoformat(' ')}"))
            seen_ts.add(t)
            prev_ts = t
    return out


def parse_lines(lines: Iterable[str], *, on_unterminated: str = "error") -> Toa5Table:
    """Parse TOA5 from an iterable of lines that still carry their terminators (a file object or StringIO)."""
    if on_unterminated not in UNTERMINATED_POLICIES:
        raise ValueError(f"on_unterminated must be one of {UNTERMINATED_POLICIES}, got {on_unterminated!r}")
    it: Iterator[tuple[int, str, bool]] = (
        (i, ln.rstrip("\r\n"), ln.endswith(("\n", "\r"))) for i, ln in enumerate(lines, start=1))
    header: list[list[tuple[str, bool]]] = []
    warnings: list[Toa5Warning] = []
    for lineno, line, terminated in it:
        if lineno == 1 and line.startswith("\ufeff"):
            line = line[1:]
        if not terminated:
            raise Toa5Error(lineno, "header line has no line terminator (file truncated inside the header)")
        header.append(split_line(line, lineno))
        if len(header) == 4:
            break
    if len(header) < 4:
        raise Toa5Error(len(header) + 1, f"file ends inside the 4-line header ({len(header)} line(s) read)")
    env = _texts(header[0])
    if len(env) != 8:
        raise Toa5Error(1, f"environment line must have 8 fields, found {len(env)}")
    if env[0] != "TOA5":
        raise Toa5Error(1, f"file type is {env[0]!r}, expected 'TOA5'")
    names, units, procs = _texts(header[1]), _texts(header[2]), _texts(header[3])
    if names == [""] or any(not nm for nm in names):
        raise Toa5Error(2, "empty field name")
    if len(set(names)) != len(names):
        dup = sorted({nm for nm in names if names.count(nm) > 1})
        raise Toa5Error(2, f"duplicate field names {dup}")
    for ln, row, what in ((3, units, "units"), (4, procs, "processing")):
        if len(row) != len(names):
            raise Toa5Error(ln, f"{what} line has {len(row)} fields, field-name line has {len(names)}")
    ts_col = rec_col = None
    if "TIMESTAMP" in names:
        if names.index("TIMESTAMP") != 0:
            raise Toa5Error(2, "TIMESTAMP must be the first field")
        ts_col = 0
    if "RECORD" in names:
        rec_col = names.index("RECORD")
        if rec_col != (1 if ts_col == 0 else 0):
            raise Toa5Error(2, "RECORD must directly follow TIMESTAMP (or be first when there is no TIMESTAMP)")
    for i, u in enumerate(units):
        if u == "TS" and i != ts_col:
            raise Toa5Error(3, f"units 'TS' on non-TIMESTAMP field {names[i]!r}")
        if u == "RN" and i != rec_col:
            raise Toa5Error(3, f"units 'RN' on non-RECORD field {names[i]!r}")

    records: list[tuple[int, dict[str, Value]]] = []
    blank_at = None
    for lineno, line, terminated in it:
        if not line.strip():
            blank_at = blank_at or lineno
            continue
        if blank_at:
            raise Toa5Error(blank_at, "blank line inside data")
        if not terminated:
            if on_unterminated == "error":
                raise Toa5Error(lineno, "last data line has no line terminator (possibly truncated mid-write); "
                                        "use on_unterminated='drop' to discard it")
            warnings.append(Toa5Warning(lineno, "unterminated_last_line_dropped",
                                        "last data line had no line terminator and was dropped"))
            continue
        toks = split_line(line, lineno)
        if len(toks) != len(names):
            raise Toa5Error(lineno, f"data line has {len(toks)} fields, expected {len(names)}")
        rec: dict[str, Value] = {}
        for i, (text, quoted) in enumerate(toks):
            if i == ts_col:
                rec[names[i]] = parse_timestamp(text, lineno)
            elif i == rec_col:
                if not _RECORD.match(text) or int(text) > RECORD_MAX:
                    raise Toa5Error(lineno, f"RECORD {text!r} is not an unsigned 32-bit integer")
                rec[names[i]] = int(text)
            else:
                rec[names[i]] = coerce_value(text, quoted, lineno)
        records.append((lineno, rec))
    warnings.extend(_sequence_warnings(records, names[ts_col] if ts_col is not None else None,
                                       names[rec_col] if rec_col is not None else None))
    warnings.sort(key=lambda w: w.line)
    environment = Toa5Environment(*env[1:])
    fields = tuple(Toa5Field(n, u, p) for n, u, p in zip(names, units, procs))
    return Toa5Table(environment, fields, tuple(r for _, r in records), tuple(warnings))


def parse_text(text: str, *, on_unterminated: str = "error") -> Toa5Table:
    return parse_lines(io.StringIO(text, newline=None), on_unterminated=on_unterminated)


def read_toa5(path: Union[str, Path], encoding: str = "utf-8", *, on_unterminated: str = "error") -> Toa5Table:
    """Read a TOA5 file. Decoding is strict: undecodable bytes raise UnicodeDecodeError."""
    with open(path, encoding=encoding, errors="strict", newline=None) as fh:
        return parse_lines(fh, on_unterminated=on_unterminated)
