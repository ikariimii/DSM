"""canari pilot: ENVIRONMENT CHECK ONLY (advisory, unverified). Job dam_canari_pilot, dev/test targets only, PAUSED.

Scope (docs/DEPENDENCY_PINS.md, App Developer proposal adopted 2026-11-02): prove pycanari 0.2.5 imports in its own
serverless env-6 environment before any modelling code is written. This script writes NOTHING to Unity Catalog:
no qa_flag, alarms, thresholds, exceedances, or any table the app reads. It prints a JSON report and exits non-zero
if the import fails. Known box result 2026-11-02: import fails because pytagi -> cutagi needs libmpi_cxx.so.40."""
import importlib.metadata as md
import json
import platform
import sys

import _common


def check():
    rep = {"python": platform.python_version(), "advisory_only": True, "writes": "none"}
    for dist in ("pycanari", "pytagi", "numpy", "statsmodels", "pandas"):
        try:
            rep[dist] = md.version(dist)
        except md.PackageNotFoundError:
            rep[dist] = None
    try:
        import canari  # noqa: F401  (PyPI 'pycanari'; the PyPI project 'canari' is unrelated)
        rep["import_canari"] = "ok"
    except Exception as e:  # noqa: BLE001 - report any loader failure verbatim
        rep["import_canari"] = f"FAILED: {type(e).__name__}: {e}"
    return rep


if __name__ == "__main__":
    a = _common.args()
    if a.catalog == "dam_prod":
        raise SystemExit("canari pilot is not allowed in dam_prod")
    r = check()
    print(json.dumps(r, indent=2, sort_keys=True))
    sys.exit(0 if r["import_canari"] == "ok" else 1)
