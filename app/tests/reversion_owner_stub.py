"""QA-058 test helper: provision the OWNER's rule_reversion_proposal in a throwaway Postgres schema, as lakebase/001 does
(table owned by dam_lakebase_owner, status-transition trigger ENABLE ALWAYS), for live tests that bootstrap the app
WITHOUT running lakebase/001. Since QA-058 the app never creates that table on Postgres and refuses to start unless
it is owner-owned with the trigger enabled. The trigger function is taken verbatim from lakebase/001 (v1_7_009 block),
the table from state.DDL (the DA's parity test pins it to lakebase/001), so this stub cannot drift from either."""
import re
from pathlib import Path

from backend.repos.state import DDL, LAKEBASE_OWNER, REVERSION_TABLE, REVERSION_TRIGGER

LB001 = Path(__file__).resolve().parents[2] / "lakebase" / "001_app_schema.sql"


def _guard_function_sql(prefix: str) -> str:
    m = re.search(r"CREATE OR REPLACE FUNCTION dam_app\.status_transition_guard\(\).*?END \$\$;", LB001.read_text(),
                  re.S)
    assert m, "lakebase/001 no longer defines dam_app.status_transition_guard()"
    return m.group(0).replace("dam_app.", prefix)


def provision(conninfo: str, schema: str, enable: str = "ALWAYS") -> None:
    """Superuser conninfo. enable: 'ALWAYS' (as lakebase/001), '' (plain ENABLE = origin) or 'DISABLE'."""
    import psycopg
    prefix = f'"{schema}".'
    ddl = next(d for d in DDL if f"{{s}}{REVERSION_TABLE} (" in d).replace("{s}", prefix)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(f"""DO $$ BEGIN
            CREATE ROLE {LAKEBASE_OWNER} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            EXCEPTION WHEN duplicate_object THEN NULL; END $$""")
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.execute(ddl)
        conn.execute(f"ALTER TABLE {prefix}{REVERSION_TABLE} OWNER TO {LAKEBASE_OWNER}")
        conn.execute(_guard_function_sql(prefix))
        conn.execute(f"CREATE OR REPLACE TRIGGER {REVERSION_TRIGGER} BEFORE UPDATE ON {prefix}{REVERSION_TABLE} "
                     f"FOR EACH ROW EXECUTE FUNCTION {prefix}status_transition_guard('applied', 'rejected')")
        action = "DISABLE" if enable == "DISABLE" else f"ENABLE {enable}".strip()
        conn.execute(f"ALTER TABLE {prefix}{REVERSION_TABLE} {action} TRIGGER {REVERSION_TRIGGER}")
