"""Render the per-dam GRANT block of sql/ddl/10b_reviewer_authority.sql from the dam list (SCHEMA_CONTRACT §2, v1.6).

Each dam group `dam-<dam_id>-members` (the groups ref.dam_row_filter uses) gets USE SCHEMA on ref and SELECT on the
least-privilege view ref.v_dam_reviewer_authority and (v1.7, app SCHEMA_CONTRACT §7 item 18: OBO reads at go-live and
flag-apply) SELECT on ref.dssmp_version (per-dam row filter in both security modes, sql/ddl/10a_*), and nothing else:
never ref.dam or ref.project_membership.
Same dam-id source and validation as tools/render_app_permissions.py. CI runs --check.

Usage:
  python tools/render_uc_reviewer_grants.py            # rewrite the block
  python tools/render_uc_reviewer_grants.py --check    # exit 1 if out of sync (CI)
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL = os.path.join(ROOT, "sql", "ddl", "10b_reviewer_authority.sql")
BEGIN = "-- BEGIN GENERATED grants (tools/render_uc_reviewer_grants.py; do not edit by hand)"
END = "-- END GENERATED grants"
VIEW = "ref.v_dam_reviewer_authority"
DSSMP = "ref.dssmp_version"

_spec = importlib.util.spec_from_file_location("_rap", os.path.join(ROOT, "tools", "render_app_permissions.py"))
_rap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rap)
DEFAULT_CSV = _rap.DEFAULT_CSV


def render(dam_ids) -> str:
    ids = sorted(dict.fromkeys(d.strip() for d in dam_ids if d and d.strip()))
    if not ids:
        raise ValueError("no dam_ids")
    for d in ids:
        if not _rap._DAM_ID.fullmatch(d):
            raise ValueError(f"invalid dam_id {d!r} for a group name")
    lines = [BEGIN]
    for d in ids:
        g = f"`dam-{d}-members`"
        lines += [f"GRANT USE SCHEMA ON SCHEMA ref TO {g};", f"GRANT SELECT ON TABLE {VIEW} TO {g};",
                  f"GRANT SELECT ON TABLE {DSSMP} TO {g};"]
    lines.append(END)
    return "\n".join(lines) + "\n"


def apply(text: str, block: str) -> str:
    i, j = text.find(BEGIN), text.find(END)
    if i < 0 or j < 0 or j < i:
        raise ValueError("GENERATED grants markers not found")
    return text[:i] + block + text[j + len(END) + 1:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dams-csv", default=DEFAULT_CSV)
    ap.add_argument("--dam-ids", default=None)
    ap.add_argument("--sql", default=SQL)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    ids = a.dam_ids.split(",") if a.dam_ids else _rap.dam_ids_from_csv(a.dams_csv)
    cur = open(a.sql, encoding="utf-8").read()
    new = apply(cur, render(ids))
    if a.check:
        if new != cur:
            print("10b_reviewer_authority.sql grants out of sync: run python tools/render_uc_reviewer_grants.py",
                  file=sys.stderr)
            return 1
        print("10b_reviewer_authority.sql grants in sync")
        return 0
    if new != cur:
        with open(a.sql, "w", encoding="utf-8") as fh:
            fh.write(new)
    print("wrote", a.sql)
    return 0


if __name__ == "__main__":
    sys.exit(main())
