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
