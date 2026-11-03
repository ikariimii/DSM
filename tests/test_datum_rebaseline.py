"""DATUM_CHANGE re-baseline.
Sources: /workspace/dam-refs/settlement-survey/verify.py Ex15 (series D1 100.0000, D1 99.9950,
D2 100.0410, D2 100.0380 m -> DATUM_CHANGE at epoch 3, settlement 5.0 mm then 8.0 mm, 'not 41 mm
heave'; DWR-OROVILLE-12D-2021 PDF p8 R-27b, p18 R-9b); /workspace/dam-refs/procedures/
settlement-survey-gnss.md §0 and §7 (DATUM_CHANGE flag SUSPECT until re-baselined);
/workspace/dam-refs/CONVENTIONS.md 'Schema additions'."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from dam_safety.alarms import AlarmState, ThresholdSpec, run_series
from dam_safety.datum import rebaseline_series, state_from_history
from dam_safety.spark import pandas_ops as po

SERIES = [("D1", 100.0000), ("D1", 99.9950), ("D2", 100.0410), ("D2", 100.0380)]
T0 = datetime(2026, 1, 1)


def _rows():
    return [{"ts_utc": T0 + timedelta(days=30 * i), "raw_value": v, "datum_id": d} for i, (d, v) in enumerate(SERIES)]


def test_ex15_pure():
    out, st = rebaseline_series(_rows(), sign_mult=-1, raw_unit="m")
    assert [o["datum_change"] for o in out] == [False, False, True, False]
    assert out[1]["eng_value"] == pytest.approx(5.0, abs=1e-6)
    assert out[2]["eng_value"] == pytest.approx(5.0, abs=1e-6)     # not -41 mm heave
    assert out[3]["eng_value"] == pytest.approx(8.0, abs=1e-6)
    assert set(st.baselines) == {"D1", "D2"}


def test_ex15_incremental_matches_batch():
    # same answer when epoch 4 arrives in a later batch (state rebuilt from reduced history)
    full, _ = rebaseline_series(_rows(), -1, "m")
    first, _ = rebaseline_series(_rows()[:3], -1, "m")
    hist = [{**r, "eng_value": o["eng_value"]} for r, o in zip(_rows()[:3], first)]
    later, _ = rebaseline_series(_rows()[3:], -1, "m", state=state_from_history(hist))
    assert later[0]["eng_value"] == pytest.approx(full[3]["eng_value"], abs=1e-9)
    assert later[0]["datum_change"] is False


def _pdf():
    return pd.DataFrame([{"reading_id": f"r{i}", "dam_id": "CRD", "sensor_id": "SP1", "instrument_id": "SP1",
                          **r, "raw_unit": "m", "calib_id": "c1", "cal_version": 1,
                          "formula_id": "SETTLEMENT_DELTA", "formula_version": "1.0.0",
                          "params": [("raw_axis", "up")], "sign_mult": -1, "offset": 0.0}
                         for i, r in enumerate(_rows())])


def test_ex15_through_pandas_pipeline_flags_suspect_datum_change():
    red = po.reduce_readings_pdf(_pdf())
    assert red["raw_value"].tolist() == [v for _, v in SERIES]          # raw untouched
    q = po.qc_pdf(red, qc_params={"spike_min_abs": 1e9})
    assert q.loc[2, "qa_reason"] == "DATUM_CHANGE" and q.loc[2, "qa_flag"] == "SUSPECT"
    assert q["corrected_value"].tolist() == pytest.approx([0.0, 5.0, 5.0, 8.0], abs=1e-6)


def test_datum_change_flag_on_non_settlement_series():
    # generic: any instrument whose datum_id changes is flagged (no re-baseline formula)
    from dam_safety.datum import datum_changes
    assert datum_changes(["A", "A", "B", "B", "A"]) == [False, False, True, False, True]
    assert datum_changes(["B"], prev="A", have_prev=True) == [True]


def test_datum_change_resets_alarm_rate_baseline():
    # a datum jump must not be evaluated as rate-of-change movement (CONVENTIONS.md 'Schema additions')
    spec = ThresholdSpec("rate1", 1, 1, "rate", "high", 100.0)
    rows = [{"ts_utc": T0, "value": 0.0, "qa_flag": "OK"},
            {"ts_utc": T0 + timedelta(days=1), "value": 500.0, "qa_flag": "SUSPECT", "qa_reason": "DATUM_CHANGE"}]
    st, trs = run_series("S", rows, [spec], AlarmState("S"))
    assert trs == [] and st.state == "NORMAL"
    rows[1]["qa_reason"] = None
    st2, trs2 = run_series("S", rows, [spec], AlarmState("S"))
    assert trs2, "control: without DATUM_CHANGE the same jump would raise"
