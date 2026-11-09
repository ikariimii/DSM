"""DA contract v1.5 alignment (root SCHEMA_CONTRACT §4.1/§5.5.1, docs/api_contract.md,
sql/migrations/v1_5_001_contract.sql, tests/fixtures/review_signature_vector.json). Read-only use of DA files."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from backend import audit_chain as ac
from backend import signoff as so
from backend.repos import contract as ct

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "review_signature_vector.json"


def _engine(mod: str):
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    return pytest.importorskip(mod)


def test_review_signature_matches_da_fixture_vector():
    if not FIXTURE.exists():
        pytest.skip("DA fixture not present")
    v = json.loads(FIXTURE.read_text())
    assert set(v["subject"]) == set(so.SUBJECT_FIELDS) and set(v["review"]) == set(so.SIGNED_FIELDS)
    assert so.signature(v["review"], v["subject"]) == v["signature_hash"]
    assert so.verify({**v["review"], "signature_hash": v["signature_hash"]}, v["subject"])
    # any change to the signed content breaks it (the record is an audit record, not a free-form note)
    assert so.signature({**v["review"], "decision": "reject"}, v["subject"]) != v["signature_hash"]
    assert so.signature(v["review"], {**v["subject"], "auto_value": 6.000001}) != v["signature_hash"]
    # canonical form: the same instant as 'Z' or as a datetime signs identically
    assert so.signature({**v["review"], "signed_ts": "2026-03-02T12:00:00Z"}, v["subject"]) == v["signature_hash"]


def test_engine_signature_agrees_with_fixture():
    corr = _engine("dam_safety.inclinometer.corrections")
    if not FIXTURE.exists() or not hasattr(corr, "review_signature"):
        pytest.skip("engine review_signature / fixture not present")
    v = json.loads(FIXTURE.read_text())
    try:
        got = corr.review_signature(v["review"], v["subject"])
    except TypeError:
        pytest.skip("engine review_signature has a different call signature")
    assert got == v["signature_hash"] == so.signature(v["review"], v["subject"])


def test_api_field_map_matches_engine():
    qc = _engine("dam_safety.inclinometer.qc")
    assert ct.API_FIELD_MAP == qc.API_FIELD_MAP
    if hasattr(qc, "ZA_UNIT"):
        assert ct.ZA_UNIT == qc.ZA_UNIT
    stat = "rule=QA021-v2;ZA=-6.0000;ZB=1.0000;abs_dD=6.0000;status=PROPOSED"
    if hasattr(qc, "parse_detection_statistic"):
        eng = {ct.API_FIELD_MAP.get(k, k): v for k, v in qc.parse_detection_statistic(stat).items()}
        app = ct._parse_statistic(stat)
        assert set(app) == set(eng) and "za" in app and "ZA" not in app


def test_no_api_payload_carries_uppercase_za():
    """QA-040: bias_qc evidence, proposed_c and unit text use `za` (v1.5 §5.5: no API payload carries `ZA`)."""
    stat = ("rule=QA021-v2;n=6;mean_dD=6.0000;bs=6.0000;sd=0.1000;se=0.0400;spread=0.2000;ZA=-6.0000;abs_dD=6.0000;"
            "abs_bs=6.0000;crosscheck_verdict=AGREE;status=PROPOSED;requires_review=True;crosscheck_diff=0.0;"
            "crosscheck_tol=2.0;reference_survey_id=REF")
    rec = {"record_id": "P1", "instrument_id": "IN1", "survey_id": "S", "axis": "A", "auto_value": -6.0,
           "detection_method": "between_set_bias_qc", "detection_statistic": stat, "applied": False,
           "superseded_by": None, "timestamp_utc": "t", "user": "engine"}
    prop = ct.bias_proposal(rec)
    summary = ct.bias_qc([{"qa_reason": "BIAS_PROPOSED_A"}], [rec])
    for payload in (prop, summary):
        text = json.dumps({k: v for k, v in payload.items() if k != "detection_statistic"}, default=str)
        assert "ZA" not in text and "ZB" not in text
    assert prop["za"] == -6.0 and float(prop["evidence"]["za"]) == -6.0
    assert summary["axes"]["A"]["proposed_c"] == -6.0 and float(summary["axes"]["A"]["evidence"]["za"]) == -6.0
    assert prop["za_unit"] == ct.ZA_UNIT == summary["axes"]["A"]["proposed_c_unit"] and "za" in ct.ZA_UNIT


@pytest.mark.parametrize("missing", ["value", "metric_value", "reading_id"])
def test_qa035_non_ack_transition_requires_values(missing):
    t = {"transition_id": "T1", "event": "RAISE", "value": 1.0, "metric_value": 1.0, "reading_id": "R1",
         "origin": "app"}
    assert ct.check_transition(dict(t)) == t
    for bad in (None, ""):
        with pytest.raises(ct.DataIntegrityError) as e:
            ct.check_transition({**t, missing: bad})
        assert e.value.code.startswith("TRANSITION_MISSING") and missing.upper() in e.value.code.upper()
    # ACK transitions carry no reading
    assert ct.check_transition({**t, "event": "ACK", missing: None})["event"] == "ACK"
    with pytest.raises(ct.DataIntegrityError) as e:
        ct.check_transition({**t, "event": "CLEAR", "origin": "manual", missing: None})   # manual: same rule
    assert e.value.table == "ops.alarm_transition"


def test_chain_record_carries_every_review_record_column():
    """The hash-chained Lakebase row maps onto audit.review_record: every REVIEW_RECORD_COLS column (except
    `source`, which the extract sets) is present and is covered by record_hash."""
    if str(ROOT / "src" / "jobs") not in sys.path:
        sys.path.insert(0, str(ROOT / "src" / "jobs"))
    spec = importlib.util.spec_from_file_location("lakebase_extract", ROOT / "src" / "jobs" / "lakebase_extract.py")
    try:
        ext = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ext)
        cols = ext.REVIEW_RECORD_COLS
    except Exception as e:                                  # pragma: no cover - DA module needs Spark
        pytest.skip(f"lakebase_extract not importable here: {e}")
    rec = {"record_id": "P1", "parameter_name": "c", "auto_value": -6.0, "before_value": None}
    base = so.build(rec, "CRD", "approve", "ok", "rev", None, "2026-11-02T00:00:00Z")
    # v1.6: the extract carries all 24 columns (incl. the chain columns the finalizer adds under the chain lock)
    row = ac.finalizer(base)(1, ac.GENESIS)
    assert len(cols) == 24 and set(ext.CHAIN_COLS) <= set(cols)
    for c in cols:
        if c != "source":
            assert c in row
            # covered by record_hash, except the hash itself and the (optional) signature columns
            assert (c in ac.HASH_EXCLUDED) != (c in ac.CHAIN_FIELDS)
    assert set(ext.REVIEW_REQUIRED) <= {c for c in cols if row.get(c) not in (None, "")}
