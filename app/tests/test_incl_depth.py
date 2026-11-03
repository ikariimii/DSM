"""QA-043 a/b/c: inclinometer depth-grid QC (backend/incl_depth.py) on SYNTHETIC rows, the demo reduction, the SQL
path and the API/UI payload. Every served row is checked against the installation grid 0, L, ... < casing depth."""
import copy

import pytest
from fastapi.testclient import TestClient

from backend import formulas as fx
from backend import incl_depth as idq
from backend import seed
from backend.repos import analytics as an_mod
from backend.repos import contract as ct

from conftest import ADMIN, Client

L, CASING = 0.5, 5.0          # 10 intervals: tops 0.0 .. 4.5


def _rows(depths, inc=1.0):
    """Engine-like reduced rows (internal names), cumulative summed bottom-up over the given rows."""
    out = sorted(({"depth_m": d, "a_inc_mm": inc, "b_inc_mm": inc, "a_chk": 10.0, "b_chk": 10.0, "qa_flag": "OK",
                   "qa_reason": None, "survey_id": "S1", "instrument_id": "I1", "survey_ts": "2026-02-01T00:00:00Z",
                   "formula_id": "INCL_PROBE_2PASS", "formula_version": "1.0.0", "calib_id": None,
                   "installation_version": "2026-01-01T00:00:00Z"} for d in depths), key=lambda r: -r["depth_m"])
    acc = 0.0
    for r in out:
        acc += r["a_inc_mm"]
        r["a_cum_mm"] = r["b_cum_mm"] = acc
        r["resultant_mm"] = acc * 2 ** 0.5
    return out


def _by(rows):
    return {r["depth_m"]: r for r in rows}


def test_expected_tops_from_casing_and_L():
    assert idq.expected_tops(5.0, 0.5) == [round(0.5 * i, 9) for i in range(10)]
    assert idq.expected_tops(30 * 0.3048, 2 * 0.3048)[-1] == pytest.approx(28 * 0.3048)
    for bad in ((5.2, 0.5), (None, 0.5), (5.0, None), (0, 0.5), (5.0, -1)):
        with pytest.raises(ValueError):
            idq.expected_tops(*bad)


def test_clean_grid_is_unchanged():
    rows = _rows([i * L for i in range(10)])
    before = copy.deepcopy(_by(rows))
    out = idq.check_profile(rows, CASING, L)
    assert [r["depth_m"] for r in out] == [i * L for i in range(10)]
    assert all(r["qa_flag"] == "OK" and not r["qa_reason"] for r in out)
    assert _by(out)[0.0]["a_cum_mm"] == before[0.0]["a_cum_mm"] == 10.0


def test_043a_missing_row_is_flagged_and_breaks_cumulative_above_it():
    rows = _rows([i * L for i in range(10) if i != 7])        # 3.5 m absent
    out = _by(idq.check_profile(rows, CASING, L))
    gap = out[3.5]
    assert gap["qa_flag"] == "MISSING" and gap["qa_reason"] == idq.DEPTH_MISSING and gap["depth_placeholder"]
    assert gap["a_inc_mm"] is None and gap["a_cum_mm"] is None and gap["survey_id"] == "S1"
    for d, r in out.items():
        if d < 3.5:                                             # above the gap: no cumulative, own increment kept
            assert r["a_cum_mm"] is None and r["b_cum_mm"] is None and r["resultant_mm"] is None
            assert idq.CUM_GAP_BELOW in r["qa_reason"] and r["a_inc_mm"] == 1.0
        elif d > 3.5:                                           # below: summed from the fixed bottom, unaffected
            assert r["a_cum_mm"] == pytest.approx((CASING - d) / L) and not r["qa_reason"]


def test_043b_duplicate_depth_is_flagged_not_last_wins():
    rows = _rows([i * L for i in range(10)])
    dup = dict(_by(rows)[2.0], a_inc_mm=9.0)
    out = idq.check_profile(rows + [dup], CASING, L)
    at2 = [r for r in out if r["depth_m"] == 2.0]
    assert len(at2) == 2                                        # both copies stay visible, neither is used
    assert all(r["qa_flag"] == "BAD" and idq.DEPTH_DUPLICATE in r["qa_reason"] and r["a_inc_mm"] is None for r in at2)
    assert all(r["a_cum_mm"] is None for r in out if r["depth_m"] <= 2.0)
    assert all(r["a_cum_mm"] is not None for r in out if r["depth_m"] > 2.0)


def test_043c_off_grid_and_below_casing_rows_are_flagged():
    out = idq.check_profile(_rows([i * L + 0.2 for i in range(10)]), CASING, L)
    off = [r for r in out if not r.get("depth_placeholder")]
    assert len(off) == 10 and all(r["qa_flag"] == "BAD" and idq.DEPTH_OFF_GRID in r["qa_reason"] for r in off)
    assert all(r[k] is None for r in off for k in ("a_inc_mm", "a_cum_mm", "resultant_mm", "a_chk"))
    ph = [r for r in out if r.get("depth_placeholder")]
    assert sorted(r["depth_m"] for r in ph) == [i * L for i in range(10)]   # every expected top is MISSING
    assert all(r["qa_flag"] == "MISSING" for r in ph)
    out = _by(idq.check_profile(_rows([i * L for i in range(11)]), CASING, L))   # 5.0 m: below the casing
    assert out[5.0]["qa_flag"] == "BAD" and idq.DEPTH_BELOW_CASING in out[5.0]["qa_reason"]
    assert out[5.0]["a_cum_mm"] is None
    # the rejected 5.0 m row may already be inside the upstream sum: every cumulative above it is withheld
    assert all(out[i * L]["a_cum_mm"] is None and idq.CUM_GAP_BELOW in out[i * L]["qa_reason"] for i in range(10))
    out = _by(idq.check_profile(_rows([i * L for i in range(10)] + [2.2]), CASING, L))   # off-grid at 2.2 m
    assert all(out[i * L]["a_cum_mm"] is None for i in range(5)) and out[2.5]["a_cum_mm"] is not None
    out = _by(idq.check_profile(_rows([-0.5] + [i * L for i in range(10)]), CASING, L))
    assert out[-0.5]["qa_flag"] == "BAD" and idq.DEPTH_OFF_GRID in out[-0.5]["qa_reason"]


def test_unknown_grid_withholds_everything():
    for casing, l in ((None, L), (5.2, L), (CASING, None)):
        out = idq.check_profile(_rows([i * L for i in range(10)]), casing, l)
        assert all(r["qa_flag"] == "BAD" and idq.DEPTH_GRID_UNKNOWN in r["qa_reason"] for r in out)
        assert all(r["a_cum_mm"] is None and r["a_inc_mm"] is None for r in out)


def test_flagged_row_already_bad_breaks_cumulative_and_keeps_worse_flag():
    rows = _rows([i * L for i in range(10)])
    _by(rows)[4.0]["qa_flag"] = "BAD"
    out = _by(idq.check_profile(rows, CASING, L))
    assert out[4.0]["qa_flag"] == "BAD"
    assert all(out[d]["a_cum_mm"] is None for d in out if d <= 4.0)
    assert out[4.5]["a_cum_mm"] == 1.0
    r = {"qa_flag": "BAD", "qa_reason": "X"}
    idq._withhold(r, "MISSING", idq.DEPTH_MISSING)
    assert r["qa_flag"] == "BAD" and r["qa_reason"] == "X,DEPTH_MISSING"


def test_unparseable_depth_is_off_grid():
    rows = _rows([i * L for i in range(10)])
    rows.append(dict(rows[0], depth_m="n/a"))
    out = idq.check_profile(rows, CASING, L)
    bad = [r for r in out if r["depth_m"] == "n/a"][0]
    assert bad["qa_flag"] == "BAD" and idq.DEPTH_OFF_GRID in bad["qa_reason"]
    assert all(r.get("a_cum_mm") is None for r in out)            # unknown depth: no cumulative can be trusted


def test_reason_codes_are_in_the_app_vocabulary():
    for code in (idq.DEPTH_MISSING, idq.DEPTH_DUPLICATE, idq.DEPTH_OFF_GRID, idq.DEPTH_BELOW_CASING,
                 idq.DEPTH_GRID_UNKNOWN, idq.CUM_GAP_BELOW, "BASELINE_DEPTH_MISSING", "DEPTH_LABEL_INVALID"):
        d = ct.qa_reason_detail(code)[0]
        assert d["known"] and d["severity"] in ("BAD", "MISSING")


def test_demo_reduction_missing_baseline_depth_is_not_an_implicit_zero():
    base = {0.0: (0.0, 0.0), 0.5: (0.0, 0.0)}
    rows = [{"depth_m": d, "a0": 10, "a180": -10, "b0": 0, "b180": 0} for d in (0.0, 0.5, 1.0)]
    prof = {r["depth_m"]: r for r in fx.reduce_inclinometer_profile(rows, base, 500.0, 25000.0)}
    assert prof[1.0]["a_inc_mm"] is None
    assert prof[0.5]["a_cum_mm"] is None and prof[0.0]["a_cum_mm"] is None and prof[0.0]["resultant_mm"] is None


def _demo(mutate):
    d = seed.build()
    s = [x for x in d["incl_surveys"] if x["instrument_id"] == "CRD-IN-1"][-1]
    mutate(s["rows"])
    return d, s["survey_id"]


def test_demo_seed_is_on_the_grid():
    m = an_mod.MockAnalytics(seed.build())
    for s in m.incl_surveys("CRD-IN-1"):
        prof = m.incl_profile("CRD-IN-1", s["survey_id"])
        assert [r["depth_m"] for r in prof] == idq.expected_tops(30.0, 0.5)
        assert not any(c.startswith(("DEPTH_", "CUM_")) for r in prof for c in ct.qa_reason_codes(r["qa_reason"]))


def test_mock_profile_flags_missing_duplicate_off_grid():
    d, sid = _demo(lambda rows: rows.pop(20))                 # 10.0 m
    prof = {r["depth_m"]: r for r in an_mod.MockAnalytics(d).incl_profile("CRD-IN-1", sid)}
    assert prof[10.0]["qa_flag"] == "MISSING" and prof[10.0]["installation_version"]
    assert all(prof[x]["a_cum_mm"] is None for x in prof if x <= 10.0)
    assert all(prof[x]["a_cum_mm"] is not None for x in prof if x > 10.0)
    d, sid = _demo(lambda rows: rows.append(dict(rows[4])))   # 2.0 m twice
    prof = an_mod.MockAnalytics(d).incl_profile("CRD-IN-1", sid)
    assert [r["qa_flag"] for r in prof if r["depth_m"] == 2.0] == ["BAD", "BAD"]
    d, sid = _demo(lambda rows: rows.append(dict(rows[0], depth_m=30.0)))
    prof = {r["depth_m"]: r for r in an_mod.MockAnalytics(d).incl_profile("CRD-IN-1", sid)}
    assert DEPTH_BELOW in prof[30.0]["qa_reason"] and prof[0.0]["a_cum_mm"] is None


DEPTH_BELOW = idq.DEPTH_BELOW_CASING


def test_api_profile_shows_flags_and_no_cumulative_across_gap(demo_settings):
    from backend.main import create_app
    d, sid = _demo(lambda rows: rows.pop(20))
    app = create_app(demo_settings)
    app.state.analytics_factory = lambda token, _m=an_mod.MockAnalytics(d): _m
    rc = Client(TestClient(app))
    p = rc.get(f"/api/instruments/CRD-IN-1/incl/profile?survey_ids={sid}", user=ADMIN).json()["profiles"][0]
    rows = {r["depth_m"]: r for r in p["rows"]}
    assert rows[10.0]["qa_flag"] == "MISSING" and rows[10.0]["display_status"] == "MISSING"
    assert rows[10.0]["cum_a"] is None and rows[10.0]["inc_a"] is None
    assert all(rows[x]["cum_a"] is None and rows[x]["resultant"] is None for x in rows if x < 10.0)
    assert all(rows[x]["inc_a"] is not None for x in rows if x < 10.0)          # increments above stay shown
    assert all(rows[x]["cum_a"] is not None for x in rows if x > 10.0)
    codes = {q["code"]: q for q in p["qa_reasons"]}
    assert codes["DEPTH_MISSING"]["severity"] == "MISSING" and codes["CUM_GAP_BELOW"]["known"]
    assert p["qa_flag"].get("MISSING") == 1


def test_sql_profile_is_rechecked_against_installation_grid():
    class Fake(an_mod.SqlAnalytics):
        def __init__(self, inst):
            self.inst, self.calls = inst, []

        def t(self, *p):
            return ".".join(p)

        def q(self, sql, params=None):
            self.calls.append(sql)
            if "incl_reduced" in sql:
                return [{**r, "cum_a": r.pop("a_cum_mm"), "cum_b": r.pop("b_cum_mm"),
                         "resultant": r.pop("resultant_mm"), "inc_a": r.pop("a_inc_mm"), "inc_b": r.pop("b_inc_mm")}
                        for r in _rows([i * L for i in range(10) if i != 7])]
            return self.inst

    good = [{"instrument_id": "I1", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
             "casing_depth_m": CASING, "probe_L_m": L}]
    f = Fake(good)
    prof = {r["depth_m"]: r for r in f.incl_profile("I1", "S1")}
    assert prof[3.5]["qa_flag"] == "MISSING" and prof[0.0]["a_cum_mm"] is None and prof[4.0]["a_cum_mm"] == 2.0
    assert any("casing_depth_m, probe_L_m" in s for s in f.calls)
    for inst in ([], good + [dict(good[0])], [dict(good[0], effective_to="garbage")]):   # none / overlap / bad end
        out = Fake(inst).incl_profile("I1", "S1")
        assert all(idq.DEPTH_GRID_UNKNOWN in r["qa_reason"] for r in out)

    class Empty(Fake):
        def q(self, sql, params=None):
            return []
    assert Empty(good).incl_profile("I1", "S1") == []
