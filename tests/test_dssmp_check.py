"""v1.7 (Genie 2026-11-09): DSSMP version usable only if it exists, is the dam's, approved and in effect.
One test per reason code (dam_safety.dssmp_check). Synthetic data only."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dam_safety import dssmp_check as dc  # noqa: E402

V = {"DSSMP-A-1": {"dam_id": "A", "status": "superseded", "effective_from": "2025-01-01T00:00:00Z",
                   "effective_to": "2026-01-01T00:00:00Z"},
     "DSSMP-A-2": {"dam_id": "A", "status": "approved", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None},
     "DSSMP-A-3": {"dam_id": "A", "status": "draft", "effective_from": "2026-12-01T00:00:00Z", "effective_to": None},
     "DSSMP-A-4": {"dam_id": "A", "status": "approved", "effective_from": "2027-01-01T00:00:00Z", "effective_to": None},
     "DSSMP-A-5": {"dam_id": "A", "status": "retired", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None},
     "DSSMP-A-6": {"dam_id": "A", "status": "superseded", "effective_from": "2026-01-01T00:00:00Z",
                   "effective_to": None},
     "DSSMP-B-1": {"dam_id": "B", "status": "approved", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None}}
NOW = "2026-11-09T16:00:00Z"
code = lambda vid, at=NOW, hist=False: (dc.dssmp_problem(vid, "A", V, at, historical=hist) or (None,))[0]


def test_approved_current_version_passes():
    assert code("DSSMP-A-2") is None


def test_dssmp_missing():
    assert code(None) == code("  ") == "dssmp_missing"


def test_dssmp_not_found():
    assert code("DSSMP-NOPE") == "dssmp_not_found"


def test_dssmp_other_dam():
    assert code("DSSMP-B-1") == "dssmp_other_dam"


def test_dssmp_draft():
    assert code("DSSMP-A-3") == "dssmp_draft"
    assert code("DSSMP-A-3", "2027-06-01T00:00:00Z") == "dssmp_draft"       # a draft never becomes usable by time


def test_dssmp_superseded_now_and_historically():
    assert code("DSSMP-A-1") == "dssmp_superseded"                          # ended
    assert code("DSSMP-A-6") == "dssmp_superseded"                          # status superseded, no end recorded
    # verifier, historical: an apply signed while A-1 was still in effect is fine; after its end it is not
    assert code("DSSMP-A-1", "2025-06-01T00:00:00Z", hist=True) is None
    assert code("DSSMP-A-1", "2026-01-01T00:00:00Z", hist=True) == "dssmp_superseded"
    assert code("DSSMP-A-6", "2026-06-01T00:00:00Z", hist=True) == "dssmp_superseded"
    # publisher (not historical): superseded status is refused even for an old timestamp
    assert code("DSSMP-A-1", "2025-06-01T00:00:00Z", hist=False) == "dssmp_superseded"


def test_dssmp_not_yet_effective():
    assert code("DSSMP-A-4") == "dssmp_not_yet_effective"
    assert code("DSSMP-A-4", "2027-02-01T00:00:00Z") is None


def test_dssmp_status_unknown():
    assert code("DSSMP-A-5") == "dssmp_status_unknown"


def test_codes_constant_matches_the_tests_and_app_names():
    src = open(__file__).read()
    for c in dc.CODES:
        assert f'"{c}"' in src, c
    # the four codes agreed with App Dev
    assert {"dssmp_not_found", "dssmp_other_dam", "dssmp_draft", "dssmp_superseded"} <= set(dc.CODES)
