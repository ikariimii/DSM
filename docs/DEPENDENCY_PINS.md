# Open-source dependency pins (proposal)

Status: **PROPOSAL.** Nothing here is installed in the app or the engine jobs yet. The Data Architect owns the
engine job code and `resources/jobs.yml`, and QA owns the validation evidence. These pins are for them to
coordinate on. Verified 2026-11-02 against PyPI JSON metadata and the wheel `METADATA`/`LICENSE` files.

Source of decisions: "Dam Safety App: Open-Source Reuse Review (ranked)" review Page. The decisions are:
adopt ioos_qc, ruptures and statsmodels; adapt pastas; do NOT add traval; use canari only in an isolated pilot job.
The review's "use pytoa5 unmodified" decision is **superseded** (see the TOA5 decision below).

## Where the pins belong

| Consumer | Needs OSS libs? | Action |
|---|---|---|
| Databricks App (`app/`, FastAPI + React) | **No.** The app only reads engine-verified results through the SQL warehouse and writes to Lakebase. It does not run QC, change-point detection or HST. | Nothing added to `app/requirements.txt`. |
| Engine jobs (`src/jobs/*`, `src/dam_safety/*`), owned by the Data Architect | They will when the DA integrates the libs. Today the engine imports only numpy, pandas, pyspark, psycopg and databricks-sdk. | The DA adds the pins to the job `environments[].spec.dependencies` in `resources/jobs.yml`. |
| canari pilot | Yes, in an isolated job only | A separate job and environment. Never in the app or the core engine jobs. |

## Proposed pins (latest stable on PyPI as of 2026-11-02)

| Decision | PyPI distribution (import name) | Pin | Released (UTC) | License | Python | Intended use |
|---|---|---|---|---|---|---|
| Adopt | `ioos-qc` (`ioos_qc`) | `ioos-qc==3.0.0` | 2026-08-17 | Apache-2.0 | >=3.11 | QARTOD gross range, spike and rate-of-change. Map flags 1/3/4/9 to OK/SUSPECT/BAD/MISSING. Its rate-of-change test is one step only, so run the 30-day check on a differenced series. |
| Adopt | `ruptures` | `ruptures==1.1.10` | 2025-09-10 | BSD-2-Clause | >=3.9,<3.14 | Change-point detection (Pelt/Binseg), for advisory use |
| Adopt | `statsmodels` | `statsmodels==0.15.0` | 2026-08-30 | BSD-3-Clause | >=3.10 | HST regression (OLS) and diagnostics |
| Adapt | `pastas` | `pastas==2.0.1` | 2026-11-02 13:34 | MIT | >=3.11,<3.16 | Response-function models for piezometers, adapted behind our own interface. Needs numpy>=2, scipy>=1.16 and numba>=0.60. |
| **Dropped** (owner decision 2026-11-02) | `PyTOA5`, plus its runtime dependency `igbpyutils` | — (do not add) | — | LGPL-3.0-or-later; igbpyutils is GPL-3.0-or-later | — | Replaced by the in-house reader `src/common/toa5.py` (stdlib only) |
| **Excluded** | `traval` | — (do not add) | 0.5.4 latest | Unresolved: MIT classifier, but no LICENSE file in the distribution | — | Not adopted |
| Pilot only (isolated) | `pycanari` (`canari`) | `pycanari==0.2.5` | 2026-08-03 | MIT | >=3.10,<3.13 | Bayesian anomaly-detection pilot |

### Decision record: PyTOA5 dropped, in-house TOA5 reader (2026-11-02)
- **Why:** `PyTOA5` is LGPL-3.0-or-later, but its required runtime dependency `igbpyutils` is **GPL-3.0-or-later**.
  This was confirmed from the wheel `License-Expression` and the bundled LICENSE. The "LGPL, unmodified dependency" rationale therefore did not
  cover the full install.
- **Decision (owner):** drop PyTOA5 and igbpyutils. Write a small in-house Campbell TOA5 reader.
- **Clean-room rule:** the reader was written only from Campbell Scientific's published file-format
  documentation (sources are cited in `src/common/toa5.py` and `docs/TOA5_READER.md`). No PyTOA5 or igbpyutils code was opened, read
  or copied, including site-packages and the review files.
- **Cleanup:** PyTOA5 and igbpyutils were uninstalled from every local venv that had them (`/tmp/pinvenv`, `/tmp/env6venv`,
  `/workspace/oss-review/.venv`). They are not in `app/.venv`, the root venv, `app/requirements.txt` or `resources/jobs.yml`.
- **Do not** re-add PyTOA5 or igbpyutils to any requirements or job environment.

All remaining packages in the adopt/adapt set are permissive (BSD/MIT/Apache/PSF). I checked every installed
dist-info in the verification environment.

### ⚠ Naming traps
- **`canari` on PyPI (3.3.10, 2019, GPLv3) is a different, unrelated project.** The Bayes-Works package is
  **`pycanari`** (imports as `canari`). Only ever install `pycanari==0.2.5`.
- The in-house reader imports as `common.toa5` (from `src/`). A bare `import toa5` would be the dropped PyTOA5 package and must not appear.

## Runtime environment compatibility

The engine jobs currently use serverless `environment_version: "2"` (Python 3.11.10, numpy 1.23.5, pandas 1.5.3,
scipy 1.11.1, statsmodels 0.14.0). The proposed pins **are not compatible with env 2 as-is**:
- pastas 2.0.1 needs numpy>=2 and scipy>=1.16;
- forcing numpy 2 onto env 2's numpy-1 builds of pandas and pyarrow risks ABI breakage.

**Recommendation for the DA:** run the jobs that use these libs on **serverless environment version 6** (released
2026-09-03, Python 3.12.3, numpy 2.3.4, pandas 2.3.3, scipy 1.16.3, numba 0.62.1, statsmodels 0.14.5,
patsy 1.0.1, matplotlib 3.10.6). Pin only the OSS libs on top. Do **not** re-pin numpy, pandas or scipy, so the
base environment's versions are kept.

Proposed `resources/jobs.yml` environment, for the DA to apply (not applied by me):
```yaml
      environments:
        - environment_key: ml
          spec:
            environment_version: "6"
            dependencies:
              - "ioos-qc==3.0.0"
              - "ruptures==1.1.10"
              - "statsmodels==0.15.0"   # upgrades env-6's 0.14.5; QA re-verified with 0.15.0
              - "pastas==2.0.1"
              - "mlflow-skinny>=2.16"   # existing
              # TOA5 parsing: in-house src/common/toa5.py (no pip dependency)
```
If the DA prefers not to upgrade statsmodels, `statsmodels==0.14.5` (the env-6 base) is the alternative. QA would
then need to re-run the verification with it.

### canari isolated pilot environment
`pycanari==0.2.5` hard-pins its own dependency tree: numpy==2.2.6, pandas==2.3.3, statsmodels==0.14.6,
matplotlib==3.10.9, ray[tune]==2.56.1, optuna==4.9.0, pytagi==0.2.1, tqdm==4.70.0, fire==0.7.1 and pytest==9.1.1.
It also requires Python <3.13. These pins **conflict with the adopted statsmodels==0.15.0 and with env-6's numpy 2.3.4**, which
confirms it must run in its own job and environment:
- a separate job, for example `dam_canari_pilot`, paused, with no writes to qa_flag, alarms, thresholds or exceedances;
- its own environment, e.g. `{environment_version: "6", dependencies: ["pycanari==0.2.5"]}`. Pip will downgrade
  numpy to 2.2.6 inside that environment only.
- Outputs go to a pilot/sandbox schema and are labelled advisory/unverified. It is never surfaced in the app.

## Verification evidence

The review Page tested **git dev builds**, not the releases: ioos_qc 0.1.dev1+ge76cfff54, ruptures
0.1.dev1+gee1c8ff8a and pastas 2.1.0b0, plus statsmodels 0.14.6 and numpy 2.2.6. So I re-verified with the exact
release pins, using the synthetic-data harness `/workspace/oss-review/verify/verify_pins.py`. The checks are:
statsmodels OLS vs numpy lstsq; the ioos_qc spike, rate-of-change, gross range and 30-day-differenced checks;
ruptures Pelt/Binseg; a TOA5-parse check; and pastas gain recovery.

**Current state (QA-022, QA re-run 2026-11-02, recorded in `QA.md` §3):** the original 16/16 result predates
the PyTOA5 drop and is **no longer reproducible as-is**. With PyTOA5 uninstalled, the unmodified harness stops at
`import toa5` (`ModuleNotFoundError`) after 11 PASS, so the pastas check never runs. QA ran a `/tmp` copy with the TOA5 block
removed (the harness itself was not edited), and it gives **12/12 PASS** in both environments. The TOA5 check is not
replaced inside the harness. TOA5 parsing is now verified by `tests/test_toa5.py` and QA's `qa/test_toa5_adversarial.py`
(see `docs/TOA5_READER.md`). Under the clean-room rule, App Dev did not open, edit or re-run the harness scripts. These
numbers are QA's. The harness owner should delete its TOA5 block so the as-is run is 12/12.

| Environment | Base | Result |
|---|---|---|
| Latest resolution (uv, Python 3.12.16) | numpy 2.5.4, pandas 3.1.0, scipy 1.18.1, numba 0.68.1 | As-is: **11 PASS, then abort** at `import toa5`. Without the TOA5 block: **12 PASS / 0 FAIL** |
| Serverless env-6 base (Python 3.12.16) | numpy 2.3.4, pandas 2.3.3, scipy 1.16.3, numba 0.62.1, patsy 1.0.1, matplotlib 3.10.6 | As-is: **11 PASS, then abort** at `import toa5`. Without the TOA5 block: **12 PASS / 0 FAIL** |
| *Superseded:* both environments, before the PyTOA5 drop | as above, with PyTOA5 installed | 16 PASS / 0 FAIL (historical; included the PyTOA5 check) |

Key values: OLS vs lstsq max diff 4.8e-15; pastas gain 0.599943 (true value 0.6, tolerance 0.02). This is library-level
evidence only. QA still needs the engine-level reference cases once the DA integrates the libraries.

## Open coordination items
1. **Resolved 2026-11-02:** PyTOA5/igbpyutils dropped. The in-house reader is `src/common/toa5.py` (see `docs/TOA5_READER.md` for the DA/QA items).
2. **Data Architect:** move the OSS-using jobs to environment_version 6 and add the pins above. Keep canari in a separate paused pilot job.
3. **QA:** re-run the verification on the final job environment. Add engine-level reference cases for the ioos_qc flag mapping and the
   HST fit.
