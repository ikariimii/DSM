"""QA-055: catalog names with zero-width / BOM characters, quotes or fullwidth letters must not escape the prod checks.
One shared helper, dam_safety.catalog.normalize_catalog, used by the app (Settings.check), the job entry points
(_common.args) and the engine (audit_chain.require_signer_for_catalog)."""
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import audit_chain as ac  # noqa: E402
from dam_safety.catalog import ALLOWED_CATALOGS, PROD_CATALOG, is_prod_catalog, normalize_catalog  # noqa: E402
import _common  # noqa: E402
import verify_review_chain as vj  # noqa: E402

PROD_SPELLINGS = [
    "dam_prod",
    "DAM_PROD", "Dam_Prod",                                  # mixed case
    " dam_prod ", "\tdam_prod\n", "\u00a0dam_prod\u3000",   # whitespace incl. NBSP / ideographic space
    "\u200bdam_prod", "dam_prod\u200b", "dam\u200b_prod",     # zero-width space, anywhere
    "dam_\u200c\u200dprod", "\u2060dam_prod",                # ZWNJ / ZWJ / word joiner
    "\ufeffdam_prod", "dam_prod\ufeff",                      # BOM
    "\u202edam_prod\u202c", "\u200edam_prod",                # bidi override / LRM (also Cf)
    "\uff44\uff41\uff4d\uff3f\uff50\uff52\uff4f\uff44",      # fullwidth 'ｄａｍ＿ｐｒｏｄ'
    "\uff24\uff21\uff2d\uff3f\uff30\uff32\uff2f\uff24",      # fullwidth upper
    " \ufeff\uff44am_PROD\u200b ",                           # all of the above at once
]
REFUSED = [
    None, "", "   ", "\u200b", "\ufeff",                     # missing / nothing left after normalization
    "`dam_prod`", '"dam_prod"', "'dam_prod'", "[dam_prod]",  # quoted: NOT tolerated (unknown -> refused)
    "dam prod", "dam-prod", "damprod", "dam_prod2", "dam_production", "prod",
    "d\u0430m_prod",                                         # Cyrillic 'а' look-alike (NFKC does not fold it)
    "dam_prod.ref", "main", "hive_metastore", "dam_staging",
    b"dam_prod", 1, ["dam_prod"],                            # not a string
]


@pytest.mark.parametrize("name", PROD_SPELLINGS)
def test_prod_spellings_normalize_to_exactly_dam_prod(name):
    assert normalize_catalog(name) == "dam_prod" and is_prod_catalog(name)


@pytest.mark.parametrize("name,canon", [("dam_dev", "dam_dev"), (" DAM_TEST ", "dam_test"),
                                        ("\ufeffdam_dev\u200b", "dam_dev"), ("\uff44am_test", "dam_test")])
def test_non_prod_names(name, canon):
    assert normalize_catalog(name) == canon and not is_prod_catalog(name)


@pytest.mark.parametrize("name", REFUSED)
def test_refused_never_none(name):
    with pytest.raises(ValueError):
        normalize_catalog(name)
    with pytest.raises(ValueError):
        is_prod_catalog(name)


def test_error_message_is_ascii_safe():
    with pytest.raises(ValueError) as e:
        normalize_catalog("dam\u202eprod_x")
    assert str(e.value).isascii() and "\\u202e" in str(e.value)


def test_allowlist_matches_databricks_yml():
    """ALLOWED_CATALOGS == every catalog databricks.yml sets (targets.*.variables.catalog, plus any default)."""
    with open(os.path.join(ROOT, "databricks.yml"), encoding="utf-8") as f:
        b = yaml.safe_load(f)
    found = {t["variables"]["catalog"] for t in b["targets"].values() if "catalog" in (t.get("variables") or {})}
    default = (b["variables"]["catalog"] or {}).get("default")
    if default:
        found.add(default)
    assert found == set(ALLOWED_CATALOGS) and PROD_CATALOG in found
    assert all(normalize_catalog(c) == c for c in found)


# ------------------------------------------------------------------ engine: require_signer_for_catalog / verify_all
@pytest.mark.parametrize("name", PROD_SPELLINGS)
def test_engine_garbled_prod_still_requires_a_real_signer(name):
    with pytest.raises(ac.SignerConfigError):
        ac.require_signer_for_catalog(name, ac.NoopSigner())


@pytest.mark.parametrize("name", [n for n in REFUSED if n is not None])
def test_engine_unknown_catalog_fails_closed(name):
    with pytest.raises(ValueError):
        ac.require_signer_for_catalog(name, None)


def test_engine_non_prod_unchanged():
    ac.require_signer_for_catalog("dam_dev", None)
    ac.require_signer_for_catalog(" DAM_TEST\u200b", None)


@pytest.mark.parametrize("name", ["dam_prod\u200b", "\ufeffDAM_PROD", "\uff44\uff41\uff4d\uff3f\uff50\uff52\uff4f\uff44"])
def test_verify_all_garbled_prod_is_refused(name):
    heads = [{"dam_id": "CRD", "chain_id": "CRD", "chain_seq": 1, "head_hash": "c" * 64}]
    out = vj.verify_all([], heads, None, catalog=name)
    assert out and all(not v["ok"] and v["reason"] == "signer_refused" for v in out)


def test_verify_all_unknown_catalog_raises():
    with pytest.raises(ValueError):
        vj.verify_all([], [], None, catalog="`dam_prod`")


# ------------------------------------------------------------------ job entry points (_common.args)
@pytest.mark.parametrize("name", PROD_SPELLINGS[:6] + ["\ufeffdam_prod", "dam\u200b_prod"])
def test_jobs_get_the_canonical_name(monkeypatch, name):
    monkeypatch.setattr(sys, "argv", ["job", "--catalog", name])
    assert _common.args().catalog == "dam_prod"


@pytest.mark.parametrize("name", ["`dam_prod`", '"dam_prod"', "dam_staging", "", "d\u0430m_prod"])
def test_jobs_refuse_unknown(monkeypatch, name):
    monkeypatch.setattr(sys, "argv", ["job", "--catalog", name])
    with pytest.raises(SystemExit):
        _common.args()


# ------------------------------------------------------------------ verify_all fail closed (QA-055 follow-up, DA)
@pytest.mark.parametrize("name", ["", " ", "dam_staging", "dam_prod2", "dam prod", "d\u0430m_prod", "'dam_prod'",
                                  "\"dam_prod\"", "main", 0, 1.5, b"dam_prod", ["dam_prod"]])
def test_verify_all_unknown_empty_or_garbled_name_raises_and_never_passes(name):
    heads = [{"dam_id": "CRD", "chain_id": "CRD", "chain_seq": 1, "head_hash": "c" * 64}]
    for recs, hs in (([], []), ([], heads)):
        with pytest.raises(ValueError):
            vj.verify_all(recs, hs, None, catalog=name)


@pytest.mark.parametrize("name", [None, "dam_prod", "DAM_PROD", "dam_prod\u200b", "\uff44\uff41\uff4d\uff3f\uff50\uff52\uff4f\uff44"])
def test_verify_all_prod_or_missing_catalog_refuses_even_with_no_chains(name):
    out = vj.verify_all([], [], None, catalog=name)
    assert out == [{"dam_id": None, "chain_id": None, "records": 0, "ok": False, "reason": "signer_refused",
                    "first_broken_seq": None, "detail": out[0]["detail"]}]
    assert "dam_prod" in out[0]["detail"]


def test_verify_all_dev_and_test_still_verify():
    assert vj.verify_all([], [], None, catalog="dam_dev") == []
    assert vj.verify_all([], [], None, catalog=" DAM_TEST\u200b") == []
