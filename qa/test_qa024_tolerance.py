"""QA-024 re-verification (QA, 2026-11-02 ~20:50 EST). All data SYNTHETIC; no restricted values.

Lead ruling: the bias cross-check tolerance is NOT a production blocker, provided that
  (a) it is configurable (BiasQCParams.crosscheck_tol_abs / crosscheck_tol_k) and only ranks proposals,
  (b) it is shown as UNVERIFIED in the API/UI and in the audit record (ref.incl_correction detection_statistic),
  (c) no bias can be applied without a reviewer-approved record (qa/test_contract_v14.py::test_qa024_*).
This file holds (a)-(c) end to end: engine -> correction record -> api_contract §4.1 proposal -> UI source."""
import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "app" / "backend", ROOT / "tests"):
    sys.path.insert(0, str(p))

from dam_safety.inclinometer import qc, corrections as C   # noqa: E402
from repos import contract                                 # noqa: E402
import test_inclinometer_bias_qc as F                      # noqa: E402  (synthetic fixtures)

TS = "2026-11-02T00:00:00Z"
CASE = dict(d0=-6.5, d180=-1.5)    # ||ΔD| − |bs|| = 3.0 with SE ≈ 0 -> tol = tol_abs


@pytest.mark.parametrize("params, verdict", [
    (qc.BiasQCParams(), "SUSPECT_CROSSCHECK"),                          # default tol 2.0 < 3.0
    (qc.BiasQCParams(crosscheck_tol_abs=3.0), "PROPOSED"),              # configured wider
    (qc.BiasQCParams(crosscheck_tol_abs=0.5, crosscheck_tol_k=1e6), "PROPOSED"),   # k·SE dominates when SE > 0
])
def test_tolerance_is_configurable_and_only_ranks(params, verdict):
    base = F.run(F.passes(**CASE, noise=0.0 if params.crosscheck_tol_k < 1e5 else 0.5))
    q = F.run(F.passes(**CASE, noise=0.0 if params.crosscheck_tol_k < 1e5 else 0.5), params=params)
    assert q.status == verdict and q.crosscheck_tol == pytest.approx(max(params.crosscheck_tol_abs,
                                                                          params.crosscheck_tol_k * q.se))
    # ranking only: the proposal itself (ZA = −ΔD) does not depend on the tolerance and is never applied
    assert q.za_proposed_c == pytest.approx(base.za_proposed_c) and q.requires_review and q.params_unverified
    rec = qc.bias_proposal_record(q, "R1", "D1", "I1", "eng", TS)
    assert rec.applied is False and rec.reviewed_by is None


@pytest.mark.parametrize("tol_abs", [2.0, 3.0])
def test_unverified_tolerance_travels_to_audit_record_and_api(tol_abs):
    q = F.run(F.passes(**CASE), params=qc.BiasQCParams(crosscheck_tol_abs=tol_abs))
    rec = qc.bias_proposal_record(q, "R1", "D1", "I1", "eng", TS)
    st = dict(kv.split("=", 1) for kv in rec.detection_statistic.split(";"))
    assert st["params_unverified"] == "True" and st["requires_review"] == "True"
    assert float(st["crosscheck_tol"]) == pytest.approx(tol_abs) and st["crosscheck_verdict"] in ("AGREE", "SUSPECT")
    p = contract.bias_proposal(dataclasses.asdict(rec))
    assert p["display_status"] == "UNVERIFIED" and p["applied_by_app"] is False
    assert p["crosscheck_tol"] == pytest.approx(tol_abs) and p["evidence"]["params_unverified"] == "True"
    assert p["approval_status"] == "proposed" and p.get("integrity_error") is None  # field added by QA-036 (a9e7d56); absent before


def test_ui_labels_tolerance_and_proposals_unverified():
    src = (ROOT / "app" / "frontend" / "src" / "pages" / "InstrumentDetail.tsx").read_text()
    i = src.index("function BiasQc(")
    body = src[i:src.index("\n}\n", i)]
    assert "UNVERIFIED, never applied by the app" in body and "UNVERIFIED, not applied by the app" in body


@pytest.mark.xfail(strict=True, reason="QA-024: at f33a5a1 the UI does not display the tolerance used; added by App Dev "
                   "a9e7d56 (unpushed). Drop this marker when that lands and it XPASSes.")
def test_ui_shows_the_unverified_tolerance_value():
    src = (ROOT / "app" / "frontend" / "src" / "pages" / "InstrumentDetail.tsx").read_text()
    i = src.index("function BiasQc(")
    body = src[i:src.index("\n}\n", i)]
    assert "q.crosscheck_tol" in body and "q.display_status" in body and "params_unverified" in body


# (c) The applied-bias cases (AGREE-ranked proposal still needs an independent reviewer; approved path) are in
# qa/test_contract_v14.py, the only QA file allowed to build applied records.
