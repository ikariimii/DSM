"""Registry self-verification: every registered formula carries citations and its embedded
test vectors (copied from the manuals, see core.SOURCES) reproduce."""
import pytest

from dam_safety.formulas import registry
from dam_safety.formulas.core import _truthy


@pytest.mark.parametrize("fid", sorted(registry.REGISTRY) if hasattr(registry, "REGISTRY") else [])
def test_vectors(fid):
    f = registry.REGISTRY[fid]
    assert f.sources, f"{fid} has no source citation"
    for tv in f.test_vectors:
        if tv.get("profile"):
            continue
        if tv.get("series"):
            from dam_safety.datum import rebaseline_series
            res, _ = rebaseline_series([{"ts_utc": i, "raw_value": v, "datum_id": "D1"} for i, v in enumerate(tv["raw"])],
                                       tv["sign_mult"], tv["raw_unit"])
            assert [o["eng_value"] for o in res] == pytest.approx(tv["expect"], abs=tv["tol"]), tv["source"]
            continue
        out = f.fn(tv["reading"], registry.normalize_params(tv["params"]))
        for k, want in tv["expect"].items():
            assert out[k] == pytest.approx(want, abs=tv["tol"]), tv["source"]


def test_normalize_params_canonical_wins():
    d = registry.normalize_params([("top_elev_m", "1"), ("top_el_m", "2")])
    assert d["top_el_m"] == "2"


@pytest.mark.parametrize("v,exp", [("true", True), ("1", True), ("1.0", True), ("YES", True), (1, True),
                                   ("false", False), ("0", False), (None, False), ("", False), (0, False)])
def test_truthy(v, exp):
    assert _truthy(v) is exp
