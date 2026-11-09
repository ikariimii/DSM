"""QA: GTILT / DigiPro vendor-export parity (user priority 1, 2026-11-02 EST).

Reads the VDV Helpcenter vendor exports BY PATH from dam-refs (manifest DS-INC-VDV-EXPORT-EXAMPLES,
usage_restriction = internal-test-only). No value from those files is stored here: every check is a structural
property or an app-vs-independent comparison computed at run time. Derived numbers live outside the repo in
$DAM_QA_PRIVATE/vdv_parity_results.json (written by $DAM_QA_PRIVATE/vdv_parity_analysis.py). Skips when absent.

Manual citations (format / defaults):
  GTILT PLUS v3: File/Tabulate prints raw data, processed data, checksums and plotted coordinates  PDF p26 [4-2]
                 plot depth = depth below the reference elevation (usually ground), not cable depth  PDF p20 [2-8]
                 stickup = reference elevation -> cable reference point                              PDF p15 [2-3]
                 cable marks = probe wheelbase MIDPOINT is GTILT's default assumption                PDF p52
                 fixed end top/bottom chosen in Set/Preferences                                      PDF p27 [4-3]
                 corrections (ROTA=, ZA=) exist only when typed into a dataset's subheader            PDF p43-44
                 ID plot = first differential of the CD plot w.r.t. depth                             PDF p11-12 [1-7,1-8]
  DigiPro (2004): correction routines disabled by default PDF p30; bottom reference default PDF p23;
                 auto depth-adjust ON by default (points plotted at interval top) PDF p18
  DigiPro2 (2025): Profile Change = old "Cumulative Displacement", Tilt Change = old "Incremental Displacement"
                 PDF p23-25; checksum = 0 + 180 reading PDF p25; summing from bottom is default; Apply Stickup and
                 Auto Depth-Adjust options PDF p28; text export = tab separated PDF p33; display units default
                 sine PDF p13.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime

import pytest

VDV = os.environ.get("DAM_VDV_EXPORTS",
                     "/workspace/dam-refs/datasets/inclinometers/vendor_export_samples_vdv")
GTILT = os.path.join(VDV, "gtilt-export-version-1.txt")
DPTXT = os.path.join(VDV, "digipro-digitester.txt")
DPCSV = os.path.join(VDV, "digipro-profiletester.csv")
L_M, K = 0.5, 25000.0          # any positive pair works for the round-trip checks below (scale cancels)


def _need(p):
    if not os.path.exists(p):
        pytest.skip(f"restricted vendor export not present: {p}")


# ----------------------------------------------------------------------------------------------- parsers (QA-owned)
def parse_gtilt(path):
    """GTILT 'Plot Coordinates' tabulation (File/Tabulate). Returns dict with dates, depths, inc[(date_idx, axis)]."""
    lines = open(path, encoding="latin-1").read().splitlines()
    out = {"title": None, "initial": None, "dates": [], "cols": [], "depths": [], "inc": {}, "decimals": set(),
           "neg_zero": 0}
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("Plot Coordinates"):
            out["title"] = s
        elif s.startswith("Relative to initial reading"):
            out["initial"] = s
        elif re.fullmatch(r"(\d{1,2} \w{3} \d{4}\s*)+", s):
            out["dates"] = [datetime.strptime(d, "%d %b %Y").date() for d in re.findall(r"\d{1,2} \w{3} \d{4}", s)]
        elif s.startswith("Depth"):
            out["cols"] = re.findall(r"Inc\s+([AB])", s)
        elif re.fullmatch(r"-?\d+(\.\d+)?(\s+-?\d+\.\d+)+", s):
            toks = s.split()
            d = float(toks[0])
            out["depths"].append(d)
            for j, t in enumerate(toks[1:]):
                out["inc"].setdefault((j // 2, out["cols"][j]), []).append(float(t))
                out["decimals"].add(len(t.split(".")[1]))
                out["neg_zero"] += t.startswith("-") and float(t) == 0.0
    return out


def parse_digipro(path, sep):
    """DigiPro/DigiPro2 plot export (text = tab, csv = comma). Returns {axis: {quantity, unit, dates, depths, val}}."""
    raw = open(path, encoding="latin-1").read().replace("\r\n", "\n").split("\n")
    blocks, cur = {}, None
    for i, ln in enumerate(raw):
        f = [x.strip() for x in ln.split(sep)]
        if len(f) == 1 and re.search(r"\s([AB])$", f[0]) and i + 1 < len(raw) and "Units:" in raw[i + 1]:
            cur = {"name": f[0], "axis": f[0][-1]}
            blocks[cur["axis"]] = cur
        elif cur is not None and len(f) >= 4 and f[2] == "Units:":
            cur["depth_unit"], cur["quantity"], cur["unit"] = f[0], f[1], f[3]
        elif cur is not None and f[0] == "Depth":
            cur["date_text"] = f[1:]
            cur["depths"], cur["val"] = [], [[] for _ in f[1:]]
        elif cur is not None and f[0] and re.fullmatch(r"-?\d+(\.\d+)?", f[0]) and "depths" in cur:
            cur["depths"].append(float(f[0]))
            for j, t in enumerate(f[1:]):
                cur["val"][j].append(float(t) if t else None)
    return blocks


def parse_dates(texts):
    """ISO or dd/mm/yyyy. Returns (dates, fmt). Month-first is accepted only if day-first is impossible."""
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", t) for t in texts):
        return [datetime.strptime(t, "%Y-%m-%d").date() for t in texts], "iso"
    for fmt in ("%d/%m/%Y", "%m/%d/%Y"):
        try:
            return [datetime.strptime(t, fmt).date() for t in texts], fmt
        except ValueError:
            continue
    raise ValueError("unparseable dates")


# ------------------------------------------------------------------------------------------- independent reduction
def cum_from_bottom(inc):
    """Cumulative from the deepest row upward, INCLUDING the row's own increment (team convention, procedure §4.0)."""
    acc, out = 0.0, [0.0] * len(inc)
    for i in range(len(inc) - 1, -1, -1):
        acc += inc[i]
        out[i] = acc
    return out


def app_reduce(depths, inc_mm):
    """Run the APP's reduction on the vendor increments: D is back-computed with D = inc*2K/(1000 L) for the current
    set and 0 for the base, so app change_mm must return inc and app cum_change_mm the bottom-up sum."""
    from dam_safety.inclinometer.installation import Probe
    from dam_safety.inclinometer.reduction import reduce_survey
    p = Probe(L_M, K, "sin*25000")
    D = {(d, "A"): v * 2.0 * K / (1000.0 * L_M) for d, v in zip(depths, inc_mm)}
    Db = {(d, "A"): 0.0 for d in depths}
    rows = reduce_survey(D, Db, p, "A")
    return [r.change_mm for r in rows], [r.cum_change_mm for r in rows]


def maxabs(a, b):
    return max((abs(x - y) for x, y in zip(a, b)), default=0.0)


# --------------------------------------------------------------------------------------------------------- GTILT
def test_gtilt_export_is_reduced_plot_coordinates_only():
    _need(GTILT)
    g = parse_gtilt(GTILT)
    assert g["title"] and g["initial"] and g["dates"] and set(g["cols"]) == {"A", "B"}
    txt = open(GTILT, encoding="latin-1").read()
    # (a) no raw A0/A180, no checksum, no probe sensitivity, no units, no interval header in the file
    for word in ("A0", "A180", "Checksum", "Sensitivity", "mm", "inch", "Stickup"):
        assert word.lower() not in txt.lower().replace("plot coordinates", "")
    assert len(set(map(len, g["inc"].values()))) == 1 and len(g["depths"]) == len(next(iter(g["inc"].values())))


def test_gtilt_depth_grid_unique_uniform_and_bottom_anchor_zero():
    _need(GTILT)
    g = parse_gtilt(GTILT)
    d = g["depths"]
    steps = {round(b - a, 6) for a, b in zip(d, d[1:])}
    assert len(steps) == 1 and len(set(d)) == len(d)            # no duplicates, no gaps
    assert all(v[-1] == 0.0 for v in g["inc"].values())         # deepest plotted point is the zero anchor
    assert g["decimals"] == {3}                                  # file rounding: 0.0005 (unit not stated)


def test_gtilt_app_cumulative_matches_independent_bottom_up_sum():
    """No vendor cumulative exists in this file, so this is app-vs-QA only (not a vendor oracle)."""
    _need(GTILT)
    g = parse_gtilt(GTILT)
    for v in g["inc"].values():
        chg, cum = app_reduce(g["depths"], v)
        assert maxabs(chg, v) < 1e-9
        assert maxabs(cum, cum_from_bottom(v)) < 1e-9


def test_gtilt_plot_depths_are_not_silently_taken_as_top_of_casing_interval_tops():
    """QA-042 (fixed 89d8a9c, verified 2026-11-08): vendor profiles carry no default datum. Without explicit, evidenced
    per-output depth metadata every GTILT/DigiPro label is rejected; a grid fit is never accepted as evidence."""
    _need(GTILT)
    from dam_safety.inclinometer.installation import interval_top_from_label
    from dam_safety.inclinometer.vendor_import import ImportRejected
    g = parse_gtilt(GTILT)
    for prof in ("gtilt", "digipro"):
        for meta in (None, {}, {"export_type": "plot"}):
            for d in g["depths"]:
                with pytest.raises(ImportRejected):
                    interval_top_from_label(prof, d, 1.0, g["depths"][-1], L_M, depth_metadata=meta)


# ------------------------------------------------------------------------------------------------------- DigiPro
@pytest.mark.parametrize("path,sep,qty", [(DPTXT, "\t", "Tilt_Change"), (DPCSV, ",", "Profile_Change")])
def test_digipro_export_structure(path, sep, qty):
    _need(path)
    b = parse_digipro(path, sep)
    assert set(b) == {"A", "B"}
    for blk in b.values():
        assert blk["quantity"] == qty and re.search(r"\bmm\b", blk["unit"])
        d = blk["depths"]
        assert len(set(d)) == len(d) and len({round(y - x, 6) for x, y in zip(d, d[1:])}) == 1
        dates, fmt = parse_dates(blk["date_text"])
        assert dates == sorted(dates) and len(set(dates)) == len(dates)
        assert all(v == 0.0 for v in blk["val"][0])               # first (initial) survey is the zero reference
        txt = open(path, encoding="latin-1").read()
        for word in ("A0", "A180", "Checksum", "Constant"):        # (a) no raw readings / checksums / probe constant
            assert word.lower() not in txt.lower()


def test_digipro_csv_dates_are_day_first():
    _need(DPCSV)
    for blk in parse_digipro(DPCSV, ",").values():
        assert parse_dates(blk["date_text"])[1] == "%d/%m/%Y"
        with pytest.raises(ValueError):
            [datetime.strptime(t, "%m/%d/%Y") for t in blk["date_text"]]


def test_digipro_tilt_change_roundtrip_through_app_scaling():
    """Tilt Change (= incremental displacement) through the app's L*D/(2K) path returns unchanged (units mm)."""
    _need(DPTXT)
    for blk in parse_digipro(DPTXT, "\t").values():
        for col in blk["val"]:
            chg, _ = app_reduce(blk["depths"], col)
            assert maxabs(chg, col) < 1e-9


def test_digipro_profile_change_roundtrip_through_app_cumulative():
    """Profile Change -> implied increments (difference of neighbours; deepest row keeps its own value because the
    file does not reach the fixed bottom) -> app cumulative must reproduce the vendor profile change. This checks
    the app's bottom-up, own-row-inclusive sum is consistent with DigiPro's default bottom reference; it is NOT an
    independent vendor oracle (the export has no Tilt Change for this hole)."""
    _need(DPCSV)
    for blk in parse_digipro(DPCSV, ",").values():
        for col in blk["val"]:
            inc = [col[i] - col[i + 1] for i in range(len(col) - 1)] + [col[-1]]
            _, cum = app_reduce(blk["depths"], inc)
            assert maxabs(cum, col) < 1e-9


# ----------------------------------------------------------------------------- app handling of depth anomalies
# SYNTHETIC inputs only (no vendor values). QA-043a/b/c fixed a4f67a7; xfail markers removed 2026-11-08 after
# checking flags, reasons and null cumulatives (not just that the old assertion stopped failing).
def _app_rows(cur_labels, cur_a0, cur_a180=None):
    import sys
    from dam_safety.inclinometer import batch
    inst = dict(instrument_id="I1", dam_id="D1", probe_L_m=L_M, probe_K=K, raw_unit="sin*25000", casing_depth_m=5.0,
                a0_azimuth_deg=0.0, baseline_survey_id="S0", system="probe", spiral_survey_id=None,
                stable_zone_top_m=3.0, depth_reference="top_of_casing")
    base = [dict(survey_id="S0", survey_ts="2026-01-01", depth_m=i * L_M, a0=100.0, a180=-90.0, b0=None, b180=None)
            for i in range(10)]
    cur_a180 = cur_a180 or [-90.0] * len(cur_labels)
    cur = [dict(survey_id="S1", survey_ts="2026-02-01", depth_m=d, a0=a, a180=c, b0=None, b180=None)
           for d, a, c in zip(cur_labels, cur_a0, cur_a180)]
    out, _ = batch.reduce_instrument(inst, base + cur, [], "B1", "2026-11-02T00:00:00")
    return [r for r in out if r["survey_id"] == "S1"]


def test_missing_depth_row_breaks_cumulative_above_it():
    labels = [i * L_M for i in range(10) if i != 7]
    rows = _app_rows(labels, [110.0 if d == 4.0 else 100.0 for d in labels])
    gap = [r for r in rows if r["depth_m"] == 3.5]
    assert len(gap) == 1 and "DEPTH_MISSING" in gap[0]["qa_reason"] and gap[0]["qa_flag"] != "OK"
    above = [r for r in rows if r["depth_m"] < 3.5]
    assert above and all(r["cum_a"] is None and r["qa_flag"] != "OK" for r in above)      # cumulative nulled above
    below = [r for r in rows if r["depth_m"] > 3.5]
    assert below and all(r["cum_a"] is not None and r["qa_flag"] == "OK" for r in below)


def test_duplicate_depth_row_is_flagged():
    labels = [i * L_M for i in range(10)] + [2.0]
    a0 = [100.0] * 10 + [130.0]
    a180 = [-90.0] * 10 + [-120.0]                                # same checksum, so the CS check cannot catch it
    rows = _app_rows(labels, a0, a180)
    r2 = [r for r in rows if r["depth_m"] == 2.0]
    assert len(r2) == 1 and r2[0]["qa_flag"] == "BAD" and "DEPTH_DUPLICATE" in r2[0]["qa_reason"]
    assert r2[0]["inc_a"] is None and r2[0]["cum_a"] is None                                # neither row silently wins
    assert all(r["cum_a"] is None for r in rows if r["depth_m"] < 2.0)


def test_off_grid_or_too_deep_labels_are_rejected_or_flagged():
    labels = [i * L_M + 0.2 for i in range(10)]
    rows = _app_rows(labels, [100.0] * 10)
    off = [r for r in rows if r["depth_m"] in labels]
    assert len(off) == 10 and all(r["qa_flag"] == "BAD" and "DEPTH_OFF_GRID" in r["qa_reason"] for r in off)
    assert all(r["cum_a"] is None for r in rows)
    deep = _app_rows([i * L_M for i in range(10)] + [5.0], [100.0] * 11)                 # 5.0 == casing depth
    d5 = [r for r in deep if r["depth_m"] == 5.0]
    assert len(d5) == 1 and d5[0]["qa_flag"] == "BAD" and "DEPTH_BELOW_CASING" in d5[0]["qa_reason"]
