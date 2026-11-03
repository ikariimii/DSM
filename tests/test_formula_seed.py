"""Formula-registry ownership + version-bump rule (SCHEMA_CONTRACT §9, app open question 4).
The committed seed must equal the generated one; an existing (formula_id, version) may not change
fingerprint - change the code => bump formula_version (registry.py) and regenerate."""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import formula_seed  # noqa: E402


def _committed():
    txt = open(formula_seed.OUT).read()
    return dict(re.findall(r"\('([A-Z0-9_]+)', '([0-9.]+)',.*?registry#sha256=([0-9a-f]{64})'\)", txt))  # noqa


def test_seed_in_sync_with_registry():
    cur = open(formula_seed.OUT).read()
    assert cur == formula_seed.render(), "run: python tools/formula_seed.py (and bump formula_version if code changed)"


def test_no_fingerprint_change_under_same_version():
    txt = open(formula_seed.OUT).read()
    committed = {(m[0], m[1]): m[2] for m in re.findall(r"\n  \('([A-Z0-9_]+)', '([0-9.]+)'.*?registry#sha256=([0-9a-f]{64})'\)", txt)}
    assert committed, "seed parse failed"
    for f, fp in formula_seed.rows():
        k = (f.formula_id, f.version)
        if k in committed:
            assert committed[k] == fp, f"{k} changed without a version bump"


def test_all_app_formulas_registered():
    ids = {f.formula_id for f, _ in formula_seed.rows()}
    assert {"VW_PIEZO", "STANDPIPE_DTW", "SETTLEMENT_DELTA", "INCL_PROBE_2PASS", "WEIR_VNOTCH", "VW_DISP_LIN", "LEVEL_DATUM"} <= ids
