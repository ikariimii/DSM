# QA-044 decision: staged vendor text/CSV imports

Status: **UNVERIFIED**, interface stub only. No vendor parser or production integration is
claimed. Production v1 remains contract CSV and TOA5 until real vendor exports pass parity.

## Scope approved by Genie

| Profile | In-scope exports |
|---|---|
| `gtilt` | GTILT text (`.txt`, `.out`) |
| `digipro`, `digipro2` | DigiPro / DigiPro2 CSV |
| `rst` | RST raw CSV |
| `geosense` | Geosense CSV |

Native `.GTL`, `.dux`, PC-SLIN and Inclinalysis binaries are out of scope. Reject them
with an **export to text/CSV** message. Renaming a binary does not make it supported.
Processed-only exports are **display/cross-check only, never re-reduced**. Their raw
values remain unchanged, just as for raw exports; no substitution of processed values
for missing raw passes is allowed.

`dam_safety.inclinometer.vendor_import` supplies `prepare_import` and the `parse_export`
stub. The feature flag `INCL_VENDOR_IMPORTERS_UNVERIFIED` defaults off; explicit `true`
only permits interface validation. Actual parsing still raises `NotImplementedError`;
raw reduction remains blocked even with opt-in. Nothing is wired into production routes.
The legacy engine `gtilt`/`digipro` depth aliases are not vendor-file parsers and are not
an approved import path. Future adapters must go through the metadata gate, not those aliases.

## QA-042: explicit metadata, never vendor defaults

Every profile must declare all five fields (including explicit zero/false values):

- `depth_datum`: `toc`, `index`, or `ground` (datum of the supplied label).
- `depth_label_ref`: `midpoint` or `top` (physical cable-mark convention).
- `ground_shift`: metres already subtracted by the exporter, e.g. applied stickup or
  DigiPro Depth Offset; explicitly zero for an unshifted raw cable label.
- `index_above_toc`: metres of index elevation above top of casing.
- `auto_adjust`: boolean declaring whether the exporter already changed a midpoint label
  to an interval-top label. Top-mark labels use false; ambiguous combinations are rejected.

Missing, null, unknown or invalid metadata is rejected. Never assume top-of-casing,
zero stickup, midpoint marks, or a vendor's auto-adjust default. Probe wheelbase and
label units must be explicit too; this interface uses metres only. Keep the original file,
raw label, passes, units and declared metadata alongside derived depths in future adapters.

Source: `/workspace/dam-refs/procedures/inclinometers.md` §3.1, per-vendor/per-setting table.
Reference: `inclinometers/verify.py::to_toc_interval_top()` and T17a–g, read by path, not copied.

`top_toc = label + ground_shift − index_above_toc − mark_shift`

`mark_shift = L/2` for still-unadjusted midpoint labels, otherwise zero. Do not subtract
L/2 twice after vendor auto-adjust. The datum declaration is provenance, not permission
to infer a numerical correction. Unverified RST/Geosense label conventions require an
explicit declaration and specialist confirmation before any adapter can be enabled.

## Acceptance gates and restricted regression

Implement clean-room from published format documentation; obtain the Inclinometer
Specialist's field mapping review. Require real vendor files with raw passes, constants,
depth settings, checksums and published processed output before claiming parity or
turning anything on. No manual text/PDF, third-party parser source, vendor file, restricted
derived scalar or Ivancich sample may enter the repo or bundle.

`tests/test_inclinometer_vendor_import.py` uses synthetic inputs for metadata rejection,
no silent TOC assumption, midpoint/top/auto-adjust conversion and feature gating. It
compares the conversion to the specialist's function by path when available.

`tests/test_ivancich_regression.py` reads the original CNR-IRPI CSV via `DAM_IVANCICH_CSV`
only, skips if unset or absent, and exercises reduction against independently calculated
raw differences. It stores neither data nor derived results. This is an internal arithmetic
regression, **not vendor-export parity or validation of the dataset's depth semantics**.
