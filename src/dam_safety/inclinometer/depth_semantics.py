"""QA-042 fail-closed vendor depth conversion (not a file parser).

Sources (citations, not copied manuals): procedures/inclinometer_depth_semantics.md
§§2–7; procedures/inclinometers.md §3.1; verify.py::to_toc_interval_top, T17a–g.
An explicit output-point declaration is essential: GTILT plot placement is UNVERIFIED
without per-output evidence. Vendor names and a grid fit are never such evidence.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from .vendor_import import DepthMetadata, ImportRejected, REQUIRED_METADATA, _finite

CONTEXT_FIELDS = frozenset({"export_type", "depth_mode", "output_point", "placement_verified",
                            "placement_evidence", "settings_verified", "additional_depth_corrections",
                            "probe_type", "apply_stickup", "stickup_value_m"})


def interval_top(profile, label, label_unit_m, casing_depth_m, L_m, metadata):
    """Convert once from immutable vendor label; reject unsupported/UNVERIFIED interpretations.

    All five basic fields plus CONTEXT_FIELDS must be explicitly supplied. Settings
    describe THIS output column, not vendor defaults. Elevations additionally require
    TOC elevation and equal named label/TOC datums. No cable/compression transform is guessed.
    """
    if not isinstance(metadata, Mapping):
        raise ImportRejected("Missing required depth-reference metadata; no vendor defaults")
    missing = [k for k in REQUIRED_METADATA | CONTEXT_FIELDS if k not in metadata or metadata[k] is None]
    if missing:
        raise ImportRejected("Missing required depth metadata: " + ", ".join(sorted(missing)))
    basic = DepthMetadata.from_mapping({k: metadata[k] for k in REQUIRED_METADATA})
    if metadata["export_type"] not in {"raw_survey", "plot", "processed"}:
        raise ImportRejected("UNVERIFIED export_type")
    if (metadata["placement_verified"] is not True or metadata["settings_verified"] is not True
            or not isinstance(metadata["placement_evidence"], str)
            or not metadata["placement_evidence"].strip()
            or metadata["placement_evidence"].strip().upper() in {"UNKNOWN", "UNVERIFIED"}):
        raise ImportRejected("UNVERIFIED output placement or settings; documented evidence required")
    if metadata["additional_depth_corrections"] != "none":
        raise ImportRejected("UNVERIFIED cable/compression depth corrections; separate transform required")
    point = metadata["output_point"]
    if point not in {"midpoint", "top", "bottom"}:
        raise ImportRejected("UNVERIFIED output_point; declare midpoint, top or bottom")
    if metadata["probe_type"] not in {"classic", "at"}:
        raise ImportRejected("UNVERIFIED probe/cable convention")
    if metadata["probe_type"] == "at" and (basic.auto_adjust or basic.depth_label_ref != "top" or point != "top"):
        raise ImportRejected("UNVERIFIED AT setting: do not apply Classic midpoint auto-adjust")
    expected = "top" if basic.auto_adjust else basic.depth_label_ref
    if point != expected and not (basic.auto_adjust and point == "bottom"):
        raise ImportRejected("UNVERIFIED output-point/auto-adjust combination")
    if type(metadata["apply_stickup"]) is not bool:
        raise ImportRejected("apply_stickup must be an explicit boolean")
    stickup = _finite(metadata["stickup_value_m"], "stickup_value_m")
    unit = _finite(label_unit_m, "label_unit_m")
    if unit <= 0:
        raise ImportRejected("label_unit_m must be positive")
    if isinstance(label, bool):
        raise ImportRejected("label must be numeric")
    try:
        y = float(label) * unit
    except (TypeError, ValueError, OverflowError) as e:
        raise ImportRejected("label must be numeric") from e
    if not math.isfinite(y):
        raise ImportRejected("label must be finite")
    L = _finite(L_m, "L_m")
    if L <= 0:
        raise ImportRejected("L_m must be positive")
    q = {"midpoint": L / 2, "top": 0.0, "bottom": L}[point]
    mode = metadata["depth_mode"]
    if mode == "depth":
        actual_shift = stickup if metadata["apply_stickup"] else 0.0
        if not math.isclose(basic.ground_shift, actual_shift, abs_tol=1e-9, rel_tol=0):
            raise ImportRejected("UNVERIFIED stickup state: ground_shift must equal the amount actually applied")
        if basic.depth_datum == "toc" and (basic.ground_shift != 0 or basic.index_above_toc != 0):
            raise ImportRejected("UNVERIFIED TOC datum contradicts declared ground/index offsets")
        if basic.depth_datum == "index" and basic.ground_shift != 0:
            raise ImportRejected("UNVERIFIED index datum contradicts applied ground shift")
        top = y + basic.ground_shift - basic.index_above_toc - q
    elif mode == "elevation":
        datum = metadata.get("label_datum")
        if not isinstance(datum, str) or not datum.strip() or datum != metadata.get("toc_datum"):
            raise ImportRejected("UNVERIFIED elevation datum: matching named label_datum/toc_datum required")
        elevation = _finite(metadata.get("toc_elevation_m"), "toc_elevation_m")
        top = elevation - y - q  # surveyed elevations already provide the datum: do NOT also apply g/h
    else:
        raise ImportRejected("UNVERIFIED depth_mode; declare depth or elevation")
    # Grid fit is validation after conversion, never evidence for the metadata itself.
    from .installation import interval_tops_m
    casing_depth_m = _finite(casing_depth_m, "casing_depth_m")
    tops = interval_tops_m(casing_depth_m, L)
    i = round(top / L)
    if top >= casing_depth_m - 1e-6:
        raise ImportRejected("DEPTH_BELOW_CASING: toe/boundary anchors are not reading intervals")
    if i < 0 or i >= len(tops) or abs(top - tops[i]) > 1e-6:
        raise ImportRejected("DEPTH_OFF_GRID: converted label is not an expected interval top")
    return tops[i]
