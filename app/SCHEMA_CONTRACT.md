# App-side schema contract: dam safety app

This file maps the app to the data layer's contract, **root `../SCHEMA_CONTRACT.md` v1.4 (PROPOSED, 2026-11-02)**, and
`../docs/api_contract.md` §4.1. The root file is authoritative for UC objects. The adapter code is
`backend/repos/contract.py`; the SQL is in `backend/repos/analytics.py`. A rename in the data layer should need a change
only in `contract.py`. Nothing in this repo has been run against a real workspace.

**App Developer sign-off on root v1.4: AGREED, conditional on the mismatches in §7** (none blocks the app; each is
handled conservatively, by withholding a value or labelling it). The §9 table lives in the Data Architect's root file,
which the app does not edit, so the DA should record this sign-off there.

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
| `ops.alarm_episode` ⨝ `ref.threshold` (⟕ `ops.reading` for `is_erroneous` only) | alarms with lineage | `contract.alarm_event`: `formula_id`, `formula_version`, `qa_flag`, `value`, reading_id, cal_version, calib_id, raise_reading_id, raise_cal_version, mixed_calibration all from the **episode** (v1.4 §4; no `ref.calibration` join, no reading qc join). An engineer's `is_erroneous` mark → `qa_flag` BAD + `ERRONEOUS_MARK` |
| `gold.instrument_latest` | latest value | as-is (warehouse, OBO) |
| `silver.incl_survey_header`, `silver.incl_reduced` | inclinometer profiles | `contract.incl_row`: API names `cum_a, cum_b, inc_a, inc_b, resultant, checksum_a/b`; v1.4 lineage `qc_rule_version`, `installation_version`, `probe_constants` passed through and shown |
| `ref.incl_installation` (v1.4 read) | approval of inclinometer rows with `calib_id` NULL | `incl_installations(instrument_id)` → `Gate(installations=…)` |
| `ref.incl_correction` | the 23 audit fields; all `between_set_bias_qc` records (proposals list) | `contract.incl_correction` (`audit_complete`, `missing_audit_fields`) |

`LEVEL_DATUM` stays hidden (no verified engine formula). The app writes **Lakebase only** (§7.2): acks →
`dam_app.alarm_ack`, which `dam_lakebase_extract` turns into `ALARM_ACK` rows in `audit.event_log` (and the evaluator
into `ACK` transitions); DSSMR drafts → `dam_app.report_draft` → `rpt.dssmr_draft`. Manual readings are not entered in
the app; the data layer's `manual_reading` extract writes `<dam>/readings/manual_<stamp>.csv`. **No synced tables**
(§7.3): the commented block was removed from `resources/app.yml`.

## 3. Display gate (accuracy rule)
A computed value is shown only when its formula version is verified, its calibration approved/current/not retired and
without issues, and its QC flag is not BAD/MISSING. Statuses: `VERIFIED`, `UNVERIFIED_FORMULA`, `UNAPPROVED_CALIBRATION`,
`UNAPPROVED_INSTALLATION`, `QC_BAD`, `MISSING`, `CORRECTION_UNVERIFIED`, `LINEAGE_INCOMPLETE`, `LINEAGE_INCONSISTENT`, `NO_ENGINE_FORMULA`.
- **Alarms (v1.4 §4):** episodes always stay visible. `formula_id`/`qa_flag` come from the episode (no app-side joins).
  The value is shown only with complete lineage (calib_id, formula_id, formula_version), a verified formula, an approved
  calibration and `qa_flag` not BAD/MISSING. v1.4 guarantees a non-NULL `value` for non-ACK transitions; the app still
  withholds a NULL value defensively (`MISSING`). `LINEAGE_INCONSISTENT` remains in the gate but is no longer produced.
- **Inclinometer approval when `calib_id` is NULL (v1.4 §5.5):** a row is `VERIFIED` only when
  `(formula_id, formula_version) = (INCL_PROBE_2PASS, 1.0.0)` matches a `ref.formula` row with `verified=true` **and**
  `(instrument_id, installation_version)` matches a `ref.incl_installation` row's `effective_from` (timestamps compared
  in UTC; any SCD2 row for that instrument). Otherwise `UNVERIFIED_FORMULA` (e.g. the never-deployed
  `1.0.0+QA021-v1`) or `UNAPPROVED_INSTALLATION` (no/wrong/unparseable `installation_version`). The profile shows
  `formula_verified`, `installation_approved`, `qc_rule_version` (e.g. `QA021-v2`), `installation_version` and
  `probe_constants`; mixed values within a survey are listed, never collapsed. Rows that do carry a `calib_id` (demo)
  still use the calibration path.
- **Inclinometers (§5.5):** values are **uncorrected**. A row with `correction_record_ids` is withheld
  (`CORRECTION_UNVERIFIED`) and its correction records are listed with their audit fields.
- **Bias proposals (v1.4, api_contract §4.1):** each axis of `bias_qc` carries `proposals`: every engine record
  (`detection_method='between_set_bias_qc'`) with status `PROPOSED` or `SUSPECT_CROSSCHECK`, ordered **AGREE first**,
  then newest. Fields: `za` (`za_unit`), `abs_dD`, `abs_bs`, `crosscheck_verdict`, `crosscheck_diff`,
  `crosscheck_tol`, `status`, `rule`, `requires_review`, `approval_status`, `reviewed_by`, `reviewed_utc`, `evidence`,
  `display_status=UNVERIFIED`, `applied_by_app=false`, `integrity_error`, `user` (author). `approval_status` takes only
  the api_contract §4.1 values: `superseded` (superseded_by set) > `approved` (applied ∧ reviewed_by set ∧ reviewed_by ≠
  user ∧ reviewed_utc set) > `proposed` (applied=false). QA-036: an applied record without a valid review is not
  given a status. It stays listed (the anomaly is visible) with `approval_status=null`,
  `integrity_error=APPLIED_WITHOUT_VALID_REVIEW` and `display_status=DATA_INTEGRITY_ERROR`, with `za`, `abs_dD`,
  `abs_bs`, the cross-check numbers and `evidence` withheld; the DA's v1.5 CHECK `ck_incl_correction_applied_reviewed`
  makes such a row unstorable. **Nothing is applied by the app**; displayed profile values stay
  uncorrected regardless of approval_status. Unparseable numbers are `null`, never invented.
- **QA-020 bias QC (§5.5 v1.2/QA-021, engine `qc.between_set_bias_qc`):** per survey and axis the API returns `bias_qc` with
  `suspect_flag` (`BIAS_CS_SHIFT_A/B`), `status` (`PROPOSED`/`SUSPECT_CROSSCHECK`/`BAD_RESURVEY` from
  `BIAS_PROPOSED_*`/`BIAS_CROSSCHECK_*`/`BIAS_BAD_RESURVEY_*`; QA-021 rule),
  and `proposed_c` (reading units) only from an engine proposal row in `ref.incl_correction`
  (`detection_method='between_set_bias_qc'`, `applied=false`, not superseded). `proposed_c`, `proposal_record_id`
  and `evidence` are shown for `PROPOSED` and for `SUSPECT_CROSSCHECK` (a proposal ranked SUSPECT, `ranking`, QA-030; v1.4: magnitude-only cross-check, no sign gate);
  never for `BAD_RESURVEY` or with no record. It is always `display_status=UNVERIFIED`,
  `applied=false`, and never changes a value. The app's own internal bias computation is never exposed
  (`proposed_bias` is dropped from every row; owner decision 2026-11-02).
- **qa_reason codes (v1.3):** `contract.QA_REASON_CODES` labels BAD:TEXT, BAD:RANGE, MISSING:PASS, MEAN_CS_GT_100_A/B,
  INDIVIDUAL_CS_A/B, BIAS_CS_SHIFT_A/B, BIAS_PROPOSED_A/B, BIAS_CROSSCHECK_A/B, BIAS_BAD_RESURVEY_A/B, STABLE_ZONE_ASSUMED,
  CORRECTION_RECORD_INVALID, CORRECTION_ERROR:&lt;type&gt;, SPIRAL_NOT_APPLIED (+ legacy CHECKSUM). Unknown codes are
  shown verbatim. The row's `qa_flag` stays the engine's; the app does not re-derive it.

## 4. Lakebase (`dam_app`, service principal)
Created by `backend/repos/state.py` (mirrored in `../lakebase/001_app_schema.sql`): `app_user, dam_role, alarm_ack,
alarm_comment, onboarding_draft, onboarding_publication, report_draft, pfm_review, audit_log`. Connection: injected
`PG*` env + `ENDPOINT_NAME` (bundle v1.4: `${resources.postgres_endpoints.dam_lakebase_primary.id}`; the app requires the full `projects/<p>/branches/<b>/endpoints/<e>` name and fails fast otherwise), `generate_database_credential` per new pooled connection, TCP keepalives and retry for HA
failover. Identity is keyed on `X-Forwarded-User`; the user token is never logged and never used for Lakebase.

## 5. Reductions vs the QA oracle (`../qa/reference_examples.py`, read-only)
`tests/test_qa_oracle.py` runs every oracle case through the app's functions (VW_PIEZO, VW_DISP_LIN, INCL_PROBE_2PASS,
load cells, thermistor). SG-01..03 are asserted not displayed (`strain_gauge` has no engine formula). Restricted cases are
read by path from `$DAM_QA_PRIVATE` and never copied.

## 5a. Bias-proposal review sign-off (QA-039)
`POST /api/instruments/{iid}/incl/surveys/{sid}/bias-proposals/{record_id}/review` (engineer or higher on the dam), body
`{decision: approve|reject, rationale (required), reviewer_pe_license?}`. It only ever **inserts** one row into Lakebase
`review_signoff` (lakebase/001 table, column-compatible with UC `audit.review_record`): `review_id`, `dam_id`,
`subject_type='incl_correction'`, `subject_id=record_id`, `review_level='engineer'`, `decision`, `comment=rationale`,
`reviewer_id` = X-Forwarded-User (demo personas: `demo:<email>`), `reviewer_pe_license`, `signed_ts` (UTC),
`signature_hash` = sha256 of canonical JSON `{"signoff": those fields, "subject": every ref.incl_correction column of
the record as reviewed (23 audit fields + reviewed_by, reviewed_utc, supersedes, review_id)}`, values normalised by
`canonical_value` (UTC ISO-8601 timestamps with microseconds, NaN → null), sort_keys, `(',', ':')`, UTF-8. This is
byte-identical to the engine's v1.5 `corrections.review_signature`, checked by an app test against the engine module
when it is importable. It is a content hash, not a key-based signature. UPDATE/DELETE are refused by triggers (sqlite and Postgres;
app-owned names `t_noupd*_review_signoff` / `app_forbid_mutation`). Refused: reviewer = record `user` (403, compared
against both X-Forwarded-User and email), record not an open `proposed` PROPOSED/SUSPECT_CROSSCHECK proposal or with an
integrity error (409), unknown record (404). The app never updates ref.incl_correction or applies anything. The
profile returns `bias_signoffs` with `signature_verified` re-computed against the current record, so a record changed
after sign-off shows a mismatch. DA follow-ups: §7 item 8.

## 6. Questions from v1.0
All 12 are answered by root §10 and implemented above. v1.3 follow-ups 1, 2, 4, 5, 6, 7 (partly), 9 are resolved by v1.4;
3 (§7.3 text) is resolved.

## 7. Mismatches / follow-ups for the Data Architect (v1.4; proposals, the app does not edit DA files)
1. **Resolved (QA-036):** the app no longer returns `unreviewed_applied`; an applied-without-review record is a
   data-integrity error (above), matching the v1.5 CHECK `ck_incl_correction_applied_reviewed`.
2. **`ops.reading` join kept for `is_erroneous` only.** The engineer's erroneous mark is not an episode column; the app
   forces `qa_flag` BAD (`ERRONEOUS_MARK`) when it is set. Proposal: either fold it into episode `qa_flag` or confirm the
   join is intended.
3. **`ENDPOINT_NAME` via `.id` is UNVERIFIED until `databricks bundle validate`/deploy.** The app accepts only the full
   `projects/…/endpoints/…` name (test-enforced); if `.id` resolves to a short id the app fails at startup with a clear
   `ValueError` instead of connecting wrongly. Please confirm the resolved value on the first deploy.
4. **Field-name casing:** api_contract uses `za`; the engine statistic key is `ZA`. The app returns `za` (with `za_unit`).
5. `installation_version` matches any SCD2 `ref.incl_installation` row for the instrument (the contract says "a row for
   that instrument"), not only the current one. Please confirm, or say if it must be the row valid at `survey_ts`.
6. Demo inclinometer alarm `reading_id` is synthetic (demo only); demo inclinometer rows carry a `calib_id`.
7. Grants: v1.4 §6 implemented in `resources/app.yml` (per-dam `dam-<dam_id>-members` CAN_USE, `dam-platform-admins`
   CAN_MANAGE); app README updated. Each newly onboarded dam needs its group added to `resources/app.yml`.
8. **QA-039 DA follow-ups (proposals; the app edits no DA file):**
   a. `src/jobs/lakebase_extract.py` copies `review_signoff` to the generic `audit.app_extract`. It should map it to
      `audit.review_record`, with the same columns, so the engine approval rule can require it.
   b. `sql/ddl/08_audit.sql`: add `incl_correction` to the `audit.review_record.subject_type` comment/enum, and
      `approve | reject` to `decision`.
   c. `ref.incl_correction`: add `delta.appendOnly = true` and no UPDATE grant. Approval then becomes a new superseding
      record (or the engine joins `audit.review_record`), not an in-place `reviewed_by`/`reviewed_utc` update.
   d. Engine approval rule (§5.5): in progress in the DA's uncommitted v1.5 work (`corrections.approval_problem` /
      `review_signature`, matching the app's hash). Please publish the canonical-JSON spec in SCHEMA_CONTRACT and
      api_contract, including the test vector the DA's docstring names (`tests/fixtures/review_signature_vector.json`).
   f. v1.5 `supersedes`/`review_id`: the app passes both through. `approval_status` still follows api_contract §4.1
      v1.4 (superseded = `superseded_by`; approved = reviewed_by/reviewed_utc). When §4.1 is updated (superseded
      derived from a later row's `supersedes`; approved requiring `review_id`), the app will follow.
   e. A key-based signature (for example a KMS/HSM-held key) is not in place: `signature_hash` gives tamper evidence
      only. Lead decision needed if REQ-22/23 requires a cryptographic signature.
