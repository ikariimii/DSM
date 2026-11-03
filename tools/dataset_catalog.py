"""Build ref.dataset_catalog rows + the restricted-path list from the dam-refs manifests.

Source of truth (owner decision relayed 2026-11-02): /workspace/dam-refs/datasets/_shared/manifest.csv.
Per CONVENTIONS.md ('Every manifest gets usage_restriction ... internal-test-only when licence is
NC, unstated, or prohibits republishing') the per-family manifests are read too; for a
dataset_id present in _shared, the _shared value wins.

usage_restriction is normalised onto the enum {'internal-test-only','unrestricted'} (CHECK
constraint in sql/ddl/13_ref_dataset_survey.sql); it is never blank. Normalisation is
conservative: anything not clearly unrestricted becomes internal-test-only, and every
derived value records its basis in restriction_basis for Geotech review.

Usage:  python tools/dataset_catalog.py [--refs /workspace/dam-refs] [--out sql/seed/dataset_catalog.csv]
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
from typing import Dict, List, Tuple

ENUM = ("internal-test-only", "unrestricted")
UNRESTRICTED_TOKENS = ("unrestricted", "none", "open", "redistributable")
RESTRICT_LICENCE = re.compile(r"\bNC\b|NonCommercial|BY-NC|no licen[cs]e|not stated|unverified|not clear|"
                              r"reproduction restrict|internal|account required|login|prohibit", re.I)
OPEN_LICENCE = re.compile(r"public domain|CC0|CC[ -]BY[ -]4\.0|CC-BY-4\.0|in-house synthetic|synthetic", re.I)


def normalise(value: str, licence: str) -> Tuple[str, str]:
    """-> (enum value, basis). Never returns blank."""
    v = (value or "").strip()
    head = v.split(";")[0].split("(")[0].strip().lower()
    if head.startswith("internal-test-only"):
        return "internal-test-only", f"manifest: {v[:120]}"
    if head and any(head.startswith(t) for t in UNRESTRICTED_TOKENS):
        if RESTRICT_LICENCE.search(licence or "") and not OPEN_LICENCE.search(licence or ""):
            return "internal-test-only", f"CONFLICT manifest '{v[:60]}' vs licence '{(licence or '')[:60]}' -> conservative"
        return "unrestricted", f"manifest: {v[:120]}"
    if head and not head.startswith(("n/a", "unavailable")):
        return "internal-test-only", f"UNRECOGNISED manifest value '{v[:60]}' -> conservative"
    # blank / n/a / unavailable: derive from licence text per CONVENTIONS.md rule
    lic = licence or ""
    if OPEN_LICENCE.search(lic) and not RESTRICT_LICENCE.search(lic):
        return "unrestricted", f"DERIVED from licence (manifest blank/n.a.): {lic[:80]}"
    return "internal-test-only", f"DERIVED conservative (manifest blank/n.a.): {lic[:80]}"


def _paths(family_dir: str, local_files: str) -> List[str]:
    out = []
    for tok in re.split(r"[\s,;()]+", local_files or ""):
        tok = tok.strip().rstrip(".")
        if not tok or tok.lower() in ("none", "not", "see"):
            continue
        cand = os.path.normpath(os.path.join(family_dir, tok))
        hits = glob.glob(cand) if any(c in tok for c in "*?[") else ([cand] if os.path.exists(cand) else [])
        if not hits and "/" in tok:            # e.g. brace pattern fmt0{1,2,3}.xlsx split by the tokenizer
            parent = os.path.dirname(cand)
            if os.path.isdir(parent) and os.path.normpath(parent) != os.path.normpath(family_dir):
                hits = [parent]
        out += [h for h in hits if os.path.exists(h)]
    return sorted(set(out))


class ManifestValidationError(ValueError):
    """Raised when a manifest row's usage_restriction is not exactly one of ENUM (QA-014)."""


def validate_manifest_row(r: Dict, where: str) -> str:
    """Ingestion gate (QA-014): usage_restriction must be EXACTLY 'internal-test-only' or 'unrestricted'
    (CONVENTIONS.md 'Dataset usage'; same enum as the CHECK on ref.dataset_catalog). Free text, blanks,
    case variants and padding are rejected - fix the manifest (tools/normalise_manifests.py), never here.
    An 'unrestricted' row whose licence text is NC / unstated / unverified is also rejected."""
    v = r.get("usage_restriction")
    if v is None:
        raise ManifestValidationError(f"{where}: no usage_restriction column")
    if v not in ENUM:
        raise ManifestValidationError(f"{where}: usage_restriction {v!r} not in {ENUM}")
    lic = r.get("license") or ""
    if v == "unrestricted" and RESTRICT_LICENCE.search(lic) and not OPEN_LICENCE.search(lic):
        raise ManifestValidationError(f"{where}: 'unrestricted' conflicts with licence {lic[:80]!r}")
    return v


def build(refs: str) -> Tuple[List[Dict], List[str]]:
    ds_root = os.path.join(refs, "datasets")
    rows: Dict[str, Dict] = {}
    restricted_paths: List[str] = []
    manifests = sorted(glob.glob(os.path.join(ds_root, "*", "manifest.csv")))
    shared = os.path.join(ds_root, "_shared", "manifest.csv")
    manifests = [m for m in manifests if m != shared] + [shared]          # _shared last -> wins
    errors = []
    for m in manifests:
        for i, r in enumerate(csv.DictReader(open(m, newline="", encoding="utf-8")), start=2):
            if (r.get("dataset_id") or "").strip():
                try:
                    validate_manifest_row(r, f"{os.path.relpath(m, refs)}:{i} {r['dataset_id']}")
                except ManifestValidationError as e:
                    errors.append(str(e))
    if errors:
        raise ManifestValidationError("manifest validation failed:\n  " + "\n  ".join(errors))
    for m in manifests:
        fam = os.path.basename(os.path.dirname(m))
        for r in csv.DictReader(open(m, newline="", encoding="utf-8")):
            did = (r.get("dataset_id") or "").strip()
            if not did:
                continue
            ur = r["usage_restriction"]                     # validated above
            basis = (r.get("usage_restriction_note") or f"manifest: {ur}")[:200]
            paths = _paths(os.path.dirname(m), r.get("local_files", ""))
            if ur == "internal-test-only":
                restricted_paths += paths
            prev = rows.get(did)
            if prev and prev["usage_restriction"] == "internal-test-only" and fam != "_shared":
                continue
            rows[did] = {"dataset_id": did, "family": fam, "title": (r.get("title") or "")[:200],
                         "license": (r.get("license") or "")[:300], "source_url": (r.get("source_url") or "")[:300],
                         "usage_restriction": ur, "restriction_basis": basis,
                         "manifest_path": os.path.relpath(m, refs)}
            if prev and prev["usage_restriction"] == "internal-test-only":
                rows[did]["usage_restriction"] = "internal-test-only"   # restriction is sticky across manifests
    for r in rows.values():
        assert r["usage_restriction"] in ENUM, r
    return sorted(rows.values(), key=lambda r: r["dataset_id"]), sorted(set(restricted_paths))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refs", default="/workspace/dam-refs")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "sql", "seed", "dataset_catalog.csv"))
    a = ap.parse_args()
    rows, paths = build(a.refs)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} datasets -> {a.out}; {sum(r['usage_restriction'] != 'unrestricted' for r in rows)} internal-test-only; "
          f"{len(paths)} restricted local paths")


if __name__ == "__main__":
    main()
