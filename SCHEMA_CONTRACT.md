# SCHEMA_CONTRACT — Dam Safety App (data layer ⇄ app ⇄ QA)

Status: **PROPOSED v1.1 (2026-11-02)**. v1.0 was agreed by the App Developer with 12 open questions (app/SCHEMA_CONTRACT.md §6);
v1.1 answers them (§10) and adds the inclinometer tables (steering 9). QA sign-off pending. It resolves **QA-003**.
Changes v1.0 → v1.1 (all additive except the `qa_reason` separator correction, which fixes the text to match the code):
`qa_reason` is **comma**-joined; `ref.incl_installation` (new) and `ref.incl_correction` (columns = audit fields);
`silver.incl_reduced` +3 nullable columns; `ref.instrument_type` vocabulary (§5.4); no synced table (§7.3);
DSSMR draft path + ack ownership (§7.2); manual readings land under `<dam>/readings/`.
Owner: data-layer builder (bundle `dam-safety-app`). DDL source of truth: `sql/ddl/*.sql`.
Change rule: any rename or removal needs a new contract version and sign-off from the App Developer and QA.
Additive columns are allowed without sign-off.

## 1. Catalogs and schemas (QA-003 resolution)

| Bundle target | Catalog | Notes |
|---|---|---|
| `dev`  | `dam_dev`  | development mode, resources prefixed per user |
| `test` | `dam_test` | CI integration target |
| `prod` | `dam_prod` | production mode, run_as service principal |

The KB (`/workspace/databricks-kb/summary.md`) recommends one catalog per environment; this table follows it.
**There is no `dam_safety` catalog.** The FERC canvas name `dam_safety.<schema>.<table>` maps to `<env catalog>.<schema>.<table>`.

**Every catalog contains all of these schemas:**

| Schema | Purpose | Writer(s) |
|---|---|---|
| `ref` | hierarchy, calibrations (SCD2), thresholds (SCD2), formulas, DSSMP/PFM/EAP, schedules | seed/onboarding job; DDL |
| `raw` | bronze: append-only as-received data and quarantine; landing volume `raw.landing` | Lakeflow pipeline only |
| `silver` | validated readings and engineering-unit reductions with lineage | pipeline (`reading_clean`, `reading_quarantine`, `incl_survey`); jobs (`reading_reduced`, `incl_reduced`) |
| `gold` | aggregates, alert views, interpretation outputs | pipeline MVs; DDL views; interpretation job |
| `ops` | alarm state machine, exceedances, incidents, inspections | alarm evaluator job; Lakebase extract job |
| `audit` | insert-only sign-offs, event log, recompute log | jobs only (INSERT); no UPDATE/DELETE grants |
| `rpt` | DSSMR, Part 12D, IC recommendations, engineering commentary | jobs; Lakebase extract |
| `ml` *(extra)* | UC-registered models with the `@Champion` alias | interpretation training job |

`ml` is additive. The KB recommends it, and it holds only MLflow models, so it is not part of the seven-schema rule.

App OLTP state lives in **Lakebase Autoscaling** in Postgres schema `dam_app` (see `lakebase/001_app_schema.sql`). It is not in UC.

### Required app config change
`app/backend/config.py` → `Settings.uc()` currently maps only `ref|silver|gold|ops|audit`. **Please add `raw` and `rpt`**
(env `DAM_SCHEMA_RAW`, `DAM_SCHEMA_RPT`, defaults `raw` and `rpt`). `catalog` stays `DAM_CATALOG`, default `dam_dev`. That already matches.

## 2. FERC canvas table → home (every entity in the FERC data-model canvas)

| FERC canvas name | Home (all catalogs) | Kind | DDL |
|---|---|---|---|
| `ref.dam` | `ref.dam` | table (row filter) | 01 |
| `ref.dssmp_version` | `ref.dssmp_version` | table SCD2 | 04 |
| `ref.pfm` | `ref.pfm` | table SCD2 | 04 |
| `ref.pfm_instrument` | `ref.pfm_instrument` | bridge SCD2 | 04 |
| `ref.instrument` | `ref.instrument` | table | 01 |
| `ref.threshold` | `ref.threshold` | table SCD2 (`threshold_id`,`threshold_version`) | 02 |
| `ref.eap_trigger` | `ref.eap_trigger` | table SCD2 | 04 |
| `ref.reading_schedule` | `ref.reading_schedule` | table | 02 |
| `ops.reading_raw` (bronze) | **`raw.reading_raw`** (pipeline streaming table) + view `ops.reading_raw` | ST + view | 05 / 11 |
| `ops.reading` (silver) | **`silver.reading_clean`** (validated) + **`silver.reading_reduced`** (engineering values, append-only) → contract view **`ops.reading`** with canvas columns | ST + table + view | 06 / 11 |
| `ops.exceedance` | `ops.exceedance` (append-only) + `ops.exceedance_status_log`; current status in `gold.v_exceedance_current` | tables + view | 07 / 11 |
| `ops.safety_incident` | `ops.safety_incident` | table | 07 |
| `ops.regulatory_report` | `ops.regulatory_report` | table | 07 |
| `ops.calibration_event` | `ops.calibration_event` | table | 07 |
| `ops.surveillance_inspection` | `ops.surveillance_inspection` | table (from Lakebase `inspection_note`) | 07 |
| `audit.review_record` | `audit.review_record` | insert-only table | 08 |
| `rpt.dssmr` | `rpt.dssmr` (artifacts in volume `rpt.artifacts`) | table | 09 |
| `rpt.part12d_inspection` | `rpt.part12d_inspection` | table | 09 |
| `rpt.ic_recommendation` | `rpt.ic_recommendation` | table | 09 |

Tables named in the plan canvas or the app that are not in the FERC canvas:

| Name used | Home | Notes |
|---|---|---|
| `ref.site` (app, plan canvas) | **view** `ref.site` over `ref.dam` | column aliases for the app (`hazard_class`, `crest_elev_m`, `normal_pool_m`) |
| `ref.structure`, `ref.calibration`, `ref.formula` | same names | 01/02/03 |
| `ref.incl_installation` *(v1.1)* | same name | SCD2 per inclinometer: probe L/K, casing depth (m), depth reference, sign_mult_A/B, baseline survey | 02 |
| `ref.incl_correction` *(v1.1, replaces the v1.0 placeholder)* | same name | versioned correction records (bias/rotation/depth/sensitivity/spiral/baseline) = `dam_safety.inclinometer.corrections.AUDIT_FIELDS` | 02 |
| `ref.instrument_type` *(v1.1)* | same name | canonical type vocabulary, §5.4 | seed |
| `ref.portfolio`, `ref.asset`, `ref.sensor`, `ref.project_membership`, `ref.qc_rule` | same names | hierarchy portfolio > dam > structure > asset > instrument > sensor(channel) |
| `silver.readings_clean` (plan canvas) | `silver.reading_clean` (singular) | pipeline-owned |
| `silver.quarantine` (plan canvas) | `silver.reading_quarantine` and `raw.reading_quarantine` | quarantine in bronze and in silver |
| `silver.reduced_readings` (plan canvas) | view over `silver.v_reading_current` | |
| `silver.v_reading_current` (app) | view: latest reduction per `reading_id` | **app reads this** |
| `silver.incl_survey` / `silver.incl_survey_header` (app) | ST `silver.incl_survey` + view `silver.incl_survey_header` | |
| `silver.incl_reduced` (app) | job table `silver.incl_reduced` | |
| `ops.alarm_event` (app) | **view** over `ops.alarm_transition` (one row per transition, `event_id` = RAISE transition id) + `ops.alarm_episode` (one row per episode) | see §4 |
| `gold.alarm_state`, `gold.alarm_events` (plan canvas) | views over `ops.alarm_state` / `ops.alarm_transition` | |
| `gold.instrument_latest` (app) | pipeline MV, read through the SQL warehouse OBO. **No synced table** (§7.3) | |
| `gold.instrument_daily`, `gold.data_freshness` | pipeline MVs | |
| `gold.threshold_exceedances` | view: **source of every SQL alert** | |
| `gold.hst_fit`, `gold.anomaly_scores` | interpretation job tables | 12 |
| `ops.alarm_state`, `ops.alarm_transition`, `ops.reading_erroneous_mark`, `ops.rule_finding` | job tables | 07 |
| `audit.event_log`, `audit.recompute_log`, `audit.alarm_recompute_diff` | insert-only | 08 |
| `rpt.engineering_commentary` | insert-only; CHECK `requires_engineer_review = true` | 09 |

## 3. Keys and conventions
- IDs are STRING. `dam_id` is on every table so the row filter and ABAC policy can key on it.
- All timestamps are UTC `TIMESTAMP`. Column names: `ts_utc` for readings, `*_ts` for events.
- Elevations are in metres above the dam's `ref.dam.datum`, in `*_el_m` columns. (App aliases with `*_elev_m` exist in `ref.site` only.)
- `reading_id = sha2(sensor_id || '|' || ts_utc || '|' || source_file, 256)`, assigned in silver.
- **Lineage on every reduced row:** `calib_id`, `cal_version`, `formula_id`, `formula_version`, `recompute_batch_id`, `reduced_ts`.
- **Append-only:** `silver.reading_reduced`, `ops.alarm_transition`, `ops.exceedance*`, `audit.*`, and `rpt.engineering_commentary`.
  - A recompute **appends** new rows. The current row comes from `silver.v_reading_current`; full history with `superseded_by` comes from `silver.v_reading_lineage`.
- `qa_flag` is one of `OK | SUSPECT | BAD | MISSING`. `qa_reason` holds **comma**-joined codes (`concat_ws(",")` in bronze/silver, `",".join` in `qc.py`; v1.0 text said `;`, which was wrong) (`RANGE_LOW`, `RANGE_HIGH`, `SPIKE`, `STUCK`, `DRY`, `CHECKSUM`, `AFTER_GAP`, `NULL_VALUE`).
- `sensor_id` is a channel of `instrument_id`. For single-channel instruments the seed sets `sensor_id = instrument_id`, as the app seed already does.

## 4. Alarm contract
- Machine states: `NORMAL`, `ALERT_UNACK`, `ALERT_ACK`, `ACTION_UNACK`, `ACTION_ACK`, `CLEARED_UNACK`. Levels come from `ref.threshold.level`: 1 = ALERT, 2 = ACTION.
- Transition events: `RAISE`, `ESCALATE`, `DEESCALATE`, `CLEAR`, `RTN_UNACK`, `ACK`.
- `ops.alarm_event.event_id` is the `transition_id` of the episode's RAISE. That is the key the app uses for `dam_app.alarm_ack`.
- App ack/close lifecycle (`open → acknowledged → closed` in Lakebase `alarm_ack`): the extract job turns an `acknowledged` row into an `ACK` transition in the machine and logs it to `audit.event_log`.
  - `closed` is app-only workflow state. It has no effect on the machine.
- `ref.threshold` canonical columns (FERC canvas): `tier`, `level`, `limit_type (magnitude|rate|cyclic)`, `direction (upper|lower|abs)`, `value`, `unit`, `deadband`, `persistence_on`, `persistence_off`, `pfm_id`, `eap_level_id`, `required_actions`.
  - **App change requested:** the app seed uses `upper_limit`/`lower_limit`/`rate_limit_per_day`/`rule`/`pfm_ids`/`response_action`. Please read `direction`+`value`+`limit_type`, `pfm_id`, and `required_actions` instead.
  - Rate limits are per day.

## 5. Columns the app reads (agreed subset)
| Object | Columns |
|---|---|
| `ref.site` | dam_id, name, dam_type, hazard_class, owner, nid_id, state, lat, lon, height_m, crest_elev_m, normal_pool_m, datum, status |
| `ref.instrument` | instrument_id, dam_id, structure_id, asset_id, instrument_type, subtype, station_m, offset_m, lat, lon, tip_el_m, top_el_m, install_date, status, replaces_instrument_id |
| `ref.calibration` | calib_id, instrument_id, sensor_id, cal_version, formula_id, formula_version, params (MAP<STRING,STRING>), effective_from, effective_to, is_current, is_retired, certificate_uri, approved_by, approved_at |
| `silver.v_reading_current` | reading_id, dam_id, instrument_id, sensor_id, ts_utc, raw_value, raw_unit, eng_value, eng_unit, corrected_value, corrected_unit, head_m, qa_flag, qa_reason, calib_id, cal_version, formula_id, formula_version, recompute_batch_id |
| `silver.incl_reduced` | instrument_id, survey_id, survey_ts, depth_m, diff_a, diff_b, checksum_a, checksum_b, inc_a, inc_b, cum_a, cum_b, resultant, fixed_end, qa_flag, *(v1.1)* depth_reference, baseline_survey_id, correction_record_ids |
| `ref.incl_installation` *(v1.1)* | instrument_id, probe_L_m, probe_K, casing_depth_m, depth_reference, sign_mult_A, sign_mult_B, baseline_survey_id, *(v1.2)* spiral_survey_id, stable_zone_top_m, effective_from, effective_to |
| `ops.alarm_episode` / `ops.alarm_event` | event_id, dam_id, instrument_id, sensor_id, event_ts, cleared_ts, machine_state, threshold_id, threshold_version, value, *(v1.1 lineage)* reading_id, cal_version, calib_id, formula_version, raise_reading_id, raise_cal_version, mixed_calibration |
| `gold.threshold_exceedances` | see `sql/ddl/11_gold_views.sql` |

### 5.4 Instrument-type vocabulary (v1.1, app Q8)
`ref.instrument_type` (seed `sql/seed/ref_instrument_type_seed.sql`) is canonical: lower snake_case codes
(`piezometer`, `reservoir_level`, `seepage_weir`, `inclinometer`, `crackmeter`, `extensometer`, `settlement`,
`strain_gauge`, `pressure_cell`, `load_cell`, `tiltmeter`, `plumbline`, `accelerometer`, `thermometer`) with units,
sign convention and `engine_formulas`. An empty `engine_formulas` means the data layer has no engine formula and the
app must not compute that type (strain_gauge today). PFM fields the app shows: `ref.pfm` `pfm_id, dam_id, pfm_code,
description, loading_condition, ch17_disposition, legacy_ch14_category, mechanism, status, is_general_health,
effective_from, effective_to`; bridge `ref.pfm_instrument`.

### 5.5 Inclinometers (v1.1, steering 9; app Q9)
- Storage rule: SI units, depth in metres **from top of casing, keyed to the interval top**; raw readings kept
  alongside (bronze `raw.incl_survey_raw` is append-only and never edited). Workbook "depth from bottom" labels are
  re-keyed with `top = casing − d − L` (`inclinometer.installation.rekey_from_bottom_label`) — **workbook import profile
  only** (`survey_import.import_reading(profile="workbook")`), stored with `depth_reference='from_bottom_label'` and the
  raw label in `raw_depth_label`. GTILT and DigiPro profiles keep native from-top depths (`depth_reference='top_of_casing'`).
- SAA measurand B is sign-flipped **once at ingest** (`sign_mult_B = -1` in `ref.incl_installation`, row flagged
  `sign_normalized`).
- Corrections are **never** manual cell edits: each is a `ref.incl_correction` row (columns, in order:
  `record_id, dam_id, instrument_id, survey_id, axis, correction_type, parameter_name, auto_value, detection_method,
  detection_statistic, override_value, applied, sign_convention, depth_range, reason, source_citation, user,
  timestamp_utc, before_value, after_value, software_version, input_file_sha256, superseded_by, reviewed_by, reviewed_utc` — the REPORT §6.3 audit fields
  plus review; exact list asserted by `tests/test_inclinometer_module.py`). Apply order: depth → bias → sensitivity → rotation.
  - **Rotation** (v1.2): axis A only (B UNVERIFIED); `sign_convention` required on every rotation record, one of
    `MIKKELSEN2003_EQ9` (A = A′ + sinα·B′), `GTILT_PLUS_SIN` (A = A′ − sin r·B), `A_corr=A-f*B`. Off by default: applied
    only with an independent review (`reviewed_by` ≠ `user`, `reviewed_utc`).
  - **Spiral** (v1.2): deferred. `spiral` stays a valid `correction_type`, but `applied=true` is rejected. Every survey on an
    installation with `ref.incl_installation.spiral_survey_id` set (or a spiral record) gets info code `SPIRAL_NOT_APPLIED`
    appended to `qa_reason`; `qa_flag` severity is unchanged. A checksum shift alone never creates a correction (it makes the survey SUSPECT).
  - **QA-020 between-set bias QC** (v1.2, procedure §7.1; `dam_safety.inclinometer.qc.between_set_bias_qc`, beside the
    checksum flag). Per survey × axis, against a reference set (default `baseline_survey_id`, user-overridable) over the
    stable zone (`ref.incl_installation.stable_zone_top_m`, required; NULL → bottom 5 intervals and
    `stable_zone_assumed=true`; never auto-detected). Output `BiasQC`: `suspect_flag` (|Δ mean checksum| > 10 A / 20 B;
    investigate only), `za_proposed_c` (**QA-021**, procedure §5 lead ruling 2026-11-02: every Δ = suspect − reference;
    ΔD = mean(ΔA0 − ΔA180) over the stable zone; ZA/ZB = −ΔD, so D′ = D + ZA), evidence `n`, `mean_dD` (= ΔD, sign
    flipped vs QA-020 v1), `sd` (sample SD of per-depth ΔD), `se` (SD/√n), `spread` (max − min, evidence only),
    `mean_cs_change` (bs = mean(ΔA0 + ΔA180)), `crosscheck_diff` (||ΔD| − |bs||), `crosscheck_tol`
    (max(2.0, 3·SE), UNVERIFIED), `sign_agree`, `reference_survey_id`, plus `stable_zone_assumed`, `params_unverified`
    (always true for the defaults), `rule` (`QA021-v1`), `status` (`NO_PROPOSAL | PROPOSED | SUSPECT_CROSSCHECK |
    BAD_RESURVEY`), `gates`, `qa_flag`. Gates: (i) n ≥ 5; (ii) |ΔD| > 3·SE; (iii) SD ≤ 8 A / 16 B — fail → `NO_PROPOSAL`;
    (iv) cross-check ||ΔD| − |bs|| ≤ max(2.0, 3·SE); (v) sign(ΔD) = sign(bs) (QA addition, not in the procedure) —
    fail → `SUSPECT_CROSSCHECK` (`qa_flag=SUSPECT`, no ZA); (vi) |ZA| ≤ 25 else `BAD_RESURVEY` (`qa_flag=BAD`).
    The QA-020 v1 `equal_opposite` 0.5 ratio is removed (superseded by the §5 cross-check). The engine **proposes
    only**: `qc.bias_proposal_record` writes an `applied=false`
    `ref.incl_correction` row (`detection_method='between_set_bias_qc'`, evidence in `detection_statistic`); applying
    is a separate reviewed row. All thresholds are configurable `BiasQCParams` (UNVERIFIED engineering judgment).
- **Q9 answer:** `silver.incl_reduced.diff_a/diff_b` are un-halved A0−A180. v1.1 adds nullable
  `depth_reference` (`top_of_casing` | `from_bottom_label`), `baseline_survey_id`, `correction_record_ids ARRAY<STRING>`; when `correction_record_ids` is
  non-empty, `diff_*`/`inc_*`/`cum_*` are post-correction values (only applied, non-superseded, and for rotation
  reviewed records, in apply order). v1.2 adds nullable `raw_depth_label` (label as received).
  - **v1.3 writer (2026-11-02):** job `src/jobs/reduce_inclinometers.py` (task `reduce_inclinometers` in
    `dam_ingest`, after `pipeline`) with pure-Python core `dam_safety.inclinometer.batch.reduce_instrument`. It
    appends rows (new `recompute_batch_id` per run; current row per (`survey_id`,`depth_m`) = latest `reduced_ts`).
    Target surveys: never reduced, or a correction record newer than the last reduce, or a newer installation row,
    or `--full_recompute true`. `formula_id='INCL_PROBE_2PASS'`, `formula_version='1.0.0+QA021-v1'` (bias-rule
    version; hook `batch.DEFAULT_BIAS_RULE`; a survey whose latest row has another `formula_version` is re-reduced). `fixed_end='bottom'`, `calib_id` NULL.
  - **qa_flag / qa_reason (comma-joined)** per row = worst of the reading flag (`BAD:TEXT`, `BAD:RANGE`,
    `MISSING:PASS`), survey checksum QC (`MEAN_CS_GT_100_A`, `INDIVIDUAL_CS_A`, …), QA-020 (`BIAS_CS_SHIFT_A`,
    `BIAS_PROPOSED_A` → SUSPECT, `BIAS_CROSSCHECK_A` → SUSPECT, `BIAS_BAD_RESURVEY_A` → BAD, `STABLE_ZONE_ASSUMED`), `CORRECTION_RECORD_INVALID`
    (stored record failed validation, skipped → SUSPECT), `CORRECTION_ERROR:<type>` (BAD, no values) and info
    `SPIRAL_NOT_APPLIED`. A proposal is never applied; when an applied bias record exists for that survey/axis the
    proposal code is suppressed.
  - Not deployed; the Spark wrapper is UNTESTED (no pyspark/Java on the build box). Until rows exist in a
    workspace the app must still treat incl values as uncorrected and say so.
- The user's own workbook and vendor-manual values are **internal-test-only**: tests live in gitignored,
  sync-excluded `testonly_restricted/` and read `/workspace/dam-refs/...` in place.

**App changes requested** (the app seed differs):
- `ref.instrument.tip_elev_m`/`top_elev_m` become `tip_el_m`/`top_el_m`.
- The calibration `certificate` column becomes `certificate_uri`.
- Inclinometer columns `a_cum_mm` become `cum_a` (mm), and the A/B fields follow the same pattern.
- The calibration params naming differs: data layer `top_el_m`/`tip_el_m`/`sensor_el_m` vs app `top_elev_m`/`tip_elev_m`. The data-layer registry accepts both spellings as aliases (see `formulas/registry.py`).

## 6. Security
- The row filter is `ref.dam_row_filter(dam_id)` and is group-based: `dam-portfolio-all` or `dam-<dam_id>-members`. It is applied to every DDL-owned table that has `dam_id`. Pipeline tables get it through the LDP `row_filter` clause.
- v1.2 (Databricks SME 2026-11-02): **mode switch** `var.security_mode` — `table` (default: `ALTER ... SET ROW FILTER` on
  DDL-owned tables in `sql/ddl/10a_row_filters_table.sql` + LDP `row_filter=` on pipeline tables) or `abac` (one
  catalog policy on governed tag `dam_scope`, `10a_row_filters_abac.sql`, which DROPs the table filters; pipelines set
  `row_filter=None`). The two cannot be combined (`UC_ABAC_MULTIPLE_ROW_FILTERS`). ABAC details still UNVERIFIED (ABAC-1).
- Pipeline row filters are managed **only** in pipeline code, never by `ALTER TABLE` on a pipeline table. The pipeline
  and the SQL alerts run as the runtime service principal (`var.runtime_sp_app_id`, member of `dam-portfolio-all`, and
  EXCEPTed in abac mode). Gold materialized views are refreshed as that principal, so they reflect its (all-dam)
  visibility; readers are still filtered as themselves.
- v1.1: row filters also on `ref.incl_installation` and `ref.incl_correction` (now in `10a_row_filters_table.sql`);
  pipeline tables get `row_filter=` in `@dp.table` (`tests/test_pipeline_row_filters.py`).
- The app also does its own dam-role checks (`dam_app.dam_role`). These are a UX layer, not a substitute for UC filters.
  - The SQL warehouse must be queried **on behalf of the user** (OBO) so the UC filters apply.

## 7. Lakebase (`dam_app`) — ownership
- App-owned and created by `state.py`: `app_user`, `dam_role`, `alarm_ack`, `alarm_comment`, `onboarding_draft`, `onboarding_publication`, `report_draft`, `pfm_review`, `audit_log`.
- Data-layer additions, proposed: `inspection_note`, `user_preference`, `exceedance_status`, `review_signoff`, `manual_reading` (append-only triggers), and `extract_watermark`.
- The extract job copies Lakebase rows to Delta:
  - `alarm_ack` → ACK transitions plus `audit.event_log`
  - `audit_log` → `audit.event_log`
  - `exceedance_status` → `ops.exceedance_status_log`
  - `review_signoff` → `audit.review_record`
  - `inspection_note` → `ops.surveillance_inspection`
  - `manual_reading` → CSV into `/Volumes/<cat>/raw/landing/<dam>/readings/manual_<stamp>.csv` (v1.0 said `manual/`, which bronze does not read), so ADAS and manual readings share one ingest path
  - `report_draft` → `rpt.dssmr_draft` (append-only snapshot per content sha256) + `rpt.dssmr` upsert while still `draft`

### 7.2 Ownership of writes (v1.1, app Q11)
The app writes Lakebase only. `dam_lakebase_extract` is the **only** writer of `ALARM_ACK` rows to `audit.event_log`;
the alarm evaluator is the **only** writer of `ACK` transitions (dedup tag `lakebase_ack:<event_id>`). `certified`/`submitted`
DSSMR status is set only by the PE sign-off path (`audit.review_record`), never by the app.

### 7.3 Synced tables (v1.1, app Q12)
**Not used.** A Lakebase synced copy of `gold.instrument_latest` read by the app service principal would bypass UC row
filters. Latest values are read through the SQL warehouse on behalf of the user. The block in `resources/app.yml` stays
commented out (`tests/test_bundle_config.py::test_app_yml_has_no_synced_table_wired`).
- Lakebase is never the system of record for a regulatory record.

## 8. Run order (job `dam_uc_setup`)
1. `sql/ddl/00`–`10` with seeds `ref_instrument_type_seed.sql` (after 01) and `ref_formula_seed.sql` (after 03), and `13`
2. First pipeline update (creates raw/silver streaming tables and gold MVs)
3. `sql/ddl/11`–`12` (views that reference pipeline tables)
(Exact task graph: `resources/uc_setup.yml`; order asserted by `tests/test_bundle_config.py`.)

## 9. Sign-off
| Party | Agent | Status |
|---|---|---|
| Data layer | (this bundle) | proposed v1.1 |
| App Developer | dd07fe27-3193-4938-9d18-5e3802ce9280 | v1.0 agreed with open items; **v1.1 pending** |
| QA Engineer (QA-003) | 798e2d02-0b70-4a0d-8dea-77eea4adeed6 | **pending** |

## 10. Answers to the App Developer's open questions (app/SCHEMA_CONTRACT.md §6)
| # | Question | v1.1 answer |
|---|---|---|
| 1 | `dam_safety` vs per-env catalogs | **Per-env** `dam_dev/test/prod` (KB). `DAM_CATALOG=${var.catalog}`; QA-003 maps canvas names to `<env catalog>`. |
| 2 | App in this bundle? | **Yes**: `resources/app.yml`; `app/**` removed from `sync.exclude` except tests/caches (`test_app_is_part_of_bundle_and_synced`). |
| 3 | `sign_mult`/`offset` | **Columns** on `ref.calibration` (`sign_mult INT ±1`, `` `offset` DOUBLE `` in corrected eng units, applied after sign_mult). Never in `params`. `method` ∈ `linear|poly`; `required_params` per formula in `ref.formula`. |
| 4 | VW_PIEZO version bump | Data layer owns `ref.formula` (seed + fingerprint guard `tools/formula_seed.py`): any change to a formula body bumps `formula_version`; old versions stay; recompute appends rows; the app reads `formula_version` per row. |
| 5 | SETTLEMENT_DELTA rebaseline | A **new baseline row per `datum_id`** (`dam_safety.datum.rebaseline_series`), not a new calibration version. |
| 6 | Alarm lineage | **Yes**: `ops.alarm_episode` carries `reading_id, cal_version, calib_id, formula_version, raise_reading_id, raise_cal_version, mixed_calibration` (§5). |
| 7 | `abs` / `cyclic` | `abs` is evaluated (|value| vs limit). `cyclic` is **not evaluated** by the machine (skipped, documented); show as "not evaluated". |
| 8 | Type vocabulary / PFM fields | §5.4. No strain-gauge engine formula; SG oracle cases stay "not displayed". |
| 9 | Inclinometer bias correction | §5.5. |
| 10 | Engine formulas for WEIR_VNOTCH / VW_DISP_LIN / LEVEL_DATUM | Now registered. WEIR_VNOTCH and VW_DISP_LIN have source-cited test vectors; **LEVEL_DATUM is `verified=False` (UNVERIFIED rule, no instrument source)** and must stay hidden. |
| 11 | DSSMR write path, ack ownership | §7 and §7.2. |
| 12 | Synced table | §7.3: no. |
