"""Runtime settings, read from environment variables (see app.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # DEMO_MODE=true: seeded in-memory analytics + SQLite state, no workspace needed.
    # Fail-safe default is False (QA-001): a deployed app must never silently serve seed data.
    demo_mode: bool = field(default_factory=lambda: _bool("DEMO_MODE", False))
    # Unity Catalog location of the analytics tables (see SCHEMA_CONTRACT.md).
    catalog: str = field(default_factory=lambda: os.getenv("DAM_CATALOG", "dam_dev"))
    schema_ref: str = field(default_factory=lambda: os.getenv("DAM_SCHEMA_REF", "ref"))
    schema_silver: str = field(default_factory=lambda: os.getenv("DAM_SCHEMA_SILVER", "silver"))
    schema_gold: str = field(default_factory=lambda: os.getenv("DAM_SCHEMA_GOLD", "gold"))
    schema_ops: str = field(default_factory=lambda: os.getenv("DAM_SCHEMA_OPS", "ops"))
    schema_audit: str = field(default_factory=lambda: os.getenv("DAM_SCHEMA_AUDIT", "audit"))
    schema_raw: str = field(default_factory=lambda: os.getenv("DAM_SCHEMA_RAW", "raw"))
    schema_rpt: str = field(default_factory=lambda: os.getenv("DAM_SCHEMA_RPT", "rpt"))
    # SQL warehouse id is injected through app.yaml valueFrom: sql-warehouse
    warehouse_id: str = field(default_factory=lambda: os.getenv("DATABRICKS_WAREHOUSE_ID", ""))
    # SQL reads always use the end user's forwarded token (UC row filters apply; api_contract.md: the app never
    # reads UC through its service principal). SQL_OBO=false is refused by check().
    sql_on_behalf_of_user: bool = field(default_factory=lambda: _bool("SQL_OBO", True))
    # Lakebase Postgres schema for app state. PGHOST/PGPORT/PGDATABASE/PGUSER/PGSSLMODE/PGAPPNAME are injected
    # by the `postgres` resource; ENDPOINT_NAME (projects/<p>/branches/<b>/endpoints/<e>) is set in app.yaml.
    state_schema: str = field(default_factory=lambda: os.getenv("STATE_SCHEMA", "dam_app"))
    sqlite_path: str = field(default_factory=lambda: os.getenv("SQLITE_PATH", ":memory:"))
    # Demo-only: default identity when no X-Forwarded-User header is present.
    demo_user: str = field(default_factory=lambda: os.getenv("DEMO_USER", "admin@demo.local"))
    # Comma-separated first-deploy admins. Applied once at startup, ONLY if no active admin exists
    # (backend/bootstrap.py). Set it for the first prod deploy, then remove it from prod config.
    # No default (QA-002); demo mode seeds its own admin user instead.
    bootstrap_admins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            e.strip().lower() for e in os.getenv("BOOTSTRAP_ADMINS", "").split(",") if e.strip()
        )
    )
    # PROVISIONAL approval rule for live cdse_ack_required edits (Genie, v1.6; the user is deciding). false (default,
    # current behaviour): the dam's CDSE may set/clear directly and approve their own proposal. true: every change
    # goes through a proposal, the approver must be the dam's CDSE AND not the proposer (routers/alarm_rules.py).
    cdse_flag_require_second_approver: bool = field(
        default_factory=lambda: _bool("CDSE_FLAG_REQUIRE_SECOND_APPROVER", False))
    static_dir: str = field(
        default_factory=lambda: os.getenv(
            "STATIC_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
        )
    )

    def uc(self, layer: str, table: str) -> str:
        schema = {"ref": self.schema_ref, "silver": self.schema_silver, "gold": self.schema_gold,
                  "ops": self.schema_ops, "audit": self.schema_audit, "raw": self.schema_raw,
                  "rpt": self.schema_rpt}[layer]
        return f"`{self.catalog}`.`{schema}`.`{table}`"

    def check(self) -> None:
        """Refuse to start in an unsafe configuration (QA-001). Called by create_app before anything is built."""
        # QA-041: seed data must never be served against the production catalog. Compared after trimming whitespace
        # and identifier quotes, case-insensitively (' DAM_PROD ', '`dam_prod`'). Deliberately NO override or escape
        # hatch (no env var, no flag): to demo, point DAM_CATALOG at a non-prod catalog.
        if self.demo_mode and is_prod_catalog(self.catalog):
            raise RuntimeError(f"DEMO_MODE=true with the production catalog ({self.catalog!r}) is refused (QA-041): "
                               "demo seed data must never be served against dam_prod; refusing to start")
        if not self.demo_mode and not self.warehouse_id:
            raise RuntimeError("DEMO_MODE is false but DATABRICKS_WAREHOUSE_ID is not set; refusing to start")
        if not self.demo_mode and not self.sql_on_behalf_of_user:
            raise RuntimeError("SQL_OBO=false is not supported: gold is read only as the signed-in user")


PROD_CATALOG = "dam_prod"


def is_prod_catalog(catalog) -> bool:
    """True for the production catalog however it is spelled in config: surrounding whitespace, identifier quotes
    (backticks / double quotes) and letter case are ignored."""
    return str(catalog or "").strip().strip("`\"").strip().lower() == PROD_CATALOG


def approval_policy(s: "Settings") -> dict:
    """Approval rule for live cdse_ack_required edits, as exposed to the UI (GET /api/me, GET .../alarm-rules).
    PROVISIONAL (Genie, v1.6): the user is deciding whether to require a second approver."""
    on = bool(s.cdse_flag_require_second_approver)
    return {"cdse_flag_require_second_approver": on, "provisional": True,
            "cdse_may_apply_directly": not on, "cdse_may_self_approve": not on,
            "note": ("Approval rule provisional: a second approver is required - the CDSE cannot set/clear directly "
                     "or approve their own proposal" if on else
                     "Approval rule provisional: currently the dam CDSE may self-approve")}
