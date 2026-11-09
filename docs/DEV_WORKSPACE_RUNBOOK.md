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
8. `sql/migrations/v1_8_001_dam_lifecycle.sql` (v1.8; the backfill `v1_8_002_lifecycle_backfill.sql` runs after
   `dam_uc_setup` and the Lakebase step, see step 7b)

Each file runs as a one-off SQL task on the synced bundle file (dev mode syncs to the deployer's `.bundle` folder;
the path below is the CLI default, UNVERIFIED):

```bash
FILES=/Workspace/Users/$(databricks current-user me -o json | jq -r .userName)/.bundle/dam_safety/$TARGET/files
for m in v1_5_001_contract v1_6_001_audit_chain v1_6_002_chain_head v1_6_003_alarm_rule_flag \
         v1_7_001_cdse_ack v1_7_002_canon_v2 v1_7_003_dssmp_registration v1_8_001_dam_lifecycle; do
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

Existing database at the v1_7_003-era 001, step by step instead (same end state). QA-059: Lakebase migration files
go through `tools/run_migration.py --lakebase` ONLY (not bare psql). It records each file in the ledger
`dam_app.schema_migration_ledger` (v1_7_011, owned by dam_lakebase_owner, append-only), refuses a file that is
already applied or older than the newest applied one unless `--rerun "<reason>"` is given, ALWAYS refuses
`v1_7_005` once `v1_7_010` is applied (no flag overrides it: 005 predates the rule_reversion_proposal hardening), and
runs the validator after every file (exit 5 on any violation). Do NOT re-run v1_7_005 after v1_7_010 by any route.

```bash
lb() { python tools/run_migration.py --lakebase --app-role "$APP_ROLE" --publisher-login "$PUBLISH_LOGIN" \
        --extract-role "$EXTRACT_ROLE" "$@"; }     # connects with the PG* variables above (or --dsn)
lb --file lakebase/migrations/v1_7_004_dssmp_outbox_contract.sql
lb --file lakebase/migrations/v1_7_005_cdse_reversion_tables.sql
lb --file lakebase/migrations/v1_7_006_chain_head_log.sql
lb --file lakebase/migrations/v1_7_007_qa056_unique_guards.sql   # run its PRE-CHECK first
lb --file lakebase/migrations/v1_7_008_cdse_outbox_status.sql
lb --file lakebase/migrations/v1_7_009_status_transition_guard.sql
lb --file lakebase/migrations/v1_7_010_rule_reversion_owner.sql
lb --file lakebase/migrations/v1_7_011_migration_ledger.sql
lb --file lakebase/migrations/v1_8_001_lifecycle_outbox.sql     # v1.8 lifecycle outbox
```

A database migrated by hand before the ledger existed: record what is already applied first (writes `baseline`
rows for that file and every older one; runs nothing), e.g. `lb --file lakebase/migrations/v1_7_010_rule_reversion_owner.sql --baseline`.
The runner also probes for the v1_7_006 / v1_7_009 / v1_7_010 effects, so 005 stays refused on such a database
even before the baseline. Exit codes: 0 applied (validator OK), 2 bad file / arguments, 3 refused (ledger rules),
4 the file failed (nothing recorded), 5 applied but the validator reports violations (fix forward).
A validator finding about a table that only a later, not-yet-applied migration creates is printed `PENDING` and
does not fail the step (v1.8).
Re-running `lakebase/001_app_schema.sql` stays safe after any migration (it never hands an owner-owned or
guard-triggered table to the app; tested). Rollbacks: reverse order only (the runner refuses a down that is not the
newest applied migration; the v1_7_005 down also refuses by itself while v1_7_009's status guard exists).

Validator (read only):

```bash
python tools/validate_lakebase.py --app-role $APP_ROLE --lb-endpoint $EP
```

Expected: the single line `lakebase append-only/ownership: OK`, exit code 0 (each violation prints a
`FAIL <check>: <detail>` line and exit 1; exit 2 = cannot connect). Closes LB-OWNER, LB-PUBLISHER, OUTBOX-CONTRACT, LB-CDSE-TABLES, LB-HEAD-LOG (role model, advisory locks and
hashtext() on Lakebase: step 4b P5), LB-PG14 (step 4b P6), LAKEBASE-AUTH (deployer path).

The app's head-log change (state.py, applied by App Dev in 478b7b5; lock order dam 4702047 -> check -> chain 4702046)
needs v1_7_006 FIRST: run the Lakebase migrations before deploying an app at or after 478b7b5 (step 5).

## 4b. UNVERIFIED primitives: one step each, with a pass/fail check (dev only, after step 4)

Run these in **dev** before step 5. Each step is independent; stop at the first FAIL and record the output in
docs/VERIFICATION.md. UC steps use a scratch schema `zz_verify` in the dev catalog (dropped at the end); Lakebase
steps use the `psql` environment of step 4. Never run P1 to P4 or P8's probe against dam_prod.

Helper for the UC steps (one statement per call through the SQL Statement Execution API; prints
`["<state>", "<error message if any>", <rows>]`, every value as a string):

```bash
q() { databricks api post /api/2.0/sql/statements --json "$(jq -n --arg s "$1" --arg w "$WH" --arg c "$CATALOG" \
  '{warehouse_id: $w, catalog: $c, statement: $s, wait_timeout: "50s", on_wait_timeout: "CANCEL"}')" \
  | jq -c '[.status.state, (.status.error.message // ""), (.result.data_array // [])]'; }
q "SELECT current_catalog()"                    # expected: ["SUCCEEDED","",[["dam_dev"]]]
q "CREATE SCHEMA IF NOT EXISTS zz_verify"       # expected: ["SUCCEEDED","",[]]
```

### P1. Delta MERGE with a subquery source (MIGRATION-3, PUBLISHER, DSSMP-REG, CDSE-ASSIGNMENT)

Same shape as `ref_change_publish.merge_sql`: the source is a subquery with an EXISTS over the target.

```bash
q "CREATE OR REPLACE TABLE zz_verify.m_t (id STRING, v INT, cur BOOLEAN)"
q "INSERT INTO zz_verify.m_t VALUES ('a', 1, true)"
q "CREATE OR REPLACE TABLE zz_verify.m_s AS SELECT * FROM VALUES ('a', 2, 1, 'insert'), ('a', 1, 1, 'close'), ('b', 2, 1, 'insert') AS s(id, v, supersedes, act)"
q "MERGE INTO zz_verify.m_t AS t USING (SELECT s.* FROM zz_verify.m_s AS s WHERE EXISTS (SELECT 1 FROM zz_verify.m_t AS c WHERE c.id = s.id AND c.cur AND c.v = s.supersedes)) AS s ON t.id = s.id AND t.v = s.v WHEN MATCHED AND s.act = 'close' AND t.cur THEN UPDATE SET cur = false WHEN NOT MATCHED AND s.act = 'insert' THEN INSERT (id, v, cur) VALUES (s.id, s.v, true)"
q "SELECT id, v, cur FROM zz_verify.m_t ORDER BY id, v"
```

PASS: the MERGE returns `SUCCEEDED` and the last line is exactly
`["SUCCEEDED","",[["a","1","false"],["a","2","true"]]]` (the stale `b` row is filtered by the EXISTS).
FAIL: any `FAILED` (e.g. subqueries not allowed in the MERGE source), or a `b` row, or `a 1` still `true`.

### P2. SQL UDF using raise_error, incl. ref.assert_dam_go_live_ready (CDSE-ASSIGNMENT)

```bash
q "CREATE OR REPLACE FUNCTION zz_verify.must_be_pos(x INT) RETURNS BOOLEAN RETURN CASE WHEN x > 0 THEN true ELSE raise_error(concat('zz_neg: ', x)) END"
q "SELECT zz_verify.must_be_pos(1)"
q "SELECT zz_verify.must_be_pos(-1)"
q "SELECT ref.assert_dam_go_live_ready('zz_no_such_dam')"
q "SELECT dam_id FROM ref.v_dam_reviewer_authority WHERE revoked_ts IS NULL LIMIT 1"   # a dam with a CDSE, if any
q "SELECT ref.assert_dam_go_live_ready('<dam_id from the previous line>')"
```

PASS, in order: `["SUCCEEDED","",[]]`; `["SUCCEEDED","",[["true"]]]`; `["FAILED","...zz_neg: -1...",[]]`;
`["FAILED","...dam_go_live_not_ready: zz_no_such_dam has no CDSE in force in ref.v_dam_reviewer_authority...",[]]`;
(after step 7.2 only) `["SUCCEEDED","",[["true"]]]` for a dam with a CDSE in force.
FAIL: the CREATE FUNCTION fails (raise_error not allowed in a SQL UDF), `-1` returns `null` / `false`, or the readiness
function returns anything but `true` / the `dam_go_live_not_ready` error.

### P3. SQL table function ref.dssmp_affected_rules (RULE-REVERSION)

```bash
q "DESCRIBE FUNCTION EXTENDED ref.dssmp_affected_rules"
q "SELECT count(*) FROM ref.dssmp_affected_rules('zz_no_such_dam', 'zz_none')"
q "SELECT (SELECT count(*) FROM ref.dssmp_affected_rules(d.dam_id, 'zz_none')) = (SELECT count(*) FROM ref.threshold t WHERE t.is_current AND t.dam_id = d.dam_id) FROM (SELECT min(dam_id) AS dam_id FROM ref.threshold) d"
```

PASS: the DESCRIBE output contains `Type: TABLE` (or `TABLE` in the type row) and the five return columns `dam_id`,
`threshold_id`, `threshold_version`, `dssmp_version_id`, `cdse_ack_required`; the count is `[["0"]]`; the comparison is
`[["true"]]` (or `[["null"]]` / `[[null]]` when ref.threshold is still empty: then re-run after the first publication).
FAIL: `FAILED` on any line (table-function syntax, or a correlated table-function argument not supported: then run the
third line with a literal dam_id instead before calling it a FAIL) or `false`.

### P4. ALTER COLUMN DROP DEFAULT / DROP NOT NULL on Delta (MIGRATION-DOWN, v1_7_001 down)

```bash
q "CREATE OR REPLACE TABLE zz_verify.d (id INT, f BOOLEAN) TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported')"
q "ALTER TABLE zz_verify.d ALTER COLUMN f SET DEFAULT false"
q "INSERT INTO zz_verify.d (id) VALUES (1)"
q "ALTER TABLE zz_verify.d ALTER COLUMN f SET NOT NULL"
q "ALTER TABLE zz_verify.d ALTER COLUMN f DROP NOT NULL"
q "ALTER TABLE zz_verify.d ALTER COLUMN f DROP DEFAULT"
q "ALTER TABLE zz_verify.d ALTER COLUMN f DROP DEFAULT"
q "INSERT INTO zz_verify.d (id) VALUES (2)"
q "SELECT id, f FROM zz_verify.d ORDER BY id"
```

PASS: every statement `SUCCEEDED` (the second DROP DEFAULT too: the down is re-run safe) and the last line is
`["SUCCEEDED","",[["1","false"],["2",null]]]` (row 1 kept its value; row 2 got no default and NULL was accepted).
FAIL: any `FAILED`, or row 2 reads `"false"` (default not dropped). If only the repeated DROP DEFAULT fails, the
v1_7_001 down is not re-run safe: record it.

### P5. Lakebase advisory locks and hashtext() (LB-HEAD-LOG, QA-045c, QA-056)

```bash
psql -X -At -c "SELECT hashtext('zz_verify') = hashtext('zz_verify'), pg_typeof(hashtext('zz_verify'))"
psql -X -q -At -c "BEGIN" -c "SELECT pg_advisory_xact_lock(4702046, hashtext('zz_verify'))" -c "SELECT pg_sleep(8)" -c "COMMIT" &
sleep 3
psql -X -At -c "SELECT pg_try_advisory_xact_lock(4702046, hashtext('zz_verify'))"
psql -X -At -c "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = 4702046 AND granted"
wait
psql -X -At -c "SELECT pg_try_advisory_xact_lock(4702046, hashtext('zz_verify'))"
psql -X -At -c "SELECT pg_try_advisory_xact_lock(4702045)"
```

PASS, in order: `t|integer`; (the background session prints two empty lines); `f` (held by the other session); `1`; after
`wait`: `t`; `t`. FAIL: an error on hashtext / pg_advisory_xact_lock (function missing or not permitted), or `t`
while the other session holds the lock (locks not shared across connections, e.g. a pooler in front of the endpoint).

### P6. PostgreSQL version, role attributes and REPEATABLE READ (LB-PG14, LB-OWNER, LB-PUBLISHER, QA-045d)

```bash
psql -X -At -c "SELECT current_setting('server_version_num')::int >= 140000, current_setting('server_version')"
psql -X -At -c "SELECT rolname, rolcanlogin, rolsuper, rolreplication, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname IN ('dam_lakebase_owner', 'dam_lakebase_publisher') ORDER BY 1"
psql -X -At -c "SELECT r, rolsuper, rolreplication, rolbypassrls, pg_has_role(r, 'dam_lakebase_owner', 'MEMBER') FROM (VALUES ('$APP_ROLE'), ('$EXTRACT_ROLE'), ('$PUBLISH_LOGIN')) v(r) JOIN pg_roles ON rolname = r ORDER BY 1"
psql -X -q -At -c "BEGIN ISOLATION LEVEL REPEATABLE READ" -c "SHOW transaction_isolation" -c "COMMIT"
(cd src/jobs && python -c "import lakebase_extract as lx; c = lx._connect('dam-safety-$TARGET', 'production', 'primary'); print(c.execute('SHOW search_path').fetchone()[0], c.execute('SHOW transaction_isolation').fetchone()[0], c.execute('SELECT count(*) FROM extract_watermark').fetchone()[0] >= 0)")
```

PASS, in order: `t|<version>` (e.g. `t|17.x`); `dam_lakebase_owner|f|f|f|f|f|f` and
`dam_lakebase_publisher|f|f|f|f|f|f`; three lines `<role>|f|f|f|f` (no superuser / replication / bypassrls, none a
member of the owner); `repeatable read`; `dam_app repeatable read True` (LB-SEARCH-PATH: the shared job connection
resolves unqualified names in dam_app). FAIL: `f` in the first column, any `t` in the role lines, a missing role line,
another isolation level, or a `RuntimeError: Lakebase schema 'dam_app' missing or not accessible`.

### P7. run_as grants (UC-GRANTS, PUBLISHER, DSSMP-DOC, CDSE-ASSIGNMENT)

The jobs run as the target's `run_as` (dev: the deployer; prod: `$PROD_SP`). Set `RUN_AS` to that principal
(`RUN_AS=$(databricks current-user me -o json | jq -r .userName)` in dev). As a catalog owner / metastore admin:

```bash
q "SHOW GRANTS \`$RUN_AS\` ON CATALOG $CATALOG"
q "SELECT table_schema, table_name, table_owner FROM information_schema.tables WHERE table_schema IN ('audit', 'ref', 'ops', 'silver', 'gold', 'rpt') AND table_type <> 'VIEW' AND table_owner <> '$RUN_AS' ORDER BY 1, 2"
q "SELECT volume_schema, volume_name, volume_owner FROM information_schema.volumes WHERE volume_owner <> '$RUN_AS'"
q "SELECT table_schema, table_name, privilege_type FROM information_schema.table_privileges WHERE grantee = '$RUN_AS' AND privilege_type IN ('MODIFY', 'SELECT') ORDER BY 1, 2, 3"
```

PASS: line 1 lists `USE_CATALOG` (and `CREATE_SCHEMA` on a fresh catalog) or the principal owns the catalog; line 2
and 3 are `[]` (the run_as owns every table and volume it writes, because `dam_uc_setup` created them as that same
principal), or every table they list appears in line 4 with both `MODIFY` and `SELECT` (inherited grants included) and
every listed volume has `READ VOLUME` + `WRITE VOLUME` (`SHOW GRANTS \`$RUN_AS\` ON VOLUME <schema>.<volume>`).
FAIL: a table written by a job (audit.review_record, audit.chain_head, audit.event_log, ref.threshold,
ref.dssmp_version, ref.dam_cdse_assignment, ...) owned by someone else and without MODIFY for the run_as, or
ref.dssmp_documents without WRITE VOLUME. Grants through a group do not show under the principal's own name: repeat
line 4 with the group name. The authoritative end-to-end check is step 6 SUCCESS under the target's run_as.

### P8. Down-script prod guard (QA-057)

Layer 1, locally (no workspace contact):

```bash
python tools/run_migration.py --catalog dam_prod --file sql/migrations/v1_7_003_dssmp_registration_down.sql --dry-run; echo "rc=$?"
python tools/run_migration.py --catalog $'dam\u200b_prod' --file sql/migrations/v1_7_003_dssmp_registration_down.sql --dry-run; echo "rc=$?"
python tools/run_migration.py --catalog dam_dev --file sql/migrations/v1_7_003_dssmp_registration_down.sql --dry-run >/dev/null; echo "rc=$?"
python tools/run_migration.py --catalog dam_prod --file sql/migrations/v1_7_003_dssmp_registration_down.sql --dry-run --break-glass-prod "runbook P8 dry run only" --log-file /tmp/bg.log >/dev/null; echo "rc=$?"; tail -1 /tmp/bg.log
```

PASS: `REFUSED: QA-057: refusing v1_7_003_dssmp_registration_down.sql on dam_prod ...` + `rc=3` (twice); `rc=0`;
`rc=0` and a JSON line with `"event": "QA-057_BREAK_GLASS_PROD"` and the reason.

Layer 2 on the warehouse, with `tools/verify/qa057_guard_probe.sql` (the downs' first statement verbatim + a
`SELECT 'reached-the-body'` marker; no USE CATALOG, touches nothing, synced by the bundle):

```bash
probe() { databricks jobs submit --json "$(jq -n --arg w "$WH" --arg p "$FILES/tools/verify/qa057_guard_probe.sql" \
  --argjson params "$1" '{run_name: "qa057 probe", tasks: [{task_key: "p", sql_task: {warehouse_id: $w,
  file: {path: $p, source: "WORKSPACE"}, parameters: $params}}]}')" > /tmp/probe.out 2>&1
  echo "rc=$? qa057=$(grep -c 'QA-057' /tmp/probe.out)"; }
probe '{"catalog": "dam_prod", "break_glass": ""}'
probe "{\"catalog\": \"dam$(printf '\u200b')_prod\", \"break_glass\": \"\"}"
probe '{"catalog": "dam_prod"}'
probe '{"catalog": "dam_prod", "break_glass": "BREAK_GLASS_PROD"}'
probe '{"catalog": "dam_dev", "break_glass": ""}'
```

PASS, in order: `rc=` non-zero and `qa057=` >= 1 (refused); the same for the zero-width name (the `\p{Cf}` class
works); `rc=` non-zero (unbound `:break_glass` fails the statement: refused; `qa057=0` is fine here); `rc=0 qa057=0`;
`rc=0 qa057=0`. FAIL: `rc=0` on any of the first three (the guard does not stop a prod rollback: do NOT use the downs
on prod until fixed), or non-zero on the last two (the guard blocks legitimate runs). Then, in dev only, one real
down through the tool (e.g. `v1_7_003_dssmp_registration_down.sql`, `--catalog dam_dev`) must end SUCCESS and be
re-applied with its up (step 3).

Clean-up: `q "DROP SCHEMA zz_verify CASCADE"` -> `["SUCCEEDED","",[]]`.

Closes MIGRATION-3 (MERGE), CDSE-ASSIGNMENT / RULE-REVERSION (UDF, table function), MIGRATION-DOWN (Delta ALTER),
LB-HEAD-LOG / QA-056 (locks), LB-PG14 / LB-OWNER / LB-PUBLISHER / QA-045d / LB-SEARCH-PATH, UC-GRANTS (run_as) and
QA-057 on Databricks.

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

## 7b. Dam lifecycle (v1.8)

1. After step 3 (v1_8_001 + `dam_uc_setup`, task `ddl_14_dam_lifecycle`) and step 4 (`v1_8_001_lifecycle_outbox`):
   `SELECT count(*) FROM dam_dev.ref.lifecycle_citation` -> 15; `... ref.lifecycle_suspension_reason` -> 6.
2. Backfill (once; idempotent): run `sql/migrations/v1_8_002_lifecycle_backfill.sql` like step 3. It fails (and
   writes nothing) if any onboarding_golive row has an unreadable ts. Then
   `SELECT lifecycle_state, count(*) FROM dam_dev.ref.v_dam_lifecycle_current GROUP BY 1` -> `live` per gone-live dam.
3. `dam_ref_publish` now runs `apply_lifecycle_transitions` between `load_dssmp_registrations` and
   `publish_ref_changes`; expected `{"pending": 0}` or one JSON line per outbox row (`loaded` / `rejected` with a
   lifecycle_* reason_code). A dam with lifecycle rows that is not yet live (draft) raises no production alarm.
4. Spot check: `SELECT * FROM dam_dev.audit.event_log WHERE event_type IN ('LIFECYCLE_LOAD', 'DAM_ARCHIVED_REFUSAL')`.
Closes LIFECYCLE-UC, LIFECYCLE-LB, LIFECYCLE-LOADER, LIFECYCLE-BACKFILL on dev.

## 8. Rollback

Pause `dam_ref_publish` and `dam_lakebase_extract` first. Nothing below drops a table, column, volume or row.

* UC, newest first, each once: `v1_8_002_lifecycle_backfill_down.sql`, `v1_8_001_dam_lifecycle_down.sql`,
  `v1_7_003_dssmp_registration_down.sql`, `v1_7_002_canon_v2_down.sql`,
  `v1_7_001_cdse_ack_down.sql`, `v1_6_003_alarm_rule_flag_down.sql`, `v1_6_002_chain_head_down.sql`,
  `v1_6_001_audit_chain_down.sql`, `v1_5_001_contract_down.sql`, ONLY through `tools/run_migration.py` (QA-057; it
  binds the `catalog` and `break_glass` parameters the downs' first statement needs, so the plain step-3 `jobs submit`
  loop fails on a down by design):
  `python tools/run_migration.py --catalog $CATALOG --warehouse-id $WH --workspace-files $FILES --file sql/migrations/<down>.sql`.
  On dam_prod the tool refuses (exit 3) unless `--break-glass-prod "<ticket + approver>"` is given; the reason is
  logged to stderr and `~/.dam_safety/break_glass.log` before submission. Each header has
  a PRE-CHECK; if it returns rows (newer audit records exist), STOP: those records are never deleted and that
  migration stays. Data-level fallback: `DESCRIBE HISTORY <table>` then `RESTORE TABLE <table> TO VERSION AS OF <n>`.
  Views / functions in 10b: re-run the previous `sql/ddl/10b_reviewer_authority.sql` from git.
* Lakebase, newest first (through `lb`): `lakebase/migrations/v1_8_001_lifecycle_outbox_down.sql` (triggers,
  function and the publisher grant only; the table and its rows stay), `v1_7_011_migration_ledger_down.sql` (no-op),
  `lakebase/migrations/v1_7_010_rule_reversion_owner_down.sql` (ownership back to the
  app role; needs dam.app_role), `v1_7_009_status_transition_guard_down.sql` (triggers + function
  only), `v1_7_008_cdse_outbox_status_down.sql`,
  `v1_7_007_qa056_unique_guards_down.sql`,
  `v1_7_006_chain_head_log_down.sql` (roll back the app patch first),
  `v1_7_005_cdse_reversion_tables_down.sql`, `v1_7_004_dssmp_outbox_contract_down.sql`. Data-level fallback: a
  Lakebase point-in-time branch / restore.
* Code: `databricks bundle deploy -t $TARGET` from the previous commit.

## 9. UNVERIFIED items this runbook closes

BUNDLE-1, LB-REF (v1.4), QA-037 / LB-REF (v1.5), LAKEBASE-AUTH, LAKEBASE-HA, APP-ACL, UC-GRANTS, MIGRATION-1,
MIGRATION-2, MIGRATION-2b, MIGRATION-2c, MIGRATION-3, MIGRATION-DOWN (if step 8 is exercised in dev), V15-LB-EXTRACT,
V16-CHAIN, QA-045d, LB-OWNER, LB-PG14, LB-PUBLISHER, OUTBOX-CONTRACT, PUBLISHER, DSSMP-REG, DSSMP-DOC, LB-CDSE-TABLES,
CDSE-ASSIGNMENT, RULE-REVERSION, LB-HEAD-LOG, QA-056, QA-057, LB-SEARCH-PATH (step 4b), LIFECYCLE-UC / -LB / -LOADER /
-BACKFILL (step 7b), PROD-SIGNER (prod
target only). Not closed here: engineering defaults
(section 4 of VERIFICATION.md), INC-WIRE (needs a pyspark run of `reduce_inclinometers`), CANARI-1, DEP-*.
