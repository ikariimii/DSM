#!/usr/bin/env python3
"""QA-057: the ONE way to run a UC migration file (sql/migrations/*.sql) on a workspace, with a prod guard for
rollbacks. Layer 1 of 2 (layer 2 is the first statement of every *_down.sql, see DOWN_GUARD_SQL).

    python tools/run_migration.py --catalog dam_dev --file sql/migrations/v1_7_003_dssmp_registration.sql \\
        --warehouse-id <wh> --workspace-files /Workspace/Users/<me>/.bundle/dam_safety/dev/files
    python tools/run_migration.py ... --file sql/migrations/v1_7_003_dssmp_registration_down.sql \\
        --catalog dam_prod --break-glass-prod "INC-1234: rollback approved by <name>"     # prod rollback only
    add --dry-run to print the decision and the job submission without contacting the workspace.

Rules
* --catalog is normalized with dam_safety.catalog.normalize_catalog (NFKC, format characters removed, case folded);
  anything that is not then exactly dam_dev | dam_test | dam_prod is refused (exit 2). Look-alikes of dam_prod
  (fullwidth, zero-width characters) normalize to dam_prod and are therefore guarded like dam_prod.
* A *_down.sql file on dam_prod is refused (exit 3) unless --break-glass-prod "<reason>" is given; the reason must
  have at least MIN_REASON non-blank characters. A break-glass run is logged BEFORE anything is submitted: one JSON
  line on stderr and appended to --log-file (default ~/.dam_safety/break_glass.log): utc, user, catalog, file, its
  sha256, reason.
* The SQL task always receives the named parameters `catalog` (the canonical name) and `break_glass`
  (BREAK_GLASS_TOKEN only on an allowed prod rollback, else ''), so layer 2 can check them.
* Up migrations need no flag on any catalog.
Exit codes: 0 submitted (or dry run OK), 2 bad catalog / file, 3 refused by the prod guard, 4 submission failed.
UNVERIFIED on Databricks: the jobs.submit call shape (databricks-sdk SqlTask with a WORKSPACE file and parameters) and
the bundle files path.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dam_safety.catalog import PROD_CATALOG, normalize_catalog  # noqa: E402

BREAK_GLASS_TOKEN = "BREAK_GLASS_PROD"
MIN_REASON = 10
MIG_DIR = os.path.join(ROOT, "sql", "migrations")


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def decide(catalog, file, break_glass_reason=None) -> dict:
    """Pure decision: {catalog, file, is_down, break_glass, parameters} or Refused(code, message)."""
    try:
        cat = normalize_catalog(catalog)
    except ValueError as e:
        raise Refused(2, str(e)) from None
    path = os.path.abspath(file)
    if os.path.dirname(path) != MIG_DIR or not path.endswith(".sql") or not os.path.isfile(path):
        raise Refused(2, f"refusing {file!r}: not a file in sql/migrations/")
    is_down = path.endswith("_down.sql")
    reason = (break_glass_reason or "").strip()
    bg = False
    if is_down and cat == PROD_CATALOG:
        if break_glass_reason is None:
            raise Refused(3, f"QA-057: refusing {os.path.basename(path)} on {PROD_CATALOG}: down scripts need "
                             "--break-glass-prod \"<reason>\"")
        if len(reason) < MIN_REASON:
            raise Refused(3, f"QA-057: --break-glass-prod needs a reason of at least {MIN_REASON} characters")
        bg = True
    return {"catalog": cat, "file": path, "is_down": is_down, "break_glass": bg, "reason": reason if bg else None,
            "parameters": {"catalog": cat, "break_glass": BREAK_GLASS_TOKEN if bg else ""}}


def log_break_glass(d: dict, log_file: str) -> dict:
    entry = {"event": "QA-057_BREAK_GLASS_PROD", "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
             "user": os.environ.get("DATABRICKS_USER") or getpass.getuser(), "catalog": d["catalog"],
             "file": os.path.relpath(d["file"], ROOT),
             "sha256": hashlib.sha256(open(d["file"], "rb").read()).hexdigest(), "reason": d["reason"]}
    line = json.dumps(entry, sort_keys=True)
    print(line, file=sys.stderr)
    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return entry


def submission(d: dict, warehouse_id: str, workspace_files: str) -> dict:
    rel = os.path.relpath(d["file"], ROOT).replace(os.sep, "/")
    return {"run_name": f"migration {os.path.basename(d['file'])} on {d['catalog']}",
            "tasks": [{"task_key": "migration", "sql_task": {
                "warehouse_id": warehouse_id, "file": {"path": f"{workspace_files.rstrip('/')}/{rel}", "source": "WORKSPACE"},
                "parameters": d["parameters"]}}]}


def submit(spec: dict) -> str:       # UNVERIFIED on Databricks
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service import jobs
    t = spec["tasks"][0]["sql_task"]
    run = WorkspaceClient().jobs.submit(run_name=spec["run_name"], tasks=[jobs.SubmitTask(
        task_key="migration", sql_task=jobs.SqlTask(
            warehouse_id=t["warehouse_id"], parameters=t["parameters"],
            file=jobs.SqlTaskFile(path=t["file"]["path"], source=jobs.Source.WORKSPACE)))]).result()
    return str(run.state.result_state)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--warehouse-id", default=os.environ.get("DAM_WAREHOUSE_ID", ""))
    ap.add_argument("--workspace-files", default=os.environ.get("DAM_BUNDLE_FILES", ""))
    ap.add_argument("--break-glass-prod", metavar="REASON", default=None)
    ap.add_argument("--log-file", default=os.path.join(os.path.expanduser("~"), ".dam_safety", "break_glass.log"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        d = decide(a.catalog, a.file, a.break_glass_prod)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return e.code
    if d["break_glass"]:
        log_break_glass(d, a.log_file)
    spec = submission(d, a.warehouse_id, a.workspace_files)
    print(json.dumps(spec, sort_keys=True))
    if a.dry_run:
        return 0
    if not a.warehouse_id or not a.workspace_files:
        print("REFUSED: --warehouse-id and --workspace-files are required to submit", file=sys.stderr)
        return 2
    try:
        print(f"result_state: {submit(spec)}")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR submitting: {type(e).__name__}: {e}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
