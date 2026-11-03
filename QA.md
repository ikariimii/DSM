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
- Inclinometer, Slope Indicator/DGSI data_reduction.pdf: sin(1°)×20000=349, combined (A0−A180)/2, checksum=20, deviation 0.4188 in, displacement 0.0012 in, unit factors 0.0006 in and 0.01 mm. DGSI_Rotation: 0.018→1.03°, 0.008→0.46°.
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
  - DMM DEMO hole 45 (PDF p100/p102): 30 depths × 11 columns match the PDF exactly. I recomputed DIFF, CHANGE, INCR.DEV (exact to 4 dp), INCR DISP, and CUM DISP bottom-up. The p102 prose example (2+25 = 27 → 13.5 → 0.0162 in) is reproduced.
  - GK-603 IH01: both .GK files match PDF p50 (51 levels × 4). The published CSV matches p51/p52/p56 (51 × 9). Checksum, shift, deflection (Eq E-1/E-2, M = 0.0003, RINT = 2) and profile (Eq E-3/E-4, M = 0.0006, unrounded Diff/2) are reproduced bottom-up.
  - Geokon 6150 p17: 8 sensors × 13 columns match the PDF. R1corr (Eq 3), dR, G·L·dR (Eq 5) and the accumulated deflection are reproduced. Surface = 24.23 mm.
  - DMM p11–12 manual-mode examples: confirmed. The PDF prints 0.0128 / −0.0117. −298/25000 = −0.01192, so "−0.0117" is a typo in the manual. 6.42 / −5.96 mm follow from the unrounded sinθ. T2 correctly uses the single-reading sinθ = R/K convention, which is consistent with D = 2K·sinθ.
  - Remaining citations confirmed in the source text:
    - DMM p63: mean checksum "typically < 50", > 100 suggests a damaged probe.
    - GTILT: ZA = −19 on PDF p45; the "ZA = 10 increases A0" sign convention on PDF p46 (p6-12); 0.01 × 0.08 × 50 m = 40 mm on PDF p43.
    - DigiPro2: 73/3962 = 0.0184 on PDF p39.
    - DGSI_Rotation: 0.018 → 1.03°.
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
| QA-007 | Low | Inclinometer Specialist | Page-citation inconsistency: verify.py T7b cites DigiPro2 "p36–37", but the value is on PDF p39 (as manifest.csv says). GTILT cites use printed pages (6-8/6-12) while others use PDF pages. | grep `3962` per page | One convention (PDF page, plus printed page in brackets) | Mixed | **Closed 2026-11-02 19:00 (verified)**: all cites are in PDF pN [printed pM] form; T7b is PDF p39 [printed p37]; 19 printed pages spot-checked against footers |
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
- **QA-007: verified, closed.** `3962`/`73` occur only on DigiPro2 PDF p39, whose footer reads 37, so T7b's "PDF p39 [printed p37]" is right. Printed-page claims were checked against footers on 19 pages: DMM p11/12/63/99/102, GK-603 p50/56/61, IPI p14/17/19, Rotation p2/3, DigiPro2 p27, Bias p3, GTILT p42/43/46 and USACE p175. All match. Nit: some case lines carry the cite only in their group comment. Acceptable.
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
| QA-010 | Low | Piezometer Specialist | `piezometers/verify.py` rewrites the dataset `datasets/piezometers/.../syn_pz_hst.csv` on every run | A verify script must be read-only; generate the file once with a separate script | Open |
| QA-011 | Low | Piezometer Specialist | WE6 (HST) is self-consistency only: the fit recovers coefficients from data the same script generated | Label as SYNTHETIC/circular, or add an external HST oracle | Open |
| QA-012 | Low | Piezometer Specialist | Procedure WE4 cites the 5.76 psi input as [p20]; it's on CS451 PDF p19 (the result is on p20). verify.py cites USBR p55 for 2.31 ft/psi, but p55 has no extractable text; only p62 confirms it. | Fix the cites to the verifiable pages | Open |
| QA-013 | Low | Piezometer Specialist | Claimed "92/92"; actual 111/111 | Report the actual count, split SOURCE/CONST/SYNTH like inclinometers | Open |
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
- The DA duplicate `src/dam_safety/toa5.py` is now a 6-line shim over common.toa5 (19:23); the duplicate is resolved. The jobs.yml:116 comment still names it as the reader (cosmetic).

### 5. New defects
| ID | Sev | Owner | Finding | Expected | Status |
|---|---|---|---|---|---|
| QA-016 | Info | App Dev | CR1000 manual not in dam-refs; p437 fixture name normalised; FP2 NAN sources conflict (Table 114 vs CR1000X help); 31-bit RECORD reporting | Add the manuals and index rows; comment the fixture | Open |
| QA-017 | Medium | App Dev | A last line with no terminator, cut inside its final numeric field or right after a comma, keeps the field count and is accepted silently (`2.5`→`2` int, or `None`). This is the typical partial-write or collection-in-progress case. | Flag or reject an unterminated final line (or return it as `partial`) | Open (strict xfail ×2) |
| QA-018 | Low | App Dev / DA | Duplicate and non-monotonic RECORD/TIMESTAMP are accepted with no diagnostics, in both the reader and toa5_landing; rollover cannot be told from regression | Report duplicates and sequence breaks (dedup at landing on table+RECORD+TIMESTAMP) | Open |
| QA-019 | Low | App Dev | `\d` without re.ASCII accepts non-ASCII digits in TIMESTAMP, RECORD and values; "+5" and "-0" are accepted as RECORD | `[0-9]` / re.ASCII; RECORD `[0-9]+` | Open (strict xfail ×3) |
| QA-020 | Medium | App Dev (with Geotech lead) | No between-set checksum-shift SUSPECT flag and no c = Δ0+Δ180 / ZA proposal function in the app; proposed_bias ignores the checksum and is returned whenever stable_from_m is set | Implement §7: SUSPECT on a shift, ZA proposed only with D-profile evidence, never auto-applied | Open (strict xfail) |
| QA-021 | Medium | Geotech lead | §5 says "Add c to A0 (ZA = c)" with c = Δ0+Δ180, but Δ's sign is undefined. With Δ = set − reference, ZA = +c doubles the error (synthetic: +6 → +12); verify T6e corrects with −6. Mikkelsen p7 says only "a correction value". Also, §7's "D-profile **or** within-set evidence": Δ0+Δ180 is identically the checksum change (S3 equals the full shift although D is consistent), so it is not independent evidence. | Define Δ (reference − set) or ZA = −c; change "or" to "and"/"sized by" | Open |
| QA-022 | Low | App Dev | DEPENDENCY_PINS "16/16" no longer reproducible: the harness aborts at `import toa5` and pastas is unchecked | Re-run with the TOA5 block removed (12/12) and update the doc | Open |

**Rerun:**
- `cd /workspace/dam-safety-app && app/.venv/bin/python -m pytest -q -p no:cacheprovider qa/test_toa5_adversarial.py qa/test_inclinometer_bias_rules.py qa/test_restricted_guard.py`
