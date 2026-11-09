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
| `ref.v_dam_reviewer_authority` (DA least-privilege view, `sql/ddl/10b`; **UNVERIFIED until the DA commits it**) | who is the dam's CDSE (v1.6 ROLE-LEVEL, §5a). The app reads **only** this view, never `ref.dam` or `ref.project_membership` | `principal` where `dam_id = :dam_id` and the grant is active (`granted_ts` ≤ now < `revoked_ts`, NULL = open). View missing → no one is CDSE (fail closed), logged once |
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
alarm_comment, onboarding_draft, onboarding_publication, report_draft, pfm_review, audit_log`, and (v1.6, live
alarm-rule flag edits, app-only so far) `alarm_rule_flag_proposal` (workflow state: pending → applied | rejected) and
`ref_change_publication` (outbox for the DA publish job, §7 item 13). Connection: injected
`PG*` env + `ENDPOINT_NAME` (bundle v1.4: `${resources.postgres_endpoints.dam_lakebase_primary.id}`; the app requires the full `projects/<p>/branches/<b>/endpoints/<e>` name and fails fast otherwise), `generate_database_credential` per new pooled connection, TCP keepalives and retry for HA
failover. Identity is keyed on `X-Forwarded-User`; the user token is never logged and never used for Lakebase.

## 5. Reductions vs the QA oracle (`../qa/reference_examples.py`, read-only)
`tests/test_qa_oracle.py` runs every oracle case through the app's functions (VW_PIEZO, VW_DISP_LIN, INCL_PROBE_2PASS,
load cells, thermistor). SG-01..03 are asserted not displayed (`strain_gauge` has no engine formula). Restricted cases are
read by path from `$DAM_QA_PRIVATE` and never copied.

## 5a. Reviewer actions: hash-chained, append-only audit (QA-039 v2)
Every reviewer action is ONE inserted row in Lakebase `review_signoff` (lakebase/001 table, column-compatible with UC
`audit.review_record` plus chain columns). Nothing is updated or deleted (triggers in sqlite and Postgres, app-owned
names `t_no{update,delete}_review_signoff` / `t_app_noupd_review_signoff` / `app_forbid_mutation`), and the app never
updates ref.incl_correction, never applies a correction and never changes a measured value.

**Guard init (QA-045b/QA-045c).** Postgres guards: row-level `t_app_noupd_review_signoff` (UPDATE/DELETE) and
statement-level `t_app_notrunc_review_signoff` (TRUNCATE). `StateStore.init_schema` runs the whole Postgres init on
ONE pooled connection in ONE transaction after `pg_advisory_xact_lock(4702045)` (the key shared with
`lakebase/001_app_schema.sql`) and uses `CREATE OR REPLACE TRIGGER` (no DROP), so concurrent replicas and the
data-layer script serialize and no session ever sees review_signoff without its guards. It refuses an autocommit
connection. Tested on local PostgreSQL 17.11 (`app/tests/test_state_pg_append_only.py`, `DAM_TEST_PG`): 8 concurrent
inits on a fresh schema with 0 errors, and 3 x 8 concurrent re-inits under an UPDATE/DELETE/TRUNCATE hammer with 0
errors and 0 mutations. Without the lock the same tests fail (UniqueViolation / DeadlockDetected). **UNVERIFIED:**
Lakebase's Postgres major version (CREATE OR REPLACE TRIGGER needs >= 14) and whether the Lakebase app role may take
advisory locks.

| action_type | endpoint (role) | subject_type / decision | original_value → corrected_value |
|---|---|---|---|
| `bias_review` | `POST /api/instruments/{iid}/incl/surveys/{sid}/bias-proposals/{record_id}/review` (engineer+) | `incl_correction` / `approve\|reject` | `{parameter_name, before_value}` → `{parameter_name, auto_value, override_value, after_value, applied_by_app:false}` (units as stored in ref.incl_correction) |
| `data_correction_review` | `POST /api/instruments/{iid}/incl/surveys/{sid}/corrections/{record_id}/review` (engineer+; any proposed ref.incl_correction record; a bias record is typed `bias_review`) | `incl_correction` / `approve\|reject` | as above |
| `alarm_ack` (CDSE acknowledgement at level ≥ 2, or on a rule flagged `cdse_ack_required`) | `POST /api/alarms/{event_id}/ack` (level 2 Evaluate and level 3 Act: **the dam's CDSE only**; level 1 Verify: engineer+, unless the rule is flagged `cdse_ack_required`, then the dam's CDSE only; see below) | `alarm_event` / `acknowledge` | `{event_id, dam_id, instrument_id, level, value, unit, event_ts, state:"open"}` → `{state:"acknowledged", measured_value_changed:false}` |
| `alarm_close` | `POST /api/alarms/{event_id}/close` (dso+) | `alarm_event` / `close` | same alarm snapshot with `state:"acknowledged"` → `{state:"closed", measured_value_changed:false}` |
| `alarm_rule_flag_propose` | `POST /api/dams/{dam_id}/alarm-rules/{threshold_id}/cdse-flag/proposals` (dam_admin, platform admin or the dam's CDSE) | `alarm_rule` / `propose` | `{threshold_id, threshold_version, cdse_ack_required}` (current rule) → `{cdse_ack_required, proposal_id, state:"pending"}` |
| `alarm_rule_flag_apply` | `POST /api/dams/{dam_id}/alarm-rules/proposals/{proposal_id}/approve` or `POST /api/dams/{dam_id}/alarm-rules/{threshold_id}/cdse-flag` (direct set/clear) (**the dam's CDSE only**) | `alarm_rule` / `apply` | current rule as above → `{cdse_ack_required, threshold_version: N+1, proposal_id, publication_id, proposed_by}` |
| `alarm_rule_flag_reject` | `POST /api/dams/{dam_id}/alarm-rules/proposals/{proposal_id}/reject` (**the dam's CDSE only**) | `alarm_rule` / `reject` | current rule as above → `{cdse_ack_required, proposal_id, state:"rejected", proposed_by}` |

Review bodies: `{decision, rationale, reviewer_pe_license?}`; ack: `{rationale}` (`comment` accepted as the older name);
close: `{reason}`. **The rationale is required and must be non-empty after trimming** (422 otherwise; DB CHECK
`length(trim(comment)) > 0` as backstop); it is stored in `comment`. original_value / corrected_value are canonical
JSON text; the original is stored next to the corrected value and never overwritten (Ch.14 §14.4.1).

**Actor app role and the versioned canonical form (v1.6, DA follow-up 1).** Every NEW chained record carries
`actor_app_role` and `canon_version = 2`; both are in the hashed content.
- `actor_app_role` holds the acting user's RAW app roles on that dam at the moment of the action. Values are `viewer`,
  `engineer`, `dso`, `dam_admin`, `admin` (platform admin) and `cdse` (the dam's named CDSE, `signoffs.is_cdse`,
  fail closed). When several apply, they are sorted and comma-joined with no spaces (e.g. `cdse,engineer`,
  `admin,cdse`).
- It is **derived server-side** in `signoffs.append` (`signoffs.actor_app_role`) from the stored grants and the CDSE
  lookup at the time of the action. It is **never client-supplied**: a request body carrying `actor_app_role` or
  `canon_version` is ignored. It is separate from `review_level`, which keeps its meaning (`CDSE` / `engineer`).
- **Canon 1** (`canon_version` NULL or absent: every record written before this change, and the DA fixtures
  `tests/fixtures/review_chain_vector.json` / `review_chain_signed_vector.json`) hashes exactly `CHAIN_FIELDS`, as
  before. **Canon 2** hashes `CHAIN_FIELDS + (actor_app_role, canon_version)`, with `canon_version` as an integer.
  Same canonical JSON (sorted keys, compact separators, `canonical_value`); `record_hash` / `signature` / `sig_alg` /
  `key_id` stay excluded.
- `verify_chain` checks, in addition to the existing reason codes:
  - a canon-2 record whose role is NULL or blank fails `actor_role_missing`;
  - a role outside the vocabulary, or not sorted and unique, fails `actor_role_invalid`;
  - a canon-1 record after a canon-2 record in the same chain fails `canon_downgrade`;
  - any other `canon_version` fails `canon_unknown`.

  Stripping or editing either field without recomputing fails `hash_mismatch`.
- The state store has CHECK `ck_review_signoff_canon` (`canon_version IS NULL OR (canon_version = 2 AND
  actor_app_role non-blank)`), which also blocks a privileged UPDATE.
- App-side vector: `app/tests/fixtures/review_chain_canon2_vector.json`, generated by `app/tests/canon2_vector.py`
  and pinned byte for byte by `app/tests/test_actor_app_role.py`. It has 2 canon-1 records then 3 canon-2 records,
  and 12 cases.

**Alarm levels vs FERC "high/critical" (Genie decision, v1.6).** FERC Ch.14 speaks of high/critical conditions;
the app's alarm levels are 1 Verify (ALERT), 2 Evaluate (ACTION) and 3 Act. **Our mapping: levels 2 Evaluate and 3 Act
are FERC "high/critical"** and their acknowledgement is the CDSE acknowledgement (`CDSE_ACK_MIN_LEVEL = 2`,
`app/backend/routers/alarms.py`). **Level 1 Verify is engineer and above.**

**Per-rule `cdse_ack_required` (Genie decision, v1.6).** A per-dam, per-alarm-rule boolean on `ref.threshold` (the
alarm-rule table: one row per dam / instrument / level / limit, SCD2 by `threshold_version`), default false, DA-owned
(§7 item 12). When a rule is flagged, acknowledging its alarms needs that dam's CDSE at **any** level, level 1
included; unflagged rules keep the level rule above. The app ORs three sources, so un-flagging a rule never weakens an
episode that is already open: `ops.alarm_episode.cdse_ack_required_at_open` (snapshot when the episode opened),
`ops.alarm_episode.cdse_ack_required` (the rule flag the DA exposes on the view) and the current
(`is_current`) `ref.threshold` row of the same `threshold_id`. A column that is absent (older schema) counts as false
and is logged once per column. API: `GET /api/alarms[/{id}]` returns `cdse_ack_required` (resolved flag), and
`requires_cdse` = level ≥ 2 OR `cdse_ack_required`. The onboarding wizard's thresholds step has a "CDSE ack required"
checkbox (default off), saved through `PUT /api/onboarding/drafts/{id}/steps/thresholds` and published as
`ref.threshold.cdse_ack_required` at go-live. Every change of the flag in a draft is an audited admin action
(`audit_log.action = alarm_rule_cdse_ack_required`, detail `{rule, instrument_id, level, from, to}`), and go-live logs
the flagged rules. Demo: `CRD-IN-1-T1` (level 1) is flagged.

**Live-dam edits of `cdse_ack_required` (Genie decision, v1.6).** Page `/dams/{dam_id}/alarm-rules`, API
`GET /api/dams/{dam_id}/alarm-rules` (viewer+: current rules with flag and version, pending and decided proposals,
`is_cdse` / `can_propose` / `can_apply`) plus the three POST actions above.
- Only the dam's CDSE (`signoffs.is_cdse`, live: `ref.v_dam_reviewer_authority`, fail closed) may apply a set or
  clear, either directly or by approving a proposal. The CDSE may also approve their own proposal.
- **PROVISIONAL approval rule** (Genie, v1.6; the user is deciding whether to require a second approver). It is one
  setting, `CDSE_FLAG_REQUIRE_SECOND_APPROVER` (env / `app.yaml`, `config.Settings.cdse_flag_require_second_approver`,
  default `false`). Decision point: `routers/alarm_rules.py` (DECISION POINT comment).
  - `false` (default, current behaviour): as above. The CDSE sets/clears directly and may self-approve.
  - `true`:
    - Direct set/clear is refused with 403 and detail `{code: "second_approver_required", message, provisional:
      true, setting}`. Every change goes through a proposal (the CDSE may propose too).
    - `approve` by the proposer returns 403 `{code: "self_approval_forbidden", ...}`. Proposer and approver are
      matched on email (case-insensitive) or the stable reviewer id. Nothing is written and the proposal stays
      pending.
    - The approver must still be that dam's CDSE, and dam_admin / admin still only propose.
    - **Consequence:** with one CDSE per dam, a CDSE's own proposal can only be rejected (that is the withdrawal;
      there is no separate withdraw action, which would need a new `decision` value in the DA CHECK), never
      approved. A CDSE-initiated change therefore needs either a second CDSE grant for the dam
      (`ref.project_membership` role CDSE, honoured by the authority lookup) or a dam_admin / admin to propose it
      for the CDSE to approve.
  - Exposed to the UI as `approval_policy` (`cdse_flag_require_second_approver`, `provisional: true`,
    `cdse_may_apply_directly`, `cdse_may_self_approve`, `note`) on `GET /api/me` and `GET .../alarm-rules`. The
    alarm-rules GET also gives `can_apply` (false when the setting is on) and, per pending proposal, `own`,
    `can_approve` and `can_reject`.
  - The Alarm rules page hides direct set/clear and self-approve accordingly. It and the wizard's thresholds step
    show the note (e.g. "Approval rule provisional: currently the dam CDSE may self-approve").
- Version N+1 copies `dssmp_version_id`, `tier`, `deadband`, `persistence_on` and `persistence_off` (ref.threshold
  NOT NULL) from version N unchanged. If version N lacks any of them, the apply (direct or by approval) is refused
  with 409 before anything is recorded or queued, and the proposal stays pending. No default is ever invented.
- dam_admin and platform admin may only propose. Their proposal stays pending until that dam's CDSE approves or
  rejects it; they can never approve one, including their own. engineer, dso and viewer get 403 on every action.
  The CDSE of another dam gets 403.
- Each propose, apply and reject needs a non-empty rationale (422 otherwise, nothing written). Each appends one
  chained record (table above) atomically with the proposal state and, for an apply, the outbox row.
- An apply never edits a `ref.threshold` row. It queues `ref.threshold` version N+1: the current row's canonical
  columns with the new flag, `effective_from` = apply time (server UTC), `effective_to` NULL, `is_current` true and
  `set_by` = the CDSE. In demo mode the version is appended at once.
- Approval is refused with 409 (stale) when the rule changed since the proposal: another version, another flag, or
  an applied change still waiting to be published. A stale proposal can still be rejected.
- Open episodes keep their protection. The ack rule ORs the episode snapshot `cdse_ack_required_at_open` with the
  current flag, so clearing a flag never weakens an episode that is already open, and setting one applies to open
  episodes immediately.

**CDSE identity (v1.6 ROLE-LEVEL, FERC Ch.14 App K 14-K-4).** The CDSE is a named person, not an app role. A caller is
the CDSE of dam D when their X-Forwarded-User id or X-Forwarded-Email equals (case-insensitive, trimmed)
`ref.dam.cdse_id` for D, or the `principal` of an active (`revoked_ts IS NULL`) `ref.project_membership` row with
`role = 'CDSE'` and `dam_id = D` exactly, as published by `ref.v_dam_reviewer_authority` (the only object the live app reads for this; demo mode uses seeded data). `dam_id = '*'` grants and group principals are **not** resolved (the app cannot
expand groups); the lookup fails closed (lookup error → not CDSE). The CDSE of one dam is not the CDSE of another.
The dam ACL still applies (the CDSE needs viewer+ on the dam). For a level ≥ 2 alarm, everyone else, including dso,
dam_admin and the platform admin, gets **403 before any write** (no chain record, no ack state, no audit entry), and
`GET /api/alarms[/{id}]` returns `requires_cdse`, `is_cdse` and `can_ack` so the UI hides the CDSE sign-off control.
Close stays dso+ (recorded at `engineer` unless the closer is the CDSE). dso / dam_admin / admin keep their user and
role administration powers. Records written before v1.6 under the earlier proposal (dso / dam_admin / admin →
`CDSE`) keep their stored `review_level` and still verify: `record_hash` is recomputed from stored content, so only new
records change (`app/tests/test_cdse_roles.py::test_pre_change_chain_still_verifies_and_only_new_records_change`).

**Columns.** `review_id` (`RS-<uuid>`), `dam_id`, `subject_type`, `subject_id`, `review_level` (v1.6 ROLE-LEVEL, Genie-confirmed: `CDSE` **only** when the
caller is the dam's CDSE, otherwise `engineer` for every app role: engineer, dso, dam_admin and platform admin get no
review elevation), `decision`, `comment`,
`reviewer_id` = X-Forwarded-User (demo personas: `demo:<email>`), `reviewer_email` = X-Forwarded-Email,
`reviewer_sub` = `sub` claim of `x-forwarded-access-token` base64url-decoded **without verification, identity only**
(NULL if absent or malformed; the token itself is never stored, logged or returned, test-enforced), `reviewer_pe_license`,
`signed_ts` (server UTC, ISO-8601 with microseconds), `signature_hash`, `action_type`, `original_value`,
`corrected_value`, `chain_id`, `chain_seq`, `prev_hash`, `record_hash`, and nullable `signature`, `sig_alg`, `key_id`.

**Engine-compatible content hash (kept).** `signature_hash` = sha256 of canonical JSON `{"signoff": review_id, dam_id,
subject_type, subject_id, review_level, decision, comment, reviewer_id, reviewer_pe_license, signed_ts, "subject": the
reviewed content}`, values normalised by `canonical_value` (UTC ISO-8601 with microseconds, NaN → null), sort_keys,
`(',', ':')`, UTF-8, ensure_ascii=False. For `incl_correction` the subject is every ref.incl_correction column (23 audit
fields + reviewed_by, reviewed_utc, supersedes, review_id), byte-identical to the engine's v1.5
`corrections.review_signature` (app test against the engine module). For `alarm_event` the subject is the
original_value object. The profile returns `bias_signoffs` with `signature_verified` re-computed against the current record.

**Hash chain.**
- Scope: **one chain per dam** (`chain_id = dam_id`), so a dam's chain verifies on its own and the per-dam endpoint is
  scoped by the dam ACL. Genesis: `prev_hash` of `chain_seq = 1` is `GENESIS = "0" × 64`.
- `record_hash` = sha256 of canonical JSON (same rules) of every column above **except** `record_hash`, `signature`,
  `sig_alg`, `key_id` (`chain_seq` as an integer). It covers content, identity, timestamp, rationale, original and
  corrected values, `signature_hash`, `chain_id`, `chain_seq` and `prev_hash`.
- `review_chain_head(chain_id PK, chain_seq, head_hash, updated_ts)` is the append lock and the tail anchor (it makes
  deletion of the last record detectable).
- Append (`StateStore.append_chained`), one transaction on one connection: `INSERT … review_chain_head … ON CONFLICT
  DO NOTHING` (genesis), `SELECT chain_seq, head_hash … FOR UPDATE` (Postgres row lock; sqlite: `BEGIN IMMEDIATE` write
  lock), INSERT the record with seq + 1 / prev = head_hash, UPDATE the head, then the action's state change (alarm
  ack/close, re-checked inside the transaction). Commit together or roll back together. `UNIQUE (chain_id, chain_seq)`
  is the backstop, so two app instances cannot fork a chain (tested with two stores on one database file, 4 threads).
- Verify: `GET /api/audit/signoffs/verify` (platform admin, every chain) and `GET /api/dams/{dam_id}/audit/signoffs/verify`
  (engineer+ on that dam). They recompute the whole chain and return `ok`, or `first_broken_seq` with `reason`: `gap`
  (missing or duplicate seq), `prev_mismatch` (link or genesis), `hash_mismatch` (content changed), `signature_invalid`,
  `head_missing`, `head_mismatch` (tail deleted or head altered). Tampering, deleting (middle or tail) and reordering
  are all detected (tests). QA-045a: when a real (non-`NoopSigner`) signer is configured, every record must carry a
  non-blank `signature`, `sig_alg` (equal to the signer's) and `key_id` that verify; otherwise `signature_missing`, or
  `signature_downgrade` when an earlier record was signed. The response also reports `signatures_required`.
  With the no-op default, behaviour is unchanged.
- Future key-based signature: `backend/audit_chain.Signer` (`alg`, `key_id`, `sign(record_hash)`, `verify(...)`),
  `NoopSigner` by default (`create_app(signer=...)`). FERC confirmed no in-app crypto signature is needed; PDF /
  certificate signing of exports is backlog (`docs/BACKLOG.md`).

## 6. Questions from v1.0
All 12 are answered by root §10 and implemented above. v1.3 follow-ups 1, 2, 4, 5, 6, 7 (partly), 9 are resolved by v1.4;
3 (§7.3 text) is resolved.

## 7. Mismatches / follow-ups for the Data Architect (v1.4 → v1.5; proposals, the app does not edit DA files)
1. **Resolved (QA-036, v1.5):** `unreviewed_applied` stays the contract's defensive status. The app never serves it:
   a gold `unreviewed_applied` row, or any applied row without reviewed_by (≠ user) / reviewed_utc / review_id, raises
   `DataIntegrityError` and its values are withheld (matches CHECK `ck_incl_correction_applied_reviewed`).
2. **Resolved (v1.5):** `erroneous_mark` / `erroneous_reason` / `erroneous_marked_by` / `erroneous_marked_ts` are read from
   `ops.alarm_episode`; the alarm query no longer joins `ops.reading`. `erroneous_mark = true` forces `qa_flag` BAD
   (`ERRONEOUS_MARK,<reason>`), so the value is withheld.
3. **`ENDPOINT_NAME` via `.id` remains UNVERIFIED (v1.5 unchanged) until `databricks bundle validate`/deploy.** The app accepts only the full
   `projects/…/endpoints/…` name (test-enforced); if `.id` resolves to a short id the app fails at startup with a clear
   `ValueError` instead of connecting wrongly. Please confirm the resolved value on the first deploy.
4. **Resolved (v1.5, QA-040):** stored statistic keys map through `API_FIELD_MAP = {"ZA": "za", "ZB": "zb"}` (== engine
   `qc.API_FIELD_MAP`, test-enforced) everywhere: `bias_proposal`, `bias_qc` evidence and `proposed_c`. Unit text is
   `ZA_UNIT = "reading units (za/zb; D' = D + za)"`. No API payload carries `ZA`.
5. **Resolved (v1.5):** `installation_version` must equal the `effective_from` of the single `ref.incl_installation` row
   with `effective_from <= survey_ts < effective_to` (NULL = open; `event_ts` for alarm episodes). No covering row or
   overlapping rows → `UNAPPROVED_INSTALLATION` (engine `batch.installation_row_at`: no row, no reduction).
6. **Resolved (v1.5):** demo data matches the fixed demo CSVs: inclinometer rows and episodes have `calib_id` NULL +
   `installation_version` (two SCD2 rows per inclinometer, boundary on day 180, half-open), episode `reading_id` is the
   real survey id (no `-PEAK-A`), and `metric_value` is filled on every reading-driven episode. The demo `no_data`
   watchdog episode has no reading (value / metric_value / reading_id NULL); it is an episode, not a transition, and is
   withheld (`LINEAGE_INCOMPLETE`). QA-035: `contract.check_transition` rejects any non-ACK transition (manual or
   app-originated) without value, metric_value and reading_id; the app writes no transitions today.
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
   f. **Resolved (v1.5):** approvals are insert-only `audit.review_record` rows; `ref.incl_correction` is append-only
      (`supersedes`, `review_id`). The app reads approval state from `gold.incl_correction_status` (approval_status,
      approved_by, approved_utc, review_rationale, review_signature_hash); demo derives `superseded_by` from a later
      row's `supersedes` (`contract.derive_superseded`). `signoff.signature` reproduces
      `tests/fixtures/review_signature_vector.json` (`c58d8c1a…3d9e`, test-enforced).
   e. **Resolved:** no KMS / in-app crypto signature (lead decision; FERC confirmed). Tamper evidence is the per-dam hash
      chain (§5a); nullable `signature`/`sig_alg`/`key_id` and the Signer hook remain for later. Document signing of
      exports is backlog (`docs/BACKLOG.md`).
9. **QA-039 v2 hash chain: exact DDL for the DA (proposal; the app creates the same in its own state.py, so either
   order works).**
   a. `lakebase/001_app_schema.sql`, after the existing `review_signoff` CREATE. The SET NOT NULL steps fail if v1 rows
      (pre-chain, possibly with NULL comment) already exist; in that case move them out or back-fill them as a
      verified genesis chain first (the app cannot check which applies to a deployed database):
```sql
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS action_type     TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS reviewer_email  TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS reviewer_sub    TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS original_value  TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS corrected_value TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS chain_id        TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS chain_seq       BIGINT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS prev_hash       TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS record_hash     TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS signature       TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS sig_alg         TEXT;
ALTER TABLE review_signoff ADD COLUMN IF NOT EXISTS key_id          TEXT;
ALTER TABLE review_signoff ALTER COLUMN comment         SET NOT NULL;
ALTER TABLE review_signoff ALTER COLUMN action_type     SET NOT NULL;
ALTER TABLE review_signoff ALTER COLUMN original_value  SET NOT NULL;
ALTER TABLE review_signoff ALTER COLUMN corrected_value SET NOT NULL;
ALTER TABLE review_signoff ALTER COLUMN chain_id        SET NOT NULL;
ALTER TABLE review_signoff ALTER COLUMN chain_seq       SET NOT NULL;
ALTER TABLE review_signoff ALTER COLUMN prev_hash       SET NOT NULL;
ALTER TABLE review_signoff ALTER COLUMN record_hash     SET NOT NULL;
ALTER TABLE review_signoff DROP CONSTRAINT IF EXISTS ck_review_signoff_rationale;
ALTER TABLE review_signoff ADD CONSTRAINT ck_review_signoff_rationale CHECK (length(trim(comment)) > 0);
ALTER TABLE review_signoff DROP CONSTRAINT IF EXISTS ck_review_signoff_action;
ALTER TABLE review_signoff ADD CONSTRAINT ck_review_signoff_action CHECK (action_type IN
  ('bias_review', 'data_correction_review', 'alarm_ack', 'alarm_close'));
CREATE UNIQUE INDEX IF NOT EXISTS review_signoff_chain_seq ON review_signoff (chain_id, chain_seq);
CREATE TABLE IF NOT EXISTS review_chain_head (
  chain_id TEXT PRIMARY KEY, chain_seq INTEGER NOT NULL, head_hash TEXT NOT NULL, updated_ts TEXT NOT NULL);
-- review_chain_head is UPDATEd by the append transaction: do NOT add forbid_mutation to it.
```
   b. `sql/ddl/08_audit.sql` (`audit.review_record`, Delta, appendOnly already):
```sql
ALTER TABLE audit.review_record ADD COLUMNS (
  action_type     STRING COMMENT 'bias_review | data_correction_review | alarm_ack | alarm_close',
  reviewer_email  STRING COMMENT 'X-Forwarded-Email at signing',
  reviewer_sub    STRING COMMENT 'unverified sub claim of the user token (identity label only)',
  original_value  STRING COMMENT 'canonical JSON; the value before the action, never overwritten',
  corrected_value STRING COMMENT 'canonical JSON; the proposed / resulting value',
  chain_id        STRING COMMENT 'per-dam chain (= dam_id)',
  chain_seq       BIGINT,
  prev_hash       STRING COMMENT 'record_hash of chain_seq-1; GENESIS = 64 x 0',
  record_hash     STRING COMMENT 'sha256 canonical JSON of all columns except record_hash, signature, sig_alg, key_id',
  signature       STRING COMMENT 'reserved (future key-based signature), NULL',
  sig_alg         STRING,
  key_id          STRING);
ALTER TABLE audit.review_record DROP CONSTRAINT IF EXISTS ck_review_record_subject_decision;
ALTER TABLE audit.review_record ADD CONSTRAINT ck_review_record_subject_decision CHECK (
  subject_type IN ('reading', 'exceedance', 'dssmr', 'threshold', 'commentary', 'rule_finding', 'incl_correction',
                   'alarm_event')
  AND (subject_type <> 'incl_correction'
       OR (decision IN ('approve', 'reject') AND comment IS NOT NULL AND length(trim(comment)) > 0))
  AND (subject_type <> 'alarm_event'
       OR (decision IN ('acknowledge', 'close') AND comment IS NOT NULL AND length(trim(comment)) > 0)));
```
   c. `src/jobs/lakebase_extract.py`: copy the new columns (item 8a) and preserve `chain_seq` order; the UC copy can be
      re-verified with the same `record_hash` rule. The `review_chain_head` table does not need extracting.
   d. The role → `review_level` mapping is **confirmed (v1.6 ROLE-LEVEL, Genie)**: `CDSE` only for the dam's
      `ref.dam.cdse_id` / active direct `ref.project_membership` CDSE principal; engineer, dso, dam_admin and platform
      admin → `engineer`. Pre-v1.6 rows that carry `CDSE` for a dso/admin stay as written (they still verify).
   e. `audit.event_log` `ALARM_ACK` (extract of `alarm_ack`) is unchanged; the chained record is the reviewer evidence.
10. **v1.5 conflicts with the chained audit record (e01e1ab) and what the DA needs to change (proposal):**
   a. **Conflict 1:** `audit.review_record` `ck_review_record_subject_decision` (v1.5 migration + `08_audit.sql`) allows
      no `alarm_event` subject. The app's `alarm_ack` / `alarm_close` chain rows (`subject_type = 'alarm_event'`,
      decision `acknowledge` / `close`) would be rejected on extract. Fix: the CHECK in item 9b (adds `alarm_event`).
   b. **Conflict 2:** `lakebase_extract.REVIEW_RECORD_COLS` has 12 columns, but a chain row has 24. Copying only 12
      drops `chain_seq`/`prev_hash`/`record_hash`/rationale/original value, so the UC copy cannot be re-verified. Fix:
      the `ADD COLUMNS` in item 9b, plus this extract change in `src/jobs/lakebase_extract.py`:
```python
CHAIN_COLS = ("action_type", "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id",
              "chain_seq", "prev_hash", "record_hash", "signature", "sig_alg", "key_id")
REVIEW_RECORD_COLS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                      "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash", "source", *CHAIN_COLS)
# in review_rows_to_records: NOT NULL check also for action_type, original_value, corrected_value, chain_id,
# chain_seq, prev_hash, record_hash (signature/sig_alg/key_id/reviewer_email/reviewer_sub stay nullable);
# extract ORDER BY chain_id, chain_seq. signature_hash is the engine content hash only for incl_correction rows;
# for alarm_event rows it is the same construction over the alarm as raised (signed fields + original alarm
# fields), so corrections.approval_problem must not be applied to them.
```
   c. **Conflict 3 (resolved in v1.6):** the role → `review_level` mapping is confirmed (item 9d); the extract copies
      `review_level` as stored, so historical `CDSE` rows from the earlier proposal are carried unchanged.
   d. **`ops.alarm_episode` + `installation_version` (proposal):** inclinometer episodes have `calib_id` NULL. Without
      `installation_version` on the episode, the app cannot approve them and withholds the value (`LINEAGE_INCOMPLETE`).
      DDL: `ALTER TABLE ops.alarm_episode ADD COLUMNS (installation_version TIMESTAMP COMMENT 'ref.incl_installation
      effective_from valid at event_ts (inclinometer episodes; NULL otherwise)')`, written by `alarm_evaluator`.
   e. **`docs/api_contract.md` (proposal):** add `POST /api/instruments/{id}/incl/corrections/{record_id}/review`,
      `GET /api/audit/signoffs/verify` (admin) and `GET /api/dams/{dam_id}/audit/signoffs/verify` (ACL). Response:
      `{ok, chain_id, n, first_broken_seq, reason}` per chain, with reason one of gap | prev_mismatch | hash_mismatch |
      head_missing | head_mismatch | signature_invalid | signature_missing | signature_downgrade.
   f. **Rebase:** this branch (`app/v1.5-audit-chain`) is built on e01e1ab → a9e7d56. a9e7d56 + e01e1ab must be rebased onto
      QA's fe4ed7b before any push (the DA reports a `docs/VERIFICATION.md` conflict). The app did not rebase.
11. **QA-043 a/b/c: inclinometer depth-grid QC (engine and app guard implemented).**
    Both layers use the expected interval-top grid. The engine change was assigned to App Dev;
    duplicate labels no longer resolve last-wins, rejected labels are never reduced, and missing
    intervals cannot contribute an implicit zero to cumulative displacement.
    - **App guard (this branch):** `backend/incl_depth.py check_profile` runs on every served profile (demo and SQL).
      The grid is the installation row valid at survey time: tops 0, L, … < casing_depth_m. Rows are flagged as follows:
      DEPTH_OFF_GRID, DEPTH_BELOW_CASING, DEPTH_DUPLICATE (every copy) and DEPTH_GRID_UNKNOWN rows are BAD, and their
      values are nulled. Each absent top gets a placeholder row with qa_flag MISSING and reason DEPTH_MISSING. Every
      cumulative (cum_a, cum_b, resultant) at or above a gap, a rejected row or a QC-BAD/MISSING row is nulled
      (CUM_GAP_BELOW), because an upstream sum may already include it. Increments stay shown. No row is dropped.
    - **Engine:** `batch.reduce_instrument` builds `interval_tops_m(casing_depth, L)` for the
      installation valid at survey time. Every duplicate copy is excluded before reduction, every
      expected top is emitted, and absent/current/baseline BAD or MISSING passes break the affected
      axis cumulative at and above the gap. Reasons include DEPTH_MISSING, BASELINE_DEPTH_MISSING,
      PASS_MISSING_A/B and CUM_GAP_BELOW_A/B. No first/last duplicate wins. Non-numeric/nonfinite
      labels fail closed with DEPTH_LABEL_INVALID. Tests: `tests/test_inclinometer_depth_grid.py`.
      The saved `app/docs/proposals/QA-043_engine_depth_grid.patch` is a historical proposal;
      the implementation additionally handles axis-specific gaps, negative/nonfinite labels and invalid grids.
      QA's strict xfail markers are intentionally unchanged.
    - **Vocabulary for the DA's qa_reason list:** DEPTH_MISSING and BASELINE_DEPTH_MISSING (MISSING); DEPTH_DUPLICATE,
      DEPTH_OFF_GRID, DEPTH_BELOW_CASING, DEPTH_GRID_UNKNOWN and DEPTH_LABEL_INVALID (BAD); CUM_GAP_BELOW (MISSING, applies
      to the cumulative only).
    - **Rule sources** (`/workspace/dam-refs`, cited, not copied):
      - `procedures/inclinometers.md` §4.0: CUM(d) = Σ Δ over every interval whose top is at depth ≥ d, summed
        from the fixed bottom [DGSI-DMM PDF p100 [printed p9-10], as cited in `inclinometers/user_tool/formula_map.md`].
        When an interval is absent, CUM is undefined at and above it.
      - `procedures/inclinometers.md` §7, "Missing depth / pass": MISSING (an engine rule).
      - `procedures/inclinometers.md` §3.1: the engine depth is the interval top from top of casing, on the L grid.
      - `inclinometers/user_tool/REPORT.md` erratum E9: `IFERROR(…,0)` turns bad input into a silent 0 displacement.
        That is the failure an implicit zero across a gap reproduces.
      - No manufacturer page prescribes software handling of missing, duplicate or off-grid rows. Treating
        DUPLICATE, OFF_GRID and BELOW_CASING as BAD is **UNVERIFIED (engineering judgment)**.
    - **Worked examples** (SYNTHETIC, L = 0.5 m, casing 5.0 m, so tops 0.0–4.5 m; every increment +1.0 mm; asserted
      in `app/tests/test_incl_depth.py` and `tests/test_inclinometer_depth_grid.py` (scaled synthetic inputs)):
      - **WE-043a (missing 3.5 m):** cum 4.5 = 1.0 mm and 4.0 = 2.0 mm; 3.5 m is MISSING. Cum at 0.0–3.0 m is
        withheld. A silent sum would show 9.0 mm at 0.0 m, understating the true value by the unknown increment.
      - **WE-043b (2.0 m twice, increments 1.0 and 9.0 mm):** both copies are BAD. Cum at 2.5–4.5 m = 5.0…1.0 mm,
        and cum ≤ 2.0 m is withheld. Last-wins would show 18.0 mm at 0.0 m, and first-wins 10.0 mm.
      - **WE-043c (extra 5.0 m row below the casing):** that row is BAD. The app guard cannot know whether the
        upstream sum included it, so it withholds every cumulative; an included row would show 11.0 mm at 0.0 m.
        With the engine patch the row is never reduced, so cum at 0.0 m = 10.0 mm. Labels offset +0.2 m: all ten
        rows are OFF_GRID BAD and all ten tops are MISSING.
    - **QA-042:** `gtilt`/`digipro` profiles now require explicit verified depth settings and output placement;
      missing or UNVERIFIED interpretations and off-grid converted tops are rejected. The stored-depth
      `contract` profile is separate. See root `docs/INCL_IMPORTERS.md` for the field contract, elevation/AT
      handling and citations to `procedures/inclinometer_depth_semantics.md` §§2–7. Parser stubs remain disabled.
12. **`cdse_ack_required` (Genie decision, v1.6; agreed with the DA, who writes the DDL; the app writes none).**
    The app relies on these DA-owned columns and reads them only when present:
    - `ref.threshold.cdse_ack_required BOOLEAN NOT NULL DEFAULT false` (per dam, per alarm rule);
    - `ops.alarm_episode.cdse_ack_required`: the rule flag, exposed on the view;
    - `ops.alarm_episode.cdse_ack_required_at_open`: the flag snapshotted by `alarm_evaluator` when the episode opens.

    The app reads `e.*` from `ops.alarm_episode` and `t.*` from `ref.threshold`, so no app SQL names these columns
    and an older schema keeps working (flag false, logged once). The onboarding publication outbox carries
    `cdse_ack_required` (bool) in each `thresholds` row. Asks for the DA:
    - the publish job writes the column;
    - the Lakebase/UC extract carries it;
    - document whether a new `threshold_version` is required when the flag changes (the app treats the flag as a
      property of the current rule plus the episode snapshot, so either choice is safe).
13. **Live-dam `cdse_ack_required` edits: what the DA's side needs (proposal; the app writes no DA SQL).** Threshold
    versioning (`threshold_version`, `effective_from` / `effective_to`, `is_current`, `set_by` in `ref.threshold`,
    `sql/ddl/02`) and the publish into `ref.*` are DA-owned. The app does the following:
    - It writes one `dam_app.ref_change_publication` row per apply:
      - `kind = 'ref.threshold.new_version'`, `status = 'pending'` (demo: `'applied'`), `proposal_id`, `requested_by`;
      - `payload = {kind, threshold_id, dam_id, supersedes_version: N, insert: {every canonical ref.threshold column of
        version N, with threshold_version: N+1, cdse_ack_required: <new>, effective_from: <apply time, UTC ISO-8601>,
        effective_to: null, is_current: true, set_by: <CDSE email>}}`.
    - It never edits version N.

    The DA publish job should, in one transaction:
    - if the current version of `threshold_id` is not `supersedes_version`, set `status = 'rejected_stale'` with a
      `message` and write nothing (the app checks this too, but UC is the authority);
    - otherwise INSERT `insert` as version N+1, close version N with the existing SCD2 pattern (`effective_to = N+1's
      effective_from`, `is_current = false`; it's the DA's decision whether that close-out counts as "editing" N), and
      set `status = 'applied'` and `applied_at`.

    The app's alarm reads then see N+1 through `is_current`. Further asks:
    - **Evaluator:** episodes keep their raising `(threshold_id, threshold_version)`. New episodes use N+1 and snapshot
      its flag into `cdse_ack_required_at_open`. The app ORs the snapshot with the current flag (§5a), so a clear
      never weakens an open episode.
    - **UC `audit.review_record`:** `ck_review_record_subject_decision` must allow `subject_type = 'alarm_rule'` with
      `decision IN ('propose', 'apply', 'reject')`. The engine's `ACTION_TYPES` and the extract must accept
      `alarm_rule_flag_propose`, `alarm_rule_flag_apply` and `alarm_rule_flag_reject` (app: `audit_chain.APP_ACTION_TYPES`).
      Until then the extract would refuse these chain rows. These action types change no hash rule; the canon-2 actor role (item 14) applies to every new record.
    - **Lakebase:** mirror `alarm_rule_flag_proposal` and `ref_change_publication` in `lakebase/001_app_schema.sql` if
      the DA wants the data-layer script to own them. The app creates them itself (`state.py`).
    - **Permissions:** the publish job runs as the setup service principal. Signed-in users never write `ref.*`.
14. **Actor app role in chained records (v1.6, DA follow-up 1; proposal, the app writes no DA SQL). Field names chosen
    by the app and FLAGGED:** `sql/migrations/v1_6_003_alarm_rule_flag.sql` (e78c62b) adds no actor-role column, so
    the names below are the app's. Rename before the DA lands the DDL if the DA prefers others. The app's own
    `review_signoff` already has them (`state.py`, ADD COLUMN IF NOT EXISTS).
    - **Columns** (UC `audit.review_record` and Lakebase `review_signoff` in `lakebase/001_app_schema.sql`):
      `actor_app_role STRING` (nullable: NULL on canon-1 rows) and `canon_version INT` (nullable: NULL = canon 1).
    - **CHECK**, alongside `ck_review_record_chain`:
      `canon_version IS NULL OR (canon_version = 2 AND actor_app_role IS NOT NULL AND length(trim(actor_app_role)) > 0)`.
      The vocabulary check (sorted, unique, comma-joined tokens from `viewer|engineer|dso|dam_admin|admin|cdse`) is
      enforced by the verifier. Optionally add it to the DDL as `actor_app_role RLIKE
      '^(viewer|engineer|dso|dam_admin|admin|cdse)(,(viewer|engineer|dso|dam_admin|admin|cdse))*$'`, which leaves
      ordering to the verifier.
    - **Verifier canon version 2:** `dam_safety.audit_chain.record_hash` / `verify_records` need the same versioning
      as the app's `audit_chain.record_hash` / `verify_chain`, with the same reason codes (`actor_role_missing`,
      `actor_role_invalid`, `canon_downgrade`, `canon_unknown`). Until then, the engine reports `hash_mismatch` on
      every canon-2 row.
      - The DA fixtures stay canon 1 and need no change. The app-side canon-2 vector is
        `app/tests/fixtures/review_chain_canon2_vector.json`.
      - `src/jobs/verify_review_chain.py` must SELECT the two columns.
      - The §7.13 authority verifier's `app_roles` callable can read `actor_app_role` from the record itself (it is
        recorded at `signed_ts`). The role remains an app-recorded value, so the DA decides whether that counts as an
        independent source.
    - **Extract column order:** `lakebase_extract.CHAIN_COLS` gains `actor_app_role, canon_version` appended after
      `key_id`, giving 24 -> 26 columns in this order: `review_id, dam_id, subject_type, subject_id, review_level,
      decision, comment, reviewer_id, reviewer_pe_license, signed_ts, signature_hash, source, action_type,
      reviewer_email, reviewer_sub, original_value, corrected_value, chain_id, chain_seq, prev_hash, record_hash,
      signature, sig_alg, key_id, actor_app_role, canon_version`. The UC DDL adds them in the same order, after
      `key_id`.
      - Both stay out of `REVIEW_REQUIRED`, because canon-1 rows have them NULL.
      - The extract must pass them through unchanged (`canon_version` as an int). Dropping them turns every canon-2
        row into a `hash_mismatch` in UC.
    - **QA impact:** `qa/test_qa039_chain_adversarial.py`
      (`test_chain_fields_match_the_contract_and_hash_rederives_independently`,
      `test_full_recompute_by_privileged_actor_is_a_documented_limit`) re-derives app-written records with the canon-1
      field set only. Those records are now canon 2, so QA's helper needs the canon-2 rule above. The app did not edit
      `qa/`.
15. **Go-live and N+1 threshold rows carry the ref.threshold NOT NULL columns (v1.6, DA follow-up 2).** The columns
    are `dssmp_version_id` (STRING), `tier` (`DESIGN_BASIS | THRESHOLD | AL1..ALn`), `deadband` (DOUBLE, hysteresis in
    the rule's own unit, >= 0), `persistence_on` and `persistence_off` (INT, consecutive readings to raise / to clear,
    >= 1).
    - **Go-live:** the wizard's thresholds step requires all five and shows their units. `onboarding.validate` makes
      each missing or invalid value an error, so go-live returns 422. `golive_payload` publishes exactly what was
      entered in each `thresholds` row and raises rather than default anything.
    - **N+1 flag apply:** copies all five from version N (§5a). If version N lacks one, the app returns 409. The DA
      backfills it; the app invents nothing.
    - **Demo seed:** sets `dssmp_version_id = DSSMP-<dam>-2026`, `tier = AL<level>`, `deadband = 2 % of the limit` and
      `persistence_on / off = 2 / 3`. The BFK cyclic rule uses `AL1, 0.05 mm, 1 / 1`. These values are demo data
      only and are not used by the app's alarm logic.
    - **DA:** the publish job inserts these columns from the outbox `insert` / `thresholds` rows as given. The app
      now checks `dssmp_version_id` against `ref.dssmp_version` (item 18).
16. **Second approver for cdse_ack_required edits (PROVISIONAL app setting `CDSE_FLAG_REQUIRE_SECOND_APPROVER`,
    §5a; a note for the DA, no DA change needed now).** If the setting goes on, the engine and the chain verifier
    could enforce the same rule independently of the app.
    - Rule: for every `alarm_rule_flag_apply` record whose `corrected_value.proposal_id` references a proposal,
      applier ≠ proposer. Compare the apply record's `reviewer_id` (and lower-cased `reviewer_email`) with the
      matching `alarm_rule_flag_propose` record of the same `proposal_id` in the same dam chain.
    - An apply with no `proposal_id` (a direct set/clear) would then be an error for records signed while the setting
      was on.
    - The setting is app config and is not recorded in the chain, so the DA would need its effective period, e.g. a
      date from which the rule applies, or a `ref.*` policy row. The DA asked (2026-11-09) NOT to write
      `CDSE_FLAG_REQUIRE_SECOND_APPROVER` into the chain yet, so no chained record carries it.
    - Fits the §7.13 authority verifier (`rule_flag_authority_problems`) as a new failure code such as
      `self_approval`.
17. **Lakebase split ownership: state.py patch applied (DA `tools/patches/v17_lakebase_owner_state_init.patch`,
    regenerated version in e8cc437, which replaces the 0274aeb one). App-side review.**
    - **Applied.** The 0274aeb version had conflicted on the canon-2 column typing. The regenerated patch is
      identical apart from that context line. Applying it to e8cc437's state.py gives exactly the app's state.py
      minus the app's canon-CHECK verification below, so the two are reconciled. Also applied:
      `tools/patches/v17_canon2_app_contract_test.patch` (`test_contract_v15`: 26 extract columns,
      `CHAIN_FIELDS_V2`).
    - **Guards are kept:**
      - Init still runs in ONE transaction after `pg_advisory_xact_lock(4702045)`; the patch's ownership probes run
        inside it.
      - On an app-owned table (local / sqlite / pre-split Lakebase), nothing changed: the app's own row and TRUNCATE
        triggers.
      - On the owner-owned table, the app runs no DDL. It verifies the following and fails closed (RuntimeError
        "re-run lakebase/001_app_schema.sql"):
        - the v2 and canon-2 columns;
        - an enabled row guard and an enabled TRUNCATE guard (`t_noupd_*` / `t_notrunc_*`, or the app's
          `t_app_*`), with no disabled trigger;
        - the chain index.
    - **App addition inside the patch (trivial):** the verification now also requires the validated CHECK
      `ck_review_signoff_canon`. Without it, a canon-2 row with no `actor_app_role` could be stored on Lakebase.
    - **No owner DDL left at app startup:**
      - `CREATE SCHEMA` is skipped when the schema is foreign-owned.
      - The canon ALTERs, constraint, chain index and `app_forbid_mutation` / trigger DDL sit after the owner-owned
        `return`.
      - The remaining statements touch only app-owned tables (`app_user` user_id, `CREATE TABLE IF NOT EXISTS`,
        which needs the granted schema CREATE).
      - Verified live on PostgreSQL 17 as a NOSUPERUSER, non-member LOGIN role
        (`app/tests/test_lakebase_owner_live.py`, provisioned by lakebase/001 alone):
        - a statement spy over a full init shows no ALTER / CREATE INDEX / TRIGGER / FUNCTION / SCHEMA DDL on owner
          objects. The only statement naming review_signoff is the no-op `CREATE TABLE IF NOT EXISTS`, which needs
          the granted schema CREATE;
        - init runs twice, and canon 2 appends and verifies;
        - UPDATE, DELETE, TRUNCATE, DISABLE TRIGGER and DROP CONSTRAINT are all refused with 42501;
        - a canon-2 row without a role is refused with 23514;
        - init refuses a dropped CHECK, a disabled guard and a dropped column.
    - **UPDATE / DELETE audit (nothing needs a new grant):** the app updates or deletes only tables it owns under the
      DA model:
      - `app_user`, `dam_role`, `alarm_ack`, `onboarding_draft`;
      - `review_chain_head` (head pointer; tail deletion is impossible because review_signoff refuses DELETE);
      - `alarm_rule_flag_proposal` (status pending → applied / rejected).

      `review_signoff` is INSERT-only. So proposal status updates do NOT conflict with the INSERT-only tables. The
      evidence of every decision is the chained `alarm_rule_flag_*` record; the proposal row is workflow state.
      **Optional, if the DA wants proposals append-only too:** freeze `alarm_rule_flag_proposal` after insert and
      add an INSERT-only `alarm_rule_flag_decision (proposal_id PK, status, decided_by, decided_by_id, decided_at,
      decision_rationale, pub_id, new_threshold_version)`. The PK makes a second decision impossible; status =
      decision row or 'pending'. The app can switch to this when the DA adds it to the owner DDL. Nothing was
      granted.
    - **DA needs:**
      - (a) Done in e8cc437: `lakebase/001` adds the canon-2 columns and `ck_review_signoff_canon`. Before it, the
        patched app refused to start ("missing columns ['actor_app_role', 'canon_version']"), as intended.
      - (b) The publish job must be able to set `status` / `applied_at` / `message` on `ref_change_publication` and
        `onboarding_publication`. 001 grants the extract role only SELECT; it needs UPDATE on those columns, or an
        append-only publication-result table.
      - (c) The extract / publisher role cannot see tables the app creates AFTER 001 ran (`alarm_rule_flag_proposal`,
        `ref_change_publication` on a first deploy). `GRANT SELECT ON ALL TABLES` covers existing tables only. It
        needs `ALTER DEFAULT PRIVILEGES FOR ROLE <app role> IN SCHEMA dam_app GRANT SELECT ON TABLES TO <extract
        role>`, or re-running 001 after the app's first start.
      - (d) Guards are accepted as enabled when `tgenabled` is O or A. The app role cannot set
        `session_replication_role`, so O suffices against the app. ALWAYS is the DA's extra hardening, re-checked by
        `validate_append_only.sql`.
      - (e) `tools/patches/v17_lakebase_owner_state_init*.{patch,py}` can be removed; the app did not delete DA files.
18. **`dssmp_version_id` must be a `ref.dssmp_version` row of the same dam (app check, fail closed).** The DDL table
    is `ref.dssmp_version` (sql/ddl/04), not `ref.dssmp`. New `Analytics.dssmp_versions(ids)`: SQL `SELECT
    dssmp_version_id, dam_id, status FROM ref.dssmp_version WHERE dssmp_version_id IN (...)`, read on behalf of the
    user.
    - **Go-live:** every threshold's id must exist and belong to the draft's dam. Otherwise there is a `thresholds`
      error in validate and go-live returns 422.
    - **Flag-apply N+1:** the copied id is re-checked. Otherwise 409, nothing recorded.
    - An unreadable table fails closed: "cannot be verified".
    - **v1.7: status IS judged (DA contract, reason codes).** `dssmp_not_found`, `dssmp_other_dam`, `dssmp_draft`
      (status 'draft') and `dssmp_superseded` (status 'superseded' OR `effective_to <= now`, now = the go-live /
      apply time), plus the DA publisher's `dssmp_not_yet_effective` (effective_from > now) and
      `dssmp_status_unknown` (any other status). Same order as `src/dam_safety/dssmp_check.dssmp_problem` (e551a88).
      App-only fail-closed code: `dssmp_unverifiable` (table unreadable). The DA's `dssmp_missing` is the app's
      NOT NULL check (item 15). Issues carry a `code` field; messages
      start with the code. The read now selects `dssmp_version_id, dam_id, version_no, effective_from, effective_to,
      status`, plus `Analytics.dssmp_versions_for_dam(dam_id)`: go-live also needs the DAM to have at least one
      approved, non-superseded version (step `dssmp`), even with no thresholds.
    - **Flag-apply N+1 uses the same codes (409).** Consequence: once the DA load job supersedes a dam's DSSMP
      version, flag edits on rules still set under the old version are refused (`dssmp_superseded`) until the DA
      re-versions those rules under the new DSSMP. Open question for Genie/DA: intended? **Decided (Genie): yes, no
      grace path; the app offers a bulk re-version (item 21).**
    - **Workflow:** answered by item 19 (wizard registration → CDSE approval → outbox → DA load job).
    - **Demo:** the seed has `DSSMP-<dam>-2026` (approved) for each dam. Tests register `DSSMP-NEW-2026` for the
      onboarded dam.
    - **Grant:** the app's users need SELECT on `ref.dssmp_version` (the reads are OBO). The DA's publisher checks
      it too.
19. **DSSMP version registration (v1.7; wizard step 2 `dssmp`; DA contract fixed).** The app NEVER writes
    `ref.dssmp_version`.
    - **Flow:** a dam_admin of the dam or a platform admin proposes. The dam's CDSE (`ref.v_dam_reviewer_authority`,
      fail closed) approves or rejects, with a rationale (422 without). Each step appends ONE canon-2 chain record
      atomically with the workflow change: `action_type` `dssmp_version_propose` / `dssmp_version_approve` /
      `dssmp_version_reject`, `decision` propose / approve / reject, `subject_type` `dssmp_version`, `subject_id` =
      `dssmp_version_id`. `original_value` / `corrected_value` carry the registration snapshot. **DA:** add the 3
      action types and the subject type to the UC CHECKs and the engine's action types.
    - **On approve, in the same transaction:** ONE row is inserted into the outbox at `pending`, with
      `approval_record_hash` = the approve record's `record_hash`.
    - **Final table name: `dam_app.dssmp_registration_outbox`** (Lakebase schema `dam_app`; app tables are
      unprefixed). Columns exactly as agreed: `outbox_id` (TEXT, `DSO-<uuid>`), `dam_id`, `dssmp_version_id`,
      `version_no` INTEGER, `effective_from` (TEXT ISO UTC `YYYY-MM-DDTHH:MM:SSZ`), `change_reason`, `source_ref`,
      `document_uri`, `document_sha256` **NOT NULL**, `proposed_by`, `approved_by` (emails), `approved_at` (ISO UTC),
      `approval_record_hash`, `status`, `reason_code`, `message`, `processed_at`.
    - **Status: `pending | loaded | rejected` only** (`CHECK (status IN ('pending', 'loaded', 'rejected'))` in the
      app DDL, in `lakebase/001` and in the `v1_7_004` block since DA 6709371; `state.OUTBOX_STATUSES`). There is no
      `rejected_invalid` in this outbox; that spelling belongs to `ref_change_publication`
      (`pending|applied|rejected_stale|rejected_invalid`). The DA load job writes `loaded`, or `rejected` +
      `reason_code` (the first code) + `message` (`<code>: <detail>`); the app's demo stand-in does the same.
      - **App reads contract values only.** `rejected` is a rejection; any other value (e.g. a legacy
        `rejected_invalid` row on a database not yet migrated) is NOT treated as loaded or as a rejection: go-live
        stays blocked (the version is not in `ref.dssmp_version`) and the wizard note names the value as "not a
        contract status ... v1_7_004". The UI badge shows it as `bad` with "(not a contract status)".
      - **Existing table with the old 4-value CHECK** (same for `dam_cdse_outbox`, item 20, migration
        `v1_7_008`). The app never ALTERs it. `CREATE TABLE IF NOT EXISTS` is a
        no-op on an existing table, and on Lakebase the table is owned by `dam_lakebase_owner` (the app runs no DDL
        on it, item 22 / owner model). At startup `StateStore._legacy_outbox_check()` reads the CHECK
        (`pg_get_constraintdef`; sqlite: `sqlite_master`) and logs ONE WARNING naming the extra values, the owner
        ("owned by the data-layer owner role" | "app-owned"; `StateStore.legacy_outbox_checks` lists
        `<table>:<value>`) and the fix: run the DA's
        `lakebase/migrations/v1_7_004_dssmp_outbox_contract.sql` (or re-run `lakebase/001`), which maps existing
        `rejected_invalid` / `rejected_stale` rows to `rejected` (reason kept) and installs the 3-value CHECK. Startup
        does not fail: the app only ever inserts `pending` (allowed by both CHECKs). Live test:
        `app/tests/test_lakebase_owner_live.py::test_old_outbox_status_check_is_reported_never_altered_and_v1_7_004_converges_it`
        (and `..._cdse_outbox_..._v1_7_008_...`).
        A local demo/dev sqlite file created before this change keeps its old CHECK (sqlite cannot alter a CHECK);
        delete the file to recreate it.
    - **Guards (app-side, while the app owns the table):** triggers refuse DELETE/TRUNCATE and any UPDATE that
      changes a column other than `status, reason_code, message, processed_at`. The app inserts only and never
      updates (demo mode excepted: a stand-in loader sets the bookkeeping columns).
    - **DA decisions to provision:**
      - a new NOLOGIN publisher role with SELECT and column-level `UPDATE (status, reason_code, message,
        processed_at)` on the outbox;
      - default SELECT for the extract;
      - SELECT on `ref.dssmp_version` for app users (OBO reads).
      If the DA moves the outbox to `dam_lakebase_owner`, the app needs INSERT+SELECT only. The app then skips its
      guard DDL (`_pg_not_owned`) and the DA owns the guards.
    - **Load job (DA):** inserts `ref.dssmp_version` (status approved, `loaded_from_outbox_id`, v1_7_003 columns),
      closes the dam's previous approved version, and sets outbox `loaded` or `rejected` + `reason_code` +
      `message`. The UI shows the rejection reason_code/message on the wizard step and the CDSE panel.
    - **Go-live:** the version must be LOADED in `ref.dssmp_version` as approved and not superseded.
      **Approved-but-not-loaded fails closed** (`dssmp_not_found`, with the message naming the pending/rejected
      outbox row).
    - **Workflow table** `dam_app.dssmp_registration` (app-owned, mutable: pending → approved | rejected; one open
      registration per id). **Document table** `dam_app.dssmp_document`: bytes as base64 TEXT, at most 20 MB,
      sha256 computed by the server. An uploaded document's `document_uri` is
      `lakebase:dam_app.dssmp_document/<document_id>`. DA: does the load job copy it to a UC volume, or keep this
      URI?
    - **API:**
      - `POST /api/dams/{dam}/dssmp-documents` `{filename, content_type, content_b64}` → `{document_id, sha256,
        size_bytes, document_uri}` (proposers only; 413 above the limit);
      - `GET .../dssmp-documents/{id}` (viewer / CDSE; header `X-Content-SHA256`);
      - `GET /api/dams/{dam}/dssmp-registrations` → registrations + outbox + ref state, `can_propose`, `can_decide`,
        `approval_policy`;
      - `POST /api/dams/{dam}/dssmp-registrations` `{dssmp_version_id, version_no, effective_from, change_reason,
        source_ref, document_uri [+document_sha256] | document_id, rationale}`: 409 `dssmp_exists` /
        `dssmp_other_dam` if the id is already in ref, `dssmp_unverifiable` if ref is unreadable;
      - `POST .../{registration_id}/approve` and `/reject` `{rationale}`;
      - `GET /api/dssmp-registrations/pending` (the caller's CDSE queue).
    - **Not collected:** a separate title or revision date (the DA contract has no columns for them; `version_no`
      and `effective_from` cover them). The `rationale` is chain-only.
    - **Self-approval (PROVISIONAL):** follows the same `CDSE_FLAG_REQUIRE_SECOND_APPROVER` switch. When on, the
      proposer can never approve (403 `self_approval_forbidden`) and can only reject. The setting is NOT written to
      the chain (DA request).
    - **New dams:** before go-live the dam has no stored app roles, so a platform admin proposes. The DA must expose
      the new dam's CDSE in `ref.v_dam_reviewer_authority` BEFORE go-live, or nobody can approve (fail closed). The
      first CDSE itself comes from the bootstrap in item 20.
    - **CONFLICT with the DA's 86993d0 / 18ebff2 (committed after the contract above; NOT resolved in the app).**
      `lakebase/001` creates `dssmp_registration_outbox` owner-owned. The app has INSERT/SELECT only, so the app's
      CREATE TABLE IF NOT EXISTS and guard DDL are no-ops/skipped there. Its columns differ from the agreed contract:

      | DA 86993d0 (lakebase/001, `dssmp_registration.OUTBOX_COLS`) | Agreed contract (what the app inserts) |
      |---|---|
      | `requested_by`, `requested_at` | `proposed_by` |
      | `supersedes_dssmp_version_id` (NULL = first version; stale re-check) | (none) |
      | `applied_at` | `processed_at` |
      | (no column) | `reason_code` |
      | status `pending\|applied\|rejected_stale\|rejected_invalid` | status `pending\|loaded\|rejected` |

      `document_sha256` is NOT NULL in the DA table; the load job's codes go into `message` as `<code>: <detail>`.
      **Until one side changes, the app's approve INSERT fails against the DA-provisioned table in live Lakebase**
      (the approve transaction rolls back, chain record included, so nothing is half-written). Decision needed from
      Genie/DA: either the DA renames to the contract, or the app inserts `requested_by`/`requested_at`/
      `supersedes_dssmp_version_id` (from `ref.dssmp_version`, fail closed) and reads `applied_at`/`message`. The
      UI shows the contract statuses only (above).
    - **Already aligned with 86993d0 (compatible with both):**
      - `approved_at` / `approved_by` = the approve record's `signed_ts` / reviewer (load-job
        `approval_record_mismatch`);
      - `document_sha256` required for URI documents too (DA NOT NULL, engine `dssmp_approval_fields_missing`);
      - `corrected_value` carries `dam_id` and `proposal_id` (= `registration_id`), so the engine links
        approve/reject to the propose record. `app/tests/test_dssmp_registration.py` runs the app's records through
        `dam_safety.dssmp_authority.dssmp_authority_problems`: no findings, and a forged dam_admin approve is
        caught.
    - **Proposer rule:** the engine (86993d0) lets the dam's CDSE propose or approve directly, with no proposal.
      The app requires a dam_admin/admin proposal followed by a CDSE decision; that is a subset the engine accepts.
      Genie: should the app also let the CDSE register directly? That would also give new dams a path without the
      platform admin. **Decided (Genie): yes, see below.**
    - **Outbox decision (Genie): the AGREED contract wins.** The app keeps its INSERT on the contract columns
      (`proposed_by`, `reason_code`, `processed_at`; status pending|loaded|rejected). The DA migrates
      `lakebase/001` / `dssmp_registration.OUTBOX_COLS` to it. Until then the live-PG approve INSERT fails against
      86993d0's table (expected). **Resolved by DA 91e7979** (`lakebase/001` + `v1_7_004_dssmp_outbox_contract`):
      the table now has exactly the contract columns and status CHECK; the live-PG test inserts a contract row with
      the approve record as the restricted app role (no longer xfail).
    - **CDSE direct registration (Genie):** `POST /api/dams/{dam}/dssmp-registrations/direct`, the dam's CDSE only
      (fail closed), same body as propose, rationale required. In ONE transaction it writes ONE
      `dssmp_version_approve` record with NO prior proposal (the engine's "CDSE may approve directly";
      `corrected_value.proposed_by` = the CDSE, `direct: true`), the registration (approved) and ONE pending outbox
      row. It mirrors the cdse_ack_required direct apply. With `CDSE_FLAG_REQUIRE_SECOND_APPROVER` on it returns
      403 `second_approver_required` (PROVISIONAL), same as the flag edits. dam_admin / admin still only propose.
      The CDSE may also upload the document.
    - **Go-live readiness: `ref.v_dam_go_live_readiness` (DA sql/ddl/10b; fail closed).** Go-live (and validate)
      requires BOTH:
      1. the dam's ONE row in the view with `ready = true` (`onboarding.go_live_readiness_issues`, step `users`):
         - `ready` false or NULL: 422 with `blocking_reason` as the code (e.g. `no_cdse_in_reviewer_authority`;
           `go_live_not_ready` if the view gives no reason);
         - no row (dam neither in `ref.dam` nor in `ref.dam_cdse_assignment`): `go_live_readiness_missing`;
         - view unreadable or no lookup: `go_live_readiness_unverifiable`;
      2. the app's own approved-DSSMP check (`dssmp_issues`, unchanged): every rule's version and the dam itself must
         have a LOADED, approved, in-effect `ref.dssmp_version`. The view's `has_current_dssmp` is informational
         only; it never overrides the app's check.
      This REPLACES the direct `ref.v_dam_reviewer_authority` check (`cdse_authority_missing` is no longer a go-live
      code). Reads: `SELECT dam_id, ready, has_current_dssmp, blocking_reason FROM ref.v_dam_go_live_readiness
      WHERE dam_id = :dam_id` (analytics `go_live_readiness`, OBO).
    - **DSSMP codes mirrored from the DA (`dam_safety.dssmp_check.CODES`):** `dssmp_missing` (a rule with no
      `dssmp_version_id`: a coded go-live issue, and the flag-apply 409 `{code: dssmp_missing}`), `dssmp_not_found`,
      `dssmp_other_dam`, `dssmp_draft`, `dssmp_superseded`, `dssmp_not_yet_effective`, `dssmp_status_unknown` (+ the
      app's `dssmp_unverifiable`). The UI explains every code (`frontend/src/codes.ts`, parity test
      `app/tests/test_ui_codes.py` against the DA's declared tuples).
    - **Flag-apply 409 `dssmp_superseded`:** the body is `{code, superseded_dssmp_version_id, threshold_id,
      affected_rules: [threshold_id of every current rule of the dam on that version], message}` and states that
      the rules must be re-versioned. There is no grace path. Other codes keep the plain 409 text.
    - **Load results shown in the UI:** `volume_uri` is shown when the DA's load adds it (outbox or `ref.dssmp_version`;
      the app does not SELECT it from UC until the DA confirms which table carries it). `reason_code
      dssmp_document_hash_mismatch` is a load rejection with its own message: re-register with the correct
      document.
20. **Dam CDSE: first-CDSE bootstrap and CDSE changes (v1.7, Genie).** The app NEVER writes `ref.dam`.
    - **Bootstrap:** `POST /api/dams/{dam}/cdse/assign` `{cdse_id, reason_code, rationale}`.
      - Who and when: the PLATFORM ADMIN only, and only while the dam has no CDSE (`ref.v_dam_reviewer_authority`
        empty AND no designated CDSE, re-read under the dam lock) and is not live (no `ref.dam` row in the app's dam
        list).
      - Refusals: 403 for any other role; 409 `dam_live` / `cdse_exists` / `cdse_assign_pending` (an assignment
        waiting for the load job); 409 `cdse_unverifiable` / `dam_state_unverifiable` when the lookups fail (fail
        closed); 422 `reason_code_invalid`, missing rationale, or a `cdse_id` that is not an e-mail principal.
      - It appends ONE chain record: `action_type dam_cdse_assign`, `decision assign`, `subject_type dam`,
        `subject_id` = dam_id. In the same transaction it writes the change row (approved) and ONE outbox row.
    - **Changes:** `POST /api/dams/{dam}/cdse/changes` `{cdse_id, reason_code, rationale}` is a platform-admin
      PROPOSAL (`dam_cdse_change_propose`; 409 `cdse_authority_missing` when the dam has no CDSE yet, so use assign;
      `cdse_unchanged`; `cdse_change_pending`). `POST .../cdse/changes/{change_id}/approve|reject` `{rationale}` is
      for the dam's CURRENT CDSE only (`dam_cdse_change_approve` / `_reject`, subject `dam`). It returns 409
      `cdse_stale` if the designated CDSE no longer equals the proposal's `previous_cdse_id`. Self-approval follows the
      PROVISIONAL `CDSE_FLAG_REQUIRE_SECOND_APPROVER` (403 `self_approval_forbidden`). Approve inserts ONE outbox row
      in the same transaction. `GET /api/dams/{dam}/cdse` returns the state; `GET /api/cdse-changes/pending` is the
      caller's queue.
    - **`reason_code` enum** (`dam_cdse_change.change_reason_code`, outbox `change_reason_code`):
      `initial_appointment`, `resignation`, `retirement`, `reassignment`, `incapacity`, `regulatory_direction`,
      `correction`.
    - **Outbox `dam_app.dam_cdse_outbox`** (same pattern and guards as item 19): `outbox_id` (`DCO-<uuid>`),
      `dam_id`, `kind` (`assign`|`change`), `previous_cdse_id` (NULL for assign), `new_cdse_id`,
      `change_reason_code`, `proposed_by`, `approved_by`, `approved_at` (= the deciding record's `signed_ts`),
      `approval_record_hash`, `status` (`pending`|`loaded`|`rejected`), `reason_code`, `message`,
      `processed_at`. The app reads contract statuses only: only `rejected` closes a first-CDSE assignment, any
      other value keeps it open (409 `cdse_assign_pending`, fail closed). CHECK `status IN ('pending', 'loaded',
      'rejected')` in the app DDL and in `lakebase/001` (v1_7_005 CREATE + `ck_dam_cdse_outbox_status` from block
      v1_7_008, DA d6d3c9c); no `rejected_invalid`. An existing table with the old CHECK is reported at startup and
      never altered by the app, exactly as in item 19; the DA's `v1_7_008_cdse_outbox_status.sql` converges it
      (rows -> `rejected`, reason kept). The app inserts only. The guard triggers allow UPDATE of `status, reason_code, message,
      processed_at` only, and refuse DELETE/TRUNCATE.
    - **DA to provision:**
      - the 4 action types and subject `dam` in the UC CHECKs and the engine;
      - the publisher role with SELECT + column-level UPDATE of the 4 bookkeeping columns;
      - the load job: verify the record (hash, signer = admin for assign, the then-current CDSE for change). For
        assign, refuse if `ref.dam.cdse_id` is already set (`cdse_exists`); for change, refuse if it is not
        `previous_cdse_id` (`cdse_stale`). Then set `ref.dam.cdse_id` and expose it in
        `ref.v_dam_reviewer_authority`.
    - **`previous_cdse_id` is ONE value (v1.7 steering): the dam's current DESIGNATED CDSE**, i.e. `ref.dam.cdse_id`
      (or the in-force `ref.dam_cdse_assignment`), never a list of reviewer-authority principals (the view also
      holds `MEMBERSHIP_CDSE` grants, which are not "the" CDSE). The app reads it through the view's `cdse_id`
      column (analytics `designated_cdse`: `SELECT DISTINCT cdse_id ... WHERE authority_source IN
      ('DAM_CDSE_ASSIGNMENT','DAM_CDSE_ID')` and in force; the app never reads `ref.dam`; more than one value is
      ambiguous and fails closed with 409 `cdse_unverifiable`).
      - **Read under the lock:** propose reads it to build the row, then re-reads it inside `check(q)` AFTER the
        per-dam advisory lock (item 22); any difference is 409 `cdse_stale` with nothing written. So the stored
        value is exactly the value as read under the lock. Approve re-reads it the same way and compares it with the
        stored `previous_cdse_id` before inserting the outbox row.
      - The same single value is stored in `dam_cdse_change.previous_cdse_id`, `dam_cdse_outbox.previous_cdse_id`,
        the propose record's `original_value.cdse_id` and `corrected_value.previous_cdse_id`. The DA's `cdse_stale`
        comparison (`_set(previous_cdse_id)`) and `approval_record_mismatch` (set compare) accept a single value
        unchanged.
    - **Load refusal codes shown in the UI** (`dam_cdse_outbox.reason_code`, `dam_safety.cdse_load.CODES`):
      `missing_required_column`, `invalid_value`, `approval_record_missing`, `approval_record_wrong_action`,
      `approval_record_mismatch`, `cdse_exists`, `cdse_stale`, `approval_not_authorized`.
    - Go-live readiness is item 19's `ref.v_dam_go_live_readiness` gate (`no_cdse_in_reviewer_authority` until the
      CDSE is loaded). Demo mode has a stand-in loader with the same refusals.
21. **Bulk re-version of rules after a DSSMP revision (v1.7, Genie).** The 409 `dssmp_superseded` stays (no grace
    path).
    - **Affected rules** = the dam's current `ref.threshold` rows whose `dssmp_version_id` is missing or is not the
      new version. The DSSMP approval screen shows their count and ids for each pending registration
      (`affected_count`, `affected_rules`); `GET /api/dams/{dam}/rule-reversions?to=<vid>` gives the preview and the
      proposals. For these DISPLAY counts the app calls the DA's `ref.dssmp_affected_rules(:dam_id, :to)` and falls
      back to its own computation if the function is missing or errors (`affected_source`: `ref.dssmp_affected_rules`
      | `app`). The proposal and its approval always use the app's computation from the current rules, because they
      build the N+1 payloads.
    - **Publisher outcomes per proposal:** the preview's `proposals[].publications` lists `{pub_id, threshold_id,
      status, code, message}` for each queued publication. `code` is parsed from the DA's `<code>: <detail>`
      message. The UI explains `reversion_approval_missing`, `reversion_approval_wrong_action`,
      `reversion_approval_mismatch`, `reversion_not_authorized`, `reversion_fields_changed`, `payload_invalid`
      (+ `unknown_kind`, `missing_not_null_column`, `unknown_columns`).
    - **Proposal:** `POST /api/dams/{dam}/rule-reversions` `{to_dssmp_version_id, rationale}`. Who: dam_admin,
      platform admin, or the dam's CDSE. It is ONE proposal covering EVERY affected rule (`rule_reversion_propose`).
      DSSMP approve / direct registration also accept `propose_reversion: true`. That proposal is created after the
      approval in its own transaction; if the version is not loaded yet, `reversion_error` is returned and the
      proposal is made as a follow-up. Refusals: target not loaded, approved and in effect (409 with the
      `dssmp_findings` code), `nothing_to_reversion`, `reversion_pending`, and rules lacking NOT NULL columns
      other than `dssmp_version_id` (a `dssmp_missing` rule is exactly what a re-version fixes).
    - **Decision:** `POST .../{proposal_id}/approve|reject` `{rationale}`, the dam's CDSE only.
      - Self-approval follows the provisional setting.
      - Approve re-checks the target and every rule. Any rule changed or not covered gives 409 `reversion_stale`
        and nothing is queued.
      - It then appends ONE `rule_reversion_approve` record (subject `rule_reversion`, subject_id = proposal_id;
      `corrected_value.publications` lists every publication id and new version) and, in the SAME transaction,
      queues one N+1 `ref.threshold.new_version` publication per rule. Values are unchanged (limits, tier,
      deadband, persistence, `cdse_ack_required`) except `dssmp_version_id` = target, `threshold_version` + 1,
      `effective_from` = now, `set_by`.
    - **Publication payload additions:** `cause: "dssmp_reversion"`, `reversion_proposal_id`, `approval_record_hash`.
      DA: the publisher and engine must accept a `rule_reversion_approve` record as the authority for these
      publications (subject `rule_reversion`, not `dssmp_version`, which the engine reserves for the dssmp actions).
      Add the 3 action types and the subject type to the CHECKs.
    - Alarms and open episodes are not touched (the flag is carried unchanged; the ack rule ORs the episode
      snapshot). `GET /api/rule-reversions/pending` is the caller's CDSE queue.
22. **QA-056: concurrent state-checked writes serialize per dam (v1.7).** Two concurrent `dam_cdse_assign` POSTs
    both returned 200, each writing a chain record and an outbox row. Now exactly one wins, and the loser gets 409
    and writes nothing.
    - **Lock:** every chained write (`routers/signoffs.append`) runs
      `pg_advisory_xact_lock(DAM_LOCK_NS = 4702047, hashtext('dam_app.dam:' || dam_id))` FIRST in its transaction.
      This two-key form is a separate advisory keyspace from the single-bigint init lock `INIT_LOCK_KEY 4702045`,
      and class 4702047 is distinct from the DA chain-lock class `CHAIN_LOCK_CLASS 4702046`.
      The route's state checks then re-run on the transaction's own connection (`check(q)`), and only after that is
      the chain locked. **The DA's `v17_chain_head_log_state.patch` (residual (a), append-only
      `review_chain_head_log`) is applied in the app with this order:** `lock_dam` → `check(q)` →
      `pg_advisory_xact_lock(4702046, hashtext(chain_id))` → read the head log top (seed from the legacy head or
      genesis) → INSERT `review_signoff` → INSERT the head-log row → upsert the legacy head → `also` rows. Lock order
      is always dam → chain, so there is no deadlock (a chained write without a dam key takes only the chain lock).
      sqlite (demo) is serialized by `BEGIN IMMEDIATE`; the checks run inside it too. Because the DA's
      `tests/test_chain_head_log.py` applies that patch forward, **DA: apply
      `tools/patches/v17_chain_head_log_tests_applied.patch`** (it accepts the already-applied state).
    - **Re-checked inside the transaction:**
      - first-CDSE assign: open assignment, or a pending CDSE outbox row (409 `cdse_assign_pending`);
      - CDSE change propose: one pending change per dam (`cdse_change_pending`);
      - CDSE change approve: no pending CDSE outbox row (`cdse_load_pending`; the double approve was already refused
        by the conditional UPDATE);
      - DSSMP propose / direct: one pending-or-approved registration per version id (`dssmp_registration_open`);
      - DSSMP approve / direct: one pending outbox row per version (`dssmp_load_pending`);
      - re-version propose: one pending per dam (`reversion_pending`);
      - re-version approve: no N+1 queued concurrently for any of its rules (`reversion_stale`);
      - cdse_ack flag apply: no other N+1 of the rule queued since the read (`flag_stale`);
      - flag proposal decide: the UPDATE is now conditional (`RETURNING`), so a concurrent decision gives 409.
    - **Unique partial indexes (belt and braces).** A violation is a 409 `concurrent_write` with nothing written.
      - Created by the app on the tables it owns: `dam_cdse_change_one_pending (dam_id) WHERE status='pending'`,
        `dssmp_registration_one_open (dssmp_version_id) WHERE status IN ('pending','approved')` and
        `rule_reversion_one_pending (dam_id) WHERE status='pending'`.
      - On tables owned by `dam_lakebase_owner`, the app skips them. `tools/patches/qa056_unique_guards.patch` was
        **applied by the DA in 424c6e3** (lakebase/001 block `v1_7_007` + migration `v1_7_007` + down, which also
        mirrors the app-owned `dam_cdse_change_one_pending` / `rule_reversion_one_pending`) with three owner-table indexes:
        - `dam_cdse_outbox_one_pending (dam_id) WHERE status='pending'`;
        - `dssmp_outbox_one_pending (dssmp_version_id) WHERE status='pending'`;
        - `ref_change_publication_one_version (dam_id, payload->>threshold_id, payload->insert->>threshold_version)
          WHERE kind='ref.threshold.new_version' AND status IN ('pending','applied')`.
      - Run the migration's duplicate pre-check first.
    - **Not serialized by the app:** `ref.dam` / `ref.v_dam_reviewer_authority` (live, cdse_exists) and
      `ref.dssmp_version`. Only the DA load jobs change them, and those are fed by the guarded outboxes.

23. **QA-058: `rule_reversion_proposal` is owner-owned; the app writes INSERT + a six-column UPDATE only (v1.7; DA
    6420d98, `lakebase/migrations/v1_7_010_rule_reversion_owner.sql`, inlined in lakebase/001 as `v1_7_010`).**
    - **DA side (fixed):** the table is owned by `dam_lakebase_owner`; the app role has `SELECT`, `INSERT` and
      `UPDATE (status, decided_by, decided_by_id, decided_at, decision_rationale, pub_ids)` - nothing else (no
      `DELETE`, no `TRUNCATE`, no other column). `t_status_rule_reversion_proposal` (v1_7_009,
      `status_transition_guard('applied', 'rejected')`) is `ENABLE ALWAYS`: pending -> applied | rejected once, a
      decided row is frozen.
    - **App writes (all of them):** propose = `st.insert_row(q, "rule_reversion_proposal", row)`; approve / reject =
      `st.decide_pending(q, "rule_reversion_proposal", "proposal_id", id, {...})` with only those six columns.
      `state.REVERSION_UPDATE_COLS` lists them and `decide_pending` raises `ValueError` before any SQL for another
      column on this table. The table name is normalized before that comparison (`state._table_key`: drops a
      `{s}` / schema prefix and double quotes, then casefolds), so `"dam_app"."Rule_Reversion_Proposal"` or
      `RULE_REVERSION_PROPOSAL` cannot bypass the allowlist. No `UPDATE` / `DELETE` / DDL on it elsewhere in
      `app/backend`.
    - **No app DDL on Postgres:** `state.OWNER_MANAGED = ("rule_reversion_proposal",)`. `init_schema` skips its
      `CREATE TABLE` and the `rule_reversion_one_pending` index there (lakebase/001 `v1_7_005` / `v1_7_007` create
      them). The `CREATE TABLE` string stays in `state.DDL` as the sqlite (demo) definition and as the text the DA's
      `tests/test_lakebase_cdse_tables.py` parity test compares against lakebase/001.
    - **Startup check (Postgres, read only, fail closed; `StateStore._verify_reversion_owner`, right after the DDL
      loop in the init transaction).** It raises `OwnerSchemaError` (a `RuntimeError` with `.code`), and the app does
      not start, when:
      - `rule_reversion_not_owner_owned`: the table is missing, or its owner is not `dam_lakebase_owner` by name. A
        superuser or app-role owner is the pre-v1_7_010 layout, so `pg_has_role` is not used here.
      - `rule_reversion_transition_guard_disabled`: `t_status_rule_reversion_proposal` is missing, or its
        `pg_trigger.tgenabled` is not `'A'` (ENABLE ALWAYS, what lakebase/001 sets). Only `'A'` passes: `'O'`
        (origin), `'D'` (disabled) and `'R'` (replica only) are all refused. QA-060: an `'O'` trigger does not fire
        under `session_replication_role = replica`, so a decided row could go applied -> pending.
      Both codes are explained in `frontend/src/codes.ts`. To recover, re-run lakebase/001 or migration v1_7_010.
    - **Re-checked on every chain verification:** `GET /api/audit/signoffs/verify` (`verify_all`) and
      `GET /api/dams/{dam}/audit/signoffs/verify` re-run the same check through `StateStore.verify_owner_tables()`,
      as QA-053 re-applies the signer guard. So disabling the trigger, making it origin-only or changing the owner
      AFTER startup is reported. Fail closed: no chain is verified; the response is `ok: false` with `reason` = the
      code (per chain in `verify_all`). sqlite (demo) has no owner model and is not affected.
    - **Tests:**
      - `app/tests/test_rule_reversion_owner.py`:
        - a sqlite trace of the real propose / reject / approve routers: every statement on the table is a
          `SELECT`, an `INSERT` or an `UPDATE` setting only the six columns;
        - a source scan finds no other write path;
        - the column guard;
        - the startup verdicts on a fake pool;
        - live, as the RESTRICTED app role on a lakebase/001 DB: INSERT and the six-column decide work; `DELETE`,
          `UPDATE` of `rationale` / `rules` and `DISABLE TRIGGER` give `InsufficientPrivilege`; applied -> pending,
          rejected -> applied and editing a decided row give `CheckViolation`; an origin-only, disabled,
          replica-only or dropped trigger, or app-role ownership, refuses startup;
        - live QA-060: with `'A'` a replica-session applied -> pending is refused; with `'O'` the same UPDATE goes
          through (rolled back), and the app refuses to start;
        - the table-name normalization (quoted, cased, schema-qualified names still hit the allowlist);
        - both verify endpoints report a change after startup (offline with a stubbed check; live as the restricted
          role: origin-only, disabled, owner changed, then recovery).
      - `app/tests/reversion_owner_stub.py` pre-provisions the owner's table and trigger (function text taken from
        lakebase/001) for the live tests that bootstrap the app without lakebase/001.

## 8. Contract items for v1.8 (logged, not implemented)

1. **DSSMP title and revision date columns (Genie).** `ref.dssmp_version` / the outbox carry no title or
   revision-date column in v1.7 (`version_no` and `effective_from` stand in). v1.8: the DA adds `title TEXT` and
   `revision_date DATE` to `ref.dssmp_version` and the outbox contract; the app then collects them in wizard step 2.
