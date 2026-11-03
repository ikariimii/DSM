# Interpretation layer (`src/dam_safety/interpretation`, `src/dam_safety/inclinometer`)

All outputs are **advisory**: they never change thresholds, alarm states or EAP levels, and every
narrative carries `requires_engineer_review = true` (CHECK constraint in `09_rpt.sql`).
FERC-CH14 PDF p36: statistical analysis is "no substitute for judgment". Defaults are listed as
UNVERIFIED in `VERIFICATION.md` §4.

| Module | What it does | Output table | Source |
|---|---|---|---|
| `hst.py` | Hydrostatic-Season-Time model, `Ŷ = F1(h)+F2(s)+F3(t)`; h normalised to [0,1]; seasonal variant `ANCOLD_FRANCE` (default) or `SALAZAR2017`; `F3 = a·t + b·e^(−t/t0)`, t0 by grid search; OLS fit, robust σ = 1.4826·MAD | `gold.hst_fit` | SALAZAR-2017 Eq1-5; ANCOLD-FRANCE p2 |
| `anomaly.py` | z = ε/σ, bands at k_warn 2 / k_alert 3; rate-of-change on residuals | `gold.anomaly_scores` | formula_library §7-8; FERC-CH14 §14.4.1 |
| `fault_classifier.py` | rule votes (QC_FAULT, DATUM_OR_CALIB, ISOLATED, …) → sensor-fault / real-behaviour / indeterminate, with reason codes | `gold.anomaly_scores` (classification columns) | SALAZAR-2021 p2; FERC-CH14 p37 |
| `pfm_rules.py` | per PFM (`ref.pfm` + `ref.pfm_instrument`) tests signatures RISING/FALLING_TREND, RISING_HEAD_WITH_POOL, ACCELERATING_DISPLACEMENT by OLS t-stat | `ops.rule_finding` | FERC-CH14 §14 p34 |
| `commentary.py` | grounded text: every sentence carries a tag that exists in the supplied facts, every number must be in the facts, else the deterministic template is used | `rpt.engineering_commentary` | — |
| MLflow placeholder (`resources/ml.yml`) | UC model `ml.anomaly_scorer`, alias `@Champion`; not trained | — | KB rec 8 |

Job: `dam_interpretation` (nightly, serverless env 6, locked `requirements/job-ml-env6.lock.txt`).

## Inclinometer module (REPORT §6 template)
`installation` + `survey_import` (SI, depth from top of casing, keyed to interval top, raw kept; SAA axis B sign flip once at ingest) →
`corrections` (versioned `ref.incl_correction` records applied depth → bias → sensitivity → rotation; spiral
NotImplemented; no manual edits) → `reduction` (`deviation_mm = L·D/(2K)·1000`, checksum, cumulative from the
bottom fixed point) → `qc` (checksums), `diagnostics` (detection only), `rates`, `alerts` (EoR thresholds from `ref.threshold`), `dssmr_appendix`; `engine` runs one survey end to end and emits audit rows (`corrections.AUDIT_FIELDS` == `ref.incl_correction` columns). Installation constants in
`ref.incl_installation`; outputs belong in `silver.incl_reduced` (writer job not wired yet — INC-WIRE).
