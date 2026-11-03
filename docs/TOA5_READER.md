# In-house Campbell TOA5 reader (`src/common/toa5.py`)

**Status:** implemented and tested (100% line and branch coverage). QA-verified, and QA-016/017/018/019 were fixed on 2026-11-02 (~19:40 EST). Owner decision 2026-11-02:
PyTOA5 and igbpyutils were dropped (igbpyutils is GPL-3.0-or-later). See the decision record in `docs/DEPENDENCY_PINS.md`.

## Clean-room rule
The reader was written **only** from Campbell Scientific's published file-format documentation, listed below. No PyTOA5 or
igbpyutils source was opened, read or copied (that includes site-packages and the open-source review files). Test fixtures are
hand-written from the Campbell examples. The Geokon check is derived from the Geokon 4500 manual.

## Specification sources
| Tag | Document | URL | What we use |
|---|---|---|---|
| CS-LN-TOA5 | Campbell Scientific, LoggerNet manual, "TOA5" | https://help.campbellsci.com/loggernet-manual/ln_manual/campbell_scientific_file_formats/toa5.htm | Header contents: file type, station, logger type, serial, OS, DLD name, DLD signature, table name, then field names, units and processing. Timestamp and record number are optional, and the timestamp has sub-second resolution. Comma-separated. One table per file. The verbatim example is used as a test fixture. |
| CS-LN-SUFFIX | Campbell Scientific, LoggerNet manual, "Field Name Suffixes" | https://help.campbellsci.com/loggernet-manual/ln_manual/campbell_scientific_file_formats/field_name_suffixes.htm | Processing-name suffixes (Avg, Min, Max, Tot, Std, ...). Informational only; the reader does not check them. |
| CS-CR1000 | Campbell Scientific, *CR1000 Measurement and Control System* operator's manual (rev. 2018) | https://s.campbellsci.com/documents/us/manuals/cr1000.pdf | §7 Tables 12–13 (p.146–147): the 4-line header, an environment line with exactly 8 fields where field 1 is "TOA5", and `name(i,j)` array field names. §8 "Data File Formats" (p.437–439): the TOA5 example (used as a fixture), units "TS"/"RN", an empty string for missing units or processing, one descriptor per field, and a RECORD that is a 32-bit counter (max &hffffffff) restarting at 0. |
| CS-CR1000-T114 | same CR1000 manual, Table 114 (PDF p.494) | (as above) | NAN/INF handling by data type. Used only to document the FP2 conflict below. |
| CS-NANINF | Campbell Scientific, CR1000X help, "Understanding NAN and INF occurrences" | https://help.campbellsci.com/CR1000X/Content/shared/Maintain/Troubleshooting/NAN_and_INF.htm | Meaning of NAN and INF. Integer types have no NAN: FP2 stores it as -7999 and Long as -2147483648. |
| GEOKON-4500 | Geokon Model 4500 Series VW Piezometers manual (`/workspace/dam-refs/4500_Piezometer.pdf`) | https://www.geokon.com/content/manuals/4500_Piezometer.pdf | Used only for the end-to-end test: §6.1 Eq.1 digits = Hz²/1000; §6.1.1 Eq.2 P = G(R1−R0); worked example R0=9082, R1=6749, G=−0.1795 kPa/digit → 418.8 kPa (PDF p.22). |

All Campbell URLs were fetched and checked on 2026-11-02. A third-party site (plotlad) shows up in searches but is **not** a source.

## API
```python
from common.toa5 import read_toa5, parse_text, Toa5Error
t = read_toa5("/path/CR1000X_Piezo.dat")      # strict UTF-8; BOM tolerated; pass encoding= if needed
t.environment.logger_model, t.environment.table_name, t.environment.program_signature
t.fields          # tuple[Toa5Field(name, units, processing)]
t.records         # tuple[dict[name -> value]]
t.column("PZ1_Freq"); t.field("PZ1_Freq").units
t.warnings        # tuple[Toa5Warning(line, code, message)], non-fatal diagnostics (see below)
read_toa5(p, on_unterminated="drop")   # default "error"; see QA-017 below
```
`parse_lines()` expects lines **with** their terminators, as from a file object or `io.StringIO`. A line without a
terminator counts as unterminated.

**Unterminated last line (QA-017).** A final data line without a line terminator may be a partial write (logger or
collection still writing). A cut "2.5" reads as "2", and a cut after a comma reads as an empty field. The field count still
matches, so this cannot be detected otherwise. Policy `on_unterminated`:
- `"error"` (**default**, strict): raises `Toa5Error` with that line number;
- `"drop"`: discards the line and adds warning `unterminated_last_line_dropped`.

There is deliberately no "keep" option, because a complete line cannot be told from a truncated one. An unterminated
*header* line is always an error. An unterminated trailing line of whitespace is ignored, like any trailing blank line.

**Sequence warnings (QA-018).** Data are kept as read (deduplication is ingest's decision), and `t.warnings` lists the following, sorted by line:
`record_duplicate` (RECORD already seen since the last rollover), `record_out_of_order` (a decrease that is not a rollover),
`timestamp_duplicate`, `timestamp_out_of_order`. A duplicate is reported instead of out-of-order for the same column.
A RECORD of 0 directly after &hffffffff, or after &h7fffffff (the CR1000 reports only 31 bits to support software, CS-CR1000 p.439), is
the documented rollover. It is not warned and starts a new duplicate window. A reset to 0 from any other value (for example after a
program change) **is** warned as `record_out_of_order`. Gaps are not warned.

**ASCII-only numerics (QA-019).** Every numeric pattern (values, RECORD, TIMESTAMP) uses `[0-9]` with `re.ASCII`, so
Arabic-Indic, full-width and other Unicode digits are rejected. RECORD must be unsigned digits only: `+5` and `-0` are rejected, and leading
zeros are accepted. Value fields may still carry an ASCII sign.
Values: TIMESTAMP becomes a naive `datetime` in logger time (TOA5 carries no zone, so ingest must attach the station zone). RECORD becomes an `int`
(0..2³²−1). NAN/INF/±INF become float nan/±inf. Unquoted integers become `int`, other numbers become `float`, and an unquoted empty field becomes `None`. A quoted field becomes `str`.
Integer NAN sentinels (−7999 FP2, −2147483648 Long) are **not** converted, because TOA5 does not record the stored type. Constants are exported
so that ingest can map them per field type.

**Source conflict on FP2 NAN (QA-016).** The two Campbell sources disagree. CS-NANINF (CR1000X help) says FP2 stores
NAN as −7999. CS-CR1000-T114 (CR1000 manual Table 114, PDF p.494) says a FLOAT variable stored to FP2 shows "NAN",
and −7999 appears only for a LONG variable stored to FP2. Because of this conflict (and because the stored type is not in the file),
the reader converts neither sentinel. Ingest must decide per logger model and field type, and that decision needs the DA and an engineer.

**Fixture provenance (QA-016).** The CR1000 §8 fixture in `tests/test_toa5.py` is **normalised, not verbatim**. The PDF
(p.437) prints the third field name as `battfivoltfiMin`, a typesetting artefact, and the fixture uses `batt_volt_Min`. The
as-printed text is covered by `qa/test_toa5_adversarial.py::test_cr1000_manual_p437_example_as_printed`. The LoggerNet
example fixture is verbatim. The CR1000 manual is **not** in `/workspace/dam-refs` (local working copy sha256 9b91e875…).
QA recommends adding it with an index row; that is the dam-refs owner's call.

Strict errors (`Toa5Error`, with `.line` set to the 1-based line):
- fewer than 4 header lines;
- an environment line that does not have 8 fields, or whose type is not exactly "TOA5";
- empty or duplicate field names;
- a units or processing line whose count differs from the field names;
- TIMESTAMP that is not the first field, or RECORD that is not directly after it (or first);
- "TS"/"RN" units on any other field;
- a data line whose field count is wrong;
- a timestamp that is not `YYYY-MM-DD HH:MM:SS[.fraction]`, is an invalid date, or has non-zero sub-microsecond digits;
- a RECORD that is not an unsigned 32-bit integer in ASCII digits (a sign is rejected);
- a number or timestamp containing non-ASCII digits;
- an unterminated last data line (default policy), and always an unterminated header line;
- unquoted non-numeric text;
- quoting errors (unterminated quote, stray quote, text after a closing quote);
- a blank line followed by more data.

## Location and coordination
- `src/common/` is a **neutral shared module**, deliberately outside the Data Architect's `src/dam_safety/` and `src/jobs/`.
  - Jobs pick it up through `src/jobs/_common.py`, which puts `src/` on `sys.path`. The bundle syncs `src/`. Tests use `pythonpath=["src"]`.
  - It imports only the stdlib.
- **One parser:** `common.toa5` (`src/common/toa5.py`) is the only TOA5 reader. Every caller imports it directly, including the ingest layer `dam_safety.toa5_landing`, which adds the channel/thermistor mapping and the required `utc_offset_min`. There is no `dam_safety` re-export (see `docs/PROVENANCE.md`).
- `/workspace/oss-review/verify/verify_pins.py` and `verify_candidates.py` still contain PyTOA5 checks. Those checks cannot run now that the package is uninstalled. Under the clean-room rule I did not open them. Whoever owns the review harness should delete those sections.

## Tests (`tests/test_toa5.py`) and QA verification
Command: `app/.venv/bin/python -m pytest -q tests/test_toa5.py --cov=common --cov-branch --cov-report=term-missing`
- Header, records and coercion on both Campbell examples (LoggerNet; CR1000 manual with CRLF and empty units).
- Strict header and data errors, each with the line number checked.
- Quoting, NAN/INF, timestamps, RECORD range and rollover, BOM, and strict decoding.
- QA-017 to QA-019: the unterminated-line policies, sequence warnings (duplicate, out of order, rollover at both maxima, reset to 0) and ASCII-only/unsigned RECORD.
- QA's adversarial suite `qa/test_toa5_adversarial.py` (77 tests, including the PZ-03 oracle) passes.
- Open-source review checks reproduced: **parse**, **logger model** (CR1000X), **digits** (3013.66 Hz → 9082.15 digits, Eq.1) and
  **kPa** (2597.88 Hz → 6748.98 digits; G=−0.1795 → 418.80 kPa, Eq.2, tol 0.05). The manual's own example gives 418.7735.
  The test also runs end to end through the engine formula `VW_PIEZO 1.0.0` (raw_unit hz) → `eng_value` 418.8 kPa.
