# QA Log — Dam Safety Monitoring App

Owner: QA Engineer bot. Entries are dated (America/New_York). Results are only what was actually run.
Independent oracle: `qa/reference_examples.py` (manufacturer worked examples, computed by QA, not imported from app code).

---

## 2026-11-02 18:25 EST — First QA pass (baseline)

### 1. What exists
**/workspace/dam-safety-app/** (code started landing ~18:21 EST during this pass)
- `app/backend/config.py` — Settings dataclass (DEMO_MODE, UC catalog/schemas, SQL OBO, Lakebase/SQLite state, bootstrap admins, static dir). Only real code file.
- `app/backend/__init__.py`, `routers/__init__.py`, `repos/__init__.py` — empty.
- `app/frontend/` — no source files yet. `app/tests/` — empty. `app/.venv` — fastapi, httpx, numpy, pytest installed.
- Referenced but missing: `app.yaml`, `SCHEMA_CONTRACT.md`, `frontend/dist`.
- No reduction modules (piezometer, inclinometer, etc.), no pipelines, no wizard, no app entrypoint.

**/workspace/dam-refs/**
- Manufacturer manuals + text extracts (Geokon 4500/4500MLP/4420/4450/4900/4000/6350/GK-603, DGSI Digitilt/DigiPro2/GTILT PLUS/Bias-shift/Rotation, Campbell AVW1/AVW200, Roctest FOP, USBR, USACE, FERC Ch9/Ch14, Salazar).
- Family folders: extensometers (with `verify.py`), inclinometers, piezometers, settlement-survey, strain-stress-load.
- `CONVENTIONS.md`, `references_index.csv` (39 lines).
- `procedures/` — EMPTY (no `<family>.md` yet).
- `verify.py` exists only for extensometers. None for piezometers or inclinometers.
- `inclinometers/saav.html` is 0 bytes.
- No GTILT/DigiPro reference outputs (sample survey files, .gtl/.rpp, printed reduced tables). Only the PDF manuals.

### 2. Unit tests
| Suite | Command | Result |
|---|---|---|
| App tests | `app/.venv/bin/python -m pytest -q` in `app/` | no tests collected (0 tests) |
| Config smoke | import `backend.config.get_settings()` | PASS: demo_mode=True, `uc('silver','reading')` -> `` `dam_dev`.`silver`.`reading` `` |
| QA oracle | `python -m pytest qa/reference_examples.py` | 1 passed |
| Extensometer specialist verify | `python dam-refs/extensometers/verify.py` | 58/58 PASS |

### 3. Reduction verification (independent recomputation vs published manual values)
`qa/reference_examples.py`: **34/34 PASS** (all cases computed by QA from the manual formulas).
- Piezometer, Geokon 4500 §6.1–6.4 and App B: linear 418.8 kPa, poly C=1597.2 and P=421.5 kPa, head 42.7 and 43.0 m, temp correction −0.92 kPa, baro factors 3.386 kPa/inHg, thermistor Steinhart-Hart (3000 Ω→24.99 °C; Table 8 points 0, 40 and −50 °C).
- Inclinometer, Slope Indicator/DGSI data_reduction.pdf: sin(1°)×20000=349, combined (A0−A180)/2, checksum=20, deviation 0.4188 in, displacement 0.0012 in, unit factors 0.0006 in and 0.01 mm. DGSI_Rotation (restricted, DGSI-DATA-REDUCTION-FIGS): both rotation examples reproduced; values by path in `/workspace/dam-refs/qa_private/inclinometer_expected.json` `reference_examples` IN-08/IN-09.
- Crackmeter, Geokon 4420: linear, poly, thermal K and correction. Load cell, Geokon 4900: linear and poly. Strain gauge, Geokon 4000: rosette example.
- Tolerance notes (these come from manual rounding, not defects): 4500 "8000 digits ≈ 354 µs" gives 7979.8 because the manual rounds the period (exact is 353.55 µs), so tol is ±25 digits. 4420 linear exact value is 21.798 mm and the manual prints 21.79 (truncated), so tol is 0.01.
- Not yet comparable: the app has no reduction code, so there is no app-vs-oracle comparison yet.
- Gotcha for implementers: data_reduction.pdf calls both (A0−A180)/2=349 and the un-halved sum 698 a "combined reading". The app must fix one definition (recommend storing the un-halved A0−A180 and dividing by 2K, as GTILT/DigiPro do) and test it.

### 4. Wizard / app mock-mode smoke test
NOT RUN. No app entrypoint, routers, wizard or frontend exist yet. Only config loads (DEMO_MODE defaults to true).

### 5. FERC traceability (canvas "FERC Regulatory Requirements & Data Model", 41 reqs)
| Group | Reqs | Code | Tests |
|---|---|---|---|
| A DSSMP mgmt | REQ-01..05 | 0 | 0 |
| B PFM traceability | REQ-06..11 | 0 | 0 |
| C Thresholds/alerting | REQ-12..17 | 0 | 0 |
| D Frequency compliance | REQ-18..21 | 0 | 0 |
| E Review/audit trail | REQ-22..25 | 0 | 0 |
| F DSSMR generation | REQ-26..29 | 0 | 0 |
| G Part 12D | REQ-30..33 | 0 | 0 |
| H EAP | REQ-34..36 | 0 | 0 |
| I Incident reporting | REQ-37..41 | 0 | 0 |
**Coverage: 0/41 implemented, 0/41 tested.** Only infrastructure exists (config). REQ-23 (append-only, no hard delete) and dam_id row filters will need explicit tests once repos land.

### 6. Defects
| ID | Sev | Area / owner | Summary | Steps | Expected | Actual | Status |
|---|---|---|---|---|---|---|---|
| QA-001 | Medium | App Dev (config.py) | `DEMO_MODE` defaults to **True** | Unset DEMO_MODE; `get_settings().demo_mode` | Fail-safe default False in deployed app. A dam-safety app must never silently serve seeded data. Demo should be opt-in, or the app should refuse to start without a warehouse. | True | Open |
| QA-002 | Low | App Dev (config.py) | `BOOTSTRAP_ADMINS` defaults to `admin@demo.local` even in non-demo mode; `demo_user` fallback when no X-Forwarded-Email header | Inspect Settings defaults | No default global admin outside demo. Requests without a forwarded identity are rejected when demo_mode=False. | Default admin is always present; the fallback can't be checked until auth code lands | Open (verify when auth lands) |
| QA-003 | Medium | Data Architect + App Dev | UC layout in config (`dam_dev` with `ref`/`silver`/`gold`) differs from the FERC canvas data model (`dam_safety` with `ref`/`ops`/`audit`/`rpt`; `ops.reading`, `audit.review_record`, `rpt.dssmr`). There's no place for audit or report tables. | Compare config.py `uc()` with canvas §3 | One agreed schema contract (SCHEMA_CONTRACT.md) that covers audit/rpt tables, with a traceability mapping | Mismatch; SCHEMA_CONTRACT.md missing | Open |
| QA-004 | Medium | Piezometer Specialist | No `procedures/piezometers.md` and no `piezometers/verify.py` per CONVENTIONS.md | `ls dam-refs/procedures dam-refs/piezometers` | Procedure plus worked-example verify.py | Missing. QA oracle PZ-01..14 is available to reuse. | **Closed 2026-11-02 19:00 (verified, with notes)**: files exist; verify.py runs 111/111 PASS (not the claimed 92/92). Follow-ups QA-010..QA-013 |
| QA-005 | Medium | Inclinometer Specialist | No `procedures/inclinometers.md`, no `inclinometers/verify.py`, no GTILT/DigiPro reference outputs (sample raw survey + reduced output) to regression-test against. `inclinometers/saav.html` is 0 bytes. | `ls dam-refs/inclinometers dam-refs/procedures` | Procedure, verify.py, and at least one raw+reduced reference dataset (e.g. DigiPro2 sample) | Missing / empty file | **Partially closed 2026-11-02 18:40**: QA-005a–d closed, QA-005e open (see entry below) |

### 7. Next QA actions
- Re-run as soon as routers, repos, reductions or the wizard land: app pytest, app reductions vs `qa/reference_examples.py`, DEMO_MODE smoke via FastAPI TestClient.
- Add append-only (REQ-23) and row-filter tests once repos exist.

## 2026-11-02 18:40 EST: QA-005 re-verification (Inclinometer Specialist fix)

**Method.** I reran `dam-refs/inclinometers/verify.py`. Then I wrote `qa/inclinometer_independent_check.py`, which re-extracts every source page straight from the PDFs with `pdftotext -layout`. It does not use the specialist's `txt/` files or `build_from_manual_text.py`. The script compares every transcribed CSV/.GK cell to its PDF cell and recomputes every published reduced value with my own formulas. I read the citation context for each verify.py case in the source text by hand.

**Results**
- verify.py: **32 PASS / 0 FAIL** (exit 0). Identical to `verify_output.txt`.
- QA independent check: **17 PASS / 0 FAIL**.
  - DMM DEMO hole 45 (PDF p100/p102): 30 depths × 11 columns match the PDF exactly. I recomputed DIFF, CHANGE, INCR.DEV (exact to 4 dp), INCR DISP, and CUM DISP bottom-up. The p102 prose example is reproduced (inputs and result by path: `/workspace/dam-refs/qa_private/inclinometer_expected.json` `dmm_p102_example`).
  - GK-603 IH01: both .GK files match PDF p50 (51 levels × 4). The published CSV matches p51/p52/p56 (51 × 9). Checksum, shift, deflection (Eq E-1/E-2, M = 0.0003, RINT = 2) and profile (Eq E-3/E-4, M = 0.0006, unrounded Diff/2) are reproduced bottom-up.
  - Geokon 6150 p17: 8 sensors × 13 columns match the PDF. R1corr (Eq 3), dR, G·L·dR (Eq 5) and the accumulated deflection are reproduced. The surface value matches the PDF (by path: `/workspace/dam-refs/qa_private/inclinometer_expected.json` `ipi_surface_mm`).
  - DMM p11–12 manual-mode examples: confirmed. The second printed sinθ is a typo in the manual: it is not R/K for the printed B reading (printed tokens by path: `/workspace/dam-refs/qa_private/inclinometer_expected.json` `dmm_intro_tokens_reported`). The metric displacements follow from the unrounded sinθ (verify T2a/T2b). T2 correctly uses the single-reading sinθ = R/K convention, which is consistent with D = 2K·sinθ.
  - Remaining citations confirmed in the source text:
    - DMM p63: gives a typical mean-checksum bound and a higher bound that suggests a damaged probe (read the restricted DMM manual PDF p63 by path).
    - GTILT: ZA = −19 on PDF p45; the "ZA = 10 increases A0" sign convention on PDF p46 (p6-12); 0.01 × 0.08 × 50 m = 40 mm on PDF p43.
    - DigiPro2: the rotation ratio of verify T7b is on PDF p39 (restricted manual; read by path).
    - DGSI_Rotation: rotation example IN-08 reproduced (by path: `/workspace/dam-refs/qa_private/inclinometer_expected.json` `reference_examples`).
    - DGSI_Bias_Shift: divisors 0.0006 / 0.01.
    - USACE EM 1110-2-1908 Table 8.2 (PDF p175): 0.25 in (6.4 mm), 3 consecutive readings.
    - Measurand: +Y is 90° CCW from +X looking down.
- Definitions in `procedures/inclinometers.md` §1: D = A0 − A180, C = D/2, dev = L·D/(2K), CS = A0 + A180. These are correct and cited (DMM p101/p99, data_reduction p2–3). The 349 vs 698 "combined" ambiguity is documented. The bias-correction factors c·L/(2K) = 0.0006 in / 0.01 mm are consistent.
- `inclinometers/saav.html`: deleted (confirmed).
- The pub_* columns are transcribed from the manuals, not computed by the specialist's code, so there is no circular derivation in T3/T4/T5.

**Strength of the 32 cases**

| Class | Cases | Count |
|---|---|---|
| Source-traced and independently reproduced | T1a, T1b, T1c, T1e, T2a–d, T3, T3b, T3c, T4, T4c, T5, T5b, T7b, T7c, T9, T10a, T10b | 20 |
| Constant/arithmetic only (no reduction function exercised) | T1d, T4b, T6a, T6b, T6c (local lambda), T7a, T10c | 7 |
| SYNTHETIC (generator and expected values from the same author; checks self-consistency only) | T6d, T6e, T8a–c | 5 |

Only the first class counts as verification against an external oracle.

**QA-005 status**
- QA-005a (procedure missing): **Closed**, verified.
- QA-005b (verify.py missing): **Closed**, verified (rerun plus independent re-derivation).
- QA-005c (saav.html 0 bytes): **Closed**, file deleted.
- QA-005d (definition ambiguity D/C/deviation): **Closed**. Definitions are correct and the ambiguity is documented.
- QA-005e (no real vendor GTILT/DigiPro output files): **OPEN**, still true. There are no .GTL, .dux, DigiPro DB, or RPP/NUM files in dam-refs; procedure §8 lists them as unavailable. The substitutes are manual-printed tables only (DMM, GK-603, 6150), and USBR 128-8 was not reviewed in this pass. File-format importers for GTILT/DigiPro therefore cannot be regression-tested. Owner: Inclinometer Specialist. Severity Medium, not blocking for reduction math.

**New defects**

| ID | Sev | Area | Defect | Steps | Expected | Actual | Status |
|---|---|---|---|---|---|---|---|
| QA-006 | Low | Inclinometer Specialist | The GTILT rotation example "40 mm" is the manual's rounded figure. The exact value is sin 0.5° × sin 4° × 50 m = **30.4 mm**. T7a only asserts the rounded arithmetic. | GTILT PDF p43; compute `sin(rad(.5))*sin(rad(4))*50000` | Label T7a as "rounded illustration, not an engine oracle". Any engine test uses 30.4 mm (±0.1). | T7a and procedure §5 present 40 mm as the example value | **Closed 2026-11-02 19:00 (verified)**: T7a is labelled as an illustration; T7d = 30.44 mm (independent: 30.437) |
| QA-007 | Low | Inclinometer Specialist | Page-citation inconsistency: verify.py T7b cites DigiPro2 "p36–37", but the value is on PDF p39 (as manifest.csv says). GTILT cites use printed pages (6-8/6-12) while others use PDF pages. | grep the T7b inputs per page | One convention (PDF page, plus printed page in brackets) | Mixed | **Closed 2026-11-02 19:00 (verified)**: all cites are in PDF pN [printed pM] form; T7b is PDF p39 [printed p37]; 19 printed pages spot-checked against footers |
| QA-008 | Low | Inclinometer Specialist | 7 of 32 verify.py cases are constant arithmetic, and 5 are synthetic and self-generated. The summary "32 PASS" overstates external verification. | See class table above | Tag cases `[CONST]` / `[SYNTHETIC]` and report the source-oracle count separately | Only the synthetic cases are tagged | **Closed 2026-11-02 19:00 (verified)**: 49 PASS = 34 SOURCE + 7 CONST + 5 SYNTHETIC + 3 RULE, reported separately |

**Gap (not a specialist defect).** verify.py tests reference functions defined inside verify.py itself. There is no inclinometer reduction code in `dam-safety-app/app` yet, so none of this exercises production code. When the App Dev lands a reduction module, point `qa/inclinometer_independent_check.py` and verify.py at it.

**Rerun:** `python3 /workspace/dam-safety-app/qa/inclinometer_independent_check.py` (needs poppler `pdftotext`).

## 2026-11-02 19:00 EST: Restricted-data audit and guard, plus fix verification (QA-004, QA-005e, QA-006..008)

### A. usage_restriction found in every `dam-refs/datasets/**/manifest.csv`
"Restricted" means the value starts with `internal-test-only`. A dataset folder without a manifest is treated as restricted (default deny).

| manifest | internal-test-only rows | other values |
|---|---|---|
| _shared | 4 ("internal-test-only; do not redistribute or ship (owner decision 2026-11-02)") | **5 rows with an empty value** (see QA-014) |
| accelerometer_strong_motion | 3 (incl. "if ever obtained") | "n/a (unavailable)" ×1 |
| extensometers | 0 | "none (CC0-1.0…)" ×1, "none (CC-BY-4.0…)" ×2 |
| inclinometers | 7 (incl. vendor_manual_samples and DS-INC-VDV-EXPORT-EXAMPLES) | redistributable ×4 (USBR, USACE, CC-BY, in-house synthetic) |
| piezometers | 3 (Enguri CC BY-NC; 2 × "no licence stated") | "none beyond CC BY 4.0 attribution" ×3, synthetic ×1 |
| plumbline | 1 | "n/a" ×1 |
| seepage | 3 (incl. "doc only; no data") | "none" ×5, "none (attribution required)" ×1, "UNAVAILABLE - files restricted" ×1 |
| settlement-survey-gnss | 4 | "unrestricted" ×2 |
| strain-stress-load | 3 (MENDELEY-ANCHOR-FORCELOSS, NCDC-XIAOWAN, QUT-SEC; only the first is on disk) | "open" ×4. The manifest appeared at 18:46; before that the folder had none and was default-denied |
| tiltmeter | 0 | "none (public domain…)" ×1 |

The resolved restricted file set is 92 files, including `../` references to the DGSI DigiPro2, Bias-Shift and Rotation PDFs and the DMM manual PDF.

### B. What had been copied into this repo, and what changed
- `qa/inclinometer_independent_check.py` **embedded no data rows or tables**. It did embed about 25 scalar values and strings quoted from restricted manuals (the DMM p102 example inputs and result, DMM/GK-603/IPI tokens, IN-06..09 published values). All of these were moved to `/workspace/dam-refs/qa_private/inclinometer_expected.json`, which is outside the repo, internal-only and read by path (`DAM_QA_PRIVATE` overrides). Without the file the script SKIPs. Re-run: 17/17 PASS.
- `qa/reference_examples.py`: IN-06..09 (DGSI-restricted published values) were removed from code and are loaded from the same fixture by path. Re-run: 34/34 PASS with the fixture, 30/30 without (IN-06..09 SKIP).
- `qa/piezometer_worked_examples.py` (new, see D): its published values come from non-restricted manuals. They are still kept out of the code in `/workspace/dam-refs/qa_private/piezometer_expected.json`, and inputs are parsed from `procedures/piezometers.md` by path.
- No PDF text copies, fixtures or dataset files were found in the repo. The full guard scan (C) reports 0 hits.
- `.gitignore` already existed (App Dev, LICENSING block). I added a QA block **above** the LICENSING block, so `tests/test_license_leak.py::test_sync_excludes_cover_gitignore_licensing_patterns` keeps passing. It covers `dam-refs/`, `**/dam-refs/`, `datasets/`, `**/datasets/`, `qa_private/`, `.cache/`, `*.GK`, `*.tenv3`, `*.xlsx`, `*INTERNAL_ONLY*`, `*RESTRICTED*`, `.mypy_cache/` and `.ruff_cache/`. `.pytest_cache/`, `__pycache__/` and `*.pyc` were already present. **Note:** the existing `qa/` rule means none of qa/ (this guard included) is tracked by git. See QA-015.

### C. Path guard: `qa/restricted_guard.py`, `qa/test_restricted_guard.py`, `qa/hooks/pre-commit`
- `build` parses every manifest and resolves the restricted files. It writes `qa/restricted_denylist.json`: 10 KB, containing only sha256 values and distinctive basenames, never data. Content fingerprints go to `/workspace/.cache/dam-safety-restricted-fp/`, outside the repo: 398,628 hashed 4-number windows and 17,844 hashed text lines.
- `scan` flags a repo file if it is:
  - an exact copy (sha256);
  - a file with a restricted basename;
  - a path under dam-refs/datasets;
  - a file with ≥3 lines carrying a restricted numeric window. A window must have ≥3 tokens with ≥3 significant digits, or be a low-precision instrument row with every token at ≥2 decimals;
  - a file with ≥1 restricted text line of 40 characters or more.
- Self-tests (planted, then removed) were each detected:
  - a renamed VDV export;
  - a same-name copy;
  - 8 DMM CSV rows pasted into a .py file;
  - 11 GTILT export rows pasted into a .md file;
  - 2 lines of DMM manual PDF text.
  
  The pre-commit hook was tested in a throwaway repo under /tmp. It blocked a commit of pasted DMM rows (exit 1) and allowed a clean commit.
- First-run false positives (18 files) came from a guard bug: `../` manifest paths widened to all of dam-refs, plus date-like tokens. The bug is fixed, and the scan is now **0 hits** across the repo, with the other bots' files included.
- pytest: `qa/test_restricted_guard.py` has 5 tests (scan, planted-copy self-test, and the 3 QA scripts), all passing.
- The hook is installed with `ln -sf ../../qa/hooks/pre-commit .git/hooks/pre-commit`.
- In CI (no dam-refs), the guard falls back to hash, name and path checks and prints a NOTE.
- Root `pytest tests/`: 119 passed, 1 failed. The failure is `test_bundle_config.py::test_referenced_files_exist`: `resources/` references `../src/jobs/interpretation_run.py`, which does not exist. It is pre-existing and App Dev's; I did not touch it. `app/tests`: 162 passed.

### D. Fix verification (re-derived from the cited PDFs)
- **QA-006: verified, closed.** sin0.5°·sin4°·50000 = 30.437 mm, and T7d asserts 30.44. T7a is labelled as a rounded illustration.
- **QA-007: verified, closed.** The T7b inputs occur only on DigiPro2 PDF p39, whose footer reads 37, so T7b's "PDF p39 [printed p37]" is right. Printed-page claims were checked against footers on 19 pages: DMM p11/12/63/99/102, GK-603 p50/56/61, IPI p14/17/19, Rotation p2/3, DigiPro2 p27, Bias p3, GTILT p42/43/46 and USACE p175. All match. Nit: some case lines carry the cite only in their group comment. Acceptable.
- **QA-008: verified, closed.** `inclinometers/verify.py` prints 49 PASS = 34 SOURCE + 7 CONST + 5 SYNTHETIC + 3 RULE.
- **QA-005e: still partially open.**
  - The VDV source page is live. The 5 files were re-downloaded to box scratch and their sha256 values match `SHA256SUMS`.
  - No licence is stated, so the manifest's `internal-test-only` / "none stated (UNVERIFIED)" is correct, and the folder carries RESTRICTED_INTERNAL_TEST_ONLY.md.
  - T12b-e were reproduced independently: dev = L·D/(2K), and absolute = cumulative from the bottom including the own row.
  - Remaining gaps:
    - These are CSV/TXT exports, not native .GTL/.dux files.
    - The Geosense file is a single survey, so there is no displacement-vs-baseline oracle.
    - The GTILT, DigiPro and RST files are parsed but have no reduced-output oracle.
    - It is unverified whether the samples are real or vendor test data.
- **QA-004: verified, closed with notes.** `procedures/piezometers.md` and `piezometers/verify.py` exist. verify.py runs **111/111 PASS, not the claimed 92/92**. Inputs were re-derived from the cited pages: Geokon 4500 p22/23/25, 4500MLP p35, Roctest p13-15, CS451 p15/19/20, USBR p62/86-87, Hvorslev p46 and USACE p272.
- **New `qa/piezometer_worked_examples.py`** (WE1-WE6, read by path): **35 PASS, 0 FAIL** (SOURCE 26, ILLUS 6, SYNTH 3).
  - WE1: Geokon linear, temperature and polynomial, plus 4500MLP.
  - WE2: Roctest cubic and Pcorr.
  - WE3: USBR head/elevation.
  - WE4: CS451 psi→ft.
  - WE5: Hvorslev (with the USACE C.2 constant ln10/(8·86400)).
  - WE6: HST fit on `syn_pz_hst.csv`, using the true coefficients read from piezometers/verify.py.

### E. New defects
| ID | Sev | Owner | Finding | Expected | Status |
|---|---|---|---|---|---|
| QA-009 | Low | owner unconfirmed (see note) | `dam-refs/references_index.csv` file lines 141-173 (33 rows, GEOKON-6160 … PEER-NGA) end in CRLF; all other lines are LF. The rows are tiltmeter ×4, plumbline ×4, accelerometer_strong_motion ×13, datasets ×3 and not-stored ×9. Their local files are dated 18:21-18:41 and are referenced from procedures/tiltmeter.md, procedures/accelerometer_strong_motion.md, tiltmeter/verify.py and accelerometer_strong_motion/verify.py. The CSV has **no owner/added_by column** and dam-refs is not a git repo, so the author can't be determined from the file. `references_index.csv.lock` (0 B, 18:46) exists. | LF only; add an `added_by` column | **Fixed, verified 19:26**: 0/189 lines CRLF. Adding `added_by` is still recommended |
| QA-010 | Low | Piezometer Specialist | `piezometers/verify.py` rewrites the dataset `datasets/piezometers/.../syn_pz_hst.csv` on every run | A verify script must be read-only; generate the file once with a separate script | **Closed 2026-11-02 20:53 EST**: verify.py only opens syn_pz_hst.csv for reading (line 144), with no writes. gen_syn_pz_hst.py uses seed 42. sha256 9e7e1fc0…d7d6 and mtime were unchanged across 2 runs. |
| QA-011 | Low | Piezometer Specialist | WE6 (HST) is self-consistency only: the fit recovers coefficients from data the same script generated | Label as SYNTHETIC/circular, or add an external HST oracle | **Closed 2026-11-02 20:53 EST**: procedures/piezometers.md:137 labels WE6 [SYNTHETIC] with a "self-consistency check only, not validation" note. The 4 verify cases are tagged synthetic, and index.md:96 agrees. |
| QA-012 | Low | Piezometer Specialist | Procedure WE4 cites the 5.76 psi input as [p20]; it's on CS451 PDF p19 (the result is on p20). verify.py cites USBR p55 for 2.31 ft/psi, but p55 has no extractable text; only p62 confirms it. | Fix the cites to the verifiable pages | **Closed 2026-11-02 20:53 EST**: pdftotext shows CS451 PDF p19 has "5.76 psig • 2.31 ft/psig = 13.3056 ft" and p20 has "So, the offset is…". USBR PDF p62 has "multiplying the readings by a factor of 2.31", "adding this value to the tip elevation" and "0.1 SCFH". **Correction to my earlier note:** USBR PDF p55 (printed p37) DOES have text: pdftotext gives 5029 chars of twin-tube procedure including "average the inlet and outlet readings", and the CITE regex passes on page index 55. Nit: verify.py:99 comment still says "USBR p55, p62" for ×2.31, but p55 has no "2.31"; it should say p62 only. |
| QA-013 | Low | Piezometer Specialist | Claimed "92/92"; actual 111/111 | Report the actual count, split SOURCE/CONST/SYNTH like inclinometers | **Reopened 2026-11-02 20:53 EST**: I measured 118 PASS / 0 FAIL, and the per-class split matches the claim exactly (SOURCE 33, CONST 6, ILLUSTRATIVE 4, RULE 10, SYNTHETIC 4, DATA 8, CITE 53 = 118). procedures/piezometers.md:4 and index.md:96/150 agree. But CONSOLIDATED_REPORT.md:9 still says "111/111", so the stale count is not fully fixed. It closes once that line says 118. |
| QA-014 | Low | Data Architect | 5 `_shared/manifest.csv` rows have an empty `usage_restriction` (USGS ×3, ZENODO-21888220, RESOPSUS). There are also free-text variants: "none", "open", "unrestricted", "n/a", "UNAVAILABLE - files restricted". | Controlled vocabulary per CONVENTIONS.md (`internal-test-only` / `unrestricted`) and no blanks | **Closed, verified 19:26**: 61 rows in 10 manifests, 0 blank; values are only internal-test-only (34) and unrestricted (27) |
| QA-015 | Low | App Dev | `.gitignore` ignores `qa/`, so the guard, its pytest and the hook are not versioned and CI never runs them. Other bots' formula modules and tests contain scalars quoted from manuals: risk note only, the guard finds 0 restricted hits. | Track `qa/restricted_guard.py`, `qa/restricted_denylist.json`, `qa/test_restricted_guard.py` and `qa/hooks/` (e.g. `qa/*` plus `!` exceptions, with matching bundle sync excludes), or move the guard to `tools/` + `tests/` | **Closed, verified 19:26** (`git check-ignore -v`): the guard, denylist, guard test and hooks/ are un-ignored by `qa/*` plus `!` exceptions; `qa/reference_examples.py` was also un-ignored (guard 0 hits) |

**Rerun:**
- `cd /workspace/dam-safety-app && .venv/bin/python -m pytest -q qa/test_restricted_guard.py`
- `python3 qa/restricted_guard.py` (rebuilds when needed, then scans)
- `.venv/bin/python qa/piezometer_worked_examples.py`


## 2026-11-02 19:26 EST: Bias rules, user-workbook audit review, TOA5 reader, suite re-runs

### 1. Inclinometer bias rules (procedures/inclinometers.md §5 and §7, lead revision)
**Wording vs Mikkelsen 2003: CONFIRMED.** QA read PDF p4, p5 and p7 from page images; the PDF has no text layer.
- p4: the checksum retains 2b and the reverse pass "cancels the bias"; a change between sets "is not normally a matter of concern".
- p5: a shift "only becomes an error if it occurs within the data set, between opposite runs".
- p7: the tabulated changes 12 and −34 give "12 −34 = −22, a correction value"; shifts are "generally less than 25 units".

INFO notes:
- "Justified in <1 % of datasets" is not from Mikkelsen; its source is unchecked.
- Mikkelsen says "opposite axes" where the procedure says "pass directions".
- The §5 caveat paragraph (line 126) still quotes the withdrawn "choose c so … matches" wording as history. That is fine, but it reads as live text.

**New `qa/test_inclinometer_bias_rules.py`** (data read by path; the p7 numbers are in `qa_private/inclinometer_bias_rules.json`): **6 PASS, 1 strict XFAIL.**
- **(a)** S3: the checksum shift flags SUSPECT. D is consistent: the residual against the neighbours (D2+D4)/2 is ≤ 1 digit. No ZA is proposed.
  - The app's `reduce_inclinometer_profile` on S3 vs S2 gives proposed_bias 0.0 mm (toe zone) and qc_checksum_ok on all rows. So it does not turn the shift into a correction, but it cannot see the shift either.
- **(b)** Synthetic survey 2 vs 1 (+6 on A0): ΔD = 6 constant below 7 m, so ZA = −6 is proposed.
  - The app's proposed_bias is 0.0600 mm per interval (= 6·L/2K).
  - Applying ZA = −6 gives a top A cumulative of 4.00 mm (= verify T6e).
- **(c)** 12 + (−34) = −22 (|c| < 25), i.e. −0.22 mm.
- **Gap (QA-020):** the app has no between-set SUSPECT flag and no Δ0+Δ180 function. Only the within-survey checksum deviation exists (`formulas.py`, `core.incl_checksum_flags`). Bias is never auto-applied, which is correct.

### 2. Review of the Inclinometer Specialist's user-workbook audit (REPORT.md)
**CONFIRMED in every sampled item.**
- `inclinometers/verify.py` gives 69 PASS / 0 FAIL, and my own count of classes is SOURCE 36, CONST 9, SYNTHETIC 5, RULE 3, USERTOOL 16.
- Workbook sha256 is da099d41…caa9b, with 14 sheets and 1873 formula cells. QA parsed it with its own zip/XML parser (no openpyxl).
- CSV raw data equals 2_RAWDATA (120/120).
- The core reduction was re-derived independently: 240/240 values match, max error 3.6e-15 in.
- LibreOffice headless recalc on a copy: 1872/1873 match. The only mismatch is 11_PRINT_REPORT!C16 = TODAY(), now 46328 = 2026-11-02, which proves that a recalculation actually ran.
- Errata sampled from formulas: E1 (the bias term cancels), E2 (dead inputs: 0 references to 4_CORRECTIONS C/E/G-J or 1_SETUP C31/C32/C37-39/C52:D66), E3, E4 (C15 = S1 absolute column G, σ typed 0.02), E5 (A24 = 0, A25 = 2, label 28 missing), E6, E7, E9 (421 IFERROR cells), E10, E12 and E13 (0 references to the boring log, and no chart uses it).
- E8 and E11 were not sampled.

### 3. Suite re-runs
| Suite | Claimed | QA result |
|---|---|---|
| app/tests (`app/.venv`) | 174 | **174 passed** |
| root tests (`.venv`) | 168 | **255 passed, 0 failed.** The count grew; it includes test_toa5 (57) and test_toa5_landing (6) |
| guard `qa/test_restricted_guard.py` | 5 | **5 passed** (fingerprints rebuilt 19:25: 97 files incl. DS-INC-USERTOOL; full scan 0 hits) |
| DEPENDENCY_PINS harness | 16/16 | **Not reproducible as-is**: in /tmp/pinvenv and /tmp/env6venv it does 11 PASS, then `ModuleNotFoundError: toa5` (PyTOA5 was uninstalled), so the pastas check never runs. A /tmp copy without the TOA5 block (harness not edited) gives **12/12 PASS** in both, pastas gain 0.599943. QA-022 |

- **test_bundle_config / jobs.yml:113:** now **passes**: `src/jobs/interpretation_run.py` exists (19:00). This is kept open per instruction pending DA confirmation; the fix itself is verified.
- `qa/reference_examples.py` is un-ignored (`!qa/reference_examples.py`; guard 0 hits; the bundle still excludes `qa/**`).
- The new QA test files stay ignored.

### 4. TOA5 reader (src/common/toa5.py)
- **App Dev's claim CONFIRMED:** 57 passed; 100 % line (181/181) and branch (86/86) coverage.
- **Primary-source check:**
  - The CR1000 manual is **not in dam-refs**. QA used Campbell's rev 2/18 PDF, and a fresh download has the same sha256 (9b91e875…). It should be added to dam-refs with an index row.
  - Header semantics match Tables 12/13 (PDF p146-147).
  - p437 prints the field `battfivoltfiMin` (a typesetting artefact); App Dev's fixture writes `batt_volt_Min`. That is normalised, not verbatim; say so in its comment.
  - RECORD max &hffffffff, then a restart at 0 (≈p439); the CR1000 reports 31 bits (&h7fffffff) to the support software, noted as INFO.
  - NAN: CR1000 Table 114 (p494) says FP2 shows "NAN" for a FLOAT variable and −7999 only for a LONG variable stored to FP2, while the CR1000X help says FP2 NAN = −7999. The sources conflict and App Dev quotes theirs faithfully; leaving sentinels unconverted is the right call (INFO).
  - The LoggerNet TOA5 page matches.
- **New `qa/test_toa5_adversarial.py`:** **72 PASS, 5 strict XFAIL**, all with hand-written fixtures. It covers:
  - BOM (file and text), double BOM rejected, UTF-16 and strict latin-1 rejected.
  - LF/CRLF/CR and mixed line endings.
  - Embedded commas in quoted values, station names and array names `Values(1,1)`; an unquoted "1,234" gives a count error.
  - Empty fields.
  - Truncation inside a quote or timestamp, or with fewer fields.
  - Duplicate and regressing records (kept, no diagnostics), RECORD bounds.
  - NAN/INF variants; Infinity and 1.#INF rejected.
  - Timestamp edge cases, header errors with line numbers, and the p437 example as printed.
- **End-to-end CONFIRMED:** QA-built TOA5 → common.toa5 → VW_PIEZO 1.0.0 → 418.77 kPa. This equals reference_examples PZ-03 = qa_private p_lin 418.8 (tol 0.05) and −0.1795·(6749−9082) = 418.7735 (Geokon 4500 PDF p22).

### 5. New defects
| ID | Sev | Owner | Finding | Expected | Status |
|---|---|---|---|---|---|
| QA-016 | Info | App Dev | CR1000 manual not in dam-refs; p437 fixture name normalised; FP2 NAN sources conflict (Table 114 vs CR1000X help); 31-bit RECORD reporting | Add the manuals and index rows; comment the fixture | Open |
| QA-017 | Medium | App Dev | A last line with no terminator, cut inside its final numeric field or right after a comma, keeps the field count and is accepted silently (`2.5`→`2` int, or `None`). This is the typical partial-write or collection-in-progress case. | Flag or reject an unterminated final line (or return it as `partial`) | CLOSED (19:4x, markers removed; see QA-017/019 CLOSED below) |
| QA-018 | Low | App Dev / DA | Duplicate and non-monotonic RECORD/TIMESTAMP are accepted with no diagnostics, in both the reader and toa5_landing; rollover cannot be told from regression | Report duplicates and sequence breaks (dedup at landing on table+RECORD+TIMESTAMP) | Open |
| QA-019 | Low | App Dev | `\d` without re.ASCII accepts non-ASCII digits in TIMESTAMP, RECORD and values; "+5" and "-0" are accepted as RECORD | `[0-9]` / re.ASCII; RECORD `[0-9]+` | CLOSED (19:4x; see below) |
| QA-020 | Medium | App Dev (with Geotech lead) | No between-set checksum-shift SUSPECT flag and no c = Δ0+Δ180 / ZA proposal function in the app; proposed_bias ignores the checksum and is returned whenever stable_from_m is set | Implement §7: SUSPECT on a shift, ZA proposed only with D-profile evidence, never auto-applied | Open (strict xfail) |
| QA-021 | Medium | Geotech lead | §5 says "Add c to A0 (ZA = c)" with c = Δ0+Δ180, but Δ's sign is undefined. With Δ = set − reference, ZA = +c doubles the error (synthetic: +6 → +12); verify T6e corrects with −6. Mikkelsen p7 says only "a correction value". Also, §7's "D-profile **or** within-set evidence": Δ0+Δ180 is identically the checksum change (S3 equals the full shift although D is consistent), so it is not independent evidence. | Define Δ (reference − set) or ZA = −c; change "or" to "and"/"sized by" | Open |
| QA-022 | Low | App Dev | DEPENDENCY_PINS "16/16" no longer reproducible: the harness aborts at `import toa5` and pastas is unchecked | Re-run with the TOA5 block removed (12/12) and update the doc | Open |

**Rerun:**
- `cd /workspace/dam-safety-app && app/.venv/bin/python -m pytest -q -p no:cacheprovider qa/test_toa5_adversarial.py qa/test_inclinometer_bias_rules.py qa/test_restricted_guard.py`

## 2026-11-02 19:55 EST: Fix verification (QA-016..022, QA-003/009/014, bundle_config), QA-021 sign convention, QA-020 thresholds, T16
Snapshot 19:49:45 EST; other agents were editing during the run, so counts are for that time.

**Reruns:** verify.py **75 PASS / 0 FAIL** (SOURCE 38, CONST 10, SYNTHETIC 6, RULE 4, USERTOOL 17, plus INFO). qa/ **115 passed, 2 xfailed** (both strict, see below). app/ **190 passed** (the 6 v1.1-migration failures seen at 19:4x are gone). root tests/ **322 passed, 3 failed, 1 skipped**. testonly_restricted/ 5 passed. tests/test_bundle_config.py 15 passed. Guard rebuilt 19:38 (98 files, 58 names); scan **0 hits**.

**Root failures (open):**
- `test_license_leak::test_catalog_seed_in_sync_with_manifests`: `sql/seed/dataset_catalog.csv` is missing DS-INC-M2007-DMM-BIASSHIFT. Re-run tools/dataset_catalog.py (Data Architect).
- `test_license_leak::test_sync_excludes_cover_gitignore_licensing_patterns`: `node_modules/` is in .gitignore but not in the sync excludes (DA / App Dev).
- `test_instrument_vocab::test_app_onboarding_types_and_units_are_in_vocab`: `ast.literal_eval` fails on a DictComp at line 24 of the app constant it parses, so the test needs updating for the new app code (App Dev / DA).

### QA-021 sign convention: NOT CLOSED
The procedures/ and inclinometers/ copies are now byte-identical (19:44:57). The new §5 paragraph defines Δ = suspect − reference, bs = Δ0+Δ180 and ZA = −bs (= −ΔD when the cross-check agrees). That is consistent with §7.1 line 171, verify.py T16 (`ZA = -dD`) and the engine's numeric output (T16a engine ZA −6.875; synthetic +6 gives ZA −6). The app has no ZA function. But mixed wording remains, and per the brief that is a defect:
1. §5 row line 115 still opens "Add c digits to A0 (GTILT `ZA=c` …)" and ends "Never set c just to make…". `c` is no longer defined.
2. §7.1 intro line 166: "The ZA sign is QA-021 and is not settled here." This is stale.
3. §7.1(v) line 171: "|c| ≤ 25". It should be |ZA| (= |ΔD|).
4. `bs` has two definitions: line 115 bs = Δ0+Δ180 (the checksum change), and line 171 "= bs = BSE/(N×C)" (a D-profile quantity, Mikkelsen p6). In the DMM example they differ (by path: `/workspace/dam-refs/qa_private/inclinometer_bias_expected.json` `dmm_bs` vs `dmm_dD`).
5. §5 line 115 says the worked example "matches verify T6e". It does not. T6e is the synthetic dataset (4.00 mm corrected, raw 5.20, 20 intervals); the worked example is 50 intervals with 3.0/0.0/6.0 mm.
6. verify.py T15c text "sign of the entered value is QA-021" and T15e text "Procedure sec.5 sizes c = d(0-pass)+d(180-pass)" are stale.
7. The engine uses the opposite naming: `qc.py` mean_dD = D_reference − D_suspect, `za_proposed_c` "ZA/ZB = c, D' = D + c"; `corrections.py` docstring "D' = D + c, c = FULL value added to A0 [procedure §5.1]" (there is no §5.1). The numbers agree; the names do not.
- The QA test file had the same problem (Δ = ref − suspect, ZA = c). It is fixed in this entry.

**Mikkelsen 2003 p7 (page image /tmp/mk03p7-07.png): VERIFIED as quoted.** "progressively negative indication of displacements, 20 mm by 1998"; "the last data set being corrected −22"; "a total positive displacement of 5 mm"; "12 −34 = −22, a correction value"; "generally less than 25 units". The inference that removing a negative bias moves the profile positive (ZA = −bs) is consistent with p6, where BSE = −10 mm is the *error*. Caveat (INFO): p7 itself calls −22 "a correction value" and says the set was "corrected −22", which on its face reads as ZA = −22. So the source wording is ambiguous, and Mikkelsen's subtraction direction for 12/−34 is unstated. The procedure already marks this UNVERIFIED, and ΔD would be 46.

**T6e / §5 worked example: arithmetic VERIFIED independently** with app `inc_deviation` (L 500 mm, K 25000, ref A0 500/A180 −480, suspect A0 506, 50 intervals): raw top 3.000 mm, ZA −6 gives 0.000 mm, ZA +6 gives 6.000 mm. verify T6e itself (4.00 = 4.00, raw 5.20) also reproduced in qa/. These are two different cases (defect 5 above).

**GTILT <1% quote:** VERIFIED at PDF p45 [printed p6-11] (earlier entry). §5/§7 AND wording is present.

### T16 (Inclinometer Specialist §5 implementation): VERIFIED, with an open item
- T16a +7 bias: ΔD 6.875, bs 7.125, tol 2.625: CORRECT, ZA −6.875.
- T16b Mikkelsen 2007 DMM A: diff ||ΔD| − |bs|| exceeds tol max(2.0, 3·SE): SUSPECT (values by path: `/workspace/dam-refs/qa_private/inclinometer_bias_expected.json` `dmm_dD`, `dmm_bs`, `dmm_tol`, `dmm_crosscheck_diff`). Inputs checked cell by cell against the PDF p16 image.
- T16c S3: no correction.
- **Open (engineering judgment, production blocker): QA-024.** The T16b verdict depends on the UNVERIFIED tolerance. It flips to CORRECT at tol_abs ≥ `dmm_rank_flip_tol_abs` or tol_k just above `dmm_rank_flip_tol_k_below` (`/workspace/dam-refs/qa_private/inclinometer_bias_expected.json`). The procedure's rounded thresholds are imprecise, because tol_k = `dmm_rank_flip_tol_k_below` still gives SUSPECT. It needs a sourced value or explicit Geotech-lead sign-off before production. **[Superseded 20:2x: QA-024 closed as not a production blocker, see the QA-024 re-verification below.]**
- **New QA-023 (Medium, Inclinometer Specialist):** engine `qc.between_set_bias_qc` has no §5 cross-check (gate iv uses equal_opposite_ratio 0.5). It returns PROPOSED ZA (`dmm_za`, `/workspace/dam-refs/qa_private/inclinometer_bias_expected.json`) on the DMM case, where §5/T16b say SUSPECT. Strict xfail `test_c_dmm_engine_does_not_propose`.

### QA-020 thresholds (§7.1): test updated, item stays OPEN (app)
- `qa/test_inclinometer_bias_rules.py` was rewritten (backup /tmp/test_inclinometer_bias_rules.pre1950.py). It uses one convention (Δ = suspect − reference, ΔD = mean(Δ0−Δ180), bs = mean(Δ0+Δ180), ZA = −ΔD) and the §7.1 parameters (CHK 10/20, SD ≤ 8/16, |ΔD| ≥ 3·SD/√n, n ≥ 5, |ZA| ≤ 25, cross-check max(2.0, 3·SE)). The reference is the baseline: workbook S1 vs suspect S3, and synthetic "0" vs "2".
- The old QA "spread ≤ 2" conflicts with the source: the DMM example SDs on both axes exceed 2 (verify T15d, reproduced; values in verify_output.txt, read by path).
- S1/S3 result: |Δ mean CS| 90, so SUSPECT. Bottom-5 fallback zone SD 33.3 > 8, so NO_PROPOSAL. The engine agrees (SUSPECT, NO_PROPOSAL).
- Strict xfail `test_a_s3_app_flags_between_set_suspect` now uses that real baseline/suspect pair. The positive control passes and the app returns no between-set flag.

### USERTOOL relabel (CONVENTIONS.md: the workbook is AI-made, layout reference only)
- qa/ bias tests use workbook readings only as real input, never `wb_*` result columns. The docstring now says so.
- `testonly_restricted/test_inclinometer_usertool.py` compared the engine against workbook values with the docstring "vs the user's workbook worked values". It is relabelled "REGRESSION checks … does NOT verify either" (backup /tmp/test_inclinometer_usertool.pre1955.py).
- tests/test_inclinometer_geotech_answers.py (workbook *import profile*) is layout-only. OK.
- **Open (DA):** `docs/VERIFICATION.md` line 13 lists "internal workbook values in testonly_restricted/" in the §1 VERIFIED table. Move it to a regression-only row.
- verify.py keeps USERTOOL as a separate class and states "Only [SOURCE] cases are verification". OK.

### Item 2 / item 3 status (from the 19:4x checks, unchanged)
- **QA-017 and QA-019: CLOSED.**
- **QA-018:** reader CLOSED; the landing residual (`toa5_landing.py` ignores warnings, no dedup) stays OPEN with the DA.
- **QA-016:** docs CLOSED; manuals not in dam-refs (INFO).
- **QA-022:** doc CLOSED; harness `/workspace/oss-review/verify/verify_pins.py` still aborts (owner unassigned).
- The adversarial diff against /tmp/test_toa5_adversarial.orig.py shows no assertion weakened beyond the strict-reject default. proposed_bias suppression is confirmed in the API, contract and frontend.
- **QA-003: NOT CLOSED.** Catalogs and the 7 schemas agree; audit.review_record, rpt.dssmr and rpt.dssmr_draft exist. But SCHEMA_CONTRACT is still "PROPOSED v1.1", says "all additive except qa_reason" while its tables carry renames (tip_el_m, certificate_uri, cum_a), contains v1.2 sections, and has no App Dev or QA sign-off.
- **QA-014: CLOSED.** Enforced at ingestion (tools/dataset_catalog.py and seed job).
- **QA-009: CLOSED.** LF only, added_by present; "unknown" attribution is INFO.
- **bundle_config: CLOSED** (15 passed).
- `00_catalog_schemas.sql` comment cites a non-existent resources/uc.yml (INFO).

### Defects added
| ID | Sev | Owner | Finding |
|---|---|---|---|
| QA-023 | Medium | Inclinometer Specialist | Engine proposes ZA on the DMM case where §5 requires SUSPECT (no cross-check tolerance in qc.py) |
| QA-024 | High (prod blocker) | Geotech lead | T16b verdict rests on the UNVERIFIED max(2.0, 3·SE) tolerance; flips at tol_abs ≥ `dmm_rank_flip_tol_abs` / tol_k above `dmm_rank_flip_tol_k_below` (`/workspace/dam-refs/qa_private/inclinometer_bias_expected.json`) |
| QA-025 | Low | Geotech lead | Mixed sign wording, items 1-6 above (c, "not settled", two bs definitions, wrong T6e cross-ref, verify T15c/T15e text) |
| QA-026 | Low | Inclinometer Specialist | Engine naming dD = ref − suspect, `za_proposed_c`, "D' = D + c", "§5.1" |
| QA-027 | Low | Data Architect | Stale dataset seed; node_modules/ sync exclude; VERIFICATION.md lists workbook tests as VERIFIED |
| QA-028 | Low | App Dev / DA | test_instrument_vocab literal_eval breaks on the new DictComp |

Rerun: `cd /workspace/dam-safety-app && app/.venv/bin/python -m pytest -q -p no:cacheprovider qa/ && .venv/bin/python -m pytest -q -p no:cacheprovider tests/ testonly_restricted/ && (cd app && .venv/bin/python -m pytest -q -p no:cacheprovider) && (cd /workspace/dam-refs/inclinometers && /workspace/dam-safety-app/.venv/bin/python verify.py | tail -1) && python3 qa/restricted_guard.py scan`

## 2026-11-02 20:05 EST: Pre-commit hook review, git history scan, QA-020 against the engine, node_modules, §5 ruling update

**Pre-commit hook (Genie edit, in commit ca3f854): WEAKENED the guard. Fixed in the working tree, not committed.**
Genie's line 5 drops `qa/(restricted_guard.py|restricted_denylist.json|test_restricted_guard.py)` from the staged list
before BOTH the name check and the content scan, using an unescaped regex. In a throwaway clone (/tmp/hookverify, origin
removed, never pushed) I planted test copies of restricted data. Genie's hook ALLOWED three of them:
- DMM rows in qa/test_restricted_guard.py
- workbook rows in qa/restricted_guard.py
- DMM rows in the lookalike qa/restricted_guardXpy (the regex dot matches any character)

Proposed hook (/tmp/pre-commit.proposed, now in qa/hooks/pre-commit; Genie's version backed up at /tmp/pre-commit.genie1945):
- The content scan still covers every staged file.
- Only the three exact paths (escaped and anchored) are exempt from the name/path check.
- Clean edits to the guard files pass, which was Genie's intended false-positive fix.
- A clean file such as docs/restricted_notes.md is still blocked by the name check. That is deliberate and conservative.

**QA-029 (guard gap, found while testing; fixed in qa/restricted_guard.py, uncommitted, backup /tmp/restricted_guard.pre2000.py).**
- Low-precision integer rows (workbook, DMM) were caught only by exact-line text hashes. The same rows evaded the guard in every file once reformatted: `# `-prefixed, tab-separated, or as a markdown table.
- Fix: add a separator-independent per-line number-sequence hash (num_lines.u64 in the cache; at least 6 numbers, at least 3 distinct non-zero).
- Result: all 6 reformatted probes are caught, the repo still shows 0 false positives, and test_restricted_guard passes 5/5.
- Remaining gap: dropping or reordering columns still evades the guard.

Matrix with the proposed hook plus the QA-029 guard:

| Plant | Result |
|---|---|
| Renamed exact copy | BLOCKED |
| Workbook rows in .md | BLOCKED |
| `# `-prefixed rows in .py | BLOCKED |
| DMM markdown table | BLOCKED |
| datasets/ path | BLOCKED |
| DMM rows in qa/test_restricted_guard.py | BLOCKED |
| Workbook rows in qa/restricted_guard.py | BLOCKED |
| qa/restricted_guardXpy | BLOCKED |
| Clean guard and denylist edits | allowed |
| Clean README edit | allowed |

With Genie's hook and the same guard, the qa/test_restricted_guard.py, qa/restricted_guard.py and qa/restricted_guardXpy plants are still ALLOWED.

**Git history: no restricted data was ever committed.**
- History: 1 commit (ca3f854), and refs main = origin/main = ca3f854 (local tracking ref).
- The remote itself could not be read: `git ls-remote` needs credentials, and the GitHub app has no access to ikariimii/DSM. Nothing was pushed by QA.
- The guard (with QA-029) reports 0 hits on all of these:
  - the HEAD tree (209 files)
  - every reachable blob (207, via rev-list --all --objects)
  - the full `git log -p --all` dump and its added lines
  - the 2 unreachable local blobs
- No history path matches dam-refs, datasets, testonly_restricted, qa_private, user_tool, mikkelsen, vendor_manual or .xlsx.
- INFO: the committed qa/restricted_denylist.json holds restricted-file basenames and sha256 values only, no data.

**node_modules: CLOSED (QA-027b).**
- It is ignored by .gitignore:66 and app/.gitignore:6, and 0 tracked files contain node_modules.
- databricks.yml sync.exclude now lists `app/frontend/node_modules/**` and `node_modules/**`.
- test_sync_excludes_cover_gitignore_licensing_patterns passes. No databricks CLI is on the box, so `bundle validate` was not run.

**QA-020 against the engine (qa/test_inclinometer_bias_rules.py; backup /tmp/test_inclinometer_bias_rules.pre2005.py).**
- BiasQCParams defaults equal the §7.1 values, and `require_sign_agreement` has been removed (asserted absent or False).
- Real pair S1 baseline vs S3, both ways round: suspect_flag and qa_flag SUSPECT, NO_PROPOSAL, and the Δ mean checksum flips sign exactly.
- App path (batch.reduce_instrument → qa_reason → contract.bias_qc), each PROPOSED and applied=False:

| Case | ZA |
|---|---|
| Synthetic +6 on A0 | −6 |
| Swapped pair | +6 |
| A180-only +6 | +6 |

- between_set_bias_qc never changes reduced data:
  - The reduced columns are identical with and without a proposal.
  - Top cum_a stays at the raw 5.20 mm, with no correction_record_ids.
  - Engine inputs are unmodified (deep-copy check).
- The old app xfail (reduce_inclinometer_profile has no between-set flag) is replaced by the batch/contract path test.

**§5 ruling update (lead, 2026-11-02): no sign gate; ZA = −ΔD always, on A0; never auto-applied; the tolerance only ranks.**
- A180-only δ = +6 gives ΔD −6, bs +6, verdict AGREE, ZA +6, and a stable-zone result of 0.0 mm. ZA = −bs would double the error. No gate rejects it, in either the engine or the QA helper.
- DMM: the engine gives SUSPECT_CROSSCHECK with verdict SUSPECT, proposes ZA = `dmm_za` (= verify T16b), and reports |ΔD| = `dmm_za` and |bs| = |`dmm_bs`| (values by path: `/workspace/dam-refs/qa_private/inclinometer_bias_expected.json`).
  - The QA helper agrees, and its ZA is unchanged when the tolerance flips the rank.
- **QA-023 CLOSED.** Its strict xfail XPASSed at about 20:00; it is now a passing test.
- **QA-032 (QA's own helper, FIXED).** The helper accepted significance at |ΔD| ≥ 3·SE; the procedure and the engine say strict >. It is now strict >. No current test case sits exactly on the boundary.
- **QA-030 (new, Medium, App Dev). Strict xfail.**
  - contract.bias_qc returns proposed_c, proposal_record_id and evidence only when status == PROPOSED.
  - A SUSPECT_CROSSCHECK proposal therefore reaches the API without the ZA, record id or magnitudes the reviewer must approve.
  - The contract.py:252 label still reads "review, no proposal".
  - Reproduced end to end: synthetic +6 on A0 plus a common +2 gives ΔD 6 and bs 10, and the API shows proposed_c None.
- QA-024 (T16b tolerance) stays open, but its impact is now ranking only, not whether data are corrected. The lead decides whether it is still a production blocker (the engine comment says yes).

**QA-021: NOT CLOSED. The sign convention now agrees across §5 (procedures/ copy), verify.py (76/76, T15c/T15e wording fixed, T16d added), the engine (ΔD = suspect − reference, ZA = −ΔD) and the app.** Residuals, documentation only:
1. dam-refs/inclinometers/inclinometers.md has diverged from procedures/inclinometers.md: its header says 75 PASS and its row 174 lacks the no-sign-gate and never-auto-applied ruling.
2. The §5 worked example still says "matches verify T6e". T6e is a different case (4.00 mm corrected, raw 5.20).
3. Line 171 still defines `bs = BSE/(N×C)`, a second, D-based meaning of bs.

QA-026 (corrections.py:9 "c = FULL value added to A0") stays open, Low.

**Also closed this run:**
- QA-027a: the seed now includes DS-INC-M2007-DMM-BIASSHIFT.
- QA-028: test_instrument_vocab passes.

QA-027c (docs/VERIFICATION.md:13 lists workbook values in the VERIFIED table without the regression-only label) stays open.

**New in-flight failure (root):** tests/test_inclinometer_bias_qc.py::test_contract_docs_and_ddl_expose_fields.
- The engine added `abs_dD`, `abs_bs` and `crosscheck_verdict`; SCHEMA_CONTRACT.md does not document them yet and still lists the removed `sign_agree`. *(Resolved in v1.4, 20:40: the contract documents the QA021-v2 fields; sign_agree has no reader.)*
- Owner: Data Architect / Inclinometer Specialist.

**Reruns at 19:59 EST:**

| Suite | Result |
|---|---|
| qa/ | 124 passed, 1 xfailed (QA-030) |
| testonly_restricted | 7 passed |
| tests/ | 328 passed, 1 failed (above), 1 skipped |
| app | 193 passed |
| verify.py | 76/76 |
| Guard | 0 hits |

| ID | Sev | Owner | Status |
|---|---|---|---|
| QA-029 | High | QA → Genie to commit | Fixed in working tree: guard missed reformatted low-precision rows |
| QA-033 | High | Genie | Hook weakening in ca3f854; fixed in working tree, needs commit |
| QA-030 | Medium | App Dev | CLOSED (2aeeb13) |
| QA-032 | Low | QA | Fixed |
| QA-023 | n/a | n/a | Closed |
| QA-027b | n/a | n/a | Closed |
| QA-028 | n/a | n/a | Closed |


## 2026-11-02 ~20:40 EST: QA-003 v1.4 sign-off review, remote guard check, QA test un-ignore

Nothing was committed or pushed by QA. The only staging was a dry run in a throwaway clone (/tmp/dsm-remote, origin removed).
The user's computer was offline, so the docs and CLI source were fetched over the box network. This had no effect on the results.

### 1. Remote verification (gh)
- origin/main = 2aeeb13; 1831f63 is an ancestor.
- The remote files match the box byte for byte (sha256): qa/hooks/pre-commit, restricted_guard.py, restricted_denylist.json, test_restricted_guard.py.
- Guard on the remote clone: 212 tracked files, 0 hits; 257 history blobs, 0 hits.
- **QA-033 and QA-029: Closed.**

### 2. QA-030 and the App Dev edit to qa/test_inclinometer_bias_rules.py
- QA-030 is verified fixed in 2aeeb13, with the xfail removed.
- Audit method: test inventory. My pre-edit version had no backup, so no byte diff was possible.
- Nothing was weakened. The opposite-sign test is framed as "no sign gate", and the SUSPECT contract test was strengthened.
- Restricted-derived literals now live in qa_private via `_exp()`, which skips when that file is absent.
- .gitignore now un-ignores qa/test_*.py and the two helper scripts. This fixes the 2/5 failure of test_restricted_guard.py on a clean checkout.
- Ready to commit: .gitignore, the qa/test_*.py files (including the new qa/test_contract_v14.py), qa/inclinometer_independent_check.py, qa/piezometer_worked_examples.py, QA.md.
- **QA-034 (open, lead decision):** QA.md (tracked, bundle-synced) and docs/VERIFICATION.md hold values derived from the restricted DMM bias-shift sample. Options: redact, add QA.md to sync.exclude, or rewrite history. QA has rewritten nothing.

### 3. QA-003: SCHEMA_CONTRACT v1.4 / api_contract v1.4 verdict: **NOT signed off**

#### Confirmed OK
- **QA021-v2 fields:**
  - abs_dD, abs_bs, crosscheck_verdict and requires_review are documented.
  - sign_agree has no reader in code, SQL or frontend.
- **incl_reduced:**
  - The DDL 06, batch.REDUCED_COLUMNS and the Spark schema match column for column.
  - formula_version is 1.0.0 (seeded INCL_PROBE_2PASS, verified), and qc_rule_version is QA021-v2.
  - installation_version and probe_constants are written.
- **NULL calib_id approval:** requires a verified INCL_PROBE_2PASS 1.0.0 plus a matching ref.incl_installation version.
- **Applied bias without reviewer approval:**
  - Raises ValueError for a missing reviewer, self-review, a missing reviewed_utc, or an empty reviewer.
  - A stored row of this kind is skipped and flagged CORRECTION_RECORD_INVALID, and its value is not applied.
  - Covered by the new qa/test_contract_v14.py.
  - No qa/ test builds such a record. That is asserted too.
- **alarm_episode:** formula_id and qa_flag come from the latest reading-driven transition.
- **§7.3 cleanup:** done.
- **Alarm history traceability:** append-only + CDF, ACK actor/ts, threshold version, formula lineage.

#### Blockers
| ID | Sev | Owner | Finding | Expected | Status |
|---|---|---|---|---|---|
| QA-035 | **Critical** | App Dev / DA | alarms.py `tr()` emits CLEAR, RTN_UNACK and DEESCALATE with metric_value=None. This violates ck_alarm_transition_value, and the alarm_evaluator pre-write guard raises RuntimeError for the whole batch. The first return to normal stops alarm evaluation. tests/test_alarm_lineage_contract.py checks only `value`. | Carry the evaluated metric on every reading-driven transition, OR drop metric_value from the CHECK and guard and update §4. Extend the test to metric_value. | Fixed in local e01e1ab (verified 20:3x, unpushed); marker removed |
| QA-036 | High | DA / App Dev | contract.approval_status returns `unreviewed_applied`, and the frontend renders it, but api_contract §4.1 lists only proposed/approved/superseded. | Document it (or remove it from the code) | Fixed in a9e7d56 + e01e1ab CHECK (verified, unpushed); test now expects DataIntegrityError |
| QA-037 | High | App Dev | The databricks.yml prod target `permissions` (dam-portfolio-all CAN_VIEW) apply to every bundle resource. The databricks/cli source (apply_bundle_permissions.go, main) maps CAN_VIEW→CAN_USE for apps and appends it when app.yml has no entry for that principal. So in prod, dam-portfolio-all gets CAN_USE on the app, which defeats per-dam CAN_USE. dev/test are clean. tests/test_bundle_config.py checks only app.yml. | Move dam-portfolio-all CAN_VIEW from the target level onto the specific jobs/pipelines. Run `databricks bundle validate -t prod` and attach the resolved app permissions. | Fixed in local e01e1ab (code/mapping verified, unpushed); `bundle validate -t prod` still unrun |
| QA-039 | High | DA | FERC REQ-22/23: ref.incl_correction is not appendOnly. Approval is mutable free text (reviewed_by/reviewed_utc), with no rationale, PE licence or signature, and no link to audit.review_record. | The contract defines approval as an insert-only record (an audit.review_record row with subject_type='incl_correction', subject_id=record_id, comment=rationale, signature_hash over the record), required by the approval rule. ref.incl_correction is appendOnly, and UPDATE is not granted. | Open |

#### Minor / Info
- The installation_version rule accepts any installation row, not only the current one. Clarify the contract wording "current".
- No ALTER ADD COLUMN exists for the v1.4 columns on existing tables. ADD CONSTRAINT fails on existing violating rows. Nothing is deployed yet.
- The relabel of the crosscheck_tol rule as "not a production blocker" (QA-024) still needs lead confirmation.

#### Suite rerun (20:40)
| Suite | Claimed | Measured |
|---|---|---|
| root `pytest` (testpaths=tests) | 341 + 1 skipped | **334 + 1 skipped**; testonly_restricted adds 7 more, for 341 total |
| app | 193 | **221 passed** |
| qa | 125 | **135 passed, 6 xfailed** (125 + 10 new passes; xfails are QA-035/036/037) |
| guard | 0 | **0 hits** |
| verify.py | | RULE 4/4, USERTOOL 17/17 PASS |

### Ledger delta
| ID | Status |
|---|---|
| QA-033 | Closed |
| QA-029 | Closed |
| QA-030 | Closed (fixed in 2aeeb13) |
| QA-034 | CLOSED (fe4ed7b, pushed) |
| QA-035 | Open, Critical |
| QA-036 | Open |
| QA-037 | Open |
| QA-039 | Open |

## 2026-11-02 ~20:55 EST: QA-034 redaction, QA-021/QA-024 re-verification

### QA-034 (Genie: redact, no history rewrite)
- **QA.md:** every value derived from a restricted source is replaced with a by-path reference: the DMM bias-shift case, DMM manual examples, DigiPro2 T7b, DGSI_Rotation IN-08/IN-09 and the Geokon 6150 sample. Keys are in `/workspace/dam-refs/qa_private/inclinometer_bias_expected.json` and `inclinometer_expected.json`, otherwise verify_output.txt or the restricted PDF by path. Verdicts and reasoning are unchanged. Public and synthetic values stay (data_reduction.pdf, GTILT, Geokon 4500, CS451, Mikkelsen 2003, T6e, T16a, T16d).
- **docs/VERIFICATION.md (Data Architect's file):** The INC-QA021 row is redacted the same way. The commit carries a one-line, index-only redaction of HEAD's line 68: the blob was built from `git show HEAD:docs/VERIFICATION.md`, so none of the Data Architect's uncommitted edits are included. Their working-tree version of line 68 is also redacted, so their next `git add` keeps the redaction. f33a5a1 and earlier still contain the old wording in history, which is not rewritten (Genie).
- **databricks.yml:** `QA.md` added to `sync.exclude`. Only that hunk is committed; the uncommitted QA-037 permissions rework in the same file is not.
- **Guard:** `qa/restricted_guard.py` gains a derived-scalar check. `build` reads `qa_private/restricted_derived_scalars.json` by path, and the out-of-repo cache stores hashes only. A line hits on one distinctive scalar, or on two or more scalars from the same source group. A file hits on three or more scalars from one group. Sign and trailing zeros are ignored. The check does not apply to `testonly_restricted/` while git ignores it (it is the sanctioned local-only location). New test: `qa/test_restricted_guard.py::test_guard_catches_restricted_derived_scalars`, which reads its probe values from qa_private at runtime.
- **Probes (planted in qa/_probe034/, then removed):**

| Probe | Result |
|---|---|
| Pre-redaction QA.md | HIT, 15 lines |
| Pre-redaction VERIFICATION.md (working tree) | HIT, line 68 |
| f33a5a1 VERIFICATION.md | HIT, line 68 (redacted in this commit: 0 hits) |
| One distinctive scalar | HIT |
| Reformatted (Unicode minus, trailing zero, md table) | HIT |
| Pair | HIT |
| Three of a group on separate lines | HIT |
| Two scalars from different groups (negative) | clean |
| Lone group member (negative) | clean |
| Synthetic T16a values (negative) | clean |

  The pre-commit hook run on a staged probe exits 1 ("restricted data detected"). The probes were unstaged and deleted.

### QA-021 re-verification (Geotech lead)
| # | Check | Result |
|---|---|---|
| 1 | `cmp` of procedures/inclinometers.md vs inclinometers/inclinometers.md | **Byte-identical** (same sha256). verify.py: **76 PASS, 0 FAIL** (SOURCE 38, CONST 10, SYNTHETIC 7, RULE 4, USERTOOL 17). The fresh run equals verify_output.txt byte for byte. |
| 2 | §5 worked example | Labelled "hand-worked illustration, not itself a verify case; the same rule is tested by T16a (A0-only +7) and T16d (A180-only +6)". No T6e claim (T6e appears only in §6 item 8, a different synthetic case that exists in verify.py). Arithmetic rechecked: Δ0 +6, Δ180 0 → ΔD = bs = +6; 6 × 0.01 × 50 = 3.0 mm; ZA −6 → 0.0 mm. **Closed.** |
| 3 | §7.1 line 171 vs Mikkelsen 2003 PDF p6 (page image rendered to /tmp; the page is a scan with no text layer) | p6: "bs = BSE / (N x C)", with bs = bias-shift correction per interval, BSE = total bias-shift error over the zone, N = reading intervals, C = reading interval/2K = 0.01 mm. That is the same per-interval sizing as the DGSI-BIAS formula, (cumulative error / N) / 0.01 mm, so "same quantity" is correct. Line 171 also says it is not the §5 bs = Δ0 + Δ180, which is a cross-check only. ΔS (§5, line 115) appears only as a reason SUSPECT means review, i.e. a cross-check. **Closed.** Info: p6's printed arithmetic, −10 mm / (42 × 0.01 mm), is −23.8, not the printed −22. This does not affect the rule. |

### QA-024 re-verification (lead: tolerance is not a production blocker)
- **Configurable:** `qc.BiasQCParams.crosscheck_tol_abs` / `crosscheck_tol_k` set tol = max(abs, k·SE). The batch accepts an injected bias rule (`batch.DEFAULT_BIAS_RULE`). Info: there is no job- or table-level parameter. A deploy-time change means a code change plus a rule-version bump, which reduces lineage, consistent with the rule. **Verified.**
- **Ranking only:** changing the tolerance changes only the verdict (AGREE vs SUSPECT_CROSSCHECK), never ZA, and never sets applied. **Verified** (new `qa/test_qa024_tolerance.py`).
- **UNVERIFIED in the audit record:** detection_statistic carries params_unverified=True, requires_review=True, crosscheck_tol (the effective configured value) and crosscheck_verdict. **Verified.**
- **UNVERIFIED in the API:** §4.1 proposal display_status "UNVERIFIED", applied_by_app False, crosscheck_tol equal to the configured value, evidence.params_unverified "True". **Verified.**
- **UNVERIFIED in the UI:** InstrumentDetail BiasQc shows "UNVERIFIED, never applied by the app", "UNVERIFIED, not applied by the app" on the proposal table, Δdiff/tol, and the display_status badge. **Verified** (static source test; not rendered in a browser).
- **No apply without a reviewer-approved record:** even an AGREE-ranked proposal raises when applied without review, with self-review, or with no review time. The independent reviewer plus reviewed_utc path is accepted. **Verified** (`qa/test_contract_v14.py::test_qa024_*`, plus the earlier QA-003 cases).
- QA-024 **closed** as not a production blocker. The tolerance stays UNVERIFIED (no source number). Caveat: at f33a5a1 the API and audit record already carry the tolerance and UNVERIFIED status, and the UI labels proposals UNVERIFIED, but the UI **displays the tolerance value only from App Dev's unpushed a9e7d56**. `qa/test_qa024_tolerance.py::test_ui_shows_the_unverified_tolerance_value` is a strict xfail until that lands.

### Suites (20:55; working tree, other bots' uncommitted edits present)
| Suite | Result |
|---|---|
| guard scan | **0 hits** |
| guard tests | 6 passed |
| qa | **145 passed, 1 xfailed (QA-036), 5 strict-XPASS failures**: QA-035 ×4 and QA-037 now pass on the working tree because of uncommitted App Dev/DA fixes (alarm_evaluator.py, databricks.yml permissions). Drop the markers when those fixes are committed and QA re-verifies them. In a clean worktree of the commit alone, these remain xfail (see report). |
| root | 359 passed, 1 skipped |
| app | 238 passed |

Clean worktree of this commit alone (f33a5a1 + QA-034 files, no other bots' edits): guard 0 hits; qa **143 passed, 9 xfailed** (QA-035 ×4, QA-036 ×1, QA-037 dev/test/prod, QA-024 UI tolerance display). Root shows 1 failed / 329 passed / 1 skipped, and app 1 failed / 220 passed. These failures are already on f33a5a1 and are not caused by QA: root `tests/test_inclinometer_bias_qc.py::test_contract_docs_and_ddl_expose_fields` (SCHEMA_CONTRACT.md lacks QA-020), and app `tests/test_contract_v14.py::test_endpoint_name_from_bundle_id_reference`. **QA-037 widened:** at f33a5a1, `resources/app.yml` grants dam-portfolio-all CAN_USE at app level in every target, not only through prod's target-level CAN_VIEW.

### Ledger delta
| ID | Status |
|---|---|
| QA-034 | Closed in repo HEAD for QA.md. VERIFICATION.md is redacted at line 68 (index-only edit; the Data Architect's edits are not included). History not rewritten (Genie). |
| QA-021 | Items 1–3 closed |
| QA-024 | Closed (not a production blocker; tolerance UNVERIFIED, configurable, review-gated). UI display of the tolerance value is pending a9e7d56 (strict xfail). |
| QA-035, QA-037 | Fix candidates in other bots' working trees; re-verify after commit |

## 2026-11-02 ~20:28 EST: independent verification of QA-035/037 (DA, uncommitted) and QA-036/039 (App Dev a9e7d56, unpushed)
QA-034 itself is pushed as fe4ed7b on origin/main, built directly on f33a5a1 so App Dev's a9e7d56 was not published. The items below were checked on the working tree and in a clean worktree of a9e7d56. The test edits listed here are **not committed**.

| Item | Result |
|---|---|
| QA-035 | **Verified (DA working tree).** CLEAR, RTN_UNACK and DEESCALATE carry value and metric_value in 4+10 series: single/two-level, direct 25→5 jump, deadband, persistence_off=2, lower limit, ACK before/after RTN, RTN from a carried-over state, rate. The evaluator's ck_alarm_transition_value pre-write guard finds 0 bad rows, so evaluation survives a return to normal. |
| QA-037 | **Verified (working tree).** Resolved app permissions are the same in dev, test and prod: dam-SYN-DAM-01..05-members CAN_USE plus dam-platform-admins CAN_MANAGE, and no dam-portfolio-all. CLI apply_bundle_permissions.go (main, re-read): apps map CAN_VIEW→CAN_USE and CAN_MANAGE→CAN_MANAGE; target permissions are appended unless the principal already has an entry. Prod target-level permissions are now only dam-platform-admins CAN_MANAGE, which overlaps the app's own entry and is skipped. dam-portfolio-all CAN_VIEW is now per job/pipeline (`&portfolio_view`). Job-level merge semantics were not verified (out of scope). |
| QA-036 | **Verified in a9e7d56 code.** `approval_status` raises DataIntegrityError for applied without review, self-review, or no reviewed_utc. `bias_proposal` returns integrity_error APPLIED_WITHOUT_VALID_REVIEW with approval_status null and proposed_c/abs_bs/evidence withheld. **CHECK ck_incl_correction_applied_reviewed exists only in the DA's uncommitted sql/ddl/02 and untracked sql/migrations/**, not in a9e7d56 or origin. |
| QA-039 | **Verified in a9e7d56 code.** Each decision inserts one review_signoff row (3 decisions → 3 rows, distinct review_id). Actual UPDATE ×2 and DELETE on sqlite were refused by triggers ("review_signoff is append-only") and the row count was unchanged. Reviewer == author returns 403. SUP, BAD_RESURVEY, approved and applied-without-review records return 409, with 0 rows written. SHA-256 recomputed by QA matches the stored signature_hash and the DA working-tree `corrections.review_signature` (AUDIT_FIELDS == SUBJECT_FIELDS, REVIEW_SIGNED_FIELDS == SIGNED_FIELDS); tampering with auto_value fails verify. Outside demo mode the reviewer comes only from X-Forwarded-User: no header, only X-Forwarded-Email, only X-Demo-User, or a blank header all return 401 end to end. The postgres trigger path was read only, not executed (no Postgres on the box). |
| a9e7d56 alone | App 2 failed / 235 passed / 1 skipped (claimed 238). New `test_approval_statuses_are_exactly_the_api_contract_values` needs the DA's uncommitted docs/api_contract.md v1.5. `test_endpoint_name_from_bundle_id_reference` already fails on f33a5a1 and needs the uncommitted resources/app.yml. The working tree also has further **uncommitted** App Dev edits on top of a9e7d56: signoff.py, state.py, deps.py, main.py, routers/alarms.py, routers/instruments.py, untracked audit_chain.py and routers/signoffs.py, and app/tests changes. The working-tree signoff.py imports the untracked audit_chain.py. |

Test edits (working tree, uncommitted): QA-035 ×4, QA-037 dev/test/prod and QA-024 UI tolerance markers dropped, each having really XPASSed. `test_api_contract_lists_every_approval_status_the_app_returns` now expects DataIntegrityError for 4 invalid applied records. `qa/test_qa024_tolerance.py` holds no applied record (fixed in fe4ed7b).
Suites (working tree, 20:28 EST): guard 0 hits, 6 passed; qa 152 passed; root 376 passed, 1 skipped; app 267 passed. (The claimed root 359 / app 238 are out of date because the working tree has more tests.)

**Update ~20:34 EST:** Lead-agent committed **e01e1ab** locally at 20:33 EST (parent a9e7d56, not pushed). It commits the DA's QA-035/037 fixes, api_contract v1.5, and the `ck_incl_correction_applied_reviewed` CHECK in sql/ddl/02 and sql/migrations/v1_5_001. On a clean worktree of e01e1ab plus the QA files from fe4ed7b and the working-tree test edits: guard 0 hits, qa 152 passed, root 376 passed / 1 skipped, app **238 passed**. QA-035/036/037/039 are verified on committed code. Local main (a9e7d56→e01e1ab) and origin/main (fe4ed7b) have diverged. Rebasing or merging conflicts only in docs/VERIFICATION.md (same redacted line in both); keep e01e1ab's version (0 guard hits). databricks.yml auto-merges, keeping the QA.md sync.exclude.

- **Stale e01e1ab snapshot note (DA "QA.md:277"):** e01e1ab carries the 1831f63 copy of QA.md. Its line 277 says the jobs.yml:116 comment still names the src/dam_safety/toa5.py duplicate. That is now stale: resources/jobs.yml:126 names src/common/toa5.py. **Closed (cosmetic).** e01e1ab does not touch QA.md, so a rebase onto fe4ed7b keeps this file.

## 2026-11-02 ~20:45 EST: v1.5 sign-off review of local e01e1ab (Lead-agent, on a9e7d56; not pushed)
Re-run on a clean worktree of e01e1ab plus the fe4ed7b QA files and the working-tree QA test edits:

| Claim (DA) | QA result |
|---|---|
| guard 0 | **0 hits** |
| testonly_restricted 7 | **7 passed** (working tree; the directory is git-ignored) |
| render_app_permissions --check in sync | **in sync**, rc 0 (a run without --check rewrote resources/app.yml byte-identically) |
| root 376 + 1 skipped | **376 passed, 1 skipped** |
| app 267 | **e01e1ab committed code: 238 passed.** 267 only with App Dev's *uncommitted* working-tree files (audit_chain.py, routers/signoffs.py, signoff.py, etc.). |
| qa 143 + 1 xfail + 8 XPASS | Confirmed: the 8 XPASS are real. Markers removed for QA-035 ×4, QA-037 dev/test/prod and QA-024 UI. The QA-036 test now expects DataIntegrityError and stays strict on hidden values: approval_status null, integrity_error set, DATA_INTEGRITY_ERROR, evidence {}, za/abs_dD/abs_bs/crosscheck_* null, and no value text in the payload. Now **151 passed, 2 xfailed (QA-040, strict)**. |

- **QA-040 (new, Medium, App Dev). v1.5 contract blocker.** SCHEMA_CONTRACT §5.5 v1.5 says the API field is `za` and "No API payload carries `ZA`", and api_contract.md:69 lists `za` for /incl/surveys and /incl/profile. However, app/backend/repos/contract.py (unchanged in e01e1ab and in the working tree) still uses its own `_parse_statistic`, so `bias_qc` (served by /incl/surveys, instruments.py:129) returns `evidence.ZA`, field `proposed_c` and unit text "ZA/ZB; D' = D + ZA", and `bias_proposal` returns `evidence.ZA`. Only `bias_proposal.za` is renamed. Fix: use `qc.parse_detection_statistic` / `API_FIELD_MAP` at the app boundary, and the `za` unit text. Alternatively the contract could be amended to allow it, but that would contradict its own "no ZA" rule. qa/test_inclinometer_bias_rules.py:412 is updated to `ev["za"]` and is a strict xfail (KeyError 'za'), plus a new strict-xfail `test_v15_no_api_payload_carries_uppercase_ZA`.
- **VERIFICATION.md rebase:** e01e1ab vs origin fe4ed7b conflicts only in docs/VERIFICATION.md (the INC-QA021 line both redacted). e01e1ab's own version has 0 guard hits. **Re-check pending until the DA has rebased.**
- **v1.5 verdict: NOT signed off yet.** Blockers: (1) QA-040; (2) the post-rebase VERIFICATION.md redaction and guard re-check; (3) a9e7d56/e01e1ab must be pushed together. a9e7d56 alone fails 2 app tests that need e01e1ab's api_contract v1.5. The app-267 claim depends on uncommitted App Dev files, which must be committed or the claim restated as 238. Everything else in QA-035/036/037/039 is verified on code. Any sign-off covers **code and contract only, not deployment**: `databricks bundle validate -t prod` and the v1_5_001 migration against a workspace or Postgres (including the review_signoff triggers) have not been run.

- 2026-11-02 20:51 EST QA-013 CLOSED: CONSOLIDATED_REPORT.md line 9 now 118/118, no "111/111" remains, verify.py line 99 cites USBR p62 only, verify.py re-run passes.

## 2026-11-02 20:55 EST: GTILT / DigiPro vendor-export parity (user priority 1; strict bar)
Scope: the 3 VDV Helpcenter exports in dam-refs `vendor_export_samples_vdv/` (manifest DS-INC-VDV-EXPORT-EXAMPLES, **internal-test-only**). They are read by path only. Every derived number is in `/workspace/dam-refs/qa_private/vdv_parity_results.json`, produced by `qa_private/vdv_parity_analysis.py`, and none is copied here. New test: `qa/test_gtilt_digipro_parity.py` (8 passed, 4 strict xfail; it skips if the samples are absent). `git check-ignore` reports the file as not ignored (`!qa/test_*.py`), and `restricted_guard.py scan` gives 0 hits. QA-041 is left free for the concurrent task.
- **Manual basis:**
  - GTILT PLUS: File/Tabulate prints raw, processed, checksums and plot coordinates (PDF p26). Plot depth = below the reference elevation, via stickup (p20, p15). Cable marks = wheelbase midpoint by default (p52). Fixed end is top or bottom (p27). Corrections exist only if typed into the subheader (p43-44).
  - DigiPro 2004: corrections disabled by default (p30), bottom reference default (p23), auto depth-adjust on by default (p18).
  - DigiPro2 2025: Tilt Change = Incremental Displacement and Profile Change = Cumulative Displacement (p23-25). Checksum = 0+180 (p25). Sum from bottom default; Apply Stickup and Auto Depth-Adjust options (p28). Tab-separated text export (p33).
- **Contents per file:**
  - **gtilt-export-version-1.txt:** "Plot Coordinates" (incremental A/B per date; the bottom row is the zero anchor; 3 dp; no unit stated).
  - **digipro-digitester.txt:** Tilt_Change (incremental, mm) for a partial hole that does not reach the fixed bottom.
  - **digipro-profiletester.csv:** Profile_Change (cumulative, mm). Dates are proven day-first, and the hole bottom is not included.
  - **None of the three** has raw A0/A180, checksums, deviations, K or L. So **the vendor checksum / D / L·D/(2K) re-derivation is impossible for every file**, and no file has both an input and a vendor output for the same quantity. These samples give **no vendor oracle**: QA-005e stays OPEN.
- **What was verified:**
  - Structural checks: one uniform step, no duplicate depths, chronological dates, initial column = 0 for DigiPro, GTILT bottom anchor = 0.
  - App vs QA independent bottom-up cumulative on the GTILT increments: max |diff| ≤ 2e-15. Not a vendor comparison.
  - DigiPro round-trips through the app's L·D/(2K) and bottom-up sum: ≤ 1e-15.
- **Defaults:** the app matches the manuals on these four points:
  - fixed bottom
  - bias/rotation never auto-applied (need review)
  - spiral deferred
  - checksum computed in QC
- **Info:** the engine reduction cannot do a fixed top (core.incl_profile can), although both vendors allow one.
- **Still unverifiable from these files:**
  - A/B sign convention
  - GTILT units
  - whether GTILT plot depth is the interval top or the midpoint
  - stickup value
  - why GTILT's first column (same date as the initial reading) is non-zero (a second set? unverified)

- **QA-042 (new, High, Inclinometer Specialist → App Dev). Depth reference for the `gtilt` and `digipro` import profiles.** `installation.IMPORT_PROFILES` treats both profiles as "top_of_casing, kept as-is".
  - The manuals say otherwise:
    - GTILT plot depth is measured below the reference elevation (stickup-shifted, p20/p15), and raw cable marks are wheelbase midpoints by default (p52).
    - DigiPro auto depth-adjust (default on, 2004 p18) and Apply Stickup (DigiPro2 p28) shift depths.
  - The GTILT sample's depth labels sit off the 0, L, 2L grid by a constant offset (value in qa_private). The app accepts them as interval tops silently.
  - Fix:
    - The Specialist states the depth semantics per vendor and per setting (stickup, auto-adjust, midpoint vs top).
    - App Dev then requires that metadata and rejects or flags labels that don't fall on `interval_tops_m`.
  - Strict xfail: `test_gtilt_plot_depths_are_not_silently_taken_as_top_of_casing_interval_tops`.
  - **Documentation follow-up (2026-11-08 EST; not QA acceptance):** `/workspace/dam-refs/procedures/inclinometer_depth_semantics.md` rechecks GTILT/DigiPro PDF and printed pages, specifies raw vs auto/stickup-adjusted output, required metadata, no-double-correction formulas and a worked `interval_tops_m` mapping. Unknown GTILT output point placement and the VDV 0.3 m offset remain UNVERIFIED; no importer/code changed in this pass.
- **QA-043 (new, App Dev). batch.reduce_instrument depth anomalies, shown on SYNTHETIC data:**
  - **(a) High.** A depth row missing from a survey is not flagged, and the cumulative sums straight across it (implicit zero change). This contradicts the "a missing interval breaks everything above it" rule, which applies only to explicit None.
  - **(b) Medium.** A duplicate depth in one survey resolves last-wins (dict) with qa_flag OK when the checksum is unchanged.
  - **(c) Medium.** Labels off the L grid, or below casing_depth_m, are reduced with qa_flag OK.
  - Fix: build the expected tops from `interval_tops_m(casing_depth, L)`, flag MISSING or DUPLICATE or OFF_GRID, and null the cumulative above a gap.
  - Strict xfails: `test_missing_depth_row_breaks_cumulative_above_it`, `test_duplicate_depth_row_is_flagged` and `test_off_grid_or_too_deep_labels_are_rejected_or_flagged`.
- **QA-044 (new, Medium, App Dev + Inclinometer Specialist). No GTILT / DigiPro / RST export importer exists.** A grep finds only comments; the only file parser is toa5. So step (c) "run the app's parser" cannot be done, and the earlier note that these files "are parsed" refers to QA tooling, not the app.
  - Any parity sign-off needs two things:
    - an importer, or an explicit decision that one is out of scope
    - a real vendor oracle: a GTILT File/Tabulate GTILT.OUT (raw + processed + checksums) or a DigiPro2 export with checksum + Tilt + Profile for one hole including its bottom, with K, L and stickup stated.
  - The Specialist owns obtaining the oracle.
  - **Oracle follow-up (2026-11-08 EST; QA-044/QA-005e not accepted):** qualifying full GTILT.OUT or DigiPro2 raw/checksum/tilt/profile oracle **not found in documented search**. Actual queries, URLs, bounds and inspected pages are in `/workspace/dam-refs/procedures/inclinometer_oracle_search_log.md`. Existing CNR Ivancich is an internal-test-only arithmetic oracle, not GTILT/DigiPro vendor parity; full depth metadata is missing/conflicted. No importer/code changed; no assertion of QA acceptance.
- **Verdicts:** all three files are NOT VERIFIED against the vendor (no raw or oracle). Structural checks PASS. App-vs-QA arithmetic PASSES, but that does not count toward vendor parity. Nothing committed or pushed.

## 2026-11-08 03:13 EST: re-verification of QA-042/043 (App Dev a4f67a7, 89d8a9c, 8a34a01; local, unpushed), suites, Ivancich reconciliation
- **Revision checked:** branch app/v1.5-audit-chain, HEAD 8a34a01, not pushed by QA.
  - Working tree carries other tasks' uncommitted edits: .gitignore, QA.md, databricks.yml, qa/restricted_guard.py, qa/test_restricted_guard.py.
  - It also carries untracked qa/ test files: the qa/ suite is entirely working-tree scope.
- **Suites:**

  | Scope | root | app | guard |
  |---|---|---|---|
  | Committed HEAD (`git archive HEAD` into /tmp, guard = committed version) | 578 passed / 2 skipped | 299 passed (1 warning) | 0 hits |
  | Working tree | 578 / 2 | 299 | 0 hits |

  - The 2 root skips include tests/test_ivancich_regression.py, which skips without `DAM_IVANCICH_CSV`. Run by path, it passes for all 3 CSVs; it is not in the 578.
  - qa/ (working tree): 1 failed, 203 passed, 4 xfailed (other files' markers). The failure is test_qa020_bias_qc_never_changes_reduced_data (below).
- **QA-042: FIXED (verified on code).**
  - `gtilt` and `digipro` are now `metadata_required`. depth_semantics.interval_top rejects with ImportRejected (a ValueError) when any of the following holds:
    - metadata is missing or partial
    - placement or settings are unverified
    - the output point, AT/auto-adjust combination or stickup state is undeclared
    - extra depth corrections are declared
    - the converted label is off-grid or at/below casing depth
  - A grid fit is not used as evidence. No default vendor datum remains: batch maps top_of_casing to the `contract` profile, not `gtilt`.
  - Strict xfail removed. The test now asserts rejection of every GTILT sample label for both profiles with metadata None, {} or {export_type: plot}.
- **QA-043a/b/c: FIXED (verified on synthetic data via batch.reduce_instrument).**
  - (a) A missing row gives DEPTH_MISSING at the gap, every row above it MISSING / CUM_GAP_BELOW with cum = None, and rows below it OK.
  - (b) A duplicate gives BAD DEPTH_DUPLICATE with inc and cum None, and cum is None above it. Neither row wins.
  - (c) Off-grid labels give BAD DEPTH_OFF_GRID with all cum None. A label at casing depth gives BAD DEPTH_BELOW_CASING.
  - Strict xfails removed; the assertions were strengthened to check flags, reasons and null cumulatives. qa/test_gtilt_digipro_parity.py: 12 passed. Guard 0; the file is not git-ignored.
- **QA-044: still OPEN** (no file importer; vendor oracle not found, per the follow-up above). QA-005e is still OPEN.
- **test_qa020_bias_qc_never_changes_reduced_data:** fails with abs(None). This is NOT edited and NOT weakened; it is reported here.
  - Cause: the fixture sets casing_depth_m = max(depth_m), so after a4f67a7 the deepest synthetic row is DEPTH_BELOW_CASING and the cumulative above it is nulled. (The unshifted interval 0.0 is also MISSING.)
  - Source: DS-INC-SYNTHETIC-01 (unrestricted, synthetic). The README and manifest state only "depth m".
    - make_synthetic.py generates depths 0.5–10.0 and says the 6.0–7.0 m shear zone covers "two intervals (6.0, 6.5)". That reads as labels = interval TOPS, which contradicts the suggested `depth_m - L`.
    - The same QA file's `_engine_inputs` already uses `depth_m - L`, so the file is internally inconsistent.
  - No source defines the casing depth or top of casing.
  - Measured outcomes (scratch, not committed): (i) shift −L with casing 10.0, or (ii) no shift with casing 10.0 + L, both give max|cum_a| = 5.20 mm (T6e raw) and 1 proposal. In (ii), interval 0.0 is correctly MISSING.
  - Owner: QA / Inclinometer Specialist, to state the synthetic depth convention, or to regenerate with an explicit one, before any fixture edit.
- **Ivancich (DS-INC-IVANCICH-CNR, internal-test-only; SHA256SUMS OK):** app reduce_survey vs the CNR file's own checksum, deviation, integral, diff and integral-diff columns, for all acquisitions of all 3 holes, both axes.
  - Agreement is within the file's 2-dp cm rounding (max |diff| ≤ half a unit).
  - Checksum = A0+A180 exactly. The integral is summed from the deepest row and includes its own row. Diff is relative to the first acquisition, with the same sign.
  - The "Pace [m]" field is in cm, not m (L is taken from the depth step).
  - Depths are negative labels. All bias and rotation metadata are zero, so corrections were not exercised.
  - Details: /workspace/dam-refs/qa_private/ivancich_parity_results.json (script ivancich_parity_analysis.py).
  - This is **raw-reduction arithmetic agreement with the CNR/Digitilt-format output**. It is NOT GTILT or DigiPro software parity: no GTILT.OUT or DigiPro2 raw+output oracle exists (QA-044).
- **Not signed off:** vendor parity, the depth-label semantics of real files, and deployment. Nothing committed or pushed.

## 2026-11-08 06:13 EST: DS-INC-SYNTHETIC-01 depth convention applied to QA fixtures; QA-020 bias test conflict (local, uncommitted)
- **Source check:**
  - The dataset README ("Explicit depth design convention") and the manifest notes now declare: depth_m = interval TOP below TOC (identity), L = 0.5 m, casing_depth_m = 10.5 = max + L, [0, 0.5] MISSING, label 10.0 = [10.0, 10.5].
  - This is consistent with make_synthetic.py (shear "6.0<=d<7.0 ... two intervals (6.0, 6.5)").
  - Re-running the generator reproduces the CSV byte-for-byte, so the data are unchanged.
  - Classification: unrestricted, is_synthetic=true, a synthetic design declaration. This is **not vendor validation**; vendor parity stays UNVERIFIED (QA-044, QA-005e open).
- **qa/test_inclinometer_bias_rules.py (fixtures only):**
  - Added `_syn_casing` (max + L) and `_syn_key` (identity).
  - `_batch` casing changed 10.0 → 10.5.
  - Removed all three `depth_m - P.L_m` re-keyings: in `_engine_inputs`, in the A180-only test, and in `test_b_engine_same_sign`, whose casing also changed to 10.5.
  - No expected value or assertion was changed, and the missing interval is not filled.
  - No other qa/, tests/, src/ or app/ file reads this dataset.
  - Pre-edit copy: qa_private/bak_test_inclinometer_bias_rules.py.20261108.
- **Results:** 20/21 pass in that file. qa/: 1 failed, 203 passed, 4 xfailed. root: 578 passed / 2 skipped. guard: 0 hits.
- **Conflict (reported, not made to pass): `test_qa020_bias_qc_never_changes_reduced_data`.**
  - Under the documented grid, the app correctly returns 21 rows. The only null cumulative is depth 0.0 (MISSING, DEPTH_MISSING); the shallowest observed cum_a is 5.20 mm at 0.5. One proposal; rows match with and without a proposal.
  - The unchanged assertion `max(abs(r["cum_a"]) for r in with_p)` iterates every row, including the missing interval, so it implicitly assumes a cumulative exists at 0.0 and raises TypeError. The README says consumers must exclude that null.
  - **Proposed** (needs lead/user approval):
    - assert that the 0.0 row is MISSING with cum_a None
    - assert that max |cum_a| over non-null rows is 5.20 at depth 0.5
- **Note:** src/dam_safety/inclinometer/vendor_import.py now shows uncommitted modifications by another task, which QA did not touch.

## 2026-11-09 10:20 EST: QA-020 assertion fix (user-approved) + local commit of QA suite and ledger
- **Authorization:** user, Genie chat 2026-11-09 10:17 EST: "Yes, QA can make the test_qa020 fix and commit its files locally." No push/deploy/messaging.
- **Change:** `qa/test_inclinometer_bias_rules.py::test_qa020_bias_qc_never_changes_reduced_data` only. Under the documented DS-INC-SYNTHETIC-01 convention (interval [0,0.5] MISSING; T6e raw 5.20 mm at shallowest observed top 0.5), the old `max(abs(cum_a) for all rows)` raised on the null at 0.0. New assertions: (a) the 0.0 m row exists, qa_flag MISSING, cum_a None; (b) max |cum_a| over non-null rows is 5.20 mm (abs=0.005) at depth 0.5. The with_p == without comparison and the inputs-untouched check are unchanged. No expected value was weakened; the missing interval is not filled.
- **Suites (venv: /workspace/.venv-dsm for root and qa/; app/.venv recreated from app/requirements-dev.txt because .venv-dsm lacks databricks-sdk):**
  - root: 705 passed, 2 skipped
  - app: 299 passed, 1 warning
  - qa/: 204 passed, 4 xfailed (0 failures)
  - guard scan: 0 hits
- **4 strict xfails (still valid; each fails under --runxfail):**
  - QA-041: Settings.check() accepts DEMO_MODE=true with DAM_CATALOG=dam_prod
  - QA-045: verify_chain accepts a stripped signature when a real Signer is configured
  - QA-045: Postgres append-only guard does not refuse TRUNCATE
  - QA-046: contract.approval_status demo fallback compares reviewed_by case-sensitively
- **Still open:** QA-044, QA-005e. Vendor reduction parity remains UNVERIFIED. No deployment sign-off.

## 2026-11-09 10:29 EST: QA-045a closed (App Dev 6cb95c9 ≡ 113060c content; HEAD 113060c = origin)
- **Local vs origin:** HEAD and origin/app/v1.5-audit-chain both at 113060c. 6cb95c9 is the same QA-045a patch (same tree for the three files) on a parallel ancestry line; 98fab6c (QA-045b TRUNCATE) is NOT on this HEAD. No merge/reset done.
- **Fix verified:** with a real Signer, verify_chain requires signature, sig_alg and key_id on every record (outside record_hash). Reasons: signature_missing / signature_downgrade / signature_invalid. NoopSigner path unchanged for unsigned chains; claimed signatures under Noop still fail (cannot vouch).
- **Independent adversarial tests** added in qa/test_qa039_chain_adversarial.py (strict xfail removed from test_signed_chain_detects_a_stripped_signature): missing/empty/None/whitespace fields; borrowed signature; truncated signature; wrong key_id; signature fields deleted from the row dict; unsigned row appended mid-chain; verify with signer unset while rows claim signatures; demo/Noop unsigned OK + hash_mismatch still caught; hash/prev checks intact under a real signer; stripping all sigs + Noop would pass (demo) but the same stripped chain fails under a real signer (signature_missing). No bypass found for an enforcing signer.
- **QA-045b kept open** (TRUNCATE xfail retained; 98fab6c not on this branch tip).
- **Still open:** QA-044, QA-005e (vendor parity UNVERIFIED), QA-041, QA-046.
- **Suites:** root 705 passed / 2 skipped (.venv-dsm); app 341 passed (app/.venv); qa/ 230 passed / 3 xfailed; guard 0.
