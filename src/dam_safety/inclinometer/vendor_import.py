"""QA-044 UNVERIFIED text-export interface; no vendor parser is enabled for production.

Depth rule: procedures/inclinometers.md §3.1; reference verify.py T17a–g.
Only metadata declared by the caller is used; vendor defaults are never inferred.
"""
from __future__ import annotations

import math
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
    """Validate the future parser boundary; intentionally does not parse a vendor format.

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
        raise ImportRejected("Text/CSV bytes required; binary files must be exported to text")
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


def parse_export(*args, **kwargs):
    """Parser stub. Even opt-in cannot masquerade as a verified parser or reach reduction."""
    request = prepare_import(*args, **kwargs)
    raise NotImplementedError(f"{request.profile} parser is UNVERIFIED and not implemented")
