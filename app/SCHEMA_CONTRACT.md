# App-side schema contract: dam safety app

This file maps the app to the data layer's contract, **root `../SCHEMA_CONTRACT.md` v1.0 (PROPOSED)**.
The root file is authoritative for UC objects. This file records what the app reads and writes, how it
maps columns, and what is still open. The adapter code is `backend/repos/contract.py`; the SQL is in
`backend/repos/analytics.py`. A rename in the data layer should need a change only in `contract.py`.

App Developer sign-off on root v1.0: **agreed, with the open items in section 6**. Nothing in this
repo has been run against a real workspace.

## 1. Catalog and schemas (QA-003)
- The QA steer asked for catalog `dam_safety` with `ref/ops/audit/rpt` (the FERC canvas model). The
  Data Architect's bundle (`../databricks.yml`, root contract section 1) instead uses **one catalog per
  target**: `dam_dev`, `dam_test` and `dam_prod`. Each has `ref, raw, silver, gold, ops, audit, rpt` (+ `ml`).
  The canvas name `dam_safety.<schema>.<t>` maps to `<env catalog>.<schema>.<t>`.
- The app follows the bundle. The catalog comes from `DAM_CATALOG`, which the bundle sets to
  `${var.catalog}`. Each schema can be overridden (`DAM_SCHEMA_REF|RAW|SILVER|GOLD|OPS|AUDIT|RPT`).
  If the team chooses a single `dam_safety` catalog, set `DAM_CATALOG=dam_safety`; no code changes.
- **Decision needed from DA and QA:** confirm per-env catalogs over `dam_safety`. QA-003 asks for
  `dam_safety`, and the KB recommends per-env catalogs.

## 2. UC objects the app reads (SQL warehouse, on behalf of the user)
| Object | Use | Adapter |
|---|---|---|
| `ref.site` (view over `ref.dam`) | portfolio | as-is |
| `ref.structure` | drill-down | as-is |
| `ref.instrument` ⨝ `ref.calibration` ⨝ `ref.formula` | registry and latest values | `contract.instrument` (`tip_el_m`/`top_el_m` → `*_elev_m`) |
| `ref.calibration` | calibration versions, params | `contract.calibration` (el aliases, `certificate_uri`) |
| `ref.formula` | verification status per formula version | `contract.formula` |
| `ref.threshold` (`is_current`) | alarm limits | `contract.threshold` (`direction`+`value`+`limit_type` → app limits; `required_actions`) |
| `ref.pfm`, `ref.pfm_instrument` (`valid_to IS NULL`) | DSSMP/PFM | `contract.pfm` |
| **`ops.reading`** (FERC canvas view) | time series | `contract.reading`: `reading_ts→ts_utc`, `value/unit→corrected_*`, `qc_flag/qc_reason→qa_*`; `is_erroneous` forces `qa_flag=BAD` + `ERRONEOUS_MARK` |
| `ops.alarm_episode` ⨝ `ref.threshold` | alarms | `contract.alarm_event` (`event_id` = ack key) |
| `gold.instrument_latest` | latest value per sensor | as-is |
| `silver.incl_survey_header`, `silver.incl_reduced` ⨝ `ref.calibration` | inclinometer profiles | `contract.incl_row` (`cum_a→a_cum_mm` …) |

Nothing is written to UC by the app. Writes go to Lakebase, and the data layer's extract job moves
them to `ops`/`audit` (root section 7).

## 3. Display gate (accuracy rule)
A computed value is shown only when all three hold: (a) its `ref.formula` version is `verified`,
(b) its calibration is approved and current, and (c) its QC flag is not `BAD`/`MISSING`.
Otherwise the UI shows the value as *unverified/hidden* and gives the reason. Every value and plot
shows its unit, `cal_version` and QC flag. Alarm-episode values are withheld until the lineage
question in section 6 is answered.

## 4. Lakebase (Postgres schema `dam_app`, service principal)
Created idempotently by `backend/repos/state.py`, mirrored in `../lakebase/001_app_schema.sql`:
`app_user, dam_role, alarm_ack, alarm_comment, onboarding_draft, onboarding_publication,
report_draft, pfm_review, audit_log`. `audit_log` is append-only from the app.

## 5. Reductions vs the QA oracle (`../qa/reference_examples.py`, read-only)
`tests/test_qa_oracle.py` runs every oracle case through the app's own functions:
- **Covered:** `VW_PIEZO` (PZ-*), `VW_DISP_LIN` (CK-*), `INCL_PROBE_2PASS` (IN-*), plus load-cell gauges (LC-*)
  and thermistor conversion. Each case must match the oracle to 1e-9 *and* the published value within the
  published tolerance. PZ-04, PZ-07 and IN-01 are checked against the published tolerance only, because
  the oracle starts from different (rounded) inputs.
- **Not displayed** (asserted absent): CK-04 thermal K (a calibration parameter) and SG-01..03 (the app
  has no strain-gauge type).
- **Restricted inclinometer cases** are read by path at test time from `$DAM_QA_PRIVATE` (default
  `/workspace/dam-refs/qa_private`) and never copied into the repo. The test is skipped if the path is absent.
- **No oracle yet:** `STANDPIPE_DTW`, `WEIR_VNOTCH`, `LEVEL_DATUM`, `SETTLEMENT_DELTA`. Their values
  are shown only while `ref.formula` marks them verified. `SETTLEMENT` stays unverified.

## 6. Open questions for the Data Architect
1. `dam_safety` vs per-env catalogs (section 1).
2. `databricks.yml` `sync.exclude` contains `app/**`, so a bundle app with `source_code_path` in `app/`
   would deploy with no source. Should the app live in this bundle? A proposal is in `resources/dam_safety_app.yml`.
3. `ref.calibration.params`: are `sign_mult` and `offset` (and the offset's unit) carried as params or as
   columns? Which `method`/`required_params` keys are canonical?
4. A `VW_PIEZO` formula-version bump when the temperature/baro terms change. Who bumps it, and how does
   the app learn of it?
5. `SETTLEMENT_DELTA` rebaseline semantics (new baseline row vs new calibration version).
6. Alarm lineage: can `ops.alarm_episode.value` be tied to the `reading_id`/`cal_version` it came from?
   Until it can, the app withholds episode values.
7. Thresholds: how should `direction=abs` and `limit_type=cyclic` be presented? The app currently shows them without evaluating them.
8. Instrument-type vocabulary (strain gauges? the oracle has SG cases) and the PFM fields the app shows.
9. Does `silver.incl_reduced` carry the bias correction? Which column?
10. The data-layer registry has no engine formulas for `WEIR_VNOTCH`, `VW_DISP_LIN` or `LEVEL_DATUM`.
    The app's implementations are unverified until it does.
11. The `rpt` write path for DSSMR drafts (app `report_draft` → `rpt.dssmr`?), the outbox/extract
    publisher, and who owns ack → `ACK` transition.
12. A synced table for `gold.instrument_latest` and threshold status. Is it wanted, given that it
    bypasses UC row filters when read by the service principal?
