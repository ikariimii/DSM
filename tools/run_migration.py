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

QA-059: Lakebase migrations (lakebase/migrations/*.sql) go through the same tool with --lakebase:

    python tools/run_migration.py --lakebase --file lakebase/migrations/v1_7_010_rule_reversion_owner.sql \
        --app-role <app SP role> [--publisher-login <login>] [--extract-role <role>] --dsn "<libpq>" | --lb-endpoint <ep>
    ... --rerun "<reason, >= 10 characters>"     # deliberately re-apply an applied (or older) migration
    ... --baseline                               # record a hand-migrated database: this file and every older up
                                                 # file as applied ('baseline'), without running anything

* Ledger dam_app.schema_migration_ledger (owned by dam_lakebase_owner, append-only; Lakebase migration v1_7_011,
  whose block the tool runs itself first, idempotently). Applied = the latest ledger row of a migration is up or
  baseline, plus PROBES (the effect of a hardening is present, so a database migrated before the ledger cannot
  have its hardening undone).
* Refused (exit 3): an up file that is applied, or OLDER than the newest applied one, unless --rerun "<reason>";
  a down file whose up is not applied or is not the newest applied, unless --rerun "<reason>"; and, always, flag or
  not, a file in SUPERSEDED once a later hardening that it would undo is applied (v1_7_005 after v1_7_010).
* After every applied file the validator lakebase/validate_append_only.sql runs; any violation = exit 5 (the file
  is applied and recorded; fix forward). A file that fails = exit 4, nothing recorded.
* A session advisory lock (hashtext(RUNNER_LOCK_KEY)) serialises concurrent runners.
Why both a ledger and the validator: the ledger stops an unsafe file BEFORE it runs (the validator only detects
afterwards, when the app may already have used the window); the validator still catches what no rule anticipated.
The files themselves are also ownership-safe on a direct psql re-run (defense in depth; QA-059 tests).
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
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dam_safety.catalog import PROD_CATALOG, normalize_catalog  # noqa: E402

BREAK_GLASS_TOKEN = "BREAK_GLASS_PROD"
MIN_REASON = 10
MIG_DIR = os.path.join(ROOT, "sql", "migrations")
LB_MIG_DIR = os.path.join(ROOT, "lakebase", "migrations")
LB_NAME = re.compile(r"^(v\d+_\d+_\d{3})_[a-z0-9_]+\.sql$")
LEDGER = "dam_app.schema_migration_ledger"
LEDGER_FILE = os.path.join(LB_MIG_DIR, "v1_7_011_migration_ledger.sql")
RUNNER_LOCK_KEY = "dam_safety:tools/run_migration.py"   # single-key advisory lock on hashtext(key); no new numeric
# lock constant (QA-056: only INIT 4702045, chain 4702046 and dam 4702047 exist; two-key locks are a separate space)
# file -> the later migrations that harden what it would undo: refused once ANY of them is applied, no flag overrides
SUPERSEDED = {"v1_7_005_cdse_reversion_tables.sql": ("v1_7_010_rule_reversion_owner.sql",)}
# migration -> SQL that is true when its effect is present (databases migrated by hand before the ledger existed)
PROBES = {
    "v1_7_010_rule_reversion_owner.sql":
        "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_roles o ON o.oid = c.relowner "
        "WHERE c.oid = to_regclass('dam_app.rule_reversion_proposal') AND o.rolname = 'dam_lakebase_owner')",
    "v1_7_009_status_transition_guard.sql":
        "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 't_status_dam_cdse_outbox')",
    "v1_7_006_chain_head_log.sql": "SELECT to_regclass('dam_app.review_chain_head_log') IS NOT NULL",
}


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


# ---------------------------------------------------------------- QA-059: Lakebase migrations + ledger
def _lb_up_name(fname):
    return fname[:-len("_down.sql")] + ".sql" if fname.endswith("_down.sql") else fname


def mig_key(fname):
    """Numeric order of v<major>_<minor>_<nnn>_*.sql (v1.8: v1_10_* sorts after v1_9_*, not before v1_2_*)."""
    m = re.match(r"^v(\d+)_(\d+)_(\d+)_", fname)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), fname) if m else (-1, -1, -1, fname)


def lakebase_files():
    return sorted((f for f in os.listdir(LB_MIG_DIR) if LB_NAME.match(f) and not f.endswith("_down.sql")),
                  key=mig_key)


def applied_set(ledger_rows, probed=()) -> set:
    """ledger_rows: [(migration, direction)] in ledger order -> up files currently applied (latest row up or
    baseline), plus every probed file."""
    last = {}
    for m, d in ledger_rows:
        last[_lb_up_name(m)] = d
    return {m for m, d in last.items() if d in ("up", "baseline")} | set(probed)


def decide_lakebase(file, ledger_rows, probed=(), rerun_reason=None) -> dict:
    """Pure decision for a Lakebase migration file: {file, name, up, is_down, rerun, reason} or Refused."""
    path = os.path.abspath(file)
    name = os.path.basename(path)
    if os.path.dirname(path) != LB_MIG_DIR or not LB_NAME.match(name) or not os.path.isfile(path):
        raise Refused(2, f"refusing {file!r}: not a migration file in lakebase/migrations/")
    up, is_down = _lb_up_name(name), name.endswith("_down.sql")
    applied = applied_set(ledger_rows, probed)
    newest = max(applied, default=None, key=mig_key)
    hard = [m for m in SUPERSEDED.get(up, ()) if m in applied]
    if hard and not is_down:
        raise Refused(3, f"QA-059: refusing {name}: {', '.join(hard)} is applied and {name} would undo it "
                         "(no flag overrides this)")
    reason = (rerun_reason or "").strip()
    problem = None
    if not is_down and up in applied:
        problem = f"{name} is already applied"
    elif not is_down and newest is not None and mig_key(up) < mig_key(newest):
        problem = f"{name} is older than the newest applied migration {newest}"
    elif is_down and up not in applied:
        problem = f"{name}: {up} is not applied"
    elif is_down and up != newest:
        problem = f"{name}: {up} is not the newest applied migration ({newest}); roll back in reverse order"
    if problem and rerun_reason is None:
        raise Refused(3, f"QA-059: refusing: {problem} (re-run deliberately with --rerun \"<reason>\")")
    if problem and len(reason) < MIN_REASON:
        raise Refused(3, f"QA-059: --rerun needs a reason of at least {MIN_REASON} characters")
    return {"file": path, "name": name, "up": up, "is_down": is_down, "rerun": bool(problem),
            "reason": reason if problem else None, "problem": problem}


def split_pending(bad, applied, exists):
    """v1.8: the validator checks the CURRENT schema, so stepping through the migrations one by one would report the
    objects of the not-yet-applied ones as missing. A violation is 'pending' (not a failure) only when every dam_app
    table it names does not exist at all AND is created by an up file that is not applied yet AND is named in no
    applied up file. Anything else (an existing table without its guard, a privilege, an owner) stays a violation.
    Returns (pending, real)."""
    files = lakebase_files()
    pend = "\n".join(open(os.path.join(LB_MIG_DIR, f)).read() for f in files if f not in applied)
    done = "\n".join(open(os.path.join(LB_MIG_DIR, f)).read() for f in files if f in applied)
    pending, real = [], []
    for check, detail in bad:
        tabs = re.findall(r"dam_app\.(\w+)", str(detail))
        if tabs and all(not exists(t) and re.search(rf"CREATE TABLE IF NOT EXISTS dam_app\.{t}\b", pend)
                        and not re.search(rf"\b{t}\b", done) for t in tabs):
            pending.append((check, detail))
        else:
            real.append((check, detail))
    return pending, real


def _ledger_block():
    s = open(LEDGER_FILE).read()
    return s[s.index("-- BEGIN v1_7_011 block"):s.index("-- END v1_7_011 block")]


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _lb_connect(a):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import validate_lakebase
    return validate_lakebase._connect(a)


def _set_gucs(cur, a):
    for k, v in (("dam.app_role", a.app_role), ("dam.extract_role", a.extract_role),
                 ("dam.publisher_login", a.publisher_login)):
        if v:
            cur.execute("SELECT set_config(%s, %s, false)", (k, v))


def run_lakebase(a, conn) -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import validate_lakebase
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtext(%s)::bigint)", (RUNNER_LOCK_KEY,))
        try:
            _set_gucs(cur, a)
            cur.execute("BEGIN")
            cur.execute(_ledger_block())                     # idempotent: the ledger exists before the first read
            cur.execute("COMMIT")
            cur.execute(f"SELECT migration, direction FROM {LEDGER} ORDER BY ledger_id")
            rows = [(r[0], r[1]) for r in cur.fetchall()]
            probed = []
            for m, q in PROBES.items():
                cur.execute(q)
                if cur.fetchone()[0]:
                    probed.append(m)
            if a.baseline:
                name = os.path.basename(os.path.abspath(a.file))
                if not LB_NAME.match(name) or name.endswith("_down.sql") or name not in lakebase_files():
                    raise Refused(2, f"--baseline needs an up file in lakebase/migrations/, not {a.file!r}")
                todo = [f for f in lakebase_files() if mig_key(f) <= mig_key(name) and f not in applied_set(rows)]
                for f in todo:
                    cur.execute(f"INSERT INTO {LEDGER} (migration, direction, sha256) VALUES (%s, 'baseline', %s)",
                                (f, _sha(os.path.join(LB_MIG_DIR, f))))
                print(json.dumps({"baseline": todo}))
                return 0
            d = decide_lakebase(a.file, rows, probed, a.rerun)
            print(json.dumps({k: d[k] for k in ("name", "is_down", "rerun", "reason", "problem")}, sort_keys=True))
            if a.dry_run:
                return 0
            try:
                cur.execute(open(d["file"]).read())
                if conn.info.transaction_status != 0:        # a file that left a transaction open: refuse to guess
                    cur.execute("ROLLBACK")
                    raise RuntimeError("the file left a transaction open; rolled back")
            except Exception as e:  # noqa: BLE001
                try:
                    cur.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                print(f"ERROR applying {d['name']}: {type(e).__name__}: {e}", file=sys.stderr)
                return 4
            _set_gucs(cur, a)
            cur.execute(f"INSERT INTO {LEDGER} (migration, direction, sha256, rerun_reason) VALUES (%s, %s, %s, %s)",
                        (d["name"], "down" if d["is_down"] else "up", _sha(d["file"]), d["reason"]))
        finally:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (RUNNER_LOCK_KEY,))
    bad = validate_lakebase.violations(conn, a.app_role)
    applied_now = applied_set(rows + [(d["name"], "down" if d["is_down"] else "up")], probed)
    with conn.cursor() as cur:
        def exists(t):
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"dam_app.{t}",))
            return cur.fetchone()[0]
        pending, bad = split_pending(bad, applied_now, exists)
    for check, detail in pending:
        print(f"PENDING {check}: {detail} (created by a later migration that is not applied yet)", file=sys.stderr)
    for check, detail in bad:
        print(f"FAIL {check}: {detail}", file=sys.stderr)
    print(f"validator: {'OK' if not bad else str(len(bad)) + ' violation(s)'}"
          + (f" ({len(pending)} pending a later migration)" if pending else ""))
    return 5 if bad else 0


def main_lakebase(a) -> int:
    if not a.app_role:
        print("REFUSED: --lakebase needs --app-role (the validator runs after every file)", file=sys.stderr)
        return 2
    try:
        conn = _lb_connect(a)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR cannot connect to Lakebase: {type(e).__name__}: {e}", file=sys.stderr)
        return 4
    try:
        return run_lakebase(a, conn)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return e.code
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--catalog")
    ap.add_argument("--file", required=True)
    ap.add_argument("--lakebase", action="store_true", help="QA-059: a lakebase/migrations file (ledger-checked)")
    ap.add_argument("--dsn", default=os.environ.get("DAM_LAKEBASE_DSN"))
    ap.add_argument("--lb-endpoint", default=os.environ.get("DAM_LAKEBASE_ENDPOINT"))
    ap.add_argument("--dbname", default="databricks_postgres")
    ap.add_argument("--app-role")
    ap.add_argument("--extract-role")
    ap.add_argument("--publisher-login")
    ap.add_argument("--rerun", metavar="REASON", default=None)
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--warehouse-id", default=os.environ.get("DAM_WAREHOUSE_ID", ""))
    ap.add_argument("--workspace-files", default=os.environ.get("DAM_BUNDLE_FILES", ""))
    ap.add_argument("--break-glass-prod", metavar="REASON", default=None)
    ap.add_argument("--log-file", default=os.path.join(os.path.expanduser("~"), ".dam_safety", "break_glass.log"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.lakebase:
        return main_lakebase(a)
    if a.catalog is None:
        print("REFUSED: --catalog is required for a UC migration", file=sys.stderr)
        return 2
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
