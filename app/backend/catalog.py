"""QA-055: ONE canonical catalog name check, shared by the app (app/backend/config.Settings.check), the job entry
points (src/jobs/_common.args) and the engine (audit_chain.require_signer_for_catalog). The app is deployed from app/
alone (resources/app.yml source_code_path ../app) and cannot import src/, so it carries a BYTE-IDENTICAL copy at
app/backend/catalog.py; app/tests/test_settings_prod_guard.py fails if the two differ. Change both together.

normalize_catalog(name) -> the canonical allowlisted name, or ValueError. Never returns None.
  1. NFKC normalization (fullwidth 'ｄａｍ＿ｐｒｏｄ' -> 'dam_prod', compatibility forms folded);
  2. remove EVERY Unicode format character (category Cf: zero-width space/joiners, word joiner, BOM U+FEFF, bidi
     marks/embeddings ...), wherever it appears;
  3. strip surrounding whitespace and compare case-insensitively;
  4. anything that is not then EXACTLY one of ALLOWED_CATALOGS is refused. No quote stripping: '`dam_prod`',
     '"dam_prod"' and "'dam_prod'" are refused as unknown (fail closed), as are None, '', non-strings, interior
     spaces and look-alike letters that NFKC does not fold (e.g. Cyrillic 'а').

ALLOWED_CATALOGS mirrors databricks.yml: targets dev / test / prod set variables.catalog to dam_dev / dam_test /
dam_prod and the catalog variable has no default (tests/test_catalog_allowlist.py fails if they drift).
"""
from __future__ import annotations

import unicodedata

ALLOWED_CATALOGS = ("dam_dev", "dam_test", "dam_prod")
PROD_CATALOG = "dam_prod"


def normalize_catalog(name) -> str:
    """The canonical allowlisted catalog name for `name`; ValueError for None, empty, garbled or unknown names."""
    if not isinstance(name, str):
        raise ValueError(f"refusing catalog {name!r}: a catalog name (str) is required (QA-055)")
    s = unicodedata.normalize("NFKC", name)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf").strip().lower()
    if s in ALLOWED_CATALOGS:
        return s
    raise ValueError(f"refusing catalog {ascii(name)}: not one of {', '.join(ALLOWED_CATALOGS)} after NFKC, "
                     "format-character and whitespace normalization (QA-055)")


def is_prod_catalog(name) -> bool:
    """True when `name` normalizes to the production catalog; ValueError (fail closed) when it is not allowlisted."""
    return normalize_catalog(name) == PROD_CATALOG
