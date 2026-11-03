# App-side schema contract: dam safety app

This file maps the app to the data layer's contract, **root `../SCHEMA_CONTRACT.md` v1.1 (PROPOSED)**, including the
v1.2/v1.3 additions to §5.5 (QA-020 bias QC, the `silver.incl_reduced` writer). The root file is authoritative for UC
objects. The adapter code is `backend/repos/contract.py`; the SQL is in `backend/repos/analytics.py`. A rename in the
data layer should need a change only in `contract.py`. Nothing in this repo has been run against a real workspace.

**App Developer sign-off on root v1.1: AGREED, conditional on the mismatches in §7** (none blocks the app; each is
handled conservatively, i.e. by withholding a value or joining a column). The §9 table lives in the Data Architect's root
file, which the app does not edit, so the DA should record this sign-off there.

## 1. Catalog and schemas (QA-003)
One catalog per target (`dam_dev|dam_test|dam_prod`) with `ref, raw, silver, gold, ops, audit, rpt` (+ `ml`). The app
reads `DAM_CATALOG` (bundle: `${var.catalog}`); each schema can be overridden (`DAM_SCHEMA_REF|RAW|...`).

## 2. UC objects the app reads (SQL warehouse, on behalf of the user; scope `sql` only)
| Object | Use | Adapter |
|---|---|---|
| `ref.site`, `ref.structure` | portfolio, drill-down | as-is |
| `ref.instrument` ⨝ `ref.calibration` ⨝ `ref.formula` | registry, latest values | `contract.instrument` (`tip_el_m`/`top_el_m`) |
| `ref.calibration` | versions | `contract.calibration`: `sign_mult`, `offset` are **columns** (§10 Q3); if found in `params` the calibration gets a `calibration_issues` entry and is not approved; `method` ∈ `linear|poly`; `certificate_uri`; `is_retired` never counts as approved |
| `ref.formula` | verified flag per version | `contract.formula` |
| `ref.instrument_type` (§5.4) | 14 types; `strain_gauge` has no engine formula | type → `NO_ENGINE_FORMULA` (never computed); onboarding `TYPE_UNITS` mirrors §5.4 (test-enforced) |
| `ref.threshold` (`is_current`) | limits | `contract.threshold`: `limit_type` (magnitude\|rate\|cyclic), `direction` (upper\|lower\|abs), `value`, `pfm_id`, `required_actions`. `abs` = \|x\| ≥ value; **cyclic is labelled "not evaluated"** (§10 Q7) |
| `ref.pfm`, `ref.pfm_instrument` | DSSMP/PFM (§5.4 PFM fields) | `contract.pfm` (pfm_code, Ch.17 / legacy Ch.14 category, loading condition, mechanism, effective dates) |
| `ops.reading` | time series | `contract.reading`; `qa_reason` is **comma**-joined; `is_erroneous` → BAD |
| `ops.alarm_episode` ⨝ `ref.threshold` ⨝ `ref.calibration` ⨝ `ops.reading` | alarms with lineage | `contract.alarm_event` (reading_id, cal_version, calib_id, formula_version, raise_reading_id, raise_cal_version, mixed_calibration) |
| `gold.instrument_latest` | latest value | as-is (warehouse, OBO) |
| `silver.incl_survey_header`, `silver.incl_reduced` | inclinometer profiles | `contract.incl_row`: API names `cum_a, cum_b, inc_a, inc_b, resultant, checksum_a/b` |
| `ref.incl_correction` | the 23 audit fields | `contract.incl_correction` (`audit_complete`, `missing_audit_fields`) |

`LEVEL_DATUM` stays hidden (no verified engine formula). The app writes **Lakebase only** (§7.2): acks →
`dam_app.alarm_ack`, which `dam_lakebase_extract` turns into `ALARM_ACK` rows in `audit.event_log` (and the evaluator
into `ACK` transitions); DSSMR drafts → `dam_app.report_draft` → `rpt.dssmr_draft`. Manual readings are not entered in
the app; the data layer's `manual_reading` extract writes `<dam>/readings/manual_<stamp>.csv`. **No synced tables**
(§7.3): the commented block was removed from `resources/app.yml`.

## 3. Display gate (accuracy rule)
A computed value is shown only when its formula version is verified, its calibration approved/current/not retired and
without issues, and its QC flag is not BAD/MISSING. Statuses: `VERIFIED`, `UNVERIFIED_FORMULA`, `UNAPPROVED_CALIBRATION`,
`QC_BAD`, `MISSING`, `CORRECTION_UNVERIFIED`, `LINEAGE_INCOMPLETE`, `LINEAGE_INCONSISTENT`, `NO_ENGINE_FORMULA`.
- **Alarms (v1.1 lineage):** episodes always stay visible. Their value is shown once lineage is present and consistent
  (calibration `formula_version` = episode `formula_version`); otherwise `LINEAGE_INCOMPLETE`/`LINEAGE_INCONSISTENT`.
- **Inclinometers (§5.5):** values are **uncorrected**. A row with `correction_record_ids` is withheld
  (`CORRECTION_UNVERIFIED`) and its correction records are listed with their audit fields.
- **QA-020 bias QC (§5.5 v1.2/QA-021, engine `qc.between_set_bias_qc`):** per survey and axis the API returns `bias_qc` with
  `suspect_flag` (`BIAS_CS_SHIFT_A/B`), `status` (`PROPOSED`/`SUSPECT_CROSSCHECK`/`BAD_RESURVEY` from
  `BIAS_PROPOSED_*`/`BIAS_CROSSCHECK_*`/`BIAS_BAD_RESURVEY_*`; QA-021 rule),
  and `proposed_c` (reading units) only from an engine proposal row in `ref.incl_correction`
  (`detection_method='between_set_bias_qc'`, `applied=false`, not superseded). It is always `display_status=UNVERIFIED`,
  `applied=false`, and never changes a value. The app's own internal bias computation is never exposed
  (`proposed_bias` is dropped from every row; owner decision 2026-11-02).
- **qa_reason codes (v1.3):** `contract.QA_REASON_CODES` labels BAD:TEXT, BAD:RANGE, MISSING:PASS, MEAN_CS_GT_100_A/B,
  INDIVIDUAL_CS_A/B, BIAS_CS_SHIFT_A/B, BIAS_PROPOSED_A/B, BIAS_CROSSCHECK_A/B, BIAS_BAD_RESURVEY_A/B, STABLE_ZONE_ASSUMED,
  CORRECTION_RECORD_INVALID, CORRECTION_ERROR:&lt;type&gt;, SPIRAL_NOT_APPLIED (+ legacy CHECKSUM). Unknown codes are
  shown verbatim. The row's `qa_flag` stays the engine's; the app does not re-derive it.

## 4. Lakebase (`dam_app`, service principal)
Created by `backend/repos/state.py` (mirrored in `../lakebase/001_app_schema.sql`): `app_user, dam_role, alarm_ack,
alarm_comment, onboarding_draft, onboarding_publication, report_draft, pfm_review, audit_log`. Connection: injected
`PG*` env + `ENDPOINT_NAME`, `generate_database_credential` per new pooled connection, TCP keepalives and retry for HA
failover. Identity is keyed on `X-Forwarded-User`; the user token is never logged and never used for Lakebase.

## 5. Reductions vs the QA oracle (`../qa/reference_examples.py`, read-only)
`tests/test_qa_oracle.py` runs every oracle case through the app's functions (VW_PIEZO, VW_DISP_LIN, INCL_PROBE_2PASS,
load cells, thermistor). SG-01..03 are asserted not displayed (`strain_gauge` has no engine formula). Restricted cases are
read by path from `$DAM_QA_PRIVATE` and never copied.

## 6. Questions from v1.0
All 12 are answered by root §10 and implemented above.

## 7. Mismatches / follow-ups for the Data Architect (proposals; the app does not edit DA files)
1. `ops.alarm_episode` has no `formula_id` or `qa_flag`; the app joins `ref.calibration` (formula_id, only when versions
   match) and `ops.reading` (qc flag). Adding them to the episode would remove the joins.
2. Episode `value` via `max_by` can pick a NULL reading; please confirm the evaluator never writes a NULL value.
3. Root §7.3 says the synced block "stays commented out"; per steering it is now **removed** (the test
   `test_app_yml_has_no_synced_table_wired` still passes). Please update the §7.3 text.
4. Root header still says v1.1 while §5 carries v1.2/v1.3 content; a version bump + changelog line would help sign-off.
5. `docs/api_contract.md` §4 should list the new response fields (`rule_label`, `evaluated`, lineage fields,
   `lineage_issue`, `bias_qc`, `qa_reasons`, `calibration_issues`).
6. **v1.3 writer rows will be withheld by the app:** `reduce_inclinometers` writes `calib_id = NULL` and
   `formula_version = '1.0.0+QA021-v1'` (bias-rule version). The gate requires a verified `(formula_id, formula_version)` in `ref.formula` and an
   approved calibration, so those rows show as `UNVERIFIED_FORMULA`/`UNAPPROVED_CALIBRATION` until (a) `ref.formula` has a
   verified `INCL_PROBE_2PASS 1.0.0+QA021-v1` row and (b) the contract says what approves an inclinometer row without a
   calib_id (e.g. an approved `ref.incl_installation` row). The app was deliberately not loosened.
7. `stable_zone_top_m` (ref.incl_installation, v1.2) is not read by the app (it does not read `ref.incl_installation`);
   the UI shows `STABLE_ZONE_ASSUMED` and the proposal's recorded `stable_zone_top_m` evidence.
8. Demo inclinometer alarm `reading_id` is synthetic (demo only).
9. Grants: `dam-portfolio-all` has `CAN_USE` on the app. Recommendation (not applied): grant `CAN_USE` to the
   per-dam `dam-<id>-members` groups instead; UC row filters plus the in-app ACL still restrict data.
