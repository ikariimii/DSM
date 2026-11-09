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
| `ref.dam` (`dam_id`, `cdse_id` only), `ref.project_membership` (`principal`, `dam_id`, `role`, `revoked_ts`) | who is the dam's CDSE (v1.6 ROLE-LEVEL, §5a); `ref.site` does not expose `cdse_id`, so `ref.dam` is read directly | `cdse_id`, plus active `role = 'CDSE'` rows for exactly that `dam_id` |
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

## 5a. Reviewer actions: hash-chained, append-only audit (QA-039 v2)
Every reviewer action is ONE inserted row in Lakebase `review_signoff` (lakebase/001 table, column-compatible with UC
`audit.review_record` plus chain columns). Nothing is updated or deleted (triggers in sqlite and Postgres, app-owned
names `t_no{update,delete}_review_signoff` / `t_app_noupd_review_signoff` / `app_forbid_mutation`), and the app never
updates ref.incl_correction, never applies a correction and never changes a measured value.

| action_type | endpoint (role) | subject_type / decision | original_value → corrected_value |
|---|---|---|---|
| `bias_review` | `POST /api/instruments/{iid}/incl/surveys/{sid}/bias-proposals/{record_id}/review` (engineer+) | `incl_correction` / `approve\|reject` | `{parameter_name, before_value}` → `{parameter_name, auto_value, override_value, after_value, applied_by_app:false}` (units as stored in ref.incl_correction) |
| `data_correction_review` | `POST /api/instruments/{iid}/incl/surveys/{sid}/corrections/{record_id}/review` (engineer+; any proposed ref.incl_correction record; a bias record is typed `bias_review`) | `incl_correction` / `approve\|reject` | as above |
| `alarm_ack` (CDSE acknowledgement at level ≥ 2) | `POST /api/alarms/{event_id}/ack` (level 2 Evaluate = HIGH and level 3 Act = CRITICAL: **the dam's CDSE only**, see below; level 1 Verify: engineer+) | `alarm_event` / `acknowledge` | `{event_id, dam_id, instrument_id, level, value, unit, event_ts, state:"open"}` → `{state:"acknowledged", measured_value_changed:false}` |
| `alarm_close` | `POST /api/alarms/{event_id}/close` (dso+) | `alarm_event` / `close` | same alarm snapshot with `state:"acknowledged"` → `{state:"closed", measured_value_changed:false}` |

Review bodies: `{decision, rationale, reviewer_pe_license?}`; ack: `{rationale}` (`comment` accepted as the older name);
close: `{reason}`. **The rationale is required and must be non-empty after trimming** (422 otherwise; DB CHECK
`length(trim(comment)) > 0` as backstop); it is stored in `comment`. original_value / corrected_value are canonical
JSON text; the original is stored next to the corrected value and never overwritten (Ch.14 §14.4.1).

**CDSE identity (v1.6 ROLE-LEVEL, FERC Ch.14 App K 14-K-4).** The CDSE is a named person, not an app role. A caller is
the CDSE of dam D when their X-Forwarded-User id or X-Forwarded-Email equals (case-insensitive, trimmed)
`ref.dam.cdse_id` for D, or the `principal` of an active (`revoked_ts IS NULL`) `ref.project_membership` row with
`role = 'CDSE'` and `dam_id = D` exactly. `dam_id = '*'` grants and group principals are **not** resolved (the app cannot
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
