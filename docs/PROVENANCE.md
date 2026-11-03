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
