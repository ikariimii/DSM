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
| Inclinometer module (REPORT §6) | `test_inclinometer_module.py` (public: DGSI 359/−339 → 0.4188 in; Mikkelsen 2003 446/−426 → D 872, CS 20); internal workbook values in `testonly_restricted/` (5 tests, not shipped) |
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
| INC-WIRE | no job writes `silver.incl_reduced` yet; the engine module is not wired into a Spark job (`pandas_ops` has a profile UDF on the old core path) |
