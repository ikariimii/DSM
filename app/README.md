# Dam safety app (Databricks App: FastAPI + React)

Modules: a portfolio of 5 dams with drill-down; an instrument registry; time-series and inclinometer
plots; alarms with acknowledgement (open → acknowledged → closed); DSSMP/PFM; DSSMR draft reports;
a 10-step instrument onboarding wizard; per-dam ACL; and user/role admin.

```
app/
  app.yaml                 Databricks Apps runtime (command, env, valueFrom resources)
                           the bundle app resource lives at ../resources/app.yml (root bundle)
  requirements.txt         runtime deps (installed by Apps); requirements-dev.txt for tests
  SCHEMA_CONTRACT.md       app-side mapping to ../SCHEMA_CONTRACT.md
  backend/                 FastAPI (factory backend.main:create_app)
    config.py deps.py main.py formulas.py verify.py onboarding.py seed.py common.py
    routers/{core,instruments,alarms,dssmp,reports,onboarding,admin}.py  repos/{analytics,contract,state}.py
  frontend/                Vite + React + TS (built to frontend/dist, served by FastAPI)
  tests/                   pytest (incl. test_qa_oracle.py against ../qa/reference_examples.py)
```

## Security model
- **Identity** is keyed on `X-Forwarded-User` (stable), set by the Databricks Apps proxy; `X-Forwarded-Email`
  is used to bind an admin-granted email on first sign-in. If `X-Forwarded-User` is missing, the app returns
  **401**. The user token is never logged. It falls back to `DEMO_USER` only when `DEMO_MODE=true` (QA-002).
- **Analytics** go to the SQL warehouse **on behalf of the user** (`x-forwarded-access-token`, `SQL_OBO=true`),
  so UC row filters apply. The only user API scope is `sql` (plus `genie` if a Genie space is added).
- **Shared writes** (acks, drafts, users/ACL, audit) go to Lakebase Autoscaling as the app **service principal**
  (injected PG* env, `ENDPOINT_NAME`, `generate_database_credential` per new pooled connection). Acks are also
  written to `audit.event_log` as `ALARM_ACK`; DSSMR drafts go to `rpt.dssmr_draft`. No synced tables.
- **Who can open the app (SCHEMA_CONTRACT v1.4 §6, `resources/app.yml`):** `CAN_USE` is granted per dam to the
  `dam-<dam_id>-members` groups (one `permissions` entry per onboarded dam; the sample bundle lists
  `dam-SYN-DAM-01-members` … `dam-SYN-DAM-05-members`). It is **no longer** granted to `dam-portfolio-all`.
  `dam-platform-admins` keeps `CAN_MANAGE`. Onboarding a dam means adding its group to `resources/app.yml` (owned by
  the Data Architect) and redeploying; portfolio-wide users need membership in each dam group (or a deliberately
  added portfolio group). `CAN_USE` only gates opening the app: **data visibility is still enforced by the UC row
  filters** on the user's own token (OBO), and the app's per-dam roles below sit on top of both.
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
- **Reviewer actions are a hash-chained, append-only audit (QA-039 v2, SCHEMA_CONTRACT §5a).** Bias-proposal reviews,
  data-correction reviews (approve/reject), alarm acknowledgements (CDSE ack) and alarm closes each insert one row in
  Lakebase `review_signoff`: SSO identity (X-Forwarded-User, X-Forwarded-Email, unverified token `sub`; the token is
  never stored or logged), server UTC timestamp, action type, a **required non-empty rationale**, and the original
  value next to the corrected value (never overwritten). Rows are chained **per dam** from a 64×`0` genesis
  (`record_hash` covers the content plus `prev_hash`); appends are serialized (Postgres `SELECT … FOR UPDATE` on
  `review_chain_head`, sqlite `BEGIN IMMEDIATE`, `UNIQUE (chain_id, chain_seq)`). Verify with
  `GET /api/audit/signoffs/verify` (admin) or `GET /api/dams/{dam_id}/audit/signoffs/verify` (engineer+ on the dam):
  `ok`, or the first broken `chain_seq` and the reason. No key-based signature in-app (lead/FERC decision); nullable
  `signature`/`sig_alg`/`key_id` and a no-op `Signer` hook are kept. PDF/certificate signing of the DSSMR and
  PE-sealed exports is in [docs/BACKLOG.md](docs/BACKLOG.md).
- **Accuracy rule:** a computed value is shown only if its formula version is verified, its calibration
  is approved (inclinometer rows with `calib_id` NULL instead need `(INCL_PROBE_2PASS, 1.0.0)` verified in `ref.formula`
  and `installation_version` matching a `ref.incl_installation.effective_from`, v1.4 §5.5) and its QC flag is not
  BAD/MISSING. Everything else is shown as *unverified/hidden*.
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
1. The app resource is `resources/app.yml` at the root (done). It refers to `${var.warehouse_id}`,
   `${var.catalog}`, `resources.postgres_branches.dam_lakebase_production` and
   `resources.postgres_endpoints.dam_lakebase_primary`.
2. The root `databricks.yml` syncs `app/` minus `.venv`, `node_modules`, caches, `.coverage` and `app/tests` (done).
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
