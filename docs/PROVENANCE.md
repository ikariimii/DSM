# Code and data provenance

Owner: Data Architect. Last updated 2026-11-02 (America/New_York). This file records where shipped code and data come from, so that licence questions can be answered without reading git history.

## 1. TOA5 (Campbell Scientific logger files)

| Component | Owner | Provenance |
|---|---|---|
| `src/common/toa5.py` (reader / parser) | App Developer | **Clean-room.** Written only from Campbell Scientific's published TOA5 format documentation. It is not derived from PyTOA5 or igbpyutils (GPL-3.0-or-later). Sources and the clean-room rule are in `docs/TOA5_READER.md`. The App Developer attests to this; QA verifies the reader. |
| `src/dam_safety/toa5_landing.py` (ingest layer) | Data Architect | **Clean-room.** Written from the same Campbell sources: [CS-LN-TOA5] for the header and naive timestamps, [CS-CR1000] §7 Tables 12–13 and §8 for the layout and RECORD, and [CS-NANINF] for the FP2 −7999 and Long −2147483648 sentinels. It uses only the public API of `common.toa5`. It performs no parsing. It maps a parsed table onto the bronze `READING_SCHEMA` (`src/pipelines/bronze.py`), with a required `utc_offset_min`, a channel/thermistor map, and declared integer sentinels. |
| `src/dam_safety/toa5.py` | Data Architect | **Deleted 2026-11-02** (Genie): the re-export shim had no callers. The earlier duplicate parser was deleted in steering 9. The only parser is `src/common/toa5.py`. |

Neither PyTOA5 nor igbpyutils is imported, vendored, copied, or listed in any requirements or lock file. `tests/test_dependency_pins.py` (forbidden packages) and `tests/test_toa5_landing.py::test_dedupe_single_parser_and_provenance_note` enforce this.

Third-party open-source review material in `/workspace/oss-review/verify` was not created by this team. It is not used by, or copied into, this repo.

## 2. Formulas and reductions

The formula engine (`src/dam_safety/formulas/`) and the inclinometer module (`src/dam_safety/inclinometer/`) are in-house implementations of published equations. Every function's docstring cites its source, for example:

- SI-DR-2001 / DGSI data reduction
- MIKKELSEN-2003 / 2007
- GEOKON manuals
- USACE EM 1110-2-1908
- the team procedures in `/workspace/dam-refs/procedures/`

No vendor software code is included.

The inclinometer module follows the **layout** of the user's workbook only: the sheet-to-module mapping in `/workspace/dam-refs/inclinometers/user_tool/REPORT.md` §6. Its arithmetic, QC rules and correction rules are not taken from the workbook (REPORT §3.3, errata E1–E13).

## 3. Data

- **Shipped sample data** (`sample_data/`, `tools/generate_sample_data.py`) is synthetic.
- **Datasets marked internal-test-only** are never shipped. The source of truth is `/workspace/dam-refs/datasets/*/manifest.csv`, and the seed is `sql/seed/dataset_catalog.csv`. These datasets include:
  - Enguri (CC BY-NC)
  - ICOLD BW2022 Themes A/C
  - USBR RISE
  - the user's inclinometer workbook (DS-INC-USERTOOL)
  - vendor-manual sample tables
- **Workbook-value tests** live in `testonly_restricted/`. That directory is gitignored and excluded from the bundle sync. The tests read the extract in place from `/workspace/dam-refs` and contain only aggregate expected values.
- **Shipped tests** (`tests/`) use only public worked values that are individually cited, plus synthetic numbers.
- **Guards:**
  - `tests/test_license_leak.py` checks file names, sha256 hashes, data lines, and that the sync excludes match the `.gitignore` entries.
  - `.gitignore` and the `databricks.yml` `sync.exclude` list contain `**/user_tool/`, `*user_tool_surveys*`, `*libreoffice_recalc_compare*`, `**/vendor_manual_samples/`, and `testonly_restricted/`.

## 4. Commit authorship (QA-054, 2026-11-09)

**Cause.** Until about 11:46 AM (America/New_York) on 2026-11-09, the shared `/workspace/dam-safety-app/.git/config`
set `user.name` to `QA-engineer-agent` (`qa-engineer@dam-safety.local`). Every commit made without an explicit
identity in any worktree of that repo was recorded under QA's name. **History is not rewritten:** other agents have
built on these commits and some are on `origin`, so the recorded author stays and this table is the correction.
Since then Genie removed the repo-level identity and enabled `extensions.worktreeConfig`; each worktree carries its
own identity (the Data Architect worktrees: `Databricks-Data-Architect <data-architect@dam-safety.local>`). No
repo-level identity is to be set.

**Method.** For every commit on `app/v1.6-integration` whose recorded author is `QA-engineer-agent`: the paths it
changes (for a merge, the diff against its first parent), its message against each team's work, and the Data
Architect's own session record of the commits it made. "Pushed" = `git branch -r --contains <hash>` on the local
remote-tracking refs (no fetch; as of 2026-11-09 11:46 AM America/New_York).

| Hash | Subject | Recorded author | True author | Evidence | Pushed to origin |
|---|---|---|---|---|---|
| 0ff6d53 | v1.7: cdse_ack_required on ref.threshold + cdse_ack_required_at_open snapshot | QA-engineer-agent | Data Architect | sql/, src/, tests/, docs/, SCHEMA_CONTRACT.md only; DA work item cdse_ack_required (migration v1_7_001); DA session record | yes (origin/app/v1.6-integration) |
| 743b8c6 | v1.7: cdse_ack_required versioning rule ... VERIFICATION: app init lock applied in 974e786 | QA-engineer-agent | Data Architect | sql/, src/, tests/, docs/, SCHEMA_CONTRACT.md only; DA threshold_publish / versioning decision; DA session record | yes (origin/app/v1.6-integration) |
| 9875813 | QA-045d: verify_records head_missing like the app; append-only audit.chain_head | QA-engineer-agent | Data Architect | sql/, src/, tests/, tools/, resources/, docs/ only (no qa/); the "QA-045d" prefix names the QA finding it fixes, the fix is DA engine/DDL work; DA session record | yes (origin/app/v1.6-integration) |
| 826f7ba | Merge branch 'fix/qa-045d-chain-head' into feat/7.13-alarm-rule-authority | QA-engineer-agent | Data Architect | merge of the DA branch fix/qa-045d-chain-head (9875813) onto 6f7a1d5 to start the DA §7.13 branch; diff vs first parent is DA paths only; DA session record | yes (origin/app/v1.6-integration) |
| 7993e53 | Merge commit 'b914222' into feat/7.13-alarm-rule-authority | QA-engineer-agent | Data Architect | merge of the DA commit b914222 (ROLE-LEVEL) into the DA §7.13 branch; diff vs first parent is DA paths only; DA session record | yes (origin/app/v1.6-integration) |
| e78c62b | §7.13: audit.review_record accepts alarm_rule propose/apply/reject ... authority verifier | QA-engineer-agent | Data Architect | sql/, src/, tests/, docs/, SCHEMA_CONTRACT.md only; DA §7.13 work (migration v1_6_003, rule_flag_authority); DA session record | yes (origin/app/v1.6-integration) |
| b3aeecd | Merge feat/v1.7-cdse-ack-hardening (0ff6d53, 743b8c6) into app/v1.6-integration | QA-engineer-agent | Data Architect | integration merge requested by Genie and performed in the DA worktree wt-integ; conflict resolution in DA docs / migration order; DA session record | yes (origin/app/v1.6-integration) |
| 487d5b4 | Prod fail-closed: verify_review_chain refuses to start on dam_prod without a real signer | QA-engineer-agent | Data Architect | src/, tests/, docs/ only; DA work item "prod fail-closed"; DA session record | no |
| 0274aeb | Lakebase ownership hardening: NOLOGIN dam_lakebase_owner ... | QA-engineer-agent | Data Architect | lakebase/, tools/, tests/, .github/, README.md, docs/, SCHEMA_CONTRACT.md; DA work item "Lakebase ownership hardening"; DA session record | no |
| 3d398dd | Merge c797151 (App Dev canon v2 ...) into app/v1.6-integration | QA-engineer-agent | Data Architect (merge only; the merged content c797151 is App Dev's, recorded as App-Developer-agent) | merge performed in the DA worktree wt-integ on Genie's instruction; diff vs first parent is App Dev's app/ changes; DA session record | no |
| e8cc437 | QA-050/051/052/053: engine canon v2 ...; prod signer key probe; verify_all re-applies the prod guard | QA-engineer-agent | Data Architect | sql/, src/, tests/, lakebase/, tools/, docs/, SCHEMA_CONTRACT.md only (no qa/); the QA-05x prefixes name the QA findings it fixes; DA session record | no |
| 99d2db6 | actor_app_role is evidence only (contract); PROVISIONAL engine second-approver check | QA-engineer-agent | Data Architect | src/, tests/, resources/, databricks.yml, docs/, SCHEMA_CONTRACT.md only; Genie item for the DA; DA session record | no |

Commits recorded as `QA-engineer-agent` that ARE QA's (checked, listed for completeness): **bb5347f** (QA.md + qa/
only: close QA-045b), **af253a8** (QA.md + qa/ only: close QA-045a), **0681292** (QA.md, qa/, plus a `.gitignore`
entry tracking qa/test_*.py and a `databricks.yml` sync.exclude of QA.md, both QA-ledger housekeeping). All three are
on origin. None of App Dev's commits is mis-attributed (they always set App-Developer-agent explicitly; consistent
with what was checked here). UNCONFIRMED: none.

### 4.1 Follow-up (QA-054, 2026-11-09)

| Hash | Subject | Recorded author | True author | Evidence | Pushed to origin |
|---|---|---|---|---|---|
| fe4ed7b | QA-034: redact restricted-derived values; guard derived-scalar check; QA suite tracking | QA-agent (2026-11-02 20:29 -05:00) | QA | paths are QA.md, qa/ (11 files) plus QA-ledger housekeeping in .gitignore, databricks.yml and one docs/VERIFICATION.md line; QA-034 is QA's own finding/suite | yes (origin/main, origin/app/v1.6-port, origin/app/v1.6-integration) |
| 9ed7b10 | stash@{0} "On feat/v1.7-cdse-ack-hardening: item2 ownership WIP" (stash commit) | QA-engineer-agent (2026-11-09 11:01 -05:00) | Data Architect; stash/WIP, never on a branch | made in DA worktree wt-v17 (branch feat/v1.7-cdse-ack-hardening at 743b8c6) when Genie re-prioritised the DA queue; only lakebase/001_app_schema.sql (dam_lakebase_owner ownership hardening), the precursor of DA commit 0274aeb; DA session record. refs/stash is repo-wide, so it lists in every worktree | no |
| c3ee790 | "index on feat/v1.7-cdse-ack-hardening: 743b8c6 ..." (stash index parent of 9ed7b10) | QA-engineer-agent | Data Architect; stash/WIP, never on a branch | git-generated index commit of the same stash; no changes vs 743b8c6 | no |
| 85d1563 | "untracked files on feat/v1.7-cdse-ack-hardening: 743b8c6 ..." (stash untracked parent of 9ed7b10) | QA-engineer-agent | Data Architect; stash/WIP, never on a branch | git-generated untracked-files commit of the same stash: lakebase/validate_append_only.sql and tools/validate_lakebase.py, which landed (revised) in DA commit 0274aeb | no |

None of 9ed7b10 / c3ee790 / 85d1563 is an ancestor of the integration tip (`git merge-base --is-ancestor` false for each).

**23c4a42 vs f0dd9c8.** App Dev's original second-approver commit 23c4a42 (`app/v1.6-port`, worktree wt-v16-port) is
NOT an ancestor of the integration tip e413293 (`git merge-base --is-ancestor 23c4a42 e413293` exits 1). The commit
that is included is **f0dd9c8**, App Dev's rebase of 23c4a42 onto 3d398dd (same subject, author App-Developer-agent;
`git merge-base --is-ancestor f0dd9c8 e413293` exits 0).

**Worktree identities.** There is no repo-level `user.name` (`git config --local --get user.name` is empty) and
`extensions.worktreeConfig` is on. Every worktree now has its own identity (`git config --worktree user.name/user.email`,
checked 2026-11-09 11:53 -05:00; `git worktree list` shows no qa043-wt, Genie pruned the stale /tmp/qa043-wt):

| Worktree | Branch / HEAD | user.name | user.email |
|---|---|---|---|
| /workspace/dam-safety-app | app/v1.5-audit-chain | Genie (lead) | genie@dam-safety.local |
| /workspace/dam-safety-app-main | main | Genie (lead) | genie@dam-safety.local |
| /workspace/qa-wt-853ca80 | detached 853ca80 | QA-engineer-agent | qa-engineer@dam-safety.local |
| /workspace/qa-wt-bed918c | detached bed918c | QA-engineer-agent | qa-engineer@dam-safety.local |
| /workspace/wt-713 | feat/7.13-alarm-rule-authority | Databricks-Data-Architect | data-architect@dam-safety.local |
| /workspace/wt-integ | app/v1.6-integration | Databricks-Data-Architect | data-architect@dam-safety.local |
| /workspace/wt-integ-app | app/v1.6-integ-appdev | Databricks-App-Developer | app-developer@dam-safety.local |
| /workspace/wt-qa045a | app/qa-045a-signature | Databricks-App-Developer | app-developer@dam-safety.local |
| /workspace/wt-qa045b | fix/qa-045b-truncate | Databricks-Data-Architect | data-architect@dam-safety.local |
| /workspace/wt-qa045c | fix/qa-045c-sig-head | Databricks-Data-Architect | data-architect@dam-safety.local |
| /workspace/wt-qa045d | fix/qa-045d-chain-head | Databricks-Data-Architect | data-architect@dam-safety.local |
| /workspace/wt-v16-port | app/v1.6-port | Databricks-App-Developer | app-developer@dam-safety.local |
| /workspace/wt-v17 | feat/v1.7-cdse-ack-hardening | Databricks-Data-Architect | data-architect@dam-safety.local |

No history is rewritten: these records are corrected here only. UNCONFIRMED: none.

## 5. Integration notes (2026-11-09)

- **Lock class in the 0aed4c4 commit message.** App Dev's commit 0aed4c4 (QA-056, `app/v1.6-integ-appdev`) says
  the per-dam lock uses class 4702046. That is wrong in the message only: the code (`app/backend/repos/state.py`
  `DAM_LOCK_NS = 4702047`, `lock_dam`) and the app SCHEMA_CONTRACT correctly use **4702047** for the dam lock;
  4702046 is the DA chain-lock class (`CHAIN_LOCK_CLASS`, review_chain_head_log) and 4702045 the init lock. Lock
  order in `append_chained` (478b7b5): dam 4702047 -> check(q) -> chain 4702046 (asserted by
  tests/test_chain_head_log.py). History is not rewritten; this note is the correction.
- **Integration of 478b7b5.** wt-integ's branch had one local commit (the runbook, 3103554) on top of 23b39c5 when
  App Dev's 478b7b5 (a fast-forward of 23b39c5) arrived, so `git merge --ff-only` was not possible; the unpushed
  runbook commit was rebased onto 478b7b5 (now 33bee49, same content). Then App Dev's
  `tools/patches/v17_chain_head_log_tests_applied.patch` was applied.
- **Applied patches removed from tools/patches.** All five were verified as applied before deletion (each added
  line present in the tree, or `git apply -R --check` clean): `v17_chain_head_log_state.patch` (App Dev 478b7b5),
  `v17_chain_head_log_tests_applied.patch` (this commit), `qa056_unique_guards.patch` (DA 424c6e3),
  `qa055_catalog_allowlist.patch` (DA e988454), `qa045c_app_state_init_lock.patch` (added in d63804f, QA-045c, author "Data Layer Engineer"; state.py carries all 42 added lines). They stay in
  git history.

## 6. v1.8 dam lifecycle (2026-11-09)

- **Code:** `src/dam_safety/lifecycle.py`, `src/dam_safety/lifecycle_load.py`, `src/jobs/apply_lifecycle_transitions.py`,
  `src/jobs/_lifecycle_guard.py`, the SQL in `sql/ddl/14_dam_lifecycle.sql`, `sql/migrations/v1_8_00*` and
  `lakebase/migrations/v1_8_001_*` are original work of the Data Architect agent for this repository (no third-party
  code). The hash scheme (length-prefixed fields, sha256) is the same construction as the v1.6 review chain.
- **Regulatory references in `ref.lifecycle_citation`:** the 15 codes and their citations are the list agreed on
  2026-11-09 14:22 (18 CFR Parts 6, 9, 12 and 385; FPA section 8). Only `transfer_final_9_3_12_12c` (18 CFR 9.3;
  12.12(c)) and `surrender_effective_6_2_6_5` (18 CFR 6.2; 6.5; 12.1) are VERIFIED; every other row is
  `UNVERIFIED-pending` and `INTERNAL` rows cite no regulation. The rows are data: verification moves a row to
  VERIFIED by a data change, never by a code change. No regulation text is copied into the repository.
- **Test data:** synthetic only (dam ids `D1`, `D2`, `NEW`, `OLD`, ...; example.org-style addresses); no restricted
  or licensee data. The backfill reads only the app's own `onboarding_golive` audit rows already in UC.

