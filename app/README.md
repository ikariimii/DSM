# Dam safety app (Databricks App: FastAPI + React)

Modules: a portfolio of 5 dams with drill-down; an instrument registry; time-series and inclinometer
plots; alarms with acknowledgement (open → acknowledged → closed); DSSMP/PFM; DSSMR draft reports;
a 10-step instrument onboarding wizard; per-dam ACL; and user/role admin.

```
app/
  app.yaml                 Databricks Apps runtime (command, env, valueFrom resources)
  requirements.txt         runtime deps (installed by Apps); requirements-dev.txt for tests
  resources/dam_safety_app.yml   PROPOSED bundle app resource (see Deploy)
  SCHEMA_CONTRACT.md       app-side mapping to ../SCHEMA_CONTRACT.md
  backend/                 FastAPI (factory backend.main:create_app)
    config.py deps.py main.py formulas.py verify.py onboarding.py seed.py common.py
    routers/{core,instruments,alarms,dssmp,reports,onboarding,admin}.py  repos/{analytics,contract,state}.py
  frontend/                Vite + React + TS (built to frontend/dist, served by FastAPI)
  tests/                   pytest (incl. test_qa_oracle.py against ../qa/reference_examples.py)
```

## Security model
- **Identity** comes from `X-Forwarded-Email`, set by the Databricks Apps proxy. If the header is missing,
  the app returns **401**. It falls back to `DEMO_USER` only when `DEMO_MODE=true` (QA-002).
- **Analytics** go to the SQL warehouse **on behalf of the user** (`x-forwarded-access-token`, `SQL_OBO=true`),
  so UC row filters apply. User API scopes are `sql` and `postgres` only (plus `dashboards.genie` if a Genie space is added).
- **Shared writes** (acks, drafts, users/ACL, audit) go to Lakebase Autoscaling as the app **service principal**.
- **Per-dam roles** are viewer < engineer < dso < dam_admin, plus a global admin, and are stored in
  `dam_app.dam_role`. They are a UX layer on top of the UC filters, not a replacement for them.
- **First admin outside demo (`BOOTSTRAP_ADMINS`):** set `BOOTSTRAP_ADMINS=<email>[,<email>]` in the bundle
  `config.env` **for the first prod deploy only, then remove it from prod config** and redeploy once the first
  admin has signed in and granted roles in the Admin UI. Rules (`backend/bootstrap.py`):
  - it is applied once at app startup, never per request, and it never overrides `app_user.is_admin`;
  - it creates the listed admins **only when the admin table is empty** (no active global admin). Otherwise
    it is ignored and changes nothing;
  - each admin it creates gets its own audit-log entry (`action=bootstrap_admin`, `actor=system`);
  - whenever it is set, the app logs a prominent `SECURITY WARNING` banner at startup, even when it is ignored.
  There is no default admin.
- **Accuracy rule:** a computed value is shown only if its formula version is verified, its calibration
  is approved and its QC flag is not BAD/MISSING. Everything else is shown as *unverified/hidden*.
  Units, calibration version and QC flags are shown on every value and plot.

## Run locally (demo mode, seed data for 5 dams, in-memory SQLite)
```bash
cd app
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
npm --prefix frontend ci && npm --prefix frontend run build
DEMO_MODE=true .venv/bin/uvicorn --factory backend.main:create_app --port 8000
# open http://localhost:8000 (acts as admin@demo.local; send X-Forwarded-Email to act as someone else)
```
If `DEMO_MODE` is unset or false and `DATABRICKS_WAREHOUSE_ID` is missing, the app refuses to start.

## Tests
```bash
cd app && .venv/bin/python -m pytest tests --cov=backend --cov-report=term-missing
```
The QA oracle cases run from `../qa/reference_examples.py`, which is never modified. Restricted
inclinometer cases are read by path from `$DAM_QA_PRIVATE`, and the test is skipped when that path is
absent. Restricted data must never be copied into this repo; check with `python ../qa/restricted_guard.py`.

## Deploy (Declarative Automation Bundle, the root `../databricks.yml`)
Prerequisites, all decided by the Data Architect (see SCHEMA_CONTRACT.md section 6):
1. Move `app/resources/dam_safety_app.yml` to `resources/app.yml` at the root. It refers to
   `${var.warehouse_id}`, `${var.catalog}` and `resources.postgres_branches.dam_lakebase_production`.
2. Remove `app/**` from `sync.exclude` in `databricks.yml`; otherwise the app source is not uploaded.
3. Build the frontend first. Apps does not build `frontend/`, because `package.json` is not at the app
   root, so `frontend/dist` is deployed as built.

```bash
npm --prefix app/frontend ci && npm --prefix app/frontend run build
databricks bundle validate -t dev
databricks bundle deploy   -t dev          # dev: 1 instance, Medium, Lakebase scale-to-zero
databricks bundle run dam_safety_app -t dev   # starts/redeploys the app
databricks bundle deploy   -t prod         # prod: Medium, 2-4 instances, run_as service principal
databricks bundle run dam_safety_app -t prod
```
None of this has been validated against a workspace; `databricks` CLI was not available on the build box.
