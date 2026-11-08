"""TOA5 -> bronze landing rows (Data Architect side of the TOA5 path).

Parsing is done ONLY by the App Developer's clean-room reader src/common/toa5.py (docs/TOA5_READER.md; replaces
PyTOA5, whose dependency igbpyutils is GPL-3.0-or-later). This module just maps a parsed Toa5Table onto the
bronze READING_SCHEMA (src/pipelines/bronze.py) so Auto Loader ingests it like any other ADAS CSV.

Rules (each one closes a gap that the reader leaves to the caller by design):
  * TOA5 timestamps carry no zone [CS-LN-TOA5]: utc_offset_min is REQUIRED (station logger offset; no default).
  * NAN/+-INF -> raw_value NULL, which silver QC flags as NULL_VALUE (it is never dropped silently).
  * Integer NAN sentinels (FP2 -7999, Long -2147483648 [CS-NANINF]) -> NULL only for fields the caller declares
    (int_sentinel_fields): TOA5 does not record the stored data type, so we never guess.
  * A mapped field missing from the file raises: a renamed logger field must not silently stop a sensor.
  * QA-018: first-wins dedup on (table name, RECORD, TIMESTAMP) within this parsed file, BEFORE channel expansion.
    Raw Toa5Table records are never modified. Other sequence anomalies are retained and flagged, not rejected.
    Reader warnings and landing diagnostics are surfaced as Toa5LandingWarning and retained on LandingRows.diagnostics;
    to_landing_diagnostics_json provides the companion append-only diagnostics output (not a readings CSV column).
    See docs/TOA5_READER.md, sequence-warning contract [CS-LN-TOA5]. RECORD is required by this landing policy.

Provenance (docs/PROVENANCE.md): clean-room. Written from the Campbell Scientific TOA5 format documentation
([CS-LN-TOA5], [CS-CR1000] §7 Tables 12-13 and §8, [CS-NANINF]) and the public API of common.toa5 only; it is NOT
derived from PyTOA5 or igbpyutils (GPL-3.0-or-later), whose code is neither imported, vendored nor copied.
The bronze column order comes from src/pipelines/bronze.py (in-house)."""
from __future__ import annotations

import csv
import io
import json
import math
import warnings
from datetime import timedelta, timezone

from common.toa5 import FP2_NAN_SENTINEL, LONG_NAN_SENTINEL, Toa5Table

LANDING_COLUMNS = ("dam_id", "logger_id", "sensor_id", "ts_utc", "raw_value", "raw_unit", "temp_raw", "temp_c",
                   "baro_kpa", "reading_type", "source", "reservoir_el_m", "datum_id")   # == bronze READING_SCHEMA
_SENTINELS = {FP2_NAN_SENTINEL, LONG_NAN_SENTINEL}


class Toa5LandingError(ValueError):
    pass


class Toa5LandingWarning(UserWarning):
    """Surfaced reader/landing diagnostic; see docs/TOA5_READER.md [CS-LN-TOA5]."""


class LandingRows(list):
    """Bronze-compatible rows plus diagnostics for the companion JSON output.

    List behaviour is retained for existing callers. Persist the JSON sidecar outside the
    readings/ directory alongside the unchanged source file; see docs/TOA5_READER.md [CS-LN-TOA5].
    """

    def __init__(self, rows=(), *, diagnostics=()):
        super().__init__(rows)
        self.diagnostics = tuple(diagnostics)


def to_landing_diagnostics_json(rows: LandingRows) -> str:
    """Serialize the QA-018 diagnostics sidecar; docs/TOA5_READER.md [CS-LN-TOA5].

    Pure output adapter: the caller appends this JSON to its diagnostics volume/path.
    No file or UC writes occur here. Empty diagnostics are serialized too.
    """
    return json.dumps({"schema_version": 1, "diagnostics": rows.diagnostics},
                      ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"


def _num(v, field, int_sentinel_fields):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None                      # str / None -> NULL (QC NULL_VALUE)
    if isinstance(v, int) and field in int_sentinel_fields and v in _SENTINELS:
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def to_landing_rows(table: Toa5Table, dam_id: str, utc_offset_min: int, channel_map: dict,
                    temp_map: dict | None = None, int_sentinel_fields: frozenset = frozenset(), *,
                    expected_interval_seconds: float | None = None) -> LandingRows:
    """Dedup/flag one parsed file without altering raw; docs/TOA5_READER.md [CS-LN-TOA5].

    Key: table+RECORD+TIMESTAMP, scoped to this dam/logger/file. First occurrence wins,
    even for conflicting values; every suppressed source row gets a diagnostic. RECORD
    gaps are flagged; timestamp gaps require caller-supplied expected cadence (no guessing).
    Persist to_landing_diagnostics_json(result) with the source-file identity. Cross-file
    replay dedup requires orchestration and is not implemented by this stateless adapter.
    """
    if utc_offset_min is None:
        raise Toa5LandingError("utc_offset_min is required: TOA5 timestamps carry no time zone")
    if expected_interval_seconds is not None and (
            isinstance(expected_interval_seconds, bool) or not isinstance(expected_interval_seconds, (int, float))
            or not math.isfinite(expected_interval_seconds) or expected_interval_seconds <= 0):
        raise Toa5LandingError("expected_interval_seconds must be finite and positive")
    temp_map = temp_map or {}
    names = set(table.names)
    if "TIMESTAMP" not in names:
        raise Toa5LandingError("TOA5 table has no TIMESTAMP column")
    if "RECORD" not in names:
        raise Toa5LandingError("TOA5 landing requires RECORD for the table+RECORD+TIMESTAMP dedup key")
    missing = sorted((set(channel_map) | set(temp_map.values())) - names)
    if missing:
        raise Toa5LandingError(f"mapped fields not in file: {missing}")
    env = table.environment
    logger_id = f"{env.logger_model}:{env.serial_number}"
    tz = timezone(timedelta(minutes=utc_offset_min))
    out = []
    diagnostics = []
    seen = {}
    by_line = {}
    for diagnostic in table.warnings:
        by_line.setdefault(diagnostic.line, []).append((diagnostic.code, diagnostic.message))
    previous = None
    for index, r in enumerate(table.records):
        # Reader permits no blank lines inside data: physical line = four headers + ordinal.
        line = index + 5
        key = (env.table_name, r["RECORD"], r["TIMESTAMP"])
        duplicate = key in seen
        entries = list(by_line.pop(line, ()))
        if duplicate:
            entries.append(("landing_duplicate_suppressed", f"first occurrence at line {seen[key]} wins"))
        else:
            seen[key] = line
        if previous is not None:
            if r["RECORD"] > previous["RECORD"] + 1:
                entries.append(("record_gap", f"RECORD {previous['RECORD']} to {r['RECORD']}"))
            if expected_interval_seconds is not None:
                elapsed = (r["TIMESTAMP"] - previous["TIMESTAMP"]).total_seconds()
                if elapsed > expected_interval_seconds:
                    entries.append(("timestamp_gap", f"interval {elapsed}s exceeds {expected_interval_seconds}s"))
        previous = r
        for code, message in entries:
            diagnostics.append(dict(dam_id=dam_id, logger_id=logger_id, table_name=env.table_name,
                                    line=line, record=r["RECORD"], timestamp=r["TIMESTAMP"].isoformat(),
                                    code=code, message=message,
                                    disposition="suppressed_duplicate" if duplicate else "retained_flagged"))
        if duplicate:
            continue
        ts = r["TIMESTAMP"].replace(tzinfo=tz).astimezone(timezone.utc)
        for field, sensor_id in channel_map.items():
            row = dict.fromkeys(LANDING_COLUMNS)
            row.update(dam_id=dam_id, logger_id=logger_id, sensor_id=sensor_id,
                       ts_utc=ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                       raw_value=_num(r[field], field, int_sentinel_fields),
                       raw_unit=table.field(field).units or None, source="ADAS",
                       temp_raw=_num(r[temp_map[field]], temp_map[field], int_sentinel_fields)
                       if field in temp_map else None)
            out.append(row)
    # Preserve file-level reader warnings too (e.g. dropped unterminated tail).
    for line, entries in by_line.items():
        for code, message in entries:
            diagnostics.append(dict(dam_id=dam_id, logger_id=logger_id, table_name=env.table_name,
                                    line=line, record=None, timestamp=None, code=code, message=message,
                                    disposition="reader_warning"))
    diagnostics.sort(key=lambda d: d["line"])
    for diagnostic in diagnostics:
        warnings.warn(f"TOA5 {env.table_name} line {diagnostic['line']} "
                      f"[{diagnostic['code']}]: {diagnostic['message']}", Toa5LandingWarning, stacklevel=2)
    return LandingRows(out, diagnostics=diagnostics)


def to_landing_csv(rows: list[dict]) -> str:
    """CSV text for /Volumes/<cat>/raw/landing/<dam>/readings/ (header in LANDING_COLUMNS order)."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LANDING_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
