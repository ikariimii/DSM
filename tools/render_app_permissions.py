"""Render the `permissions:` block of resources/app.yml from the dam list (SCHEMA_CONTRACT §6, v1.5).

Bundles cannot loop, so the per-dam CAN_USE grants (`dam-<dam_id>-members`, the same groups the UC row filter
uses) are GENERATED here and committed. CI runs `--check` and fails when the committed block is out of sync.

Source of dam_ids (first that applies):
  --dam-ids SYN-DAM-01,SYN-DAM-02     explicit list (e.g. from a target variable)
  --dams-csv PATH                     a ref.dam export with a `dam_id` column (default: sample_data/ref/dam.csv)
Rows with status DECOMMISSIONED (if a `status` column exists) are skipped. dam-platform-admins CAN_MANAGE is
always appended. No other principal is ever written (portfolio-wide access = membership in each dam group).

Usage:
  python tools/render_app_permissions.py            # rewrite the block in resources/app.yml
  python tools/render_app_permissions.py --check    # exit 1 if resources/app.yml differs (CI)
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_YML = os.path.join(ROOT, "resources", "app.yml")
DEFAULT_CSV = os.path.join(ROOT, "sample_data", "ref", "dam.csv")
BEGIN = "      # BEGIN GENERATED permissions (tools/render_app_permissions.py; do not edit by hand)"
END = "      # END GENERATED permissions"
_DAM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
ADMIN_GROUP = "dam-platform-admins"


def dam_ids_from_csv(path: str) -> list[str]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [r["dam_id"].strip() for r in rows
            if r.get("dam_id") and str(r.get("status") or "").strip().upper() != "DECOMMISSIONED"]


def render(dam_ids) -> str:
    ids = sorted(dict.fromkeys(d.strip() for d in dam_ids if d and d.strip()))
    if not ids:
        raise ValueError("no dam_ids: the app would have no CAN_USE grant")
    for d in ids:
        if not _DAM_ID.fullmatch(d):
            raise ValueError(f"invalid dam_id {d!r} for a group name")
    lines = [BEGIN, "      permissions:"]
    for d in ids:
        lines += [f"        - group_name: dam-{d}-members", "          level: CAN_USE"]
    lines += [f"        - group_name: {ADMIN_GROUP}", "          level: CAN_MANAGE", END]
    return "\n".join(lines) + "\n"


def apply(text: str, block: str) -> str:
    i, j = text.find(BEGIN), text.find(END)
    if i < 0 or j < 0 or j < i:
        raise ValueError(f"markers not found in resources/app.yml: {BEGIN!r} ... {END!r}")
    return text[:i] + block + text[j + len(END) + 1:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dams-csv", default=DEFAULT_CSV)
    ap.add_argument("--dam-ids", default=None, help="comma-separated; overrides --dams-csv")
    ap.add_argument("--app-yml", default=APP_YML)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    ids = a.dam_ids.split(",") if a.dam_ids else dam_ids_from_csv(a.dams_csv)
    cur = open(a.app_yml, encoding="utf-8").read()
    new = apply(cur, render(ids))
    if a.check:
        if new != cur:
            print("resources/app.yml permissions are out of sync: run python tools/render_app_permissions.py",
                  file=sys.stderr)
            return 1
        print("resources/app.yml permissions in sync")
        return 0
    if new != cur:
        with open(a.app_yml, "w", encoding="utf-8") as fh:
            fh.write(new)
    print("wrote", a.app_yml)
    return 0


if __name__ == "__main__":
    sys.exit(main())
