"""Dependency pins (steering 8; docs/DEPENDENCY_PINS.md). Versions checked on PyPI 2026-11-02; env-6 base from
docs.databricks.com/aws/en/release-notes/serverless/environment-version/six (requirements/serverless-env6-base.txt)."""
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = os.path.join(ROOT, "requirements")
FORBIDDEN_CORE = {"pytoa5", "igbpyutils", "traval", "pycanari", "pytagi", "canari"}


def _pins(path):
    out = {}
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if line:
            m = re.match(r"([A-Za-z0-9_.\-\[\]]+)\s*(==|>=)\s*([^\s;]+)", line)
            assert m, line
            out[m.group(1).lower().replace("_", "-")] = (m.group(2), m.group(3))
    return out


def _envs():
    envs = {}
    for f in os.listdir(os.path.join(ROOT, "resources")):
        d = yaml.safe_load(open(os.path.join(ROOT, "resources", f))) or {}
        jobs = dict(d.get("resources", {}).get("jobs", {}))
        for t in (d.get("targets") or {}).values():
            jobs.update((t.get("resources") or {}).get("jobs", {}))
        for jn, j in jobs.items():
            for e in j.get("environments", []):
                envs[(jn, e["environment_key"])] = e["spec"]
    return envs


def test_lock_contains_top_level_pins():
    for name in ("job-ml-env6", "canari-pilot-env6"):
        top, lock = _pins(f"{R}/{name}.in"), _pins(f"{R}/{name}.lock.txt")
        for pkg, (op, ver) in top.items():
            assert pkg in lock and lock[pkg][0] == "==", (name, pkg)
            if op == "==":
                assert lock[pkg][1] == ver, (name, pkg, lock[pkg])


def test_ml_lock_keeps_env6_base_except_statsmodels():
    base, lock = _pins(f"{R}/serverless-env6-base.txt"), _pins(f"{R}/job-ml-env6.lock.txt")
    changed = {p for p in lock if p in base and lock[p][1] != base[p][1]}
    assert changed == {"statsmodels"}, changed          # numpy 2.3.4 / pandas 2.3.3 / scipy 1.16.3 untouched


def test_jobs_reference_locks_and_env_versions():
    envs = _envs()
    ml = envs[("dam_interpretation", "ml")]
    assert ml["environment_version"] == "6"
    assert ml["dependencies"] == ["-r ${workspace.file_path}/requirements/job-ml-env6.lock.txt"]
    can = envs[("dam_canari_pilot", "canari")]
    assert can["environment_version"] == "6" and "canari-pilot-env6.lock.txt" in can["dependencies"][0]


def test_forbidden_packages_not_in_core_envs():
    for (job, key), spec in _envs().items():
        if job == "dam_canari_pilot":
            continue
        deps = " ".join(spec.get("dependencies", [])).lower()
        for bad in FORBIDDEN_CORE:
            assert bad not in deps, (job, bad)
    lock = _pins(f"{R}/job-ml-env6.lock.txt")
    assert not FORBIDDEN_CORE & set(lock)


def test_canari_pilot_isolated_paused_not_prod():
    d = yaml.safe_load(open(os.path.join(ROOT, "resources", "canari_pilot.yml")))
    assert set(d["targets"]) == {"dev", "test"} and "resources" not in d
    j = d["targets"]["dev"]["resources"]["jobs"]["dam_canari_pilot"]
    assert j["schedule"]["pause_status"] == "PAUSED"
    src = open(os.path.join(ROOT, "src", "jobs", "canari_pilot.py")).read()
    assert not re.search(r"saveAsTable|insertInto|\.sql\(|MERGE|INSERT", src)   # writes nothing


def test_lock_files_are_synced_not_ignored():
    gi = open(os.path.join(ROOT, ".gitignore")).read()
    bundle = open(os.path.join(ROOT, "databricks.yml")).read()
    for pat in ("requirements", "*.txt", "*.lock"):
        assert not re.search(rf"^\s*-?\s*['\"]?{re.escape(pat)}", gi + "\n" + bundle, re.M), pat
