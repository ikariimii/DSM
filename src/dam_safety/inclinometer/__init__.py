"""Traversing-probe / SAA inclinometer module, laid out per the Inclinometer Specialist's template
(/workspace/dam-refs/inclinometers/user_tool/REPORT.md §6.1 sheet-to-module mapping) and the team procedure
(/workspace/dam-refs/procedures/inclinometers.md). The user's workbook supplies the LAYOUT only; its correction and
QC arithmetic is NOT reproduced (REPORT §3.3 errata E1-E13). Workbook and vendor-manual data are internal-test-only
and never live in this package (tests read them in place from dam-refs; see testonly_restricted/).

Module map (REPORT §6.1):
  1_SETUP          -> installation     interval list, depth from top of casing, SI, explicit L and K
  2_RAWDATA        -> survey_import    validated immutable raw readings (raw kept), SAA B sign flip once at ingest
  3_QC_CHECKSUMS   -> qc               CS mean/SD per axis, individual CS, dCS vs previous survey (procedure §7)
  4_CORRECTIONS    -> corrections      versioned correction records (REPORT §6.3 audit fields), applied by engine
  5_CALCULATIONS   -> reduction        D -> deviation -> change vs selectable baseline -> cumulative from bottom
  6_THRESHOLDS     -> alerts           EoR thresholds per depth zone (no sigma/FERC claims), USACE 3-in-a-row rule
  7_VELOCITY       -> rates            mm/day, mm/yr between surveys
  9_DIAGNOSTICS    -> diagnostics      rotation fit (factor, r, sign convention), curvature of the survey's own D,
                                       depth-shift search
  11_PRINT_REPORT  -> dssmr_appendix   sign-safe max |change|, computed survey count, correction table from records
Plots / context loads / seasonal (REPORT §6.1 rows 7-10) are app-side (docs/api_contract.md)."""
