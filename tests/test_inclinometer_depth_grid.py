"""QA-043 a/b/c (proposed with the batch.py patch): depth-grid QC in the silver.incl_reduced core. SYNTHETIC rows."""
import pytest

from dam_safety.inclinometer import batch

L = 0.5
INST = dict(instrument_id="I1", dam_id="D1", probe_L_m=L, probe_K=25000.0, raw_unit="sin*25000", casing_depth_m=5.0,
            a0_azimuth_deg=0.0, baseline_survey_id="S0", system="probe", spiral_survey_id=None,
            stable_zone_top_m=3.0, depth_reference="top_of_casing")


def _run(cur, base=None, inst=INST):
    base = base if base is not None else [i * L for i in range(10)]
    rows = [dict(survey_id="S0", survey_ts="2026-01-01", depth_m=d, a0=100.0, a180=-90.0, b0=None, b180=None)
            for d in base]
    rows += [dict(survey_id="S1", survey_ts="2026-02-01", depth_m=d, a0=a, a180=-90.0, b0=None, b180=None)
             for d, a in cur]
    out, _ = batch.reduce_instrument(inst, rows, [], "B1", "2026-11-02T00:00:00")
    return [r for r in out if r["survey_id"] == "S1"]


def _by(rows):
    return {r["depth_m"]: r for r in rows}


def test_clean_survey_has_no_depth_flags_and_full_cumulative():
    rows = _by(_run([(i * L, 101.0) for i in range(10)]))
    assert sorted(rows) == [i * L for i in range(10)]
    assert all("DEPTH_" not in (r["qa_reason"] or "") and "CUM_GAP" not in (r["qa_reason"] or "") for r in rows.values())
    assert rows[0.0]["cum_a"] is not None and rows[0.0]["cum_a"] == pytest.approx(10 * rows[4.5]["inc_a"])


def test_missing_row_is_emitted_missing_and_cumulative_is_cut_above():
    rows = _by(_run([(i * L, 101.0) for i in range(10) if i != 7]))
    assert rows[3.5]["qa_flag"] == "MISSING" and "DEPTH_MISSING" in rows[3.5]["qa_reason"] and rows[3.5]["inc_a"] is None
    for d, r in rows.items():
        if d < 3.5:
            assert r["cum_a"] is None and r["resultant"] is None and "CUM_GAP_BELOW" in r["qa_reason"]
            assert r["inc_a"] is not None
        elif d > 3.5:
            assert r["cum_a"] is not None


def test_duplicate_rows_are_both_bad_and_not_reduced():
    out = _run([(i * L, 101.0) for i in range(10)] + [(2.0, 130.0)])
    r2 = [r for r in out if r["depth_m"] == 2.0]
    assert len(r2) == 1 and r2[0]["qa_flag"] == "BAD" and "DEPTH_DUPLICATE" in r2[0]["qa_reason"]
    assert r2[0]["inc_a"] is None and all(r["cum_a"] is None for r in out if r["depth_m"] <= 2.0)


def test_off_grid_and_below_casing_labels_are_bad():
    rows = _by(_run([(i * L, 101.0) for i in range(10)] + [(1.2, 101.0), (5.0, 101.0)]))
    assert rows[1.2]["qa_flag"] == "BAD" and "DEPTH_OFF_GRID" in rows[1.2]["qa_reason"] and rows[1.2]["inc_a"] is None
    assert rows[5.0]["qa_flag"] == "BAD" and "DEPTH_BELOW_CASING" in rows[5.0]["qa_reason"]
    assert rows[0.0]["cum_a"] is not None                       # rejected rows were never summed: grid itself intact
    rows = _by(_run([(-0.5, 101.0)] + [(i * L, 101.0) for i in range(10)]))
    assert rows[-0.5]["qa_flag"] == "BAD" and "DEPTH_OFF_GRID" in rows[-0.5]["qa_reason"]


def test_baseline_gap_cuts_cumulative_of_later_surveys():
    rows = _by(_run([(i * L, 101.0) for i in range(10)], base=[i * L for i in range(10) if i != 6]))
    assert "BASELINE_DEPTH_MISSING" in rows[3.0]["qa_reason"] and rows[3.0]["qa_flag"] != "OK"
    assert all(rows[d]["cum_a"] is None for d in rows if d <= 3.0)


def test_unknown_grid_rejects_every_row():
    rows = _run([(i * L, 101.0) for i in range(10)], inst=dict(INST, casing_depth_m=5.2))
    assert rows and all(r["qa_flag"] == "BAD" and "DEPTH_GRID_UNKNOWN" in r["qa_reason"] and r["cum_a"] is None
                        for r in rows)


def test_non_numeric_label_flags_the_survey():
    rows = _run([(i * L, 101.0) for i in range(10)] + [("n/a", 101.0)])
    assert rows and all("DEPTH_LABEL_INVALID" in r["qa_reason"] and r["qa_flag"] == "BAD" for r in rows)


@pytest.mark.parametrize("label", [-0.5, float("nan"), float("inf"), None])
def test_invalid_labels_do_not_crash_or_alias_a_valid_interval(label):
    rows = _run([(i * L, 101.0) for i in range(10)] + [(label, 101.0)])
    assert any(r["qa_flag"] == "BAD" for r in rows)
    assert _by(rows)[4.5]["inc_a"] is not None


def test_duplicate_order_never_selects_a_winner():
    cur = [(i * L, 101.0) for i in range(10)] + [(2.0, 130.0)]
    forward, reverse = _by(_run(cur)), _by(_run(list(reversed(cur))))
    for rows in (forward, reverse):
        assert rows[2.0]["inc_a"] is None and rows[2.0]["diff_a"] is None
        assert "DEPTH_DUPLICATE" in rows[2.0]["qa_reason"]
    assert [r["cum_a"] for r in forward.values()] == [r["cum_a"] for r in reverse.values()]


def test_missing_one_axis_breaks_only_that_axis_cumulative():
    base, current = [], []
    for i in range(10):
        d = i * L
        base.append(dict(survey_id="S0", survey_ts="2026-01-01", depth_m=d,
                         a0=100, a180=-90, b0=100, b180=-90))
        current.append(dict(survey_id="S1", survey_ts="2026-02-01", depth_m=d,
                            a0=101, a180=-90, b0=None if i == 7 else 101,
                            b180=None if i == 7 else -90))
    out, _ = batch.reduce_instrument(INST, base + current, [], "B", "t")
    rows = _by([r for r in out if r["survey_id"] == "S1"])
    assert rows[0.0]["cum_a"] is not None and rows[0.0]["cum_b"] is None
    assert rows[0.0]["qa_flag"] != "OK" and "CUM_GAP_BELOW_B" in rows[0.0]["qa_reason"]
    assert rows[4.0]["cum_b"] is not None


def test_explicit_bad_pass_breaks_cumulative_and_is_not_ok():
    rows = _by(_run([(i * L, "invalid" if i == 7 else 101) for i in range(10)]))
    assert rows[3.5]["qa_flag"] == "BAD" and rows[3.5]["inc_a"] is None
    assert rows[0.0]["cum_a"] is None and rows[0.0]["qa_flag"] != "OK"


@pytest.mark.parametrize("depth", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_expected_grid_fails_closed(depth):
    rows = _run([(i * L, 101.0) for i in range(10)], inst=dict(INST, casing_depth_m=depth))
    assert rows and all(r["qa_flag"] == "BAD" and r["cum_a"] is None for r in rows)
