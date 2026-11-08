# QA-044 decision: staged vendor text/CSV imports

Status: **UNVERIFIED**, staged structural parsers with explicit caller-declared layouts.
No vendor parity or production integration is claimed. **QA-005e and QA-044 stay open.**
Production v1 remains contract CSV and TOA5 until real vendor exports pass parity.

## Scope approved by Genie

| Profile | In-scope exports |
|---|---|
| `gtilt` | GTILT text (`.txt`, `.out`) |
| `digipro`, `digipro2` | DigiPro / DigiPro2 CSV |
| `rst` | RST raw CSV — planned, parser deferred beyond this stage |
| `geosense` | Geosense CSV — planned, parser deferred beyond this stage |

Native `.GTL`, `.dux`, PC-SLIN and Inclinalysis binaries are out of scope. Reject them
with an **export to text/CSV** message. Renaming a binary does not make it supported.
Processed-only exports are **display/cross-check only, never re-reduced**. Their raw
values remain unchanged, just as for raw exports; no substitution of processed values
for missing raw passes is allowed.

`dam_safety.inclinometer.vendor_import` supplies `prepare_import` and staged `parse_export`
implementations for `gtilt`, `digipro` and `digipro2`. The feature flag
`INCL_VENDOR_IMPORTERS_UNVERIFIED` defaults off; explicit `true` permits structural parsing
only. Raw reduction remains blocked even with opt-in. Nothing is wired into production routes.
`installation.IMPORT_PROFILES` no longer treats `gtilt`/`digipro` as identity aliases:
`interval_top_from_label` and `survey_import.import_reading` reject them without explicit
per-output metadata. This is a coordinate-conversion boundary, not a vendor-file parser.
Already-normalised silver rows use the separate `contract` profile and still undergo
batch grid QC. Never select `contract` to bypass vendor depth validation.

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


## QA-042 implemented profile gate

The updated source is `/workspace/dam-refs/procedures/inclinometer_depth_semantics.md`
§§2–7 (supplementing §3.1 above). `depth_semantics.interval_top` additionally requires:

- `export_type`: `raw_survey`, `plot`, or `processed`;
- `depth_mode`: `depth` or `elevation`;
- `output_point`: actual `midpoint`, `top`, or `bottom`, not just the physical cable marks;
- `placement_verified`, `settings_verified`: explicitly true, with nonblank
  `placement_evidence` identifying the per-output evidence;
- `additional_depth_corrections`: explicitly `none`; cable/compression transforms are
  unsupported and rejected as UNVERIFIED;
- `probe_type`: explicitly `classic` or `at`;
- `apply_stickup`: explicit boolean, and `stickup_value_m`: exact entered value.

For depth output, `ground_shift` must equal the amount actually applied (zero when
Apply Stickup is off, regardless of the header value). For elevation output additionally
require `toc_elevation_m` and matching named `label_datum`/`toc_datum`; use TOC elevation
minus label elevation minus the point-to-top distance, without applying ground/index
corrections again. AT with Classic midpoint auto-adjust is rejected. Unknown GTILT plot
placement, unresolved raised-index stickup, unknown datums and additional depth transforms
stop conversion as UNVERIFIED. A grid fit is never evidence for those declarations.

After conversion, labels must match `interval_tops_m(casing_depth, L)` within 1e-6 m;
o unexplained offset is snapped to the grid. Toe anchors are not additional intervals.
`RawReading.depth_metadata_json` keeps an immutable snapshot of declared provenance next
to the unchanged raw label and raw passes. Raw-reading import refuses processed/plot-only
exports even when coordinate conversion is permitted for display. There is no automatic second conversion.

`tests/test_inclinometer_depth_semantics.py` covers synthetic adjustment combinations,
unknown placement, off-grid/too-deep labels, AT, elevations and source-function parity.
No restricted vendor numbers or manual text are included. This gate does not establish
vendor-file parity or enable vendor reduction or establish compatibility with undocumented native layouts.

## QA-043 engine implementation

`batch.reduce_instrument` now builds the expected grid from the installation valid at
survey time. It rejects every duplicate copy and every off-grid/below-casing reading,
emits missing slots, and nulls each affected axis cumulative at and above a current or
baseline gap. Valid increments elsewhere remain available. Source: procedures/inclinometers.md
§§3.1, 4.0, 7; depth-semantics §7. BAD for duplicate/off-grid rows remains an explicit
engineering rule, not a claimed vendor software requirement. Synthetic cases are in
`tests/test_inclinometer_depth_grid.py`. The saved proposal patch is historical, not a
pending application step.

## Reproducing all test suites

Install `.venv/bin/python -m pip install -r requirements/dev.txt`, then run each suite
in a fresh process (their existing module/conftest names collide in a joint pytest run):

```
.venv/bin/python -m pytest -q tests -ra
.venv/bin/python -m pytest -q app/tests -ra
.venv/bin/python -m pytest -q qa -ra
.venv/bin/python qa/restricted_guard.py scan
```

QA's strict xfail markers are intentionally not modified. Successful fixes therefore
appear as strict XPASS failures until QA independently updates its markers. Independent
QA files and guard enhancements may still be pending in the working tree; report those
suite results separately from a clean committed snapshot.


## QA-044 staged structural parser contract

Clean-room format-description sources read for this stage (no manual example tables or
vendor files were used):

- GTILT PLUS User's Manual v3, **File/Tabulate**, PDF p26 [printed 4-2]: identifies raw,
  processed, checksum and plot-coordinate output categories and monospaced text presentation.
- DigiPro2 Manual, footer 2025/5/29, **Exporting Plots**, PDF p33 [printed 31]: describes
  tab-separated text and regionalized spreadsheet separators/decimals. This describes plot
  export, not an authoritative raw-pass CSV schema. DigiPro compatibility is provisional.

Neither description defines universal literal headers, per-survey framing or fixed-width
column offsets. The implementation deliberately embeds **no native header template**.
A successful structural parse is not evidence that a supplied column map is a correct
vendor mapping. These remain UNVERIFIED until reviewed against actual permitted exports.

### Explicit layout and result API

`parse_export(profile, filename, raw_content, depth_metadata, probe_L_m,
processed_only=..., enabled=..., layout=ExportLayout(...))` requires all five depth fields
listed above. They are preserved, not inferred from file names, labels, units or grid fits.
This stage does **not** convert labels or create `RawReading` objects; QA-042/043 code is
unchanged. A later conversion/reduction step still requires the already-verified gates.

`ExportLayout` requires:

- An ordered tuple of `TableLayout` descriptors;
- an explicit delimiter: GTILT `whitespace`, `fixed`, or tab; DigiPro/DigiPro2 CSV comma,
  semicolon or tab (the filename remains `.csv` within this stage's scope);
- an explicit decimal convention (`.` or `,`, distinct from the field delimiter);
- an explicit supported encoding (`utf-8`, `utf-8-sig`, `cp1252`, `latin-1`);
- optionally an explicit count of preamble lines (default zero, never detected).

Each `TableLayout` declares its kind (`raw`, `processed`, `plot`, `checksums`), axes,
survey ID, canonical-to-literal-header column map and units. Multiple tables require
unique, ordered, exact section-marker lines. Fixed-width tables require explicit increasing
character spans for every source column; data outside those spans is rejected. Whitespace
mode cannot encode an empty field: use fixed-width or tab for missing raw passes.

Required canonical columns:

- All tables: `depth` in explicitly declared metres, preserving the source label;
- Raw: `a0`/`a180` and/or `b0`/`b180`, according to explicit axes;
- Checksums: `checksum_a` and/or `checksum_b`, with an explicit quantity label;
- Processed/plot: `value_a` and/or `value_b`, with explicit quantity and units.

Header spelling, survey dates/IDs, axis order, quantities, raw-unit scale, locale, sentinel
meaning and footer behavior are never guessed. Blank/duplicate headers, missing required
columns, malformed quoting, wrong field counts, missing depth and nonnumeric/nonfinite
mapped values are rejected. Blank pass/value cells remain null and receive MISSING flags;
there is no processed-value substitution. No row is labelled OK or vendor-verified.
Unmapped source columns are retained as original cells, not promoted into raw fields.

`ParsedExport` keeps its request (original immutable bytes and depth metadata), immutable
tables/headers/cells and parsed numeric values. `raw_rows` selects only explicitly raw
tables; `display_tables` keeps other kinds separate. All results carry UNVERIFIED status.
A processed-only file must be declared `processed_only=True`; otherwise it is rejected.
Both processed-only and raw-containing results refuse `require_reduction()`.

Native `.GTL`, `.dux`, PC-SLIN/Inclinalysis and unsupported binary extensions are rejected
with an export-to-text/CSV message. Renamed binary signatures, control bytes and invalid
encoding are rejected too. Unsupported/ambiguous text layouts fail rather than being
silently coerced. Atlas arrays, graphics, unknown date-wide headers and automatic splitting
of repeated-survey tables are not supported by a guessed schema.

### Questions for the Inclinometer Specialist (format only)

1. Authoritative literal header sets, section markers and column boundaries for each
   GTILT File/Tabulate output kind and version; how multiple dates/surveys are framed.
2. DigiPro/DigiPro2 CSV/TSV header/axis/date layout and locale declarations; whether any
   supported export actually carries raw passes (the cited page documents plot export).
3. Explicit units/scales, blank/sentinel rules, optional checksum columns, footer lines,
   repeated sections and distinguishing plot boundary rows from reading rows in each format.
4. Specialist-approved mappings to accompany real vendor exports for QA parity. No need
   to reopen the already-verified QA-042/043 coordinate/grid rules.

Synthetic structural cases only are in `tests/test_inclinometer_vendor_parsers.py`.
**QA-044 and QA-005e remain open; there is no claim of vendor parity or production readiness.**
