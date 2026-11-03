"""Instrument-type vocabulary (SCHEMA_CONTRACT v1.1 §5.4). Units follow
/workspace/dam-refs/CONVENTIONS.md 'Units and symbols' (kPa, m, mm, degC, L/s, microstrain, kN)."""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(ROOT, "sql", "seed", "ref_instrument_type_seed.sql")


def _vocab():
    txt = open(SEED).read()
    out = {}
    for m in re.finditer(r"\n  \('([a-z_]+)', '[^']*', array\(([^)]*)\), '[^']*', (?:'[^']*'|NULL), array\(([^)]*)\)", txt):
        units = [u.strip().strip("'") for u in m.group(2).split(",") if u.strip()]
        forms = [u.strip().strip("'") for u in m.group(3).split(",") if u.strip()]
        out[m.group(1)] = (units, forms)
    return out


def test_vocab_parses_and_covers_strain_family():
    v = _vocab()
    assert len(v) == 14
    assert v["strain_gauge"][0] == ["microstrain"] and v["strain_gauge"][1] == []   # no engine formula yet -> flagged


def test_app_onboarding_types_and_units_are_in_vocab():
    # read-only parse of the app's TYPE_UNITS (app/backend/onboarding.py); not imported, not modified
    src = open(os.path.join(ROOT, "app", "backend", "onboarding.py")).read()
    node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "TYPE_UNITS")
    app = ast.literal_eval(node.value)
    v = _vocab()
    for t, units in app.items():
        assert t in v, t
        assert set(units) <= set(v[t][0]), (t, units)


def test_engine_formulas_exist_in_registry():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from dam_safety.formulas import registry
    ids = {f.formula_id for f in registry.REGISTRY.values()}
    for t, (_, forms) in _vocab().items():
        assert set(forms) <= ids, (t, forms)
