# API contract: data layer ⇄ app ⇄ QA

Version: **1.5 (2026-11-02, PROPOSED)**. Source of truth for UC objects: `../SCHEMA_CONTRACT.md` v1.5 (changelog there).
v1.5: approval status adds unreviewed_applied (defensive, §4.1), correction fields `supersedes`/`review_id`, approval via
`audit.review_record` (SCHEMA_CONTRACT §5.5.1), lowercase `za`, alarm `erroneous_*` fields, `metric_value` required.
This page lists the **interfaces** each side may rely on: what UC objects the app reads, what it writes to Lakebase,
how those writes reach UC, and the HTTP surface the app exposes (owned by the App Developer, listed here for QA).
Nothing here has been exercised against a live workspace (see `VERIFICATION.md` BUNDLE-1).

## 1. Read interface (app → UC, SQL warehouse, on behalf of the user)
All reads run as the signed-in user (Apps user authorization, scope `sql`) so `ref.dam_row_filter` applies.
The app never reads UC through its service principal and never reads a synced copy (SCHEMA_CONTRACT §7.3).

| Object | Grain | Key | Guaranteed columns |
|---|---|---|---|
| `ref.site` (view) | dam | `dam_id` | SCHEMA_CONTRACT §5 |
| `ref.instrument` | instrument | `instrument_id` | §5 |
| `ref.calibration` | sensor × version (SCD2) | `calib_id` | §5; `sign_mult`, `offset` are columns (§10 Q3) |
| `ref.formula` | formula × version | (`formula_id`,`formula_version`) | `verified` gates display |
| `ref.threshold` | threshold × version (SCD2) | (`threshold_id`,`threshold_version`) | §4 |
| `ref.pfm`, `ref.pfm_instrument` | PFM (SCD2) | `pfm_id` | §5.4 |
| `ref.instrument_type` | type | `instrument_type` | §5.4 |
| `ref.incl_installation` | inclinometer (SCD2) | `instrument_id`,`effective_from` | §5 |
| `silver.v_reading_current` / `ops.reading` | reading (latest reduction) | `reading_id` | §5, lineage per row |
| `silver.incl_survey_header`, `silver.incl_reduced` | survey × depth | (`survey_id`,`depth_m`) | §5, §5.5 |
| `ops.alarm_event`, `ops.alarm_episode` | episode | `event_id` (RAISE transition id) | §4, lineage §5 |
| `gold.instrument_latest` | sensor | `sensor_id` | pipeline MV |
| `gold.threshold_exceedances` | open exceedance | — | source of every SQL alert |

**Display gate** (app/SCHEMA_CONTRACT §3, agreed): show a computed value only if the formula version is
`verified`, the calibration is approved and current, and `qa_flag` ∉ {BAD, MISSING}. `qa_reason` is a
**comma**-separated code list.

## 2. Write interface (app → Lakebase `dam_app`, service principal)
| Lakebase table | Mutability | UC destination (job `dam_lakebase_extract`) |
|---|---|---|
| `alarm_ack` | mutable (open→acknowledged→closed) | `audit.event_log` `ALARM_ACK` → evaluator writes one `ACK` transition |
| `alarm_comment`, `pfm_review`, `audit_log`, `review_signoff`, `exceedance_status`, `inspection_note` | append-only | `audit.app_extract` / `audit.event_log` / `audit.review_record` / `ops.exceedance_status_log` / `ops.surveillance_inspection` |
| `manual_reading` | append-only | CSV → `/Volumes/<cat>/raw/landing/<dam>/readings/` (same Auto Loader path as ADAS) |
| `report_draft` | mutable | `rpt.dssmr_draft` snapshot per content sha256; `rpt.dssmr` only while `draft` |
| `onboarding_draft`, `onboarding_publication`, `app_user`, `dam_role` | app-owned | not extracted (UX state) |

Ownership rules: SCHEMA_CONTRACT §7.2. Lakebase is never the system of record for a regulatory record.

## 3. Engine interface (Python, for QA)
| Module | Contract |
|---|---|
| `dam_safety.formulas.registry` | `get(formula_id, version)`; each formula carries `sources` and `test_vectors` (asserted by `tests/test_registry.py`) |
| `dam_safety.qc` | `qa_flag` vocabulary + comma-joined `qa_reason` |
| `dam_safety.alarms` | state machine of SCHEMA_CONTRACT §4 |
| `dam_safety.inclinometer.*` | REPORT §6 module map; `corrections.AUDIT_FIELDS` == `ref.incl_correction` columns |
| `dam_safety.inclinometer.qc.between_set_bias_qc` (QA-020, rule QA-021) | `(installation, axis, suspect_survey_id, suspect, reference, reference_survey_id=None, params=BiasQCParams())` → `BiasQC` (rule `QA021-v2`) with `suspect_flag`, `za_proposed_c` (ZA = −ΔD, applied to A0; set for `PROPOSED` and `SUSPECT_CROSSCHECK`, None otherwise; Δ = suspect − reference), evidence `n`, `mean_dD`, `sd`, `se`, `spread`, `mean_cs_change`, `abs_dD`, `abs_bs`, `crosscheck_diff`, `crosscheck_tol` (UNVERIFIED, ranks only), `crosscheck_verdict` (`AGREE`/`SUSPECT`), `requires_review` (always true), `reference_survey_id`, `rule`, plus `stable_zone_assumed`, `params_unverified`, `status` (`NO_PROPOSAL | PROPOSED | SUSPECT_CROSSCHECK | BAD_RESURVEY`), `gates`, `qa_flag`. `sign_agree` and `equal_opposite` removed (no sign gate). Proposes only; `bias_proposal_record` → `applied=false` correction row; an applied bias needs reviewer approval. Fields per SCHEMA_CONTRACT §5.5 |
| `dam_safety.inclinometer.batch.reduce_instrument` (job core) | `(inst_row, survey_rows, correction_rows, batch_id, reduced_ts, target_surveys=None, bias_rule=DEFAULT_BIAS_RULE, label_unit_m=1.0)` → `(rows with REDUCED_COLUMNS == silver.incl_reduced, [BiasQC proposals])`; writer job `src/jobs/reduce_inclinometers.py` (SCHEMA_CONTRACT §5.5 v1.3) |
| `common.toa5` (App Dev, canonical) + `dam_safety.toa5_landing` | TOA5 → bronze `READING_SCHEMA` rows; the `dam_safety.toa5` shim was deleted 2026-11-02 (no callers) |

## 4. App HTTP surface (owned by the App Developer; listed, not changed)
`GET /api/health`, `/api/me`, `/api/portfolio`, `/api/dams/{dam_id}`, `/api/formulas`,
`/api/dams/{dam_id}/instruments`, `/api/instruments/{id}`, `/api/instruments/{id}/timeseries`,
`/api/instruments/{id}/incl/surveys`, `/api/instruments/{id}/incl/profile`, `/api/formulas/{formula_id}/{version}/check`,
`GET /api/alarms`, `GET /api/alarms/{event_id}`, `POST /api/alarms/{event_id}/ack|close|comment`,
`GET /api/dams/{dam_id}/dssmp`, `POST /api/dams/{dam_id}/pfms/{pfm_id}/review`,
`GET|POST /api/dams/{dam_id}/reports`, `GET /api/dams/{dam_id}/reports/{report_id}`,
`/api/onboarding/*` (steps, drafts, parse-csv, formulas/check, validate, golive), `/api/admin/*` (users, roles, audit).
(Extracted from `app/backend/routers/*.py` by route decorator; request/response schemas are the app's.)

### 4.1 Response fields the data layer provides (v1.5; App Developer to expose)
| Endpoint | Field | Source |
|---|---|---|
| `/api/instruments/{id}/incl/surveys`, `/incl/profile` (bias proposals per survey × axis) | `za` (lowercase; = −ΔD, reading units, added to A0: D′ = D + za; mapped from the engine/storage key by `qc.bias_qc_api` / `qc.parse_detection_statistic`), `abs_dD` (\|ΔD\|), `abs_bs` (\|Δ0+Δ180\|), `crosscheck_verdict` (`AGREE`/`SUSPECT`), `crosscheck_diff`, `crosscheck_tol`, `status`, `rule` | `BiasQC` / `ref.incl_correction` (`correction_type='bias'`, `detection_method='between_set_bias_qc'`, `auto_value`, `detection_statistic`) |
| same | `approval_status`: `proposed` (applied=false), `approved` (applied=true with a matching signed `audit.review_record` approval, SCHEMA_CONTRACT §5.5.1), `superseded` (a later version row supersedes it), `unreviewed_applied` (defensive app-only: applied=true without a valid review; impossible under the DB CHECK, never shown as approved) | `gold.incl_correction_status` (v1.5) |
| same | `record_id`, `supersedes` (v1.5: record_id of the version this row replaces; NULL for a first proposal), `review_id` (v1.5: the `audit.review_record` approval; NULL unless applied), `superseded_by` (derived), `approved_by`, `approved_utc`, `review_rationale`, `review_signature_hash`, `review_level` and `review_level_legacy_unverified` (v1.6: TRUE when a CDSE-level approval was signed by someone who was not the dam's CDSE at `signed_ts`; the record is unchanged, show it as a warning, SCHEMA_CONTRACT §5.5.2) | `gold.incl_correction_status` |
| same | `formula_verified`, `installation_version` (the `ref.incl_installation` row valid at survey time), `probe_constants`, `qc_rule_version`, `correction_record_ids` | `silver.incl_reduced` + `ref.formula.verified` (SCHEMA_CONTRACT §5.5 "Approval when `calib_id` is NULL") |
| `GET /api/alarms`, `/api/alarms/{event_id}` | `formula_id`, `formula_version`, `qa_flag` (no join needed), `value` (never NULL), `erroneous_mark`, `erroneous_reason`, `erroneous_marked_by`, `erroneous_marked_ts` (v1.5; do not read `ops.reading`), `cdse_ack_required_at_open` (v1.7: binding snapshot from the opening rule version; NULL for pre-v1.7 episodes), `cdse_ack_required` (v1.7: current flag of the opening rule), `raise_threshold_id`, `raise_threshold_version` | `ops.alarm_episode` (SCHEMA_CONTRACT §4, §5) |
| `POST` review sign-off (app route) | writes Lakebase `dam_app.review_signoff` (`subject_type='incl_correction'`, `subject_id`=record_id, `decision` `approve`/`reject`, `comment`=rationale, required; `signature_hash` per SCHEMA_CONTRACT §5.5.1); reviewer ≠ author | extracted to `audit.review_record` (SCHEMA_CONTRACT §7) |
App-originated or manual non-ACK alarm transitions (if the app ever writes one) must fill `value`, `metric_value` and `reading_id` (CHECK `ck_alarm_transition_value`, SCHEMA_CONTRACT §4 v1.5).
SUSPECT is a review ranking, not a rejection: show both PROPOSED and SUSPECT_CROSSCHECK proposals, ranked AGREE first.

### 4.2 Review and audit-chain endpoints (v1.6; app SCHEMA_CONTRACT §5a, §7 10e; paths from `app/backend/routers/*.py`)
| Method + path | Who | Request | Response / errors |
|---|---|---|---|
| `POST /api/instruments/{instrument_id}/incl/surveys/{survey_id}/corrections/{record_id}/review` | engineer+ on the dam; reviewer ≠ author | `{decision: "approve"\|"reject", rationale: 1..8000 chars, reviewer_pe_license?}` | appends one chained `review_signoff` (`action_type='data_correction_review'`, `subject_type='incl_correction'`); 422 bad decision / empty rationale, 403 self-review, 409 record not `proposed` or integrity error, 400 not an inclinometer, 404 unknown record |
| `POST /api/instruments/{instrument_id}/incl/surveys/{survey_id}/bias-proposals/{record_id}/review` | same | same | same, `action_type='bias_review'` |
| `POST /api/alarms/{event_id}/ack`, `/close` | engineer+ | (app schema) | chained `review_signoff` `alarm_event` / `acknowledge`\|`close` (`alarm_ack` / `alarm_close`) |
| `GET /api/audit/signoffs/verify` | platform admin (`require_admin`) | – | `{ok, chains: [chain], genesis, scope: "per-dam", verified_utc}` |
| `GET /api/dams/{dam_id}/audit/signoffs/verify` | engineer+ on that dam (dam ACL) | – | `chain` + `genesis`, `scope`, `verified_utc` |

`chain` = `{chain_id, records, ok, first_broken_seq, reason, detail, signed_records}` plus `head_seq`, `head_hash` when
`ok`; `reason` ∈ `gap | prev_mismatch | hash_mismatch | signature_invalid | head_missing | head_mismatch`.
Discrepancies (flagged to the App Developer): app §7 10e lists `POST /api/instruments/{id}/incl/corrections/{record_id}/review`
and the lead's request named `/api/dams/{dam_id}/incl/corrections/{record_id}/review`; neither exists in the router,
which has the `…/incl/surveys/{survey_id}/corrections/{record_id}/review` path above. §7 10e names the record count `n`;
the code returns `records`. The UC copy can be re-verified without the app (`dam_safety.audit_chain`, SCHEMA_CONTRACT §5.5.2).

## 5. Change rule
Renames/removals → new contract version + App Developer and QA sign-off. Additive nullable columns → allowed,
announced in SCHEMA_CONTRACT's change list.
