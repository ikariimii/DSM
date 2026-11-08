"""1_SETUP -> installation (REPORT §6.1 row 1; procedure §0, §3, §4.0).

All lengths are stored in SI (metres) with depth measured DOWN FROM THE TOP OF CASING, and every increment is keyed to
the depth of the TOP of its interval (procedure §4.0 team convention). The probe is described by explicit L and K, not
the workbook's Q = 2K (REPORT D1), so metric probes (K = 25000, L = 0.5 m) and English probes (K = 20000, L = 24 in)
share one code path [procedure §3 table: K and L rows]."""
from __future__ import annotations

from dataclasses import dataclass
import math

FT = 0.3048
IN = 0.0254


@dataclass(frozen=True)
class Probe:
    L_m: float            # wheelbase / reading interval, metres
    K: float              # instrument constant: reading = K*sin(theta)  [procedure §3]
    raw_unit: str         # e.g. 'sin*20000' (procedure §8 raw_unit vocabulary)

    def __post_init__(self):
        if not (math.isfinite(self.L_m) and math.isfinite(self.K) and self.L_m > 0 and self.K > 0):
            raise ValueError("L_m and K must be positive")


ENGLISH_DIGITILT = Probe(L_m=24 * IN, K=20000.0, raw_unit="sin*20000")     # [procedure §3: K 20000, L 24 in]
METRIC_DIGITILT = Probe(L_m=0.5, K=25000.0, raw_unit="sin*25000")          # [procedure §3: K 25000, L 500 mm]


@dataclass(frozen=True)
class Installation:
    instrument_id: str
    dam_id: str
    probe: Probe
    casing_depth_m: float            # bottom of the deepest interval, metres below top of casing
    a0_azimuth_deg: float            # A0 azimuth (A0 = downstream by convention) [procedure §3]
    baseline_survey_id: str | None   # selectable baseline (REPORT D9); None = earliest valid survey
    system: str = "probe"            # 'probe' | 'saa'
    spiral_survey_id: str | None = None   # spiral survey on file (REPORT §6.1 row 1 input). Spiral correction is
                                          # DEFERRED: its presence only raises info flag SPIRAL_NOT_APPLIED (corrections.py)
    stable_zone_top_m: float | None = None   # REQUIRED for between-set bias QC (QA-020, procedure §7.1): top of the
                                             # stable zone from boring log / as-built, metres below top of casing.
                                             # Never auto-detected; missing -> bottom 5 intervals + stable_zone_assumed

    def interval_tops_m(self) -> list[float]:
        """Interval tops, top-down: 0, L, 2L, ... while top + L <= casing depth."""
        return interval_tops_m(self.casing_depth_m, self.probe.L_m)


def interval_tops_m(casing_depth_m: float, L_m: float) -> list[float]:
    """Expected interval tops; procedure §3.1/§4.0 and depth-semantics §7 (QA-043)."""
    if not all(math.isfinite(v) and v > 0 for v in (casing_depth_m, L_m)):
        raise ValueError("casing depth and L must be finite and positive")
    n = int(round(casing_depth_m / L_m))
    if not n or abs(n * L_m - casing_depth_m) > 1e-6:
        raise ValueError("casing depth is not a whole number of intervals")
    return [round(i * L_m, 9) for i in range(n)]


def rekey_from_bottom_label(label_from_bottom_m: float, casing_depth_m: float, L_m: float) -> float:
    """Workbook-style label (height of the interval's BOTTOM above the casing bottom) -> interval TOP depth from top.
    REPORT D2 / verify T13d: label d belongs to the interval whose top is d + L above the bottom, i.e.
    casing_depth - d - L below the top (30 ft casing, L 2 ft: label 26 ft -> 2 ft from top). Author intent
    UNVERIFIED (inferred from T13e); the mapping is therefore an explicit, named import option, never implicit."""
    top = casing_depth_m - label_from_bottom_m - L_m
    if top < -1e-9:
        raise ValueError("label above top of casing")
    return round(max(top, 0.0), 9)


# --- Depth-label import profiles (Geotech lead ruling 2026-11-02) -------------------------------------------------
# Stored depth is ALWAYS the interval top measured down from the top of casing (procedure §4.0). depth_reference records
# how the SOURCE file labelled depth, and the raw label is kept next to the re-keyed value.
#   workbook : labels are feet from the BOTTOM; top = casing_depth - d - L (workbook: 28 - d ft) [REPORT D2; verify
#              T13d/T13e]. Author intent UNVERIFIED (docs/VERIFICATION.md INC-LABEL): inferred from T13e only.
#   contract : already-normalised silver depth (NOT a vendor import), checked by batch's full-grid QC.
#   gtilt/digipro: REQUIRE explicit per-output depth metadata; unknown placement/settings are rejected.
# Source: procedures/inclinometer_depth_semantics.md §§2–7; procedures/inclinometers.md §3.1.
DEPTH_REFERENCES = ("top_of_casing", "from_bottom_label")
IMPORT_PROFILES = {"workbook": "from_bottom_label", "contract": "top_of_casing",
                   "gtilt": "metadata_required", "digipro": "metadata_required"}


def interval_top_from_label(profile: str, raw_label, label_unit_m: float, casing_depth_m: float | None = None,
                            L_m: float | None = None, *, depth_metadata=None) -> tuple[float, str]:
    """(depth_top_m from top of casing, depth_reference) for one source depth label. No implicit guessing: the
    from-bottom mapping happens only for 'workbook'. Vendor profiles require declared metadata
    plus casing depth and L; no native-from-top assumption is made (QA-042)."""
    if profile not in IMPORT_PROFILES:
        raise ValueError(f"unknown import profile {profile!r}; expected one of {sorted(IMPORT_PROFILES)}")
    ref = IMPORT_PROFILES[profile]
    if ref == "metadata_required":
        from .depth_semantics import interval_top
        return interval_top(profile, raw_label, label_unit_m, casing_depth_m, L_m, depth_metadata), "top_of_casing"
    d = float(raw_label) * label_unit_m
    if not math.isfinite(d):
        raise ValueError("depth label must be finite")
    if ref == "from_bottom_label":
        if casing_depth_m is None or L_m is None:
            raise ValueError("workbook profile needs casing_depth_m and L_m")
        return rekey_from_bottom_label(d, casing_depth_m, L_m), ref
    if d < -1e-9:
        raise ValueError("negative depth from top")
    return round(d, 9), ref
