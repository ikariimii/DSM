"""QA-041: DEMO_MODE=true is refused against the production catalog, however the catalog is spelled, at startup,
with no override."""
import pytest

from backend.config import Settings, is_prod_catalog
from backend.main import create_app

PROD_SPELLINGS = ["dam_prod", "DAM_PROD", " DAM_PROD ", "\tdam_prod\n", "Dam_Prod", "`dam_prod`", '"dam_prod"',
                  " ` DAM_PROD ` "]


@pytest.mark.parametrize("cat", PROD_SPELLINGS)
def test_check_refuses_demo_mode_on_prod_catalog(cat):
    assert is_prod_catalog(cat)
    with pytest.raises(RuntimeError, match="QA-041"):
        Settings(demo_mode=True, catalog=cat).check()


@pytest.mark.parametrize("cat", ["dam_dev", "dam_test", "dam_prod2", "prod", "dam-prod", "", None])
def test_demo_mode_on_non_prod_catalogs_still_starts(cat):
    assert not is_prod_catalog(cat)
    Settings(demo_mode=True, catalog=cat).check()


@pytest.mark.parametrize("cat", PROD_SPELLINGS[:3])
def test_startup_refuses_from_env(monkeypatch, cat):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DAM_CATALOG", cat)
    built = []
    monkeypatch.setattr("backend.main.build_state", lambda *a, **k: built.append(1))
    with pytest.raises(RuntimeError, match="QA-041"):
        create_app()
    assert built == []                                               # refused before any state is built


def test_no_override_or_escape_hatch(monkeypatch):
    for name in ("ALLOW_DEMO_IN_PROD", "DEMO_MODE_ALLOW_PROD", "DAM_ALLOW_DEMO_PROD", "FORCE_DEMO", "UNSAFE",
                 "DAM_UNSAFE_DEMO", "QA041_OVERRIDE", "SKIP_SETTINGS_CHECK"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DAM_CATALOG", " DAM_PROD ")
    with pytest.raises(RuntimeError, match="QA-041"):
        Settings().check()
    with pytest.raises(RuntimeError, match="QA-041"):
        create_app()


def test_prod_without_demo_keeps_the_existing_rules():
    with pytest.raises(RuntimeError, match="DATABRICKS_WAREHOUSE_ID"):
        Settings(demo_mode=False, catalog="dam_prod", warehouse_id="").check()
    Settings(demo_mode=False, catalog="dam_prod", warehouse_id="wh-1").check()
