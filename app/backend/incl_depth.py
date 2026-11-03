"""QA-043: inclinometer depth-grid QC applied to every served profile (demo and silver.incl_reduced).

The expected interval tops are 0, L, 2L, ... while top + L <= casing depth (same rule as the engine's
Installation.interval_tops_m), all measured down from the top of casing and taken from the ref.incl_installation
row valid at survey time. Against that grid, each served row is checked:

  DEPTH_OFF_GRID      label not on the L grid (or negative)                  -> qa_flag BAD, no values
  DEPTH_BELOW_CASING  on the grid but the interval would extend below casing -> qa_flag BAD, no values
  DEPTH_DUPLICATE     more than one row for one interval top in one survey  -> every copy BAD, no values
  DEPTH_MISSING       an expected top has no row: a placeholder row is added  -> qa_flag MISSING, no values
  DEPTH_GRID_UNKNOWN  casing depth / L missing or not a whole number of intervals -> every row BAD

The cumulative is summed from the fixed bottom, so any interval that is not a single valid on-grid row breaks
every cumulative value above it. Those rows keep their own incremental values but cum_a / cum_b / resultant are
nulled (CUM_GAP_BELOW). No implicit zero is ever used across a gap (REPORT E9: a missing interval makes every
cumulative value above it MISSING). Depth semantics (stickup, midpoints, vendor depth references) are QA-042 and
are NOT changed here."""
from __future__ import annotations

TOL_M = 1e-6
DEPTH_OFF_GRID, DEPTH_BELOW_CASING = "DEPTH_OFF_GRID", "DEPTH_BELOW_CASING"
DEPTH_DUPLICATE, DEPTH_MISSING, DEPTH_GRID_UNKNOWN = "DEPTH_DUPLICATE", "DEPTH_MISSING", "DEPTH_GRID_UNKNOWN"
CUM_GAP_BELOW = "CUM_GAP_BELOW"
VALUE_KEYS = ("a_inc_mm", "b_inc_mm", "a_cum_mm", "b_cum_mm", "resultant_mm", "a_chk", "b_chk", "a_chk_dev",
              "b_chk_dev", "dev_a", "dev_b", "diff_a", "diff_b")
CUM_KEYS = ("a_cum_mm", "b_cum_mm", "resultant_mm")
_SEV = {"OK": 0, "SUSPECT": 1, "MISSING": 2, "BAD": 3}
# survey-level lineage copied onto a DEPTH_MISSING placeholder so the display gate treats it like any other row
_LINEAGE = ("instrument_id", "dam_id", "survey_id", "survey_ts", "calib_id", "cal_version", "installation_version",
            "probe_constants", "formula_id", "formula_version", "eng_unit", "qc_rule_version", "depth_reference",
            "baseline_survey_id", "recompute_batch_id", "reduced_ts", "correction_basis")


def expected_tops(casing_depth_m, L_m) -> list[float]:
    """Interval tops, top-down. ValueError when the grid cannot be built (never guessed)."""
    try:
        c, L = float(casing_depth_m), float(L_m)
    except (TypeError, ValueError):
        raise ValueError("casing_depth_m / probe_L_m missing") from None
    if not (c > 0 and L > 0):
        raise ValueError("casing_depth_m and probe_L_m must be positive")
    n = int(round(c / L))
    if abs(n * L - c) > TOL_M:
        raise ValueError("casing depth is not a whole number of intervals")
    return [round(i * L, 9) for i in range(n)]


def _add_reason(row: dict, code: str) -> None:
    codes = [x.strip() for x in str(row.get("qa_reason") or "").split(",") if x.strip()]
    if code not in codes:
        codes.append(code)
    row["qa_reason"] = ",".join(codes)


def _set_flag(row: dict, flag: str) -> None:
    cur = (row.get("qa_flag") or "OK").upper()
    row["qa_flag"] = flag if _SEV.get(flag, 0) >= _SEV.get(cur, 0) else cur


def _withhold(row: dict, flag: str, code: str) -> None:
    _set_flag(row, flag)
    _add_reason(row, code)
    for k in VALUE_KEYS:
        if k in row:
            row[k] = None
    row["qc_checksum_ok"] = None


def check_profile(rows: list[dict], casing_depth_m, L_m) -> list[dict]:
    """Rows of ONE survey -> rows top-down with depth QC applied (input rows are modified in place; placeholders
    are new dicts). Rows are never dropped: a flagged row stays visible with its flag and no values."""
    rows = list(rows)
    try:
        tops = expected_tops(casing_depth_m, L_m)
    except ValueError:
        for r in rows:
            _withhold(r, "BAD", DEPTH_GRID_UNKNOWN)
            for k in CUM_KEYS:
                if k in r:
                    r[k] = None
        return sorted(rows, key=lambda r: float(r.get("depth_m") or 0.0))
    L = float(L_m)
    slot: dict[int, list[dict]] = {}
    # a rejected row may already be inside the upstream bottom-up sum, so every cumulative at or above its depth is
    # contaminated: `contaminated_from` = deepest such depth (inf = unknown depth -> whole profile)
    contaminated_from = None
    for r in rows:
        try:
            d = float(r.get("depth_m"))
        except (TypeError, ValueError):
            _withhold(r, "BAD", DEPTH_OFF_GRID)
            contaminated_from = float("inf")
            continue
        i = int(round(d / L))
        if d < -TOL_M or abs(i * L - d) > TOL_M:
            _withhold(r, "BAD", DEPTH_OFF_GRID)
        elif i >= len(tops):
            _withhold(r, "BAD", DEPTH_BELOW_CASING)
        else:
            slot.setdefault(i, []).append(r)
            continue
        contaminated_from = d if contaminated_from is None else max(contaminated_from, d)
    for i, rs in slot.items():
        if len(rs) > 1:
            for r in rs:
                _withhold(r, "BAD", DEPTH_DUPLICATE)
    out = list(rows)
    template = rows[0] if rows else {}
    for i, top in enumerate(tops):
        if i not in slot:
            ph = {k: template.get(k) for k in _LINEAGE if k in template}
            ph.update(depth_m=top, qa_flag="MISSING", qa_reason=DEPTH_MISSING, correction_record_ids=[],
                      **{k: None for k in VALUE_KEYS}, qc_checksum_ok=None, depth_placeholder=True)
            slot[i] = [ph]
            out.append(ph)
    # bottom -> top: once an interval is not a single valid row, every cumulative above it is withheld
    broken = False
    for i in range(len(tops) - 1, -1, -1):
        rs = slot[i]
        bad = len(rs) != 1 or rs[0].get("depth_placeholder") or (rs[0].get("qa_flag") or "").upper() in ("BAD", "MISSING")
        if bad or (contaminated_from is not None and tops[i] <= contaminated_from + TOL_M):
            broken = True
        if broken:
            for r in rs:
                for k in CUM_KEYS:
                    if k in r and r[k] is not None:
                        r[k] = None
                        _add_reason(r, CUM_GAP_BELOW)
                if not bad:
                    _add_reason(r, CUM_GAP_BELOW)
    return sorted(out, key=lambda r: (float(r["depth_m"]) if isinstance(r.get("depth_m"), (int, float)) else 1e18))
