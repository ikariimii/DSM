# Dam Safety Monitoring — Databricks Asset Bundle (AWS, Databricks-native)

Ingest → QC → reduce → alarm → interpret → report for a dam portfolio, using only Databricks services:
UC volumes, Auto Loader, serverless Lakeflow Declarative Pipelines (LDP), Jobs, SQL alerts, Databricks
notification destinations, Lakebase, a Databricks App and MLflow. **No SNS, Kinesis or external pager.**

> **Status: NOT DEPLOYED.** Nothing has been deployed or validated against a workspace (no CLI/Spark on the
> build box). See `docs/VERIFICATION.md` for every UNVERIFIED item.

## Architecture
```
/Volumes/<cat>/raw/landing/<dam>/{readings,inclinometer}/   (ADAS/TOA5 drops via toa5_landing, app manual entry)
   │ Auto Loader — pipeline dam_pipeline_<cat> (serverless LDP, src/pipelines)
   ▼
raw.reading_raw / raw.incl_survey_raw  append-only, + raw.*_quarantine          (bronze.py)
   ▼
silver.reading_clean (expect_all_or_drop) + silver.reading_quarantine, silver.incl_survey   (silver.py)
   ▼
job dam_ingest_<cat>: pipeline → reduce_readings (→ silver.reading_reduced: SCD2 calibration + formula engine + QC, lineage)
                      → alarm_evaluator (state machine → ops.alarm_state/transition/exceedance)
gold.instrument_latest / instrument_daily / data_freshness (gold.py); gold.threshold_exceedances view (11_gold_views.sql)
   ▼
SQL alerts (resources/alerts.yml) → Databricks notification destinations / email (ops + independent life-safety path)
job dam_recompute_<cat>: history recompute with lineage + alarm replay
job dam_interpretation_<cat>: HST, anomaly, fault classifier, PFM rules, commentary (serverless env 6)
job dam_lakebase_extract_<cat>: Lakebase app writes → audit / ops / rpt
job dam_heartbeat_<cat>: weekly-minimum pipeline run;  job dam_uc_setup_<cat>: DDL 00..13 + seeds
   ▼
Databricks App (app/): OBO SQL reads under row filters; writes to Lakebase `dam_app`
```
Hierarchy: portfolio > dam > structure > asset > instrument > channel(sensor).
Catalogs `dam_dev | dam_test | dam_prod`, schemas `ref raw silver gold ops audit rpt ml`.
Security: `ref.dam_row_filter` (`sql/ddl/10_security.sql`) applied in ONE mode, `var.security_mode` = `table` (default,
`10a_row_filters_table.sql` + LDP `row_filter=`) or `abac` (governed tag `dam_scope`, `10a_row_filters_abac.sql`); column masks.
SCD2: `ref.calibration`, `ref.threshold`, `ref.pfm`, `ref.incl_installation`, `ref.incl_correction`.
DSSMP/PFM/EAP tables: `04_ref_dssmp_pfm_eap.sql`; reports: `09_rpt.sql`.

## Layout
| Path | Contents |
|---|---|
| `databricks.yml`, `resources/*.yml` | bundle: uc_setup, pipeline, jobs, alerts, app, lakebase, ml, canari_pilot |
| `sql/ddl/00..13` | UC DDL (run in order, see SCHEMA_CONTRACT §8) |
| `lakebase/001_app_schema.sql` | Lakebase `dam_app` DDL |
| `src/pipelines` | LDP bronze/silver/gold |
| `src/dam_safety` | formulas (registry), qc, alarms, calibration, datum, inclinometer, interpretation, toa5_landing |
| `src/common/toa5.py` | App Dev's clean-room TOA5 reader (canonical) |
| `src/jobs` | job entry points |
| `tools/` | sample-data generator, formula seed, dataset catalog, requirement lock |
| `sample_data/` | synthetic 5-dam data (SYN-DAM-01..05) |
| `tests/` | pytest; every test cites its `/workspace/dam-refs` source |
| `testonly_restricted/` | internal-only tests on restricted data (gitignored, sync-excluded) |
| `app/` | Databricks App (App Developer) |
| `qa/` | QA agent's area — not ours, excluded from our runs |
| `docs/` | api_contract, VERIFICATION, INTERPRETATION, PROVENANCE, TOA5_READER, DEPENDENCY_PINS |

## Run tests
```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q -p no:cacheprovider                      # shipped tests (tests/)
.venv/bin/python -m pytest -q -p no:cacheprovider testonly_restricted/ # internal build box only
```
Tests needing `/workspace/dam-refs` skip when it is absent (CI).

## Sample data
`python tools/generate_sample_data.py` → `sample_data/ref/*.csv` + `sample_data/landing/<dam>/...` (seed 20261102,
deterministic). Fully synthetic: 60 days × 6-hourly; headwater, two VW piezometers, standpipe, weir per dam;
inclinometer surveys for SYN-DAM-01/02 (02 has a 7.5–8.5 m shear zone). Planted defects: NaN, ×3 spike, bad
timestamp, blank sensor_id, rising PZ-02 on SYN-DAM-03. Upload to `/Volumes/dam_dev/raw/landing/` to exercise the pipeline.

## Licensing / restricted data
Enguri (CC BY-NC), ICOLD BW2022 Themes A/C, USBR RISE, the user's inclinometer workbook and vendor-manual data are
**internal-test-only**. They are never in `sample_data/`, shipped tables, the app or the bundle sync
(`databricks.yml` excludes, `.gitignore`); `tests/test_license_leak.py` fails on a leak. TOA5 reader is clean-room
(`docs/PROVENANCE.md`); PyTOA5/igbpyutils (GPL) not used.

## KB adoption (`/workspace/databricks-kb/summary.md`)
| # | Recommendation | Status |
|---|---|---|
| 1 | Isolated envs, service principals | Adopted as one catalog per env + prod `run_as` SP. **Partial:** targets may share a workspace; separate workspaces are a deployment choice (not decided). |
| 2 | Auto Loader, file events, **S3 external volume** | Auto Loader adopted. **Disagree (for now):** `raw.landing` is a managed volume — no S3 location/storage credential was provided; switch to an external volume when one exists. File events not enabled (UNVERIFIED availability). |
| 3 | Layered expectations, `expect_or_fail` only on gold | Bronze/silver drop + quarantine adopted. **Disagree on gold `expect_or_fail`:** a failing gold update would stop the tables that feed life-safety alerts; gold has no fail expectations, and freshness is surfaced by `gold.data_freshness` instead. |
| 4 | ABAC tags, row filters, masks | Adopted (syntax UNVERIFIED, ABAC-1). |
| 5 | App Medium, 2+ instances prod, OBO scopes | Adopted (dev 1 instance). |
| 6 | Lakebase Autoscaling + **Triggered synced tables for gold lookups** | Lakebase adopted. **Disagree:** no synced tables — Postgres copies bypass UC row filters/ABAC, so a user could see other dams' readings; app reads gold via the SQL warehouse OBO instead (SCHEMA_CONTRACT §7.3). |
| 7 | SQL alerts on `gold.threshold_exceedances`, staggered, + independent life-safety path | Adopted, but routed only to Databricks notification destinations/emails per the user's native-only rule (no PagerDuty/Teams wired; a destination ID can point there later). |
| 8 | Built-in anomaly detection + MLflow 3 `@Champion` | **Partial:** MLflow `@Champion` placeholder only; built-in data-quality anomaly detection not enabled (Public Preview); HST residual scoring used instead. |
| 9 | Everything in bundles, GitHub OIDC | Adopted (`.github/workflows/bundle.yml`). Dashboards/Genie not in bundle yet. |
| 10 | Lakebase region, tags/billing | Adopted as variables; region not chosen. |

## Schema additions (v1.1, see SCHEMA_CONTRACT.md)
`ref.incl_installation` (SCD2 probe constants, depth_reference, sign multipliers, baseline);
`ref.incl_correction` expanded to the REPORT §6.3 audit fields; row filters on both; `ref.instrument_type`
vocabulary; `silver.incl_reduced` + `depth_reference`, `baseline_survey_id`, `correction_record_ids`.

## Deploy (NOT RUN — needs user approval)
```
databricks bundle validate -t dev
databricks bundle deploy  -t dev --var warehouse_id=<id> --var ops_email=<addr> ...
databricks bundle run -t dev dam_uc_setup     # DDL 00..13 + seeds
databricks bundle run -t dev dam_ingest       # pipeline → reduce → alarms; alerts PAUSED via var.alert_paused
```
