"""pandas wrappers (what Spark applyInPandas calls). Values re-use the GEOKON-4500 Eq.2 PDF p.22
example [/workspace/dam-refs/4500_Piezometer.pdf] and DGSI p.12 [/workspace/dam-refs/DGSI_Digitilt_Probe.pdf]."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from dam_safety.spark import pandas_ops as po


def test_reduce_readings_pdf_vw_and_lineage():
    pdf = pd.DataFrame([{"reading_id": "r1", "sensor_id": "S1", "instrument_id": "I1", "ts_utc": datetime(2025, 1, 1),
                         "raw_value": 6749.0, "raw_unit": "digits", "calib_id": "c1", "cal_version": 1,
                         "formula_id": "VW_PIEZO", "formula_version": "1.0.0",
                         "params": [("method", "linear"), ("G", "0.1795"), ("R0", "9082"), ("sensor_elev_m", "100")],
                         "sign_mult": -1, "offset": 0.0}])
    out = po.reduce_readings_pdf(pdf)
    assert list(out.columns) == po.REDUCED_COLUMNS
    assert out.loc[0, "eng_value"] == pytest.approx(418.8, abs=0.05)
    assert out.loc[0, "corrected_value"] == pytest.approx(100 + 42.70, abs=0.05)
    assert out.loc[0, "calib_id"] == "c1"


def test_reduce_without_calibration_is_null():
    pdf = pd.DataFrame([{"reading_id": "r1", "sensor_id": "S1", "instrument_id": "I1", "ts_utc": datetime(2025, 1, 1),
                         "raw_value": 1.0, "raw_unit": "digits", "calib_id": None, "cal_version": None,
                         "formula_id": None, "formula_version": None, "params": None}])
    out = po.reduce_readings_pdf(pdf)
    assert out.loc[0, "corrected_value"] is None


def test_qc_pdf_range_and_history_drop():
    t = [datetime(2025, 1, 1) + timedelta(hours=i) for i in range(5)]
    pdf = pd.DataFrame({"ts_utc": t, "corrected_value": [1, 2, 3, 99, 4.0], "min_valid": 0, "max_valid": 10})
    out = po.qc_pdf(pdf, {"stuck_count": 100}, history=1)
    assert len(out) == 4 and out.loc[2, "qa_flag"] == "BAD"


def test_incl_profile_pdf():
    base = dict(instrument_id="IN1", interval_m=0.6096, k=20000, fixed_end="bottom", checksum_tol=50.0, b0=0, b180=0)
    rows = [dict(base, depth_m=1.0, a0=349, a180=-349, is_initial=True, survey_ts=None),
            dict(base, depth_m=1.0, a0=350, a180=-350, is_initial=False, survey_ts=datetime(2025, 1, 1))]
    out = po.incl_profile_pdf(pd.DataFrame(rows))
    # 0.6096 m = 24 in; diff 700 vs 698 -> 0.0012 in = 0.03048 mm   (DGSI p.12)
    assert out.loc[0, "inc_a"] == pytest.approx(0.0012 * 25.4, abs=1e-4)


def test_alarms_pdf():
    t = [datetime(2025, 1, 1) + timedelta(hours=i) for i in range(3)]
    pdf = pd.DataFrame({"sensor_id": "S1", "ts_utc": t, "value": [1, 11, 11.0], "qa_flag": "OK"})
    specs = {"S1": [dict(threshold_id="T", version=1, level=1, limit_type="magnitude", direction="high", value=10)]}
    trs, st = po.alarms_pdf(pdf, specs, {}, {})
    assert list(trs["event"]) == ["RAISE"] and st.state == "ALERT_UNACK"
