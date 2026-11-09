"""FastAPI entrypoint for the Dam Safety Databricks App.

Demo mode (DEMO_MODE=true): in-memory seeded analytics for 5 dams + SQLite state.
Databricks mode: Databricks SQL warehouse reads on behalf of the user (OBO token
from X-Forwarded-Access-Token) and Lakebase Postgres state writes as the app
service principal.

Run with the factory form so importing this module has no side effects:
    uvicorn --factory backend.main:create_app --host 0.0.0.0 --port $DATABRICKS_APP_PORT"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import seed
from .audit_chain import NoopSigner
from .bootstrap import bootstrap_admins
from .config import Settings
from .repos.analytics import MockAnalytics, SqlAnalytics
from .repos.state import StateStore, lakebase_pool
from .routers import admin, alarm_rules, alarms, core, dssmp, instruments, onboarding, reports, signoffs

log = logging.getLogger("dam_safety_app")


def build_state(settings: Settings, pg_pool=None) -> StateStore:
    if settings.demo_mode:
        st = StateStore("sqlite", sqlite_path=settings.sqlite_path)
        for email, name, is_admin in seed.SEED_USERS:
            st.upsert_user(email, name, is_admin, "seed")
        for email, dam, role in seed.SEED_ROLES:
            st.set_role(email, dam, role, "seed")
        return st
    # Lakebase as the app service principal (injected PG* env + ENDPOINT_NAME); never as the user.
    return StateStore("postgres", schema=settings.state_schema, pg_pool=pg_pool or lakebase_pool())


def create_app(settings: Settings | None = None, pg_pool=None, analytics_factory=None, signer=None) -> FastAPI:
    settings = settings or Settings()
    settings.check()
    app = FastAPI(title="Dam Safety Monitoring", version="0.1.0")
    app.state.settings = settings
    app.state.store = build_state(settings, pg_pool)
    bootstrap_admins(app.state.store, settings.bootstrap_admins)
    if analytics_factory is not None:
        app.state.analytics_factory = analytics_factory
    elif settings.demo_mode:
        mock = MockAnalytics(seed.build())
        app.state.analytics_factory = lambda token: mock
    else:
        app.state.analytics_factory = lambda token: SqlAnalytics(settings, token)
    app.state.signer = signer or NoopSigner()  # QA-039 v2: future key-based signer hook (no-op by default)
    for r in (core, instruments, alarms, alarm_rules, dssmp, reports, onboarding, admin, signoffs):
        app.include_router(r.router)

    dist = Path(settings.static_dir)
    if (dist / "index.html").exists():
        if (dist / "assets").exists():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            f = (dist / path).resolve()
            if path and f.is_file() and dist.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")
    else:
        log.warning("frontend build not found at %s; serving API only", dist)
    return app
