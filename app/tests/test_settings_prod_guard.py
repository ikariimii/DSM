"""QA-041 + QA-055: the catalog must normalize to exactly an allowlisted name (dam_dev | dam_test | dam_prod) or the
app refuses to start; DEMO_MODE=true is refused against dam_prod however it is spelled; no override. The app's
backend/catalog.py must stay byte-identical to the shared src/dam_safety/catalog.py."""
from pathlib import Path

import pytest

from backend.config import Settings, is_prod_catalog
from backend.main import create_app

APP = Path(__file__).resolve().parents[1]
PROD_SPELLINGS = ["dam_prod", "DAM_PROD", " DAM_PROD ", "\tdam_prod\n", "Dam_Prod",
                  "\u200bdam_prod", "dam\u200b_prod", "\ufeffdam_prod", "dam_prod\ufeff", "\u2060DAM_PROD\u200d",
                  "\uff44\uff41\uff4d\uff3f\uff50\uff52\uff4f\uff44", "\u00a0dam_prod\u3000"]
REFUSED = ["`dam_prod`", '"dam_prod"', "'dam_prod'", " ` DAM_PROD ` ", "dam_prod2", "prod", "dam-prod",
           "dam prod", "d\u0430m_prod", "dam_staging", "", "   ", "\u200b", None]


def test_app_copy_is_byte_identical_to_the_shared_helper():
    shared = APP.parent / "src" / "dam_safety" / "catalog.py"
    assert shared.is_file(), "src/dam_safety/catalog.py (DA patch qa055_catalog_allowlist) is missing"
    assert (APP / "backend" / "catalog.py").read_bytes() == shared.read_bytes()


@pytest.mark.parametrize("cat", PROD_SPELLINGS)
def test_check_refuses_demo_mode_on_prod_catalog(cat):
    assert is_prod_catalog(cat)
    with pytest.raises(RuntimeError, match="QA-041"):
        Settings(demo_mode=True, catalog=cat).check()


@pytest.mark.parametrize("cat", PROD_SPELLINGS)
def test_prod_spellings_canonicalize_without_demo(cat):
    s = Settings(demo_mode=False, catalog=cat, warehouse_id="wh-1")
    s.check()
    assert s.catalog == "dam_prod" and s.uc("ref", "dam") == "`dam_prod`.`ref`.`dam`"


@pytest.mark.parametrize("demo", [True, False])
@pytest.mark.parametrize("cat", REFUSED)
def test_unknown_or_garbled_catalog_refuses_to_start(cat, demo):
    with pytest.raises(RuntimeError, match="QA-055"):
        Settings(demo_mode=demo, catalog=cat, warehouse_id="wh-1").check()


@pytest.mark.parametrize("cat,canon", [("dam_dev", "dam_dev"), (" DAM_TEST\u200b", "dam_test")])
def test_demo_mode_on_non_prod_catalogs_still_starts(cat, canon):
    s = Settings(demo_mode=True, catalog=cat)
    s.check()
    assert s.catalog == canon and not is_prod_catalog(cat)


@pytest.mark.parametrize("cat", PROD_SPELLINGS[:3] + ["\ufeffdam_prod", "\uff44am_prod"])
def test_startup_refuses_from_env(monkeypatch, cat):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DAM_CATALOG", cat)
    built = []
    monkeypatch.setattr("backend.main.build_state", lambda *a, **k: built.append(1))
    with pytest.raises(RuntimeError, match="QA-041"):
        create_app()
    assert built == []                                               # refused before any state is built


def test_startup_refuses_unknown_from_env(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DAM_CATALOG", "`dam_prod`")
    with pytest.raises(RuntimeError, match="QA-055"):
        create_app()


def test_no_override_or_escape_hatch(monkeypatch):
    for name in ("ALLOW_DEMO_IN_PROD", "DEMO_MODE_ALLOW_PROD", "DAM_ALLOW_DEMO_PROD", "FORCE_DEMO", "UNSAFE",
                 "DAM_UNSAFE_DEMO", "QA041_OVERRIDE", "QA055_OVERRIDE", "SKIP_SETTINGS_CHECK", "DAM_EXTRA_CATALOGS"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DAM_CATALOG", "\ufeff DAM_PROD\u200b ")
    with pytest.raises(RuntimeError, match="QA-041"):
        Settings().check()
    with pytest.raises(RuntimeError, match="QA-041"):
        create_app()


def test_prod_without_demo_keeps_the_existing_rules():
    with pytest.raises(RuntimeError, match="DATABRICKS_WAREHOUSE_ID"):
        Settings(demo_mode=False, catalog="dam_prod", warehouse_id="").check()
    Settings(demo_mode=False, catalog="dam_prod", warehouse_id="wh-1").check()
