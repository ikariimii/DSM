# VERIFICATION — what is verified, how, and what is not

Rule: a value or behaviour is **VERIFIED** only if a test reproduces a cited source (manual page, standard,
procedure in `/workspace/dam-refs`) or it was checked against a live system. Everything else is **UNVERIFIED** and
listed here with the reason and what would close it. Build box: Linux, Python 3.13 venv, **no pyspark, no
Databricks CLI, no workspace** — so nothing Spark/LDP/UC/bundle-level has been executed.

## 1. Verified by tests (local)
| Area | Evidence |
|---|---|
| Formulas (VW piezo, standpipe, weirs, VW displacement, settlement sign, sign/offset) | `tests/test_formulas_*.py`, `test_registry.py`, `test_sign_mult.py`, `test_settlement_sign.py`: manual worked examples, each test cites its dam-refs file/page |
| QC flags, alarm state machine, calibration SCD2, datum rebaseline | `test_qc.py`, `test_alarms.py`, `test_calibration_scd2.py`, `test_datum_rebaseline.py` |
| Inclinometer module (REPORT §6) | `test_inclinometer_module.py` (public: DGSI 359/−339 → 0.4188 in; Mikkelsen 2003 446/−426 → D 872, CS 20); internal workbook values in `testonly_restricted/` (not shipped) are **regression-only**: they show the engine reproduces the user's workbook, not that either is correct — **not independent verification** (QA) |
| TOA5 landing | `test_toa5_landing.py` (on App Dev's `common.toa5`, which QA verifies via `test_toa5.py`) |
| Licensing / leak | `test_license_leak.py`; QA guard `qa/restricted_guard.py scan` → 0 hits on new files (run read-only) |
| Bundle config (static) | `test_bundle_config.py` (YAML parses, files exist, task order, no synced table) |
| Sample data | `test_sample_data.py` (headers == bronze schemas, deterministic, SYN-DAM-02 shear zone reduces to 7.5 ± 0.5 mm) |
| Row filters declared | `test_pipeline_row_filters.py`, `test_inclinometer_module.py` (incl tables) |

## 2. UNVERIFIED — platform (needs a workspace)
| ID | Item | Why | Close by |
|---|---|---|---|
| BUNDLE-1 | `databricks bundle validate/deploy` never run; YAML anchors in `targets`; new: `${var.security_mode}` interpolation inside a `sql_task.file.path` (uc_setup `ddl_10a_row_filters`), and `run_as` on pipeline/alerts needs `runtime_sp_app_id` set (empty value breaks dev) | no CLI on box | `databricks bundle validate -t dev` |
| LDP-1 | `@dp.table(row_filter=…)`, `cluster_by`, `expect_all_or_drop`, Auto Loader options. **SME 2026-11-02 (KB 03):** row_filter is managed only in pipeline code (never `ALTER TABLE` a pipeline table, asserted by `test_no_alter_table_row_filter_on_pipeline_tables`); pipeline `run_as` = runtime SP (member of `dam-portfolio-all`); refreshes evaluate as owner/run_as, so gold MVs reflect that principal's visibility and readers are filtered as themselves | no LDP runtime; row_filter in LDP is Public Preview | dev pipeline update |
| ABAC-1 | **SME 2026-11-02 (KB 02):** ABAC cannot be combined with a table-level filter, incl. LDP row_filter (`UC_ABAC_MULTIPLE_ROW_FILTERS`) → mode switch `var.security_mode` (`table` default \| `abac`): `10a_row_filters_table.sql` vs `10a_row_filters_abac.sql` (DROP ROW FILTER on the same tables + CREATE POLICY ... EXCEPT `dam-portfolio-all`); pipelines set `row_filter=None` in abac mode. Still **UNVERIFIED (test in dev):** `IDENTIFIER(:catalog)` inside `ON CATALOG`, principal `` `account users` ``, EXCEPT of a group covering its SP members during refresh, governed tag `dam_scope` pre-created; abac file tags only one column so far | syntax from KB, not executed | run uc_setup with `security_mode=abac` in dev |
| LAKEBASE-AUTH | **Verified per SME/KB 05 (2026-11-02):** `w.postgres.generate_database_credential(endpoint="projects/<p>/branches/<b>/endpoints/<e>")` → token (1 h, checked at login only) minted per new connection (`src/jobs/lakebase_extract.py`). Not yet exercised on a workspace. App `app/backend/repos/state.py` TODO(SME) is App Dev's to close | — | run `dam_lakebase_extract` in dev |
| LAKEBASE-HA | **Verified per SME/KB 05:** endpoint `group: {min, max}` (min = max; 1 = off) → `var.lb_ha_nodes` default 1, prod 2; HA forbids scale-to-zero (prod `lb_no_suspension: true`). DAB field not yet deployed | — | deploy prod-like target in dev |
| ALERT-1 | SQL alert v2 schema, schedule stagger, destinations. **SME 2026-11-02:** each alert has `run_as.service_principal_name: ${var.runtime_sp_app_id}` (CLI ≥ 0.279.0; SP must be in `dam-portfolio-all` or row filters hide rows from counts); `empty_result_state: OK` kept; 250 concurrent alert runs/workspace, runs over the cap are **skipped silently** (KB 07) | not deployed | deploy dev (alerts PAUSED) |
| QC-CONTEXT | per-batch spike/stuck in `spark/udfs.py` lack trailing history | streaming batches | test small-batch incremental vs full recompute |
| DATUM-LATE | late rows before an existing datum break are not re-chained | design gap | recompute window |
| ENV-2 | **Verified per SME 2026-11-02:** serverless env 2 = Python 3.11.10 (engine jobs in `jobs.yml`); env 6 = Python 3.12.3 (ENV-6). `uc_setup` seed task moved to env 6 (stdlib csv + Spark only) | — | — |

## 3. UNVERIFIED — dependencies (steering 8)
| ID | Item |
|---|---|
| ENV-6 | env 6 Python 3.12.3 **verified per SME 2026-11-02**; package versions: env 6 base = Python 3.12.3, numpy 2.3.4, pandas 2.3.3, scipy 1.16.3 (`requirements/serverless-env6-base.txt`) taken from the Databricks env-6 release notes as read 2026-11-02; not resolved on a workspace. Used **only** by `dam_interpretation` (`requirements/job-ml-env6.lock.txt`, `-r` include). |
| DEP-1 | statsmodels 0.15.0 newly pulls `formulaic==1.2.2` (lock shows it); behaviour change vs 0.14 not reviewed. |
| DEP-2 | pastas 2.0.1 was released 2026-11-02 13:34 UTC (same day) — no field history; pinned but not yet imported by any module. |
| DEP-3 | ioos-qc, ruptures, statsmodels, pastas are pinned in the `ml` env but **not imported anywhere in `src/` yet**; the engine still uses its own numpy implementations. |
| CANARI-1 | `pycanari==0.2.5` import fails locally: `libmpi_cxx.so.40` missing. Pilot job is env-check only, writes nothing, dev/test only, PAUSED. Unknown whether serverless env 6 has the lib. |
| TOA5-1 | PyTOA5/igbpyutils dropped (GPL dependency); traval excluded (no LICENSE file). |

## 4. UNVERIFIED — engineering defaults (site settings, engineer-owned)
| ID | Item |
|---|---|
| QC-DEF | **Engineering sign-off item** (Geotech / FERC specialists; Databricks SME 2026-11-02 left it open): spike n_sigma 6, stuck 24, gap factor 2.0 — no manufacturer values exist |
| ALARM-DEF | **Engineering sign-off item** (Geotech / FERC specialists): deadband / persistence defaults per threshold version |
| INT-CONF | fault-classifier and commentary confidence heuristics |
| INT-PFM | PFM rule t_crit / windows |
| INT-REAL-1 | HST fit on a real seepage series: thresholds are regression baselines, not literature values; hold-out shows strong negative bias (documented gap) |
| ANOM-K | **Engineering sign-off item** (Geotech / FERC specialists): anomaly bands k_warn 2 / k_alert 3, rate window and k_r |
| LEVEL_DATUM | `EL = datum_el_m + sign_mult·reading + offset` — rule from CONVENTIONS only, `verified=False`; app must hide. App tests: 174 passed (read-only run 2026-11-02 ~19:31 ET). |
| CYCLIC | `limit_type=cyclic` thresholds are not evaluated |

## 5. UNVERIFIED — inclinometer (steering 9)
| ID | Item |
|---|---|
| INC-SENS | sensitivity correction `D·s` — no team worked example |
| INC-ZERO | depth-shift fill `'zero'` (only to reproduce one verify case); default fill is `'missing'` |
| INC-DTOL | ΔCS site tolerance `delta_tol` default None (procedure gives no value) — statistic reported, nothing flagged |
| INC-LABEL | **UNVERIFIED author intent.** Workbook import profile only: label `d` = ft from bottom → interval top `casing − d − L` (workbook `28 − d` ft; REPORT D2, numerically consistent with verify T13d/T13e). Geotech lead ruling 2026-11-02; the workbook author has not confirmed. Stored `depth_reference='from_bottom_label'` + `raw_depth_label`. GTILT/DigiPro keep native from-top depths |
| INC-ROT | rotation axis A implemented: `MIKKELSEN2003_EQ9` (A′ − A = −B′·sinα_A, MIKKELSEN-2003 PDF p9 eq 9; Fig 7 sin a = 35/4500 = 0.0078), `GTILT_PLUS_SIN` (engine error +sin(rot)·B0, verify INFO T14f / T7d 30.4 mm), `A_corr=A-f*B` (MIKKELSEN-2007 p17). `sign_convention` required on every rotation record. Off by default: applied only with an independent review (TRB-EC129 PDF p44) |
| INC-ROTB | **UNVERIFIED:** B-axis rotation (Mikkelsen 2003 eq 10, B′ − B = A′·sinα_B, opposite sign to eq 9) not implemented; an applied B record is rejected |
| INC-SPIRAL | spiral correction **deferred**: `spiral` kept in the correction_type enum, `applied=true` rejected (never "Applied", REPORT E2); info flag `SPIRAL_NOT_APPLIED` on every survey of an installation with a spiral survey on file |
| INC-BIASQC | QA-020 between-set bias QC (`qc.between_set_bias_qc`, procedure §7.1): suspect band 10 (A) / 20 (B), significance 3·SD/√n, spread SD ≤ 8 (A) / 16 (B), equal-and-opposite ratio 0.5, \|c\| ≤ 25 else BAD/resurvey, stable-zone fallback bottom 5 — all configurable `BiasQCParams`, **UNVERIFIED engineering judgment** (`params_unverified=True`); only n ≥ 5 is sourced as typical. Proposes only, never applies. Equal-and-opposite tolerance has no source (procedure gives none) |
| INC-CURV | curvature reported, not converted to a correction |
| INC-WIRE | **Core verified locally, Spark wrapper UNTESTED.** `src/jobs/reduce_inclinometers.py` writes `silver.incl_reduced` via `dam_safety.inclinometer.batch` (tests/test_inclinometer_batch.py, synthetic). pyspark and Java are not installed on the build box, so the cogroup/applyInPandas wrapper, the incremental target selection and the Arrow schema have never run; `test_spark_wrapper_schema_matches_core` is skipped. Verify on a workspace before first run. `pandas_ops.incl_profile_pdf` (old core path) is no longer the writer. UNVERIFIED: `silver.incl_survey.depth_m` is metres for workbook files (label_unit_m=1.0 assumed; labels are ft from bottom, REPORT D2) |
| INC-QA021 | **Implemented; tolerances UNVERIFIED but NOT a production blocker** (they only rank proposals). `qc.between_set_bias_qc` rule `QA021-v2` (procedure §5 bias row + §7.1 `bias_*` row, lead rulings 2026-11-02): Δ = suspect − reference, ΔD = mean(ΔA0 − ΔA180), **ZA = −ΔD always, applied to A0; bs = mean(ΔA0 + ΔA180) is never used for the value**. Cross-check compares MAGNITUDES only: ||ΔD| − |bs|| ≤ max(`crosscheck_tol_abs` 2.0, `crosscheck_tol_k` 3 × SE) → `crosscheck_verdict` AGREE (`PROPOSED`), else SUSPECT (`SUSPECT_CROSSCHECK`, still carries ZA: a review flag, not a rejection). The v1 sign-agreement gate (`require_sign_agreement`, `sign_agree`) is **removed**: it wrongly rejected an A180-only bias (procedure §7.1, verify T16d: δ = +6 on A180 → ΔD = −6, bs = +6, ZA = +6, corrected 0.0 mm; `tests/test_inclinometer_bias_qc.py::test_a180_only_shift_is_proposed_and_corrects_to_zero`). **Never auto-applied:** every proposal carries ZA, `abs_dD`, `abs_bs`, `crosscheck_verdict`, `requires_review=true`; `qc.bias_proposal_record` writes `applied=false`; `CorrectionRecord` rejects an applied bias record without reviewer approval (`reviewed_by` ≠ `user`, `reviewed_utc`) and `active()` ignores unreviewed ones. Why not a blocker: the procedure says bias correction is "justified in less than one percent of datasets" [GEOKON-GTILT PDF p45 [printed p6-11]] and a reviewer decides each one, so a mis-set tolerance only mis-ranks. The 2.0 and 3·SE numbers still have no source (procedure: "No source gives a numeric agreement tolerance"); the DMM example is SUSPECT (its cross-check difference exceeds the tolerance) and would rank AGREE at tol_abs ≥ `dmm_rank_flip_tol_abs` or tol_k above `dmm_rank_flip_tol_k_below` (restricted-derived values, read by path: `/workspace/dam-refs/qa_private/inclinometer_bias_expected.json`). Significance uses the strict `|ΔD| > 3·SE`. Tests: tests/test_inclinometer_bias_qc.py, tests/test_inclinometer_batch.py (A180-only, SUSPECT ranked), testonly_restricted/test_inclinometer_dmm_bias.py (DMM → SUSPECT_CROSSCHECK, ZA = `dmm_za`). `BIAS_RULE_VERSION` goes into `silver.incl_reduced.qc_rule_version` (formula_version stays `1.0.0` so it joins the verified `ref.formula` row); the job re-reduces any survey whose latest row has another formula_version+qc_rule_version. Hook unchanged: `batch.DEFAULT_BIAS_RULE` |
| ALARM-NULL (v1.4) | **Static tests only.** `ops.alarm_transition` CHECK `ck_alarm_transition_value` (non-ACK rows need value, metric_value, reading_id), the evaluator's pre-write guard, and `ops.alarm_episode` taking value/lineage from the latest reading-driven transition (an ACK no longer blanks `value`) are covered by `tests/test_alarm_lineage_contract.py`. UNVERIFIED on a workspace: the `ALTER TABLE ... DROP CONSTRAINT IF EXISTS / ADD CONSTRAINT` statements and `max_by` ignoring NULL ordering keys have not been executed (no Spark/UC). |
| LB-REF (v1.4) | **Docs-checked, not validated.** Lakebase references now use `${resources.postgres_*.<key>.id}`, which docs.databricks.com/aws/en/oltp/projects/manage-with-bundles says "references the ... resource name" (full `projects/.../branches/.../endpoints/...` path) and which every bundle example uses. `.name` is the API/Terraform output-only full path and probably resolved too, but it is undocumented as a substitution. Close with `databricks bundle validate` + deploy. |
| APP-ACL (v1.4) | **Config only.** App CAN_USE goes to per-dam `dam-<dam_id>-members` groups (five sample dams listed; bundles cannot loop, so add one entry per onboarded dam). Group existence is UNVERIFIED (no workspace). |
| V15-APPROVAL (v1.5) | **Local tests only.** QA-039 approval via `audit.review_record` + signature (`corrections.approval_problem`), the `ck_incl_correction_applied_reviewed` / `ck_review_record_subject_decision` CHECKs (evaluated in sqlite, not Delta), `delta.appendOnly`, and `gold.incl_correction_status` (SQL text only, never run). **NOT IMPLEMENTED:** no job yet writes the applied version row (applied=true, `supersedes`=proposal, `review_id`) after an app approval; until it exists an app approval produces a review row but nothing is applied. |
| V15-SIG (v1.5) | **Verified locally against the app code, not live.** `tests/test_review_signature.py` reproduces `tests/fixtures/review_signature_vector.json` from the engine, from the spelled-out rule and from App Dev's `backend/signoff.signature` (a9e7d56). Not run end-to-end through Lakebase → extract → UC. |
| V15-LB-EXTRACT (v1.5) | `review_signoff` → `audit.review_record` mapping tested offline (`tests/test_lakebase_extract_review.py`); the Lakebase query and Delta append never ran. |
| MIGRATION-1 (v1.5) | `sql/migrations/v1_5_001_contract.sql` is static-tested only (`tests/test_migrations.py`); SQL scripting (`BEGIN … END`, `EXECUTE IMMEDIATE`) support on the target warehouse and the run itself are UNVERIFIED. |
| QA-037 / LB-REF (v1.5) | **Pending `databricks bundle validate -t prod`** (no CLI here): the resolved-config test mirrors `apply_bundle_permissions.go` but the real CLI has not confirmed `dam-portfolio-all` never reaches the app, nor that `${resources.postgres_endpoints.<k>.id}` resolves (ENDPOINT `.id` UNVERIFIED). |
| V15-SCD2 (v1.5) | Installation row valid at survey time enforced in `batch.installation_row_at` + job (tested in core; Spark wrapper untested). Limitation: the baseline survey is converted with the target survey's row constants; if L/K change between them the result is UNVERIFIED (question to the Inclinometer Specialist). |
| V15-DEMO (v1.5) | `sample_data/demo/` reading_id/survey_id hashes assume Spark `CAST(ts AS STRING)` = `YYYY-MM-DD HH:MM:SS` (UTC session) and `_metadata.file_path` = `/Volumes/dam_dev/raw/landing/...`; UNVERIFIED. |
| V16-CHAIN (v1.6) | **Local tests only.** `audit.review_record` chain columns, `ck_review_record_subject_decision` (alarm_event) and `ck_review_record_chain` are tested in SQLite (`tests/test_lakebase_extract_review.py`); Delta CHECK behaviour, the Lakebase query and the 24-column append never ran. `dam_safety.audit_chain` reproduces the app's interop vector (`tests/test_audit_chain.py`, generated from the App Developer's branch code, read only). The chain head is not extracted (tail deletion is detectable only in the app). |
| QA-045c (v1.6) | `dam_safety.audit_chain.verify_records(rows, signer=None, expected_head=None)` mirrors the app's QA-045a `verify_chain` (same reason codes: signature_missing / signature_downgrade / signature_invalid / head_mismatch). **Tested locally** against `tests/fixtures/review_chain_signed_vector.json`, generated by `tools/gen_review_chain_signed_vector.py` from the App Developer's code (read only) with a public TEST-ONLY HMAC signer: engine verdict == app verdict on 16 adversarial cases, with and without a signer. **UNVERIFIED / limits:** no production signer or key exists (FERC: no in-app crypto signature required), so enforcement is untested with a real key; a fully stripped chain verifies when no signer is passed (same as the app), so jobs must pass the signer once signing is enabled; (superseded by QA-045d: the head is no longer caller-supplied). |
| QA-045d (v1.6) | `verify_records(rows, signer=None, head=None)`: no head + records = `head_missing`, never ok (app `verify_chain` semantics, App Dev 0e79376 read only); `expected_head` removed. Signed vector regenerated from the app's code: the 16 earlier cases unchanged, 8 new head cases (no_head, no_head_empty_chain, truncated_tail_several, truncated_all_records, head_rewound_consistent, head_rewound_and_tail_deleted, head_forged_hash_and_seq, head_of_other_chain); engine == app on all 24, with and without the signer. New append-only `audit.chain_head` (sql/ddl/08, migration v1_6_002, `delta.appendOnly` = no UPDATE/DELETE), appended by `dam_lakebase_extract` from Lakebase `review_chain_head` in one REPEATABLE READ snapshot with the records; task `verify_review_chain` reads it (`verify_with_chain_heads`: latest head + every stored head as a checkpoint). Tests: `tests/test_chain_head.py`. **Limits / UNVERIFIED:** truncation is detectable only from the first stored head on; a consistent rewind + truncation before any head was stored is undetectable (the app alone accepts it too: vector case head_rewound_and_tail_deleted); the engine also rejects a head naming another chain_id even with matching seq/hash (stricter than the app, which looks heads up by chain_id); Spark/Delta paths and `{{job.run_id}}` never ran; Lakebase `review_chain_head` is app-owned and mutable, so a Lakebase-side rewind is recorded and flagged, not prevented. |
| MIGRATION-2b (QA-045d) | `sql/migrations/v1_6_002_chain_head.sql` static-tested only (`tests/test_migrations.py`); never run. |
| PROD-FAIL-CLOSED (v1.6) | `verify_review_chain` (the only job that verifies chains; a test scans `src/jobs` for others) calls `audit_chain.require_signer_for_catalog` at start, before any Spark read: on `dam_prod` it exits unless a real signer is configured (enforcing, non-empty alg and key_id, not a `*-test` alg). `--chain_signer` / `DAM_CHAIN_SIGNER` accepts only `none` today; any other value is refused everywhere (no signer implementation exists). Tested offline (`tests/test_prod_fail_closed.py`). **Consequence:** until a signer exists, the prod `verify_review_chain` task fails every run (the `dam_lakebase_extract` job run then shows failed; the extract task itself still succeeds). UNVERIFIED on a workspace. |
| LB-OWNER (v1.7) | Lakebase split ownership (`lakebase/001_app_schema.sql`): NOLOGIN `dam_lakebase_owner` owns the schema, `exceedance_status`, `review_signoff`, `manual_reading`, `extract_watermark` and the trigger functions; guard triggers ENABLE ALWAYS; the app role gets SELECT+INSERT on the guarded tables, USAGE+CREATE on the schema and owns its mutable tables; 001 refuses a SUPERUSER/REPLICATION/BYPASSRLS app role. Validator `lakebase/validate_append_only.sql` + `tools/validate_lakebase.py` (read only). **Tested on local PostgreSQL 17.11** (`tests/test_lakebase_ownership.py`, `DAM_TEST_PG`): as the app role DISABLE TRIGGER, TRUNCATE, UPDATE/DELETE, SET [LOCAL] session_replication_role, self-GRANT + TRUNCATE, DROP TABLE/TRIGGER/SCHEMA, replacing forbid_mutation(), ALTER OWNER and SET ROLE dam_lakebase_owner all fail; a superuser in replica mode is still refused TRUNCATE (ALWAYS triggers); the validator passes the good setup and reports the exact check for 17 deliberately bad setups (exit 1). App patch `tools/patches/v17_lakebase_owner_state_init.patch` (NOT applied; App Dev owns app/): unpatched state.py cannot init as the app role after 001 (`permission denied for database`), patched it inits, appends and verifies a chain, re-inits idempotently, and refuses to start when a guard is disabled (`tools/patches/v17_lakebase_owner_state_init_check.py`); app suite with the patch: 460 passed. CI job `lakebase-pg` (postgres:17 service) and a post-deploy validator step gated on `vars.LAKEBASE_ENDPOINT` / `vars.LAKEBASE_APP_ROLE`. **UNVERIFIED:** Lakebase's deployer role (CREATEROLE? able to ALTER OWNER?), the app SP role's attributes, `has_parameter_privilege` (PG >= 15) on Lakebase, the CI workflow (never run), the `--lb-endpoint` host attribute path. Deviation from the brief, by design: the app role keeps CREATE on the schema and owns its mutable tables (state.py manages them); only the append-only objects are owner-owned. |
| SECOND-APPROVER (PROVISIONAL, v1.7) | Engine check behind the app's setting `CDSE_FLAG_REQUIRE_SECOND_APPROVER` (App Dev f0dd9c8, same name, default false, same `_bool` parsing): when on, an `alarm_rule_flag_apply` naming a proposal recorded earlier in the chain by the same person (reviewer_id, or email case-insensitive) is `apply_by_proposer` (error). One bundle variable `cdse_flag_require_second_approver` (default "false") feeds the app env (resources/app.yml) and the verify task parameter. Tested offline both settings + default off (`tests/test_rule_flag_authority.py`). `actor_app_role` documented as evidence only (SCHEMA_CONTRACT §8). **UNVERIFIED / gaps:** the setting is not in the chain, so the verifier applies the current value to all records (switching it on flags earlier self-approvals); a direct apply while on is not flagged (app refuses it); bundle precedence of `config.env` over `app/app.yaml` not validated. |
| PROD-SIGNER (QA-052 / QA-053, v1.7) | On top of the prod fail-closed check (487d5b4): QA-052 `audit_chain.signer_probe_problem` runs inside `require_signer_for_catalog` for dam_prod: the signer signs a fixed payload (`PROBE_PAYLOAD`), verifies it with the same signer, and must reject that signature for a different payload; a raising sign()/verify() (key not loaded), a wrong/mismatched key, an empty signature, a missing sign() or a verify() that accepts anything refuses start (before any read). QA-053: `verify_review_chain.verify_all` re-applies the guard itself (catalog not given = production, fail closed) and returns ok=False reason `signer_refused` per chain without verifying, so calling it directly cannot bypass start(); `main` passes `catalog=a.catalog`. Tested offline (`tests/test_prod_fail_closed.py`). Not gated: the library `audit_chain.verify_records` / `verify_with_chain_heads` (no catalog; the job is the prod entry point) and `authority_failures` (authority only, no integrity claim). **UNVERIFIED:** no real signer exists yet, so the probe has only run against test signers. |
| CANON-V2 (QA-050 / QA-051, v1.7) | Engine `dam_safety.audit_chain` mirrors App Dev's c797151 canon v2 (`app/backend/audit_chain.py`, read only): canon 1 bytes unchanged, canon 2 hashes `actor_app_role` + `canon_version` (int), reason codes `canon_unknown`, `actor_role_missing`, `actor_role_invalid`, `canon_downgrade` with the app's triggering order. Tested offline: all 12 cases of `app/tests/fixtures/review_chain_canon2_vector.json` give the app's verdict, the live app module and the engine agree on every canon-2 and canon-1 (signed + plain) vector case, extract -> 26 columns -> still verifies (`tests/test_canon_v2.py`); QA's `qa/test_qa039_canon2.py` (in App Dev's worktree, run on a scratch copy, not committed here) XPASSes both strict xfails. UC: `actor_app_role STRING`, `canon_version INT`, `ck_review_record_canon` (sql/ddl/08 + migration `v1_7_002_canon_v2.sql`, down script drops the CHECK only); Lakebase 001 adds both columns + `ck_review_signoff_canon` (live PG 17 test as the app role). §7.13: role from the record for canon 2 (`apply_by_admin`, `propose_no_authority` now real). **UNVERIFIED:** migration not run on a workspace; Delta CHECK with `length(trim())` on a workspace; whether the DA accepts the app-recorded role as authority evidence (it is hashed but app-derived). |
| MIGRATION-2c (§7.13) | `sql/migrations/v1_6_003_alarm_rule_flag.sql` static-tested only; CHECK bodies exercised in SQLite (`tests/test_rule_flag_authority.py`, `tests/test_lakebase_extract_review.py`); never run on Delta. |
| RULE-FLAG-AUTH (§7.13) | Authority rules (App Dev via Genie 2026-11-09) tested offline, one test per code incl. admin applying and a non-CDSE engineer rejecting (`tests/test_rule_flag_authority.py`). **UNVERIFIED / gaps:** no app-role (dam_admin/admin) source in UC: Lakebase `dam_role` / `app_user` have no history and are not extracted, so a non-CDSE proposal is only `propose_role_unverified` (warn) and an admin apply reports as `apply_not_cdse`; ask App Dev to record the actor's app role in the chained record (e.g. in `corrected_value`) or to keep role history. `ref.dam.cdse_id` has no history (current value used for past records). The task reads `ref.v_dam_reviewer_authority` as the job principal (row filter: it must be in `dam-portfolio-all`); Spark timestamp tz of `signed_ts` not checked on a workspace. Publish job for `ref_change_publication` and the Lakebase mirror of `alarm_rule_flag_proposal` / `ref_change_publication` not done (mirror skipped per Genie). |
| LB-PG14 (QA-045c) | `lakebase/001_app_schema.sql` uses `CREATE OR REPLACE TRIGGER` (PostgreSQL >= 14) and runs as ONE transaction under `pg_advisory_xact_lock(4702045)`. **Tested on local PostgreSQL 17.11** (`tests/test_lakebase_append_only.py`, `DAM_TEST_PG`): init twice, 3 rounds x 4 concurrent psql inits on an existing and a fresh DB, guards never absent (probe: 0 successful TRUNCATE/UPDATE/DELETE). The pre-QA-045c script showed 12 guard breaches and 8/40 `tuple concurrently updated` failures under the same load. **UNVERIFIED:** Lakebase's Postgres major version (must be >= 14), whether the Lakebase role may take advisory locks and run CREATE OR REPLACE TRIGGER, Matching app patch `tools/patches/qa045c_app_state_init_lock.patch`: **applied by App Dev in 974e786** (merge on `app/v1.6-port`); App Dev reports 0 init errors and 0 guard mutations on live PG (their run, not re-run by the DA). |
| MIGRATION-2 (v1.6) | `sql/migrations/v1_6_001_audit_chain.sql` is static-tested only (`tests/test_migrations.py`); never run on a warehouse. |
| MIGRATION-3 (v1.7) | `sql/migrations/v1_7_001_cdse_ack.sql` static-tested only. Delta behaviour taken from the Databricks ALTER TABLE ... COLUMN docs (read 2026-11-09: ADD COLUMN cannot set a DEFAULT, existing rows read NULL, SET DEFAULT needs `allowColumnDefaults`, SET NOT NULL validates existing data); never run. `CREATE TABLE ... DEFAULT` in `sql/ddl/02` with the feature property also never ran. |
| CDSE-ACK (v1.7) | `cdse_ack_required` / `cdse_ack_required_at_open` logic tested with the pure machine (`tests/test_cdse_ack_required.py`, un-flag mid-episode keeps the snapshot TRUE); the evaluator Spark job and the views never ran. Which DSSMP clause sets the flag per rule is an engineer input (not in this repo). App enforcement: App Dev (c4a384c reads `ref.threshold.cdse_ack_required`, `ops.alarm_episode.cdse_ack_required_at_open` / `cdse_ack_required`; names checked against this branch 2026-11-09). |
| THRESHOLD-VERSION (v1.7) | Decision: a `cdse_ack_required` change is a new `threshold_version`, never an in-place UPDATE. Enforced in `src/dam_safety/threshold_publish.py` (`plan_threshold_publication`, `assert_versions_immutable`; `tests/test_threshold_publish.py`) and visible in `gold.v_threshold_cdse_ack_changes`. **Not enforceable in Delta DDL** (no triggers; a CHECK cannot compare versions), so a hand UPDATE is only detectable (change data feed is on for `ref.threshold`). **Gap:** no publisher job for the app's `onboarding_publication` outbox exists in this repo; the app's go-live threshold rows also lack the NOT NULL columns `dssmp_version_id`, `tier`, `deadband`, `persistence_on`, `persistence_off`, so a publisher must fill or reject them. Never run on Spark. |
| ROLE-LEVEL (v1.6) | **Confirmed by Genie 2026-11-09** (App Dev a88b07f): CDSE level only for `ref.dam.cdse_id` or a CDSE membership for that dam; engineer/dso/dam_admin/admin → engineer. Legacy CDSE-level rows are kept and flagged `review_level_legacy_unverified` (`gold.v_review_level_check`, `gold.incl_correction_status`, extract); tested in SQLite + pure Python (`tests/test_reviewer_authority.py`). **UNVERIFIED:** `ref.dam.cdse_id` has no history (compared as of now); Databricks SQL `count()` over a LEFT JOIN in the view and the view never ran on a warehouse. |
| UC-GRANTS (v1.6) | `ref.v_dam_reviewer_authority` + per-dam `GRANT USE SCHEMA` / `GRANT SELECT ON TABLE <view>` (`sql/ddl/10b`, generated). **UNVERIFIED:** never executed; dynamic-view evaluation of `ref.dam_row_filter` (is_account_group_member for the querying user) and its interaction with the `ref.dam` row filter / ABAC policy through a view owned by the setup SP; `GRANT ... ON TABLE` for a view; **USE CATALOG for the dam groups is not granted anywhere in this repo** (deployment prerequisite). No GRANT on `ref.dam` / `ref.project_membership` exists in `sql/` (test asserts it). |
| V16-EPISODE-INST (v1.6) | `installation_version` on `ops.alarm_transition` / `alarm_episode` tested statically and in the pure helper; Spark wrapper untested. **No inclinometer feed:** the evaluator reads `silver.v_reading_current`, inclinometer results are in `silver.incl_reduced`, so the column stays NULL until a feed exists. |

## QA-018 landing sequence residual (2026-11-08)

Sources: `docs/TOA5_READER.md` (QA-018 sequence-warning contract) and **CS-LN-TOA5**,
Campbell Scientific LoggerNet, “TOA5”:
https://help.campbellsci.com/loggernet-manual/ln_manual/campbell_scientific_file_formats/toa5.htm.
The following is the **application ingest policy**, not a Campbell-prescribed deduplication algorithm.

- `common.toa5` remains the sole, unchanged reader. `to_landing_rows` returns a list-compatible
  `LandingRows` object; the bronze `LANDING_COLUMNS` and readings CSV are unchanged.
- Within one parsed file, dedup is **first-wins on (table_name, RECORD, TIMESTAMP)** before sensor/channel
  expansion, scoped to the caller's dam/logger. Conflicting values with the same key are also suppressed
  from derived landing rows and explicitly diagnosed. The parsed source records/source file are never edited,
  deleted, or overwritten: retain them append-only. No UPDATE/DELETE of raw is introduced.
- Duplicate RECORD alone or duplicate TIMESTAMP alone is **not** the composite key. These records and
  non-monotonic rows remain in source order, with `retained_flagged` diagnostics. They are review flags,
  not automatic rejection or evidence of a sensor fault. The adapter does not sort or repair readings.
- Every `Toa5Table.warnings` entry is surfaced as `Toa5LandingWarning` and retained unchanged in code,
  message, and physical line in `result.diagnostics`, including a dropped unterminated tail. Additional
  codes are `landing_duplicate_suppressed`, `record_gap` (forward jump greater than one), and
  `timestamp_gap` (only when `expected_interval_seconds` is explicitly provided; never infer cadence).
  Documented counter rollovers do not become gaps. RECORD-less TOA5 is valid for the reader, but this
  adapter fails closed because its chosen dedup key requires RECORD; TIMESTAMP remains required too.
- **Companion output contract:** persist `to_landing_diagnostics_json(result)` as an append-only JSON
  sidecar, e.g. `/Volumes/<catalog>/raw/landing/<dam>/toa5_diagnostics/<source-id>.json`, separate from
  `readings/` (the bronze CSV glob must not ingest it). Each ingestion attempt needs a unique source-id
  associated with the retained original file; do not overwrite an earlier attempt. The serializer emits
  `schema_version: 1` and a `diagnostics` array with dam_id, logger_id, table_name, physical line, RECORD,
  logger-local ISO timestamp (the required station offset is applied separately to reading ts_utc), code,
  message, and disposition (`retained_flagged`, `suppressed_duplicate`, `reader_warning`). For file-level
  warnings RECORD/timestamp are null. A clean file emits an empty array. Persist the sidecar even if all
  duplicate derived rows are suppressed; writing the readings CSV alone is not sufficient for audit.
- Tests in `tests/test_toa5_landing.py` use synthetic sequence cases only: conflicting/exact duplicate keys,
  multi-channel expansion, non-monotonic records/timestamps, gaps and explicit cadence validation, both
  documented counter rollovers, sidecar disk round-trip, tail diagnostics, and unchanged raw/CSV schema.
- **UNVERIFIED / not implemented here:** this is a stateless per-file output adapter, not an orchestrator.
  Sidecar retention/ACLs and end-to-end Auto Loader/UC execution have not been tested on a workspace.
  Cross-file/replay dedup is not guaranteed by this function; the caller must provide replay/idempotency
  control separately. No new deployed pipeline diagnostics table or automatic volume writer is claimed.

Local validation on the current `app/v1.5-audit-chain` tree (not the older `bed918c` main worktree),
with suites run separately using `.venv/bin/python`:

| Suite | Result |
|---|---|
| Landing regression (`tests/test_toa5_landing.py`) | 22 passed |
| Root (`tests/`) | 705 passed, 2 skipped |
| App (`app/`, invoked from that directory) | 299 passed |
| QA (`qa/`, including pre-existing uncommitted QA files) | 203 passed, 4 xfailed, 1 failed |
| Focused TOA5 QA (`qa/test_toa5_adversarial.py`, `qa/test_toa5_fix_verification.py`) | 100 passed |
| Internal-test-only suite (run locally, no data copied) | 7 passed |
| Full restricted guard (`qa/restricted_guard.py scan`) | 0 hits |

The QA failure is `qa/test_inclinometer_bias_rules.py::test_qa020_bias_qc_never_changes_reduced_data`:
its cumulative-displacement assertion calls `abs()` on a null value. It is outside this landing change;
no inclinometer or QA files were modified to hide/fix it. App and QA also emit the existing
Starlette TestClient/httpx deprecation warning. Workspace validation/deployment was not run.

## PUBLISHER (ref_change_publication -> ref.threshold, v1.7)

- `src/dam_safety/ref_change_publish.py` (pure plan) + `src/jobs/publish_ref_changes.py`, own bundle job
  `dam_ref_publish` (every 5 min, max 1 concurrent run). One conditional MERGE per run: closes version N
  (effective_to, is_current only) and inserts N+1, guarded inside the statement by "N is still current" (stale
  re-check). Post-check that no existing version's rule fields changed, then the outbox status is written back.
- Outcomes: `applied`, `already_applied` (re-run), `rejected_stale`, `rejected_invalid`, `deferred` (second pending row
  for the same rule). Rejections carry `<code>: <detail>`; codes `unknown_kind`, `payload_invalid`,
  `missing_required_column` (dssmp_version_id, tier, deadband, persistence_on, persistence_off),
  `missing_not_null_column`, `unknown_columns`, `invalid_value`, and the DSSMP codes `dssmp_missing`,
  `dssmp_not_found`, `dssmp_other_dam`, `dssmp_draft`, `dssmp_superseded`, `dssmp_not_yet_effective`,
  `dssmp_status_unknown` (`src/dam_safety/dssmp_check.py`, checked against `ref.dssmp_version` at the new version's
  effective_from).
- The same DSSMP check runs in the §7.13 verifier on every `alarm_rule_flag_apply` (historically, at signed_ts, on the
  DSSMP version of rule version N); a missing ref.threshold row is the warning `dssmp_unverifiable`.
- Evidence: tests/test_dssmp_check.py, tests/test_ref_change_publish.py (incl. an outbox row written by the app's own
  demo apply path, and the MERGE text executed on local PostgreSQL 17 for apply / idempotent re-run / concurrent
  stale), tests/test_rule_flag_authority.py.
- Finding for App Dev: the demo seed's threshold rows carry `output_name`, which is not a `ref.threshold` column, so a
  demo outbox row is rejected `unknown_columns` (live rows come from `SELECT t.*` on ref.threshold and lack it).
- UNVERIFIED: Delta MERGE with the EXISTS subquery over the target (only PostgreSQL executed it); the run_as
  principal's MODIFY on ref.threshold; the Lakebase publisher grants on a deployed Lakebase.

## DSSMP-REG (v1_7_003_dssmp_registration, v1.7)

- Migration `sql/migrations/v1_7_003_dssmp_registration.sql` + `_down.sql`; DDL `sql/ddl/04` and `sql/ddl/08`
  (dssmp_version subject on audit.review_record). Loader `src/jobs/load_dssmp_registrations.py`
  (`dam_safety.dssmp_registration`), authority `dam_safety.dssmp_authority` (wired into verify_review_chain).
  Contract: root SCHEMA_CONTRACT.md §7.1c.
- Evidence: tests/test_dssmp_registration.py (plan cases, UC CHECKs executed in SQLite, MERGE executed on local
  PostgreSQL 17: insert + close in one statement, idempotent re-run, in-statement stale re-check, first version),
  tests/test_dssmp_authority.py, tests/test_migrations.py.
- UNVERIFIED: not run on a workspace; Delta MERGE with subqueries in the USING source; ADD CONSTRAINT on a
  ref.dssmp_version that already holds approved rows without approval fields fails by design (PRE-CHECK in the
  header); the app side (outbox INSERT, dssmp_version chain records) does not exist yet - App Dev.

## LB-PUBLISHER (Lakebase publisher role + default privileges, v1.7; app SCHEMA_CONTRACT §7 items 17 / 18)

- `lakebase/001_app_schema.sql`: the three publication outboxes (`ref_change_publication`, `onboarding_publication`,
  `dssmp_registration_outbox`) are owned by `dam_lakebase_owner`; the app role has SELECT + INSERT only (the app never
  updates them: checked in app/backend/repos). NOLOGIN `dam_lakebase_publisher`: USAGE on dam_app, SELECT and column
  UPDATE (status, message, applied_at) on those three tables, nothing else; the dam_ref_publish login joins it via
  `SET dam.publisher_login` (refused if it is the app role or an owner member). The extract role stays SELECT-only
  and gets `ALTER DEFAULT PRIVILEGES FOR ROLE dam_lakebase_owner` and `FOR ROLE <app role>` `GRANT SELECT ON
  TABLES`; there is no default privilege for the publisher.
- `lakebase/validate_append_only.sql` adds outbox_owner, app_outbox_privilege, publisher_* , extract_privilege and
  extract_default_select_missing checks.
- UC: `tools/render_uc_reviewer_grants.py` (CI --check) grants each dam group SELECT on `ref.dssmp_version`; the table
  is row-filtered per dam in both security modes (`sql/ddl/10a_row_filters_table.sql` SET ROW FILTER,
  `10a_row_filters_abac.sql` DROP ROW FILTER + dam_scope tag).
- Evidence: tests/test_lakebase_publisher_role.py on local PostgreSQL 17.11 (publisher can set outcome columns only;
  UPDATE of payload / kind / dam_id / requested_* / document_sha256 / approval_record_hash, DELETE, INSERT, TRUNCATE,
  CREATE and reads of other tables are refused; app cannot UPDATE / DELETE / TRUNCATE outboxes; extract sees tables
  created later by the app and the owner, publisher does not; the validator catches each widening), app live tests
  tests/test_lakebase_owner_live.py + test_state_pg_append_only.py pass on the new 001.
- UNVERIFIED: Lakebase role model (membership of an OAuth SP role in a NOLOGIN group role, ALTER DEFAULT PRIVILEGES
  by the deployer), the publish job's login name; the ABAC tag on ref.dssmp_version.
- Test hazard: roles are cluster-global, so the ownership tests that flip `dam_lakebase_owner` attributes can break
  another agent's run on the same PostgreSQL cluster (seen once on 2026-11-09; re-run passed).

## OUTBOX-CONTRACT (dssmp_registration_outbox = app SCHEMA_CONTRACT §7 item 19, 2026-11-09)

- Lakebase migration `lakebase/migrations/v1_7_004_dssmp_outbox_contract.sql` (block inlined verbatim in
  lakebase/001, so a fresh or re-run 001 converges) + rollback `_down.sql` (renames back, no data dropped).
  Loader `src/jobs/load_dssmp_registrations.py` writes `status loaded|rejected`, `reason_code`, `message`,
  `processed_at`; root SCHEMA_CONTRACT.md §7.1c.
- Evidence (local PostgreSQL 17.11): tests/test_dssmp_outbox_contract.py - column list equal in the app's state.py DDL,
  the app router's INSERT dict, lakebase/001 and dssmp_registration.OUTBOX_COLS; an old DA-shaped table converted in
  place by 001 (idempotent re-run); **the app's own `StateStore.insert_dssmp_outbox` (app/ at HEAD, connected as the
  app role) inserts the router's row**; the load job's UPDATE_SQL succeeds as the publisher login; the publisher may not
  touch proposed_by, the app may not touch status; old rows with requested_at values make the migration raise and
  change nothing; migration -> rollback (twice) keeps every row and reason_code. tests/test_lakebase_publisher_role.py
  (per-outbox column grants), tests/test_dssmp_registration.py (statuses / reason codes).
- Differences between the steering list and §7.19 (§7.19 followed): the status set also allows `rejected_invalid`;
  the app's own DDL leaves `document_sha256` nullable while lakebase/001 has NOT NULL (both lists say required).
- Finding (pre-existing, not fixed here): the jobs' Lakebase SQL uses unqualified table names and
  `lakebase_extract._connect` sets no search_path; the live test sets `search_path TO dam_app` explicitly.
- UNVERIFIED: not run on a Lakebase instance.

## QA-055 (catalog allowlist) + verify_all fail closed (2026-11-09)

- Commit A applies App Dev's tools/patches/qa055_catalog_allowlist.patch (e988454) unchanged: src/dam_safety/catalog.py,
  audit_chain.require_signer_for_catalog and jobs/_common.args canonicalize, tests/test_catalog_allowlist.py.
- Commit B: `verify_review_chain.verify_all` canonicalizes the catalog itself; None = dam_prod (refused without a real
  signer); an empty, unknown, garbled or non-string name raises ValueError; a prod refusal with no chains still returns
  one ok=False `signer_refused` verdict instead of an empty list. Tests: tests/test_catalog_allowlist.py (verify_all
  cases), tests/test_prod_fail_closed.py.
- Note: the patch docstring names a byte-identical app copy `app/backend/catalog.py` checked by
  app/tests/test_settings_prod_guard.py; neither exists on e988454 (App Dev). There is no qa/test_prod_signer_fail_closed.py
  in this tree; the prod fail-closed tests are tests/test_prod_fail_closed.py.

## DSSMP-DOC (document copy to UC volume ref.dssmp_documents, v1.7 (b))

- `src/dam_safety/dssmp_documents.py`, wired in `src/jobs/load_dssmp_registrations.py` between plan and MERGE;
  `ref.dssmp_version.volume_uri` (sql/ddl/04, v1_7_003); volume `ref.dssmp_documents` (sql/ddl/00 + v1_7_003; the down
  script keeps the volume, its files and the column); lakebase/001 mirrors the app's `dssmp_document` DDL and grants
  `dam_lakebase_publisher` SELECT on it; the validator flags any other publisher privilege on it or a missing SELECT.
- Evidence: tests/test_dssmp_documents.py - copy to the contract path + read-back; mismatch before copy (outbox hash,
  dssmp_document.sha256, content, bad base64) writes nothing; mismatch after copy removes the copy; an existing
  different file is not overwritten; missing / other-dam / copy-failure codes; garbled catalog and path traversal
  refused; a refused decision leaves the MERGE source empty and writes reason_code back; live PG 17: the app's own
  `StateStore.insert_dssmp_document` stores the document as the app role, the publisher login reads it with DOC_SQL
  (and cannot UPDATE it), the copy lands in a temporary volume root, the validator passes and then flags a revoked SELECT.
- "Declared in the bundle": the volume is created by sql/ddl/00 (bundle job `dam_uc_setup`, task ddl_00) like
  raw.landing / rpt.artifacts / ops.checkpoints, not as a `resources.volumes` entry: the schema `ref` is created by that
  DDL job after `bundle deploy`, so a bundle-managed volume would fail on a fresh workspace and would double-own an
  object the DDL also creates. Say if a `resources.volumes` entry is wanted anyway.
- UNVERIFIED: not run on a workspace - Python file I/O on /Volumes from the serverless job, WRITE VOLUME / READ VOLUME
  grants for the run_as principal (not added to 10b), the publisher's SELECT on dssmp_document on Lakebase, large
  documents (content_b64 is read whole into memory).

## LB-CDSE-TABLES (lakebase/001 + migration v1_7_005; app SCHEMA_CONTRACT §7 items 20 / 21)

- lakebase/001 carries the app's DDL verbatim for `dam_cdse_change`, `dam_cdse_outbox`, `rule_reversion_proposal`
  (block `v1_7_005`, identical in `lakebase/migrations/v1_7_005_cdse_reversion_tables.sql`). `dam_cdse_outbox` is
  owned by dam_lakebase_owner (app SELECT + INSERT; publisher SELECT + UPDATE (status, reason_code, message,
  processed_at)); the two workflow tables are app-owned. Validator covers the new outbox. Rollback
  `_down.sql` revokes the publisher and returns the outbox to the app role; it drops no table, column or row.
- Evidence (PG 17.11): tests/test_lakebase_cdse_tables.py (fresh 001 -> validator 0 rows, the app's own StateStore
  writes all three tables as the app role, publisher limited to the 4 columns; upgrade from 001@dfc08bb by migration
  (twice), rollback (twice) keeps all rows and removes the publisher grant, re-apply and re-run of 001 converge),
  tests/test_lakebase_publisher_role.py (dam_cdse_outbox added to every outbox case).
- UNVERIFIED: not run on a Lakebase instance.

## CDSE-ASSIGNMENT (app SCHEMA_CONTRACT §7 item 20; readiness view / function)

- CHECKs (08 + v1_7_003): subject `dam` with the 4 actions; `ck_dam_cdse_assignment`. Engine
  `dam_safety.cdse_assignment`, planner `dam_safety.cdse_load`, job `src/jobs/apply_cdse_assignments.py` (outbox-driven,
  audited per outcome in audit.event_log), `verify_review_chain` replays the same engine (`dam_cdse_*` errors).
  Views `ref.v_dam_reviewer_authority` (rewired), `ref.v_dam_go_live_readiness`, function `ref.assert_dam_go_live_ready`.
- Rollback: v1_7_003 `_down.sql` drops only `ck_dam_cdse_assignment` (the history table and every row stay; Delta
  RESTORE is the fallback); the view / function are CREATE OR REPLACE (re-run the previous 10b to roll back).
- Evidence (local): tests/test_cdse_assignment.py 46 tests: engine (bootstrap, change, reject, membership CDSE,
  not-live, second-approver switch), every refusal code, load plan (`cdse_exists`, `cdse_stale` incl. MERGE re-check,
  deferral), audit rows; PG 17.11: the assignment MERGE + dam sync, the audit INSERT run twice -> 1 row, the app's
  outbox row read and updated by the job's own SQL as the publisher login on a fresh lakebase/001; SQLite renders of
  the views with the job's SQL.
- UNVERIFIED: never run on Databricks (Delta MERGE / SQL UDF raise_error / grants for the run_as principal) or a
  Lakebase instance; the readiness function on a live dam.

## RULE-REVERSION (app SCHEMA_CONTRACT §7 item 21; (C) DSSMP re-versioning)

- CHECKs (08 + v1_7_003, identical bodies): subject `rule_reversion`, the 3 actions. Engine
  `dam_safety.rule_reversion_authority` (chain + publication checks), wired into `verify_review_chain` and
  `ref_change_publish.plan` (job `publish_ref_changes` loads approval records and authority only when a re-version row
  is pending). `ref.v_dssmp_affected_rules`, `ref.dssmp_affected_rules` (10b).
- Rollback: v1_7_003 `_down.sql` restores the v1_6_003 CHECKs (pre-check now also lists `rule_reversion` records; it
  never deletes them); the view / function are CREATE OR REPLACE objects without data (drop or re-run the previous
  10b); Delta RESTORE is the data fallback. Publisher code rollback: pause dam_ref_publish first.
- Evidence (local): tests/test_rule_reversion.py 25 tests: applied path, every refusal code, deferral, fail-closed
  without context, flag rows unchanged, re-run idempotent, chain codes, verify job wiring, CHECKs in SQLite; PG 17.11:
  the CHECKs, the reversion row through the publisher's MERGE (twice), the affected-rules view.
- UNVERIFIED: Databricks SQL table function syntax for `ref.dssmp_affected_rules`; never run on a workspace.

## LB-HEAD-LOG (residual a: tamper-resistant Lakebase chain head, v1.7)

- `lakebase/001_app_schema.sql`: `review_chain_head_log (chain_id, chain_seq, head_hash, updated_ts, PK (chain_id,
  chain_seq))`, owned by `dam_lakebase_owner`, guard triggers t_noupd_/t_notrunc_ ENABLE ALWAYS, app role SELECT +
  INSERT only, seeded once from the legacy `review_chain_head`. The validator treats it like review_signoff.
- Extract (`src/jobs/lakebase_extract.py`): current head per chain = highest chain_seq over the log and the legacy
  table (log wins at equal seq), so a rewound / deleted legacy head cannot lower what audit.chain_head records.
- App change for App Dev (NOT applied): `tools/patches/v17_chain_head_log_state.patch` (state.py: per-chain advisory
  lock instead of SELECT ... FOR UPDATE, append a head row per record, read the latest head from the log, keep the
  legacy row in step; app/tests/test_audit_chain.py updated for the new behaviour). Re-based on the app at 9000bf7
  (append_chained now has also_row; OUTBOX_GUARDED): with the patch, a copy of the app suite passes (725) and the app
  live test passes on the new 001.
- Re-based again on the app at 0aed4c4 (QA-056): the chain advisory lock (class 4702046) is taken AFTER the app's
  `lock_dam` (class 4702047) and `check(q)`, so the lock order stays dam -> chain. Patched copy of the app suite: 740
  passed (QA-056 live concurrency tests included).
- Migration for an existing Lakebase: `lakebase/migrations/v1_7_006_chain_head_log.sql` (block byte-identical to 001,
  owner, ENABLE ALWAYS guards, app SELECT + INSERT, extract SELECT); rollback `_down.sql` drops nothing (table, rows
  and guards stay) and only revokes the app role's INSERT. Roll back the app patch first.
- Evidence: tests/test_chain_head_log.py on local PostgreSQL 17.11 (also: 001@ea66581 + migration twice -> validator
  0 rows, rollback twice keeps every head row and refuses app INSERT, re-apply restores it): the app role cannot UPDATE (rewind / forge),
  DELETE, TRUNCATE, disable triggers, drop the table or use session_replication_role; even dam_lakebase_owner is
  stopped by the guard; the patched StateStore, run as the app role, appends 0,1,2,3 and its chain verifies.
- UNVERIFIED: Lakebase (advisory locks and hashtext() on Lakebase, the role model); an UNPATCHED app keeps working
  (it only updates the legacy table) but its new heads are then not tamper-resistant until App Dev applies the patch;
  a privileged actor who can drop the guard can still rewrite the log (same limit as review_signoff).

## MIGRATION-DOWN (residual (b): rollback scripts for every UC migration)

- New: `sql/migrations/v1_5_001_contract_down.sql`, `v1_6_001_audit_chain_down.sql`, `v1_6_002_chain_head_down.sql`,
  `v1_6_003_alarm_rule_flag_down.sql`, `v1_7_001_cdse_ack_down.sql` (v1_7_002 / v1_7_003 downs existed; v1_7_002 got
  its PRE-CHECK line). Each header names Delta time travel / RESTORE TABLE as the data fallback, lists its PRE-CHECK and
  what it deliberately keeps. No down drops a table, column, volume or row, updates data, or removes
  `delta.appendOnly`: downs only drop the CHECKs a migration added or restore the previous CHECK bodies (copied from
  the earlier migration), and v1_7_001_down only drops NOT NULL / DEFAULT (the false backfill and the Delta
  allowColumnDefaults protocol feature stay).
- Evidence: tests/test_migration_downs.py: static (header, forbidden statements, the CHECK state after each down
  equals the state before its migration, simulated over the whole chain); PostgreSQL 17.11 with a mechanical
  Delta -> PG translation: base tables, every up twice, every down twice in reverse order with the constraint set
  checked after each, rows / columns / audit.chain_head / ref.dam_cdse_assignment kept, every up again.
- UNVERIFIED: none of the downs ran on Delta (`ALTER COLUMN ... DROP DEFAULT` / `DROP NOT NULL`, SQL scripting); the
  PG translation does not cover Delta-specific semantics (appendOnly, table features, volumes).

## QA-056 (unique guard indexes, App Dev 0aed4c4, lakebase block v1_7_007)

- App Dev's tools/patches/qa056_unique_guards.patch applied unchanged (owner-owned tables:
  `dam_cdse_outbox_one_pending`, `dssmp_outbox_one_pending`, `ref_change_publication_one_version`), plus, for the
  single schema source, the app's own guards on the app-owned tables lakebase/001 also creates:
  `dam_cdse_change_one_pending`, `rule_reversion_one_pending` (names / predicates = app UNIQUE_GUARDS). The down drops
  only the three owner-table indexes (the app recreates its own at start-up).
- Evidence (PG 17.11): tests/test_lakebase_qa056_guards.py: blocks identical and equal to the app's predicates; fresh
  001 refuses a second pending outbox row and a second pending CDSE change for the app role; down / re-apply; upgrade
  from 001@d150288 by the migration (twice) -> validator 0 rows.
- Note: the 0aed4c4 commit message says lock class 4702046; the code and app SCHEMA_CONTRACT use DAM_LOCK_NS 4702047
  (4702046 is the DA chain-lock class). The code is right; the message is not.
- UNVERIFIED: not run on Lakebase; PRE-CHECK duplicates must be resolved before the migration on a live database.

## QA-057 (down scripts refuse dam_prod; two layers)

- Layer 1: `tools/run_migration.py` (new; there was no runner). Normalizes `--catalog` with
  `dam_safety.catalog.normalize_catalog` (unknown names, e.g. Cyrillic look-alikes, fail closed, exit 2); refuses any
  `sql/migrations/*_down.sql` on dam_prod, including NFKC / zero-width / soft-hyphen look-alikes that normalize to
  dam_prod (exit 3), unless `--break-glass-prod "<reason>"` (>= 10 characters) is given; the break-glass run is logged
  (JSON: utc, user, catalog, file, sha256, reason) to stderr and `--log-file` before submission. Always binds the named
  parameters `catalog` (canonical) and `break_glass` ('' or `BREAK_GLASS_PROD`). Only sql/migrations files accepted.
- Layer 2: the first statement of all 7 UC downs is an identical `SELECT CASE WHEN ... THEN raise_error(...)` guard
  on `:catalog` (format characters / whitespace removed, lower-cased) OR `current_catalog()` = 'dam_prod' without
  `:break_glass = 'BREAK_GLASS_PROD'`; it is repeated on `current_catalog()` right after `USE CATALOG`. A missing
  parameter makes the statement fail (unbound marker), i.e. refused; a session whose default catalog is dam_prod is
  refused too (fail closed). Ups are unchanged.
- Evidence: tests/test_qa057_prod_guard.py (runner: prod + 7 look-alikes, every down, empty / short reason, unknown
  catalogs, dev / test allowed, ups allowed, prod + flag allowed and logged, CLI subprocess; SQL: every down starts
  with the guard, guard executed on PostgreSQL 17 with raise_error emulated and current_catalog() bound per case
  (13 cases), the whole v1_7_003 down refused for dam_prod); tests/test_migration_downs.py full chain still passes with
  the guard bound to dam_dev.
- UNVERIFIED on Databricks: named parameter markers inside a sql_task file and their behaviour when unbound,
  `raise_error` inside `SELECT CASE`, the `\p{Cf}` regex class in `regexp_replace`, `current_catalog()` before /
  after `USE CATALOG`, the databricks-sdk `jobs.submit` call shape in the tool. Lakebase downs
  (lakebase/migrations/*_down.sql) are not catalog-scoped and are not guarded by this tool (run by hand per runbook 8).

## LB-SEARCH-PATH (QA item 4: unqualified Lakebase table names in the jobs)

- `src/jobs/lakebase_extract._connect(project, branch, endpoint, schema)` now calls `_session(conn, schema)`: schema
  name validated (`^[a-z_][a-z0-9_]{0,62}$`, else ValueError before any SQL), `SET search_path TO "<schema>"`
  (pg_catalog stays implicitly first; no `"$user"` / public fallback), verified with `current_schemas(false)` =
  [schema] (missing schema or no USAGE -> RuntimeError, connection closed), committed as a session setting, then
  REPEATABLE READ (QA-045d unchanged). Schema from config: bundle variable `lakebase_schema` (default dam_app) ->
  `--lb_schema` on all four Lakebase tasks (extract, apply_cdse_assignments, load_dssmp_registrations,
  publish_ref_changes); apply_cdse_assignments' own `SET search_path TO dam_app` removed (now shared).
- Evidence (PG 17.11): tests/test_lakebase_search_path.py: without the set-up an unqualified name resolves to
  public; with it to dam_app only, survives commit, isolation repeatable read; missing / inaccessible schema fails
  closed; `_connect` with the SDK faked uses the set-up and closes on failure; jobs.yml / databricks.yml wiring.
- UNVERIFIED: on Lakebase (the jobs' OAuth role must have USAGE on dam_app, i.e. the extract / publisher roles).

## RUNBOOK-PRIMITIVES (QA item 2, 2026-11-09)

- docs/DEV_WORKSPACE_RUNBOOK.md step 4b: one step per UNVERIFIED primitive (P1 MERGE with subquery source, P2 SQL UDF
  raise_error + assert_dam_go_live_ready, P3 table function ref.dssmp_affected_rules, P4 Delta ALTER COLUMN DROP
  DEFAULT / DROP NOT NULL, P5 advisory locks + hashtext(), P6 PG version / role attributes / REPEATABLE READ /
  search_path, P7 run_as grants, P8 QA-057 down guard), each with copy-pasteable commands and the exact PASS output.
  Lakebase order now includes v1_7_007 (up and down).
- Checked locally: P1 MERGE result on PG 17.11 (a|1|f, a|2|t); P5 / P6 psql commands and outputs on PG 17.11; P8
  layer-1 commands and outputs; tools/verify/qa057_guard_probe.sql = the downs' first statement (test) and its PG
  emulation. NOT checked: every Databricks-side expected output (Statement Execution API JSON shape, DESCRIBE FUNCTION
  text, information_schema column names, jobs submit exit codes) - the steps themselves are the verification.

## OUTBOX-STATUS-3 (v1.7 steering 2026-11-09: dssmp_registration_outbox status pending | loaded | rejected)

- 'rejected_invalid' dropped from `ck_dssmp_outbox_status` in lakebase/001 (CREATE TABLE and the v1_7_004 block) and
  in lakebase/migrations/v1_7_004_dssmp_outbox_contract.sql (same block; existing 'rejected_invalid' rows are mapped
  to 'rejected', reason_code / message kept; re-running converges a database that ran the earlier version). The down
  is unchanged (maps rejected -> rejected_invalid for the pre-v1.7 shape). `dssmp_registration.STATUSES` = 3 values.
  dam_cdse_outbox is NOT changed (not in the steering).
- App side: App Dev drops 'rejected_invalid' from the app's dssmp_registration_outbox CHECK in step; until then
  tests/test_dssmp_outbox_contract.py accepts the app CHECK with or without it (nothing else).
- Evidence (PG 17.11): fresh 001 refuses 'rejected_invalid' and accepts the three values; a 4-value CHECK with a
  'rejected_invalid' row is converged by the migration (twice): row -> rejected, constraint without rejected_invalid.
- UNVERIFIED: on Lakebase.
