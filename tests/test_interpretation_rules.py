"""Fault classifier, PFM rules, grounded commentary.
Sources: /workspace/dam-refs/Salazar_2021_Anomaly_Detection_Water.pdf p2 (isolated-device errors);
/workspace/dam-refs/FERC_Eng_Guidelines_Ch14.pdf PDF p34 (SMP per Category II PFM), p36 (verify by more
frequent readings; statistics no substitute for judgment), p37 (redundant data; calibration paramount);
/workspace/dam-refs/formula_library.md §8; labelled real data
/workspace/dam-refs/datasets/piezometers/boddington/ + piezometers/cases.csv (BOD-RA-01..03)."""
import csv
import datetime as dt
import json
import os

import numpy as np
import pytest

from conftest import DATASETS
from dam_safety.interpretation import commentary, pfm_rules
from dam_safety.interpretation.fault_classifier import Evidence, classify


def test_within_band_is_normal():
    assert classify(Evidence(z=1.5)).label == "NORMAL"


def test_isolated_transient_is_sensor_fault():
    """Salazar-2021 p2 + FERC Ch14 p37 redundant check + FERC p36 transient."""
    c = classify(Evidence(z=6.0, neighbour_z=[0.3, -0.5], run_length=1, returned_next=True))
    assert c.label == "SENSOR_FAULT" and "ISOLATED" in c.reasons and "TRANSIENT" in c.reasons
    assert 0 < c.confidence <= 0.9


def test_corroborated_persistent_is_real():
    c = classify(Evidence(z=3.5, neighbour_z=[2.8, 3.1, 0.2], run_length=5))
    assert c.label == "REAL_BEHAVIOUR" and any(r.startswith("CORROBORATED") for r in c.reasons)


def test_opposite_sign_neighbours_do_not_corroborate():
    c = classify(Evidence(z=4.0, neighbour_z=[-3.0], run_length=1, returned_next=True))
    assert c.label != "REAL_BEHAVIOUR"


def test_qc_flag_dominates():
    """formula_library.md §8: data QA first."""
    c = classify(Evidence(z=8, qa_reason="SPIKE", neighbour_z=[2.5], run_length=1))
    assert c.label == "SENSOR_FAULT" and "QC_FAULT" in c.reasons


def test_datum_or_calibration_change_is_indeterminate():
    """FERC Ch14 PDF p37: check calibration first ('paramount')."""
    c = classify(Evidence(z=5, neighbour_z=[3, 3], run_length=10, datum_or_calibration_change=True))
    assert c.label == "INDETERMINATE" and "CHECK_CALIBRATION_FIRST" in c.reasons


def test_no_redundancy_halves_confidence():
    a = classify(Evidence(z=5, neighbour_z=[], run_length=5))
    b = classify(Evidence(z=5, neighbour_z=[0.1], run_length=5))
    assert "NO_REDUNDANCY" in a.reasons and a.confidence < 0.5


def test_signatures_synthetic():
    t = np.arange(60.0)
    rng = np.random.default_rng(0)
    assert pfm_rules.signature_matches("RISING_TREND", t, 0.05 * t + rng.normal(0, 0.1, 60))[0]
    assert not pfm_rules.signature_matches("RISING_TREND", t, rng.normal(0, 0.1, 60))[0]
    assert pfm_rules.signature_matches("FALLING_TREND", t, -0.05 * t + rng.normal(0, 0.1, 60))[0]
    assert pfm_rules.signature_matches("ACCELERATING_DISPLACEMENT", t, 0.002 * t ** 2 + rng.normal(0, 0.05, 60))[0]
    assert not pfm_rules.signature_matches("ACCELERATING_DISPLACEMENT", t, 0.05 * t + rng.normal(0, 0.05, 60))[0]
    pool = np.sin(t / 10)
    ok, st = pfm_rules.signature_matches("RISING_HEAD_WITH_POOL", t, 0.02 * t + pool + rng.normal(0, 0.05, 60), pool)
    assert ok and st["pool_coef"] > 0
    assert not pfm_rules.signature_matches("RISING_HEAD_WITH_POOL", t, pool + rng.normal(0, 0.05, 60), pool)[0]
    assert not pfm_rules.signature_matches("RISING_HEAD_WITH_POOL", t, t, None)[0]


def test_pfm_severity_and_review_flag():
    """FERC Ch14 PDF p34: rules tied to each PFM's instruments; always requires review."""
    t = list(range(30))
    up = [0.1 * x for x in t]
    flat = [0.0] * 30
    S = pfm_rules.InstrumentSeries
    one = pfm_rules.evaluate_pfm("CRD", "PFM-1", [S("A", "PRIMARY", "RISING_TREND", t, up), S("B", "SUPPORTING", "RISING_TREND", t, flat)])
    assert one["severity"] == "WATCH" and one["requires_engineer_review"] is True
    two = pfm_rules.evaluate_pfm("CRD", "PFM-1", [S("A", "PRIMARY", "RISING_TREND", t, up), S("B", "SUPPORTING", "RISING_TREND", t, up)])
    assert two["severity"] == "CONCERN" and two["classification"] == "REAL_BEHAVIOUR"
    faulty = pfm_rules.evaluate_pfm("CRD", "PFM-1", [S("A", "PRIMARY", "RISING_TREND", t, up, classification="SENSOR_FAULT"),
                                                     S("B", "SUPPORTING", "RISING_TREND", t, up)])
    assert faulty["severity"] == "WATCH"
    assert pfm_rules.evaluate_pfm("CRD", "PFM-1", [S("A", "PRIMARY", "RISING_TREND", t, flat)]) is None
    assert json.loads(two["evidence"])["rule_version"] == pfm_rules.RULE_VERSION


BOD = os.path.join(DATASETS, "piezometers", "boddington", "Boddington_PWPs_Embankment.csv")


@pytest.mark.skipif(not os.path.exists(BOD), reason="dam-refs Boddington absent")
def test_real_data_boddington_construction_rise_is_real_behaviour():
    """REAL DATA (CC BY 4.0, BATH-01380-BODDINGTON). cases.csv BOD-RA-01..03 label P1_88m, P1A_112m, P1A_102m
    as real_anomaly 2020-11-07..2020-12-09 (embankment construction, Briggs et al. 2024 Fig 9c). Daily medians
    of the construction window must give a CONCERN / REAL_BEHAVIOUR finding."""
    rows = list(csv.reader(open(BOD)))
    hdr = rows[0]
    by_day = {}
    for r in rows[2:]:
        if not r[0]:
            continue
        d = dt.datetime.strptime(r[0], "%d/%m/%Y %H:%M").date()
        if dt.date(2020, 11, 7) <= d <= dt.date(2020, 12, 9):
            by_day.setdefault(d, []).append(r)
    days = sorted(by_day)
    series = []
    for name in ("P1_88m", "P1A_112m", "P1A_102m"):
        i = hdr.index(name)
        vals = [float(np.nanmedian([float(x[i]) if x[i] not in ("", "NaN") else np.nan for x in by_day[d]])) for d in days]
        series.append(pfm_rules.InstrumentSeries(name, "PRIMARY" if name == "P1_88m" else "SUPPORTING", "RISING_TREND",
                                                 [(d - days[0]).days for d in days], vals))
    f = pfm_rules.evaluate_pfm("BODDINGTON", "CONSTRUCTION_PWP", series)
    assert f is not None and f["severity"] == "CONCERN" and f["classification"] == "REAL_BEHAVIOUR"
    assert len(json.loads(f["evidence"])["instruments"]) == 3


def _facts():
    f = pfm_rules.evaluate_pfm("CRD", "PFM-1", [pfm_rules.InstrumentSeries("A", "PRIMARY", "RISING_TREND", list(range(30)), [0.1 * x for x in range(30)])])
    return commentary.build_facts("CRD", [f], anomalies=[{"reading_id": "r1", "sensor_id": "A", "z_score": 3.4, "band": "OUTSIDE_3S",
                                                         "classification": "INDETERMINATE", "confidence": 0.2, "model_id": "m1"}],
                                  pfms=[{"pfm_id": "PFM-1", "title": "Internal erosion"}], dssmp_version_id="D-7"), f


def test_commentary_fallback_is_grounded_and_requires_review():
    facts, f = _facts()
    row = commentary.generate(facts)
    assert row["requires_engineer_review"] is True and row["generator"] == "DETERMINISTIC_FALLBACK"
    assert commentary.validate_text(row["text"], facts) == []
    c = json.loads(row["citations"])
    assert f["finding_id"] in c["finding_ids"] and "PFM-1" in c["pfm_ids"] and c["dssmp_version_id"] == "D-7"
    assert row["confidence"] <= f["confidence"]


def test_commentary_rejects_ungrounded_llm_output():
    facts, f = _facts()
    bad = [
        "The dam is safe. [F:%s]" % f["finding_id"],                       # forbidden conclusion
        "Piezometer A rose by 47 kPa. [F:%s]" % f["finding_id"],           # invented number
        "Piezometer A is rising. [F:not-a-finding]",                         # unknown citation
        "Piezometer A is rising.",                                           # uncited
    ]
    for text in bad:
        row = commentary.generate(facts, llm=lambda s, u, t=text: t, llm_name="test-llm")
        assert row["generator"] == "DETERMINISTIC_FALLBACK" and json.loads(row["citations"])["llm_rejections"]
        assert row["requires_engineer_review"] is True


def test_commentary_accepts_grounded_llm_output_and_survives_errors():
    facts, f = _facts()
    good = f"Primary instrument A shows a rising trend linked to PFM-1. [F:{f['finding_id']}] [P:PFM-1]\nSensor A z-score 3.4 is INDETERMINATE. [R:r1] [M:m1]"
    row = commentary.generate(facts, llm=lambda s, u: good, llm_name="endpoint-x/v1")
    assert row["generator"] == "endpoint-x/v1" and row["requires_engineer_review"] is True

    def boom(s, u):
        raise TimeoutError
    row = commentary.generate(facts, llm=boom)
    assert row["generator"] == "DETERMINISTIC_FALLBACK"
