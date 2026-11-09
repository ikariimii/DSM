#!/usr/bin/env python3
"""v1.7: run lakebase/validate_append_only.sql against a live Postgres/Lakebase and fail on any violation.

    python tools/validate_lakebase.py --app-role <app SP role> --dsn "host=... dbname=... user=..."
    python tools/validate_lakebase.py --app-role <role> --lb-endpoint projects/<p>/branches/<b>/endpoints/<e>

--dsn (or PG* env vars when omitted) connects directly; --lb-endpoint mints a 1 h OAuth token for the caller with
databricks-sdk w.postgres.generate_database_credential (same as src/jobs/lakebase_extract.py; UNVERIFIED on a
workspace). Exit codes: 0 pass, 1 violations (printed one per line), 2 cannot connect / query failed.
READ ONLY: the script only SELECTs from the catalogs (the session GUC dam.app_role is set for this session).
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_PATH = os.path.join(ROOT, "lakebase", "validate_append_only.sql")


def violations(conn, app_role: str) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('dam.app_role', %s, false)", (app_role,))
        cur.execute(open(SQL_PATH).read())
        return [(r[0], r[1]) for r in cur.fetchall()]


def _connect(args):
    import psycopg
    if args.lb_endpoint:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        token = w.postgres.generate_database_credential(endpoint=args.lb_endpoint).token
        host = w.postgres.get_endpoint(name=args.lb_endpoint).status.hosts.host     # UNVERIFIED attribute path
        return psycopg.connect(host=host, dbname=args.dbname, user=w.current_user.me().user_name, password=token,
                               sslmode="require", autocommit=True)
    return psycopg.connect(args.dsn or "", autocommit=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--app-role", required=True, help="the app service principal's Postgres role name")
    ap.add_argument("--dsn", default=os.environ.get("DAM_LAKEBASE_DSN"))
    ap.add_argument("--lb-endpoint", default=os.environ.get("DAM_LAKEBASE_ENDPOINT"))
    ap.add_argument("--dbname", default="databricks_postgres")
    args = ap.parse_args(argv)
    try:
        with _connect(args) as conn:
            bad = violations(conn, args.app_role)
    except Exception as e:  # noqa: BLE001 - any failure to check is a failed check
        print(f"ERROR cannot validate Lakebase: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    for check, detail in bad:
        print(f"FAIL {check}: {detail}")
    print("lakebase append-only/ownership: " + ("OK" if not bad else f"{len(bad)} violation(s)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
