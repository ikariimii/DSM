"""Licensing guard: internal-test-only datasets must never leak into shipped paths.

Source: owner decision 2026-11-02 relayed by the Geotech Instrumentation Expert; source of truth
/workspace/dam-refs/datasets/_shared/manifest.csv; rule /workspace/dam-refs/CONVENTIONS.md
'Dataset usage' + 'Schema additions' (usage_restriction in {'internal-test-only','unrestricted'}).
Shipped paths = the whole repo except .git, .venv, qa/ (QA agent), caches and the gitignored
.internal_test_artifacts/. Checks: file names, sha256 of every restricted file, data lines
(>= 20 chars containing a digit) of restricted text files, the bundle prod target, the bundle
sync excludes, and the catalog seed."""
import csv
import hashlib
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFS = "/workspace/dam-refs"
sys.path.insert(0, os.path.join(ROOT, "tools"))
import dataset_catalog  # noqa: E402

SKIP_DIRS = {".git", ".venv", "qa", ".pytest_cache", "__pycache__", ".internal_test_artifacts", ".databricks"}
TEXT_EXT = {".csv", ".txt", ".json", ".geojson", ".gk", ".tenv3", ".rdb", ".sql", ".py", ".md", ".yml", ".yaml", ".html"}
GENERIC_NAMES = {"readme.txt", "read_me.txt", "c.txt", "sen.txt", "meteo.txt", "manifest.csv", "license", "readme.md"}
OWNER_NAMED = {"ENGURI-PZ-1997-2022", "ICOLD-BW2022-A", "ICOLD-BW2022-C", "USBR-RISE-10785"}
needs_refs = pytest.mark.skipif(not os.path.isdir(os.path.join(REFS, "datasets")), reason="dam-refs not mounted (CI)")


def _shipped_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            yield os.path.join(root, f)


def _expand(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, fs in os.walk(p):
                for f in fs:
                    yield os.path.join(root, f)
        elif os.path.isfile(p):
            yield p


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def restricted():
    rows, paths = dataset_catalog.build(REFS)
    return rows, sorted(set(_expand(paths)))


@needs_refs
def test_owner_named_datasets_are_internal_test_only(restricted):
    rows, _ = restricted
    by = {r["dataset_id"]: r["usage_restriction"] for r in rows}
    for d in OWNER_NAMED:
        assert by[d] == "internal-test-only", d


@needs_refs
def test_no_restricted_file_names_or_bytes_shipped(restricted):
    _, files = restricted
    assert files, "restricted file list unexpectedly empty"
    names = {os.path.basename(f).lower() for f in files} - GENERIC_NAMES
    hashes = {_sha(f) for f in files}
    bad = []
    for f in _shipped_files():
        if os.path.basename(f).lower() in names:
            bad.append(("name", f))
        elif os.path.getsize(f) and _sha(f) in hashes:
            bad.append(("sha256", f))
    assert not bad, bad


@needs_refs
def test_no_restricted_data_lines_shipped(restricted):
    _, files = restricted
    lines = set()
    for f in files:
        if os.path.splitext(f)[1].lower() not in TEXT_EXT or f.endswith(".py"):
            continue
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for i, ln in enumerate(fh):
                if i > 5000:
                    break
                s = ln.strip()
                if len(s) >= 20 and any(c.isdigit() for c in s):
                    lines.add(s)
    hits = []
    for f in _shipped_files():
        if os.path.splitext(f)[1].lower() not in TEXT_EXT or os.path.getsize(f) > 50_000_000:
            continue
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for n, ln in enumerate(fh, 1):
                if ln.strip() in lines:
                    hits.append(f"{os.path.relpath(f, ROOT)}:{n}")
    assert not hits, hits[:20]


def test_catalog_seed_enum_never_blank():
    p = os.path.join(ROOT, "sql", "seed", "dataset_catalog.csv")
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    assert rows
    for r in rows:
        assert r["usage_restriction"] in ("internal-test-only", "unrestricted"), r["dataset_id"]
        assert r["restriction_basis"].strip()
    by = {r["dataset_id"]: r["usage_restriction"] for r in rows}
    for d in OWNER_NAMED:
        assert by[d] == "internal-test-only", d


@needs_refs
def test_catalog_seed_in_sync_with_manifests():
    rows, _ = dataset_catalog.build(REFS)
    p = os.path.join(ROOT, "sql", "seed", "dataset_catalog.csv")
    seed = {r["dataset_id"]: r["usage_restriction"] for r in csv.DictReader(open(p, encoding="utf-8"))}
    assert seed == {r["dataset_id"]: r["usage_restriction"] for r in rows}, "re-run tools/dataset_catalog.py"


def test_normalise_rules():
    n = dataset_catalog.normalise
    assert n("internal-test-only; do not redistribute", "")[0] == "internal-test-only"
    assert n("none (CC0)", "CC0-1.0")[0] == "unrestricted"
    assert n("", "CC BY-NC 4.0")[0] == "internal-test-only"
    assert n("", "No license stated")[0] == "internal-test-only"
    assert n("", "CC BY 4.0")[0] == "unrestricted"
    assert n("maybe", "CC BY 4.0")[0] == "internal-test-only"          # unrecognised -> conservative
    assert n("open", "CC BY-NC 3.0")[0] == "internal-test-only"        # conflict -> conservative


def _yaml():
    yaml = pytest.importorskip("yaml")
    p = os.path.join(ROOT, "databricks.yml")
    assert os.path.exists(p), "databricks.yml missing"
    return yaml.safe_load(open(p))


def test_prod_target_never_references_testonly():
    import yaml as _y
    cfg = _yaml()
    prod = cfg["targets"]["prod"]
    assert "testonly" not in _y.safe_dump(prod).lower()
    for f in os.listdir(os.path.join(ROOT, "resources")):    # top-level resources deploy to every target
        txt = open(os.path.join(ROOT, "resources", f), encoding="utf-8").read().lower()
        assert "testonly" not in txt, f"{f} is deployed to prod and mentions testonly"
    for t in ("dev", "test"):
        assert "testonly" in _y.safe_dump(cfg["targets"][t]).lower(), f"{t} should declare the test-only job"


def test_sync_excludes_cover_gitignore_licensing_patterns():
    cfg = _yaml()
    excl = set(cfg["sync"]["exclude"])
    gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read().split("# ---- LICENSING")[1].split("\n", 1)[1]
    pats = [ln.strip() for ln in gi.splitlines() if ln.strip() and not ln.startswith("#")]
    missing = [p for p in pats if p not in excl and p.rstrip("/") + "/**" not in excl]
    assert not missing, missing
    assert "qa/**" in excl and ".venv/**" in excl


def test_manifest_ingestion_rejects_non_enum_values():
    """QA-014: only 'internal-test-only' | 'unrestricted' are accepted (CONVENTIONS.md 'Dataset usage':
    /workspace/dam-refs/CONVENTIONS.md lines ~51-57). Free text, blanks, case/padding variants are rejected."""
    v = dataset_catalog.validate_manifest_row
    E = dataset_catalog.ManifestValidationError
    assert v({"usage_restriction": "internal-test-only", "license": "CC BY-NC 4.0"}, "x") == "internal-test-only"
    assert v({"usage_restriction": "unrestricted", "license": "CC BY 4.0"}, "x") == "unrestricted"
    for bad in ("", "none", "open", "n/a", "UNAVAILABLE - files restricted", "Unrestricted", " unrestricted",
                "internal-test-only (if ever obtained)", "redistributable"):
        with pytest.raises(E):
            v({"usage_restriction": bad, "license": "CC BY 4.0"}, "x")
    with pytest.raises(E):
        v({"license": "CC BY 4.0"}, "x")                                         # column missing
    with pytest.raises(E):
        v({"usage_restriction": "unrestricted", "license": "CC BY-NC 4.0"}, "x")  # licence conflict


def test_manifest_build_fails_on_bad_manifest(tmp_path):
    d = tmp_path / "datasets" / "fam"
    d.mkdir(parents=True)
    (d / "manifest.csv").write_text("dataset_id,license,local_files,usage_restriction\nX,CC BY 4.0,,open\n")
    (tmp_path / "datasets" / "_shared").mkdir()
    (tmp_path / "datasets" / "_shared" / "manifest.csv").write_text("dataset_id,license,local_files,usage_restriction\n")
    with pytest.raises(dataset_catalog.ManifestValidationError, match="'open'"):
        dataset_catalog.build(str(tmp_path))


@needs_refs
def test_all_live_manifests_on_enum():
    """QA-014 regression: every row of every /workspace/dam-refs/datasets/*/manifest.csv is on the enum."""
    import csv
    import glob
    for m in glob.glob(os.path.join(REFS, "datasets", "*", "manifest.csv")):
        for i, r in enumerate(csv.DictReader(open(m, newline="", encoding="utf-8")), start=2):
            if (r.get("dataset_id") or "").strip():
                dataset_catalog.validate_manifest_row(r, f"{m}:{i}")


@needs_refs
def test_references_index_lf_and_added_by():
    """QA-009: /workspace/dam-refs/references_index.csv is LF-only and carries an added_by column."""
    import csv
    raw = open(os.path.join(REFS, "references_index.csv"), "rb").read()
    assert b"\r" not in raw
    rows = list(csv.DictReader(open(os.path.join(REFS, "references_index.csv"), newline="", encoding="utf-8")))
    assert rows and all((r.get("added_by") or "").strip() for r in rows)
