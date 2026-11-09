# Dev workspace runbook (v1.7 integration, branch app/v1.6-integration)

The first real run on a Databricks workspace. Each step lists the exact command, the expected output, and the
UNVERIFIED item (docs/VERIFICATION.md) it closes. None of these commands has been run yet: the build box has no CLI
and no workspace. Stop at the first step whose output is not the expected one, and record what you saw in
docs/VERIFICATION.md.

Placeholders (set them once in the shell):

```bash
export TARGET=dev                       # dev | test | prod
export CATALOG=dam_dev                  # databricks.yml targets.<t>.variables.catalog
export WH=<serverless SQL warehouse id>
export OPS_EMAIL=<ops mailbox>
export RUNTIME_SP=<runtime SP application id, member of dam-portfolio-all>
export PROD_SP=<prod run_as SP application id>          # prod only
export APP_ROLE=<the app service principal's Postgres role>      # Lakebase GUC dam.app_role
export EXTRACT_ROLE=<role of the dam_lakebase_extract run_as>    # GUC dam.extract_role
export PUBLISH_LOGIN=<role of the dam_ref_publish run_as>        # GUC dam.publisher_login
VARS="--var warehouse_id=$WH --var ops_email=$OPS_EMAIL --var runtime_sp_app_id=$RUNTIME_SP"
```

## 0. Local gates (build box, before anything touches the workspace)

```bash
python -m pytest -q tests                      # expected: all passed, 2 skipped (pyspark, restricted CSV)
(cd app && python -m pytest -q)                # expected: all passed
python -m pytest -q qa                         # expected: only the 4 known qa/test_qa039_chain_adversarial.py failures
python qa/restricted_guard.py                  # expected: "0 hit(s)"
python tools/render_uc_reviewer_grants.py --check    # expected: "10b_reviewer_authority.sql grants in sync"
python tools/render_app_permissions.py --check       # expected: exit 0
```

## 1. Bundle validate (dev and prod) - closes BUNDLE-1, LB-REF, QA-037 / LB-REF (v1.5)

```bash
databricks bundle validate -t dev  $VARS
databricks bundle validate -t prod $VARS --var service_principal_app_id=$PROD_SP
databricks bundle validate -t prod $VARS --var service_principal_app_id=$PROD_SP -o json > /tmp/prod.json
```

Expected: both print `Validation OK!` with no warnings. In `/tmp/prod.json`:
`.resources.apps.dam_safety_app.permissions` lists only the `dam-<id>-members` groups and `dam-platform-admins` (never
`dam-portfolio-all`), and every `${resources.postgres_endpoints.dam_lakebase_primary.id}` reference has resolved to
`projects/dam-safety-prod/branches/production/endpoints/primary`. The `ddl_10a_row_filters` file path resolves to
`sql/ddl/10a_row_filters_table.sql`.

## 2. Deploy (dev)

```bash
databricks bundle deploy -t dev $VARS
databricks bundle summary -t dev
```

Expected: the jobs `dam_uc_setup_dam_dev`, `dam_ingest_dam_dev`, `dam_lakebase_extract_dam_dev`,
`dam_ref_publish_dam_dev`, the app `dam-safety-dev` and the Lakebase project `dam-safety-dev` (branch `production`,
endpoint `primary`) are listed. Closes LAKEBASE-HA (dev size) and the deploy half of BUNDLE-1.

## 3. Unity Catalog: schema and migrations

Fresh catalog (no tables yet): skip the migrations and only run `dam_uc_setup`.

Existing catalog created before v1.7: run the migrations **in this order**, each once (all are idempotent; re-running
one is safe), BEFORE `dam_uc_setup`. Run each file's PRE-CHECK queries (in its header) first; every one must return the
documented result.

1. `sql/migrations/v1_5_001_contract.sql`
2. `sql/migrations/v1_6_001_audit_chain.sql`
3. `sql/migrations/v1_6_002_chain_head.sql`
4. `sql/migrations/v1_6_003_alarm_rule_flag.sql`
5. `sql/migrations/v1_7_001_cdse_ack.sql`
6. `sql/migrations/v1_7_002_canon_v2.sql`
7. `sql/migrations/v1_7_003_dssmp_registration.sql`

Each file runs as a one-off SQL task on the synced bundle file (dev mode syncs to the deployer's `.bundle` folder;
the path below is the CLI default, UNVERIFIED):

```bash
FILES=/Workspace/Users/$(databricks current-user me -o json | jq -r .userName)/.bundle/dam_safety/$TARGET/files
for m in v1_5_001_contract v1_6_001_audit_chain v1_6_002_chain_head v1_6_003_alarm_rule_flag \
         v1_7_001_cdse_ack v1_7_002_canon_v2 v1_7_003_dssmp_registration; do
  databricks jobs submit --json "{\"run_name\": \"migrate $m\", \"tasks\": [{\"task_key\": \"m\",
    \"sql_task\": {\"warehouse_id\": \"$WH\", \"file\": {\"path\": \"$FILES/sql/migrations/$m.sql\",
    \"source\": \"WORKSPACE\"}, \"parameters\": {\"catalog\": \"$CATALOG\"}}}]}" || break
done
```

Expected: every run ends `SUCCESS`. Then:

```bash
databricks bundle run -t $TARGET dam_uc_setup
```

Expected: every task `SUCCESS`, including `ddl_10b_reviewer_authority` (views `ref.v_dam_reviewer_authority`,
`ref.v_dam_go_live_readiness`, `ref.v_dssmp_affected_rules`, functions `ref.assert_dam_go_live_ready`,
`ref.dssmp_affected_rules`). Spot checks in the SQL editor:

```sql
SHOW TBLPROPERTIES dam_dev.audit.review_record ('delta.appendOnly');            -- true
SELECT * FROM dam_dev.information_schema.check_constraints
 WHERE constraint_schema IN ('audit', 'ref') ORDER BY constraint_name;          -- ck_review_record_* (rule_reversion
                                                                                -- and dam subjects), ck_dam_cdse_assignment, ...
DESCRIBE VOLUME dam_dev.ref.dssmp_documents;                                   -- exists
SELECT count(*) FROM dam_dev.ref.v_dam_go_live_readiness;                       -- one row per dam
```

Closes MIGRATION-1, MIGRATION-2, MIGRATION-2b, MIGRATION-2c, MIGRATION-3, the Delta half of DSSMP-REG / CANON-V2 /
CDSE-ASSIGNMENT / RULE-REVERSION (CHECKs, views, SQL table function), DSSMP-DOC (volume) and UC-GRANTS (if the dam
groups exist).

## 4. Lakebase: schema, migrations, validator

Connect as the deployer (a member of `dam_lakebase_owner` and of the app role). Token and host via the SDK, the same
calls as `src/jobs/lakebase_extract.py` (`w.postgres.generate_database_credential`, `w.postgres.get_endpoint`;
attribute path UNVERIFIED):

```bash
EP=projects/dam-safety-$TARGET/branches/production/endpoints/primary
export PGHOST=$(python -c "from databricks.sdk import WorkspaceClient as W; print(W().postgres.get_endpoint(name='$EP').status.hosts.host)")
export PGPASSWORD=$(python -c "from databricks.sdk import WorkspaceClient as W; print(W().postgres.generate_database_credential(endpoint='$EP').token)")
export PGUSER=$(databricks current-user me -o json | jq -r .userName) PGDATABASE=databricks_postgres PGSSLMODE=require
export PGOPTIONS="-c dam.app_role=$APP_ROLE -c dam.extract_role=$EXTRACT_ROLE -c dam.publisher_login=$PUBLISH_LOGIN"
```

Fresh database, or simply converge any database to the current schema (idempotent):

```bash
psql -X -v ON_ERROR_STOP=1 -f lakebase/001_app_schema.sql
```

Existing database at the v1_7_003-era 001, step by step instead (same end state):

```bash
psql -X -v ON_ERROR_STOP=1 -f lakebase/migrations/v1_7_004_dssmp_outbox_contract.sql
psql -X -v ON_ERROR_STOP=1 -f lakebase/migrations/v1_7_005_cdse_reversion_tables.sql
psql -X -v ON_ERROR_STOP=1 -f lakebase/migrations/v1_7_006_chain_head_log.sql
```

Validator (read only):

```bash
python tools/validate_lakebase.py --app-role $APP_ROLE --lb-endpoint $EP
```

Expected: the single line `lakebase append-only/ownership: OK`, exit code 0 (each violation prints a
`FAIL <check>: <detail>` line and exit 1; exit 2 = cannot connect). Closes LB-OWNER, LB-PUBLISHER, OUTBOX-CONTRACT, LB-CDSE-TABLES, LB-HEAD-LOG (role model, advisory locks and
hashtext() on Lakebase), LB-PG14, LAKEBASE-AUTH (deployer path).

The app patch for the head log (`tools/patches/v17_chain_head_log_state.patch`) is App Dev's to apply AFTER
v1_7_006; an unpatched app keeps working.

## 5. App

```bash
databricks bundle run -t $TARGET dam_safety_app
```

Expected: app status RUNNING; the app's landing page answers for a member of a `dam-<id>-members` group; the app's own start-up schema check passes against the guarded tables (no `table_missing` / `not_guarded`
error in the app logs). Closes APP-ACL (group existence) and the app half of LAKEBASE-AUTH.

## 6. Jobs and their expected outputs

```bash
databricks bundle run -t $TARGET dam_lakebase_extract
databricks bundle run -t $TARGET dam_ref_publish
```

* `dam_lakebase_extract` / task `extract`: appends to `audit.review_record` and `audit.chain_head`; task
  `verify_review_chain` prints one JSON line per chain with `"ok": true`, a line `{"CDSE_FLAG_REQUIRE_SECOND_APPROVER":
  false}`, no error lines, and ends SUCCESS. Any `dam_cdse_*`, `rule_reversion_*`, `apply_*` / `reject_*`,
  `propose_*` error code, or `signer_refused` on prod, fails the task (by design). Closes V15-LB-EXTRACT, V16-CHAIN,
  QA-045d, PROD-SIGNER (prod).
* `dam_ref_publish`: task `apply_cdse_assignments` prints `{"pending": 0}` or one JSON line per outbox row
  (`"status": "loaded"` or `"rejected"` with `reason_code` `cdse_exists` / `cdse_stale` / `approval_*`); task
  `load_dssmp_registrations` and `publish_ref_changes` likewise (`{"pending": 0}` or one line per row). Closes
  PUBLISHER, DSSMP-REG, CDSE-ASSIGNMENT (load job, audit.event_log `CDSE_ASSIGNMENT_LOAD` rows) and RULE-REVERSION
  (publisher) on Delta.

## 7. Go-live of a dam (order matters)

1. Platform admin (ADMIN on `'*'`) assigns the first CDSE in the app (`dam_cdse_assign`, reason code
   `initial_appointment`) while the dam is not live.
2. `databricks bundle run -t $TARGET dam_ref_publish` -> `apply_cdse_assignments` line `"status": "loaded"`.
3. `SELECT dam_dev.ref.assert_dam_go_live_ready('<dam_id>');` -> `true` (otherwise it raises
   `dam_go_live_not_ready: ...`; do not continue).
4. DSSMP: dam_admin (or a platform admin) only PROPOSES the DSSMP version (`dssmp_version_propose`); the CDSE
   APPROVES it (`dssmp_version_approve`), which registers it. dam_admin never approves; the CDSE cannot register a
   version without a proposal (app 403).
5. `databricks bundle run -t $TARGET dam_ref_publish` -> `load_dssmp_registrations` line `"status": "loaded"`;
   `SELECT status FROM dam_dev.ref.dssmp_version WHERE dam_id = '<dam_id>'` -> `approved`, `volume_uri` set.
6. Only now publish thresholds (app onboarding -> `ref_change_publication` -> `publish_ref_changes`). A threshold
   publication before step 5 is refused (`dssmp_missing` / `dssmp_not_found` / `dssmp_draft` / ...).
7. Later DSSMP versions: rules stay on their version; `SELECT * FROM dam_dev.ref.v_dssmp_affected_rules` lists the
   rules to re-version; the CDSE approves one `rule_reversion` proposal in the app; `publish_ref_changes` applies the
   per-rule N+1 versions (payload `cause: "dssmp_reversion"`).

## 8. Rollback

Pause `dam_ref_publish` and `dam_lakebase_extract` first. Nothing below drops a table, column, volume or row.

* UC, newest first, each once: `v1_7_003_dssmp_registration_down.sql`, `v1_7_002_canon_v2_down.sql`,
  `v1_7_001_cdse_ack_down.sql`, `v1_6_003_alarm_rule_flag_down.sql`, `v1_6_002_chain_head_down.sql`,
  `v1_6_001_audit_chain_down.sql`, `v1_5_001_contract_down.sql` (same `jobs submit` loop as step 3). Each header has
  a PRE-CHECK; if it returns rows (newer audit records exist), STOP: those records are never deleted and that
  migration stays. Data-level fallback: `DESCRIBE HISTORY <table>` then `RESTORE TABLE <table> TO VERSION AS OF <n>`.
  Views / functions in 10b: re-run the previous `sql/ddl/10b_reviewer_authority.sql` from git.
* Lakebase, newest first: `lakebase/migrations/v1_7_006_chain_head_log_down.sql` (roll back the app patch first),
  `v1_7_005_cdse_reversion_tables_down.sql`, `v1_7_004_dssmp_outbox_contract_down.sql`. Data-level fallback: a
  Lakebase point-in-time branch / restore.
* Code: `databricks bundle deploy -t $TARGET` from the previous commit.

## 9. UNVERIFIED items this runbook closes

BUNDLE-1, LB-REF (v1.4), QA-037 / LB-REF (v1.5), LAKEBASE-AUTH, LAKEBASE-HA, APP-ACL, UC-GRANTS, MIGRATION-1,
MIGRATION-2, MIGRATION-2b, MIGRATION-2c, MIGRATION-3, MIGRATION-DOWN (if step 8 is exercised in dev), V15-LB-EXTRACT,
V16-CHAIN, QA-045d, LB-OWNER, LB-PG14, LB-PUBLISHER, OUTBOX-CONTRACT, PUBLISHER, DSSMP-REG, DSSMP-DOC, LB-CDSE-TABLES,
CDSE-ASSIGNMENT, RULE-REVERSION, LB-HEAD-LOG, PROD-SIGNER (prod target only). Not closed here: engineering defaults
(section 4 of VERIFICATION.md), INC-WIRE (needs a pyspark run of `reduce_inclinometers`), CANARI-1, DEP-*.
