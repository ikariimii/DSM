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

Provenance (docs/PROVENANCE.md): clean-room. Written from the Campbell Scientific TOA5 format documentation
([CS-LN-TOA5], [CS-CR1000] §7 Tables 12-13 and §8, [CS-NANINF]) and the public API of common.toa5 only; it is NOT
derived from PyTOA5 or igbpyutils (GPL-3.0-or-later), whose code is neither imported, vendored nor copied.
The bronze column order comes from src/pipelines/bronze.py (in-house)."""
from __future__ import annotations

import csv
import io
import math
from datetime import timedelta, timezone

from common.toa5 import FP2_NAN_SENTINEL, LONG_NAN_SENTINEL, Toa5Table

LANDING_COLUMNS = ("dam_id", "logger_id", "sensor_id", "ts_utc", "raw_value", "raw_unit", "temp_raw", "temp_c",
                   "baro_kpa", "reading_type", "source", "reservoir_el_m", "datum_id")   # == bronze READING_SCHEMA
_SENTINELS = {FP2_NAN_SENTINEL, LONG_NAN_SENTINEL}


class Toa5LandingError(ValueError):
    pass


def _num(v, field, int_sentinel_fields):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None                      # str / None -> NULL (QC NULL_VALUE)
    if isinstance(v, int) and field in int_sentinel_fields and v in _SENTINELS:
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def to_landing_rows(table: Toa5Table, dam_id: str, utc_offset_min: int, channel_map: dict,
                    temp_map: dict | None = None, int_sentinel_fields: frozenset = frozenset()) -> list[dict]:
    if utc_offset_min is None:
        raise Toa5LandingError("utc_offset_min is required: TOA5 timestamps carry no time zone")
    temp_map = temp_map or {}
    names = set(table.names)
    if "TIMESTAMP" not in names:
        raise Toa5LandingError("TOA5 table has no TIMESTAMP column")
    missing = sorted((set(channel_map) | set(temp_map.values())) - names)
    if missing:
        raise Toa5LandingError(f"mapped fields not in file: {missing}")
    env = table.environment
    logger_id = f"{env.logger_model}:{env.serial_number}"
    tz = timezone(timedelta(minutes=utc_offset_min))
    out = []
    for r in table.records:
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
    return out


def to_landing_csv(rows: list[dict]) -> str:
    """CSV text for /Volumes/<cat>/raw/landing/<dam>/readings/ (header in LANDING_COLUMNS order)."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LANDING_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
