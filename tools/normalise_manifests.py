"""QA-014 one-time migration: rewrite usage_restriction in every
/workspace/dam-refs/datasets/*/manifest.csv onto the enum {'internal-test-only','unrestricted'}
(CONVENTIONS.md 'Dataset usage': never blank; NC / unstated / unverifiable -> internal-test-only).
The original free text is preserved in a new trailing column usage_restriction_note together with the
basis and any FLAG. Idempotent: rows already on the enum keep their value; existing notes are kept.
Explicit OVERRIDES win over the heuristic normalise() and are listed in the report.
Usage: python tools/normalise_manifests.py [--refs /workspace/dam-refs] [--dry-run]"""
import argparse
import csv
import glob
import io
import os

from dataset_catalog import ENUM, normalise

OVERRIDES = {
    # dataset_id: (value, reason)
    "USGS-09421000-72036": ("internal-test-only", "FLAG: agency_cd in file is USBR; USGS public-domain statement not verified for this series"),
    "KARKHEH-DRAINS": ("internal-test-only", "FLAG: record licence CC BY 4.0 but files are access-restricted (not obtained); re-check when files are released"),
    "NCDC-TGD-HEEL-STRESS-2015-2021": ("internal-test-only", "FLAG: CC BY 4.0 on landing page but download needs NCDC account; platform terms UNVERIFIED"),
    "UNAVAILABLE-PEER-NGA": ("internal-test-only", "FLAG: not obtained; account terms UNVERIFIED"),
    "UNAVAILABLE-PLUMBLINE-PUBLIC": ("internal-test-only", "FLAG: placeholder row, no data, licence n/a"),
}


def migrate(path, dry=False):
    raw = open(path, "rb").read()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    fields = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")).fieldnames)
    if "usage_restriction_note" not in fields:
        fields.append("usage_restriction_note")
    report = []
    for r in rows:
        old = (r.get("usage_restriction") or "").strip()
        did = r["dataset_id"].split(" ")[0]
        if did in OVERRIDES:
            new, basis = OVERRIDES[did]
        elif old in ENUM:
            new, basis = old, ""
        else:
            new, basis = normalise(old, r.get("license", ""))
        assert new in ENUM
        if new != old or (did in OVERRIDES and not r.get("usage_restriction_note")):
            note = f"QA-014 2026-11-02: was '{old}'; {basis}".strip()
            r["usage_restriction_note"] = "; ".join(x for x in (r.get("usage_restriction_note"), note) if x)
            report.append((os.path.relpath(path), r["dataset_id"], old[:50], new, basis[:110]))
        r["usage_restriction"] = new
        r.setdefault("usage_restriction_note", "")
    if not dry:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields, lineterminator=nl)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") or "" for k in fields})
        open(path, "w", encoding="utf-8", newline="").write(buf.getvalue())
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refs", default="/workspace/dam-refs")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    for p in sorted(glob.glob(os.path.join(a.refs, "datasets", "*", "manifest.csv"))):
        for line in migrate(p, a.dry_run):
            print(" | ".join(line))
