"""BOOTSTRAP_ADMINS: first-deploy only, empty admin table only, one audit entry per admin, loud startup warning."""
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.bootstrap import BANNER, bootstrap_admins
from backend.config import Settings
from backend.repos.analytics import MockAnalytics
from backend.repos.state import StateStore
from backend import seed
from conftest import Client

LOGGER = "dam_safety_app.bootstrap"


def _boot_audits(st):
    return [e for e in st.audit_entries() if e["action"] == "bootstrap_admin"]


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def test_not_set_is_noop_and_silent(caplog):
    st = StateStore("sqlite")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert bootstrap_admins(st, ()) == []
    assert st.list_users() == [] and st.audit_entries() == [] and _warnings(caplog) == []


def test_empty_table_creates_each_admin_with_one_audit_entry(caplog):
    st = StateStore("sqlite")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        created = bootstrap_admins(st, ("A@corp.com", "b@corp.com", "a@corp.com", " "))
    assert created == ["a@corp.com", "b@corp.com"]  # normalised, de-duplicated, blanks dropped
    for e in created:
        u = st.get_user(e)
        assert u["is_admin"] and u["active"] and u["created_by"] == "bootstrap"
    audits = _boot_audits(st)
    assert sorted(a["target"] for a in audits) == created  # exactly one entry per admin created
    assert all(a["actor"] == "system" and a["detail"] == {"reason": "admin table empty at startup"} for a in audits)
    msgs = _warnings(caplog)
    assert BANNER in msgs[0] and "SECURITY WARNING: BOOTSTRAP_ADMINS is set" in msgs[0] and "FIRST deploy only" in msgs[0]
    assert sum("created global admin" in m for m in msgs) == 2


def test_existing_active_admin_blocks_bootstrap_but_still_warns(caplog):
    st = StateStore("sqlite")
    st.upsert_user("owner@corp.com", "Owner", True, "admin-ui")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert bootstrap_admins(st, ("intruder@corp.com",)) == []
    assert st.get_user("intruder@corp.com") is None and _boot_audits(st) == []
    msgs = _warnings(caplog)
    assert BANNER in msgs[0] and any("ignored: 1 active admin(s) already exist" in m for m in msgs)


def test_existing_non_admin_user_is_not_re_granted_once_admins_exist():
    st = StateStore("sqlite")
    st.upsert_user("owner@corp.com", "Owner", True, "admin-ui")
    st.upsert_user("eng@corp.com", "Eng", False, "admin-ui")
    assert bootstrap_admins(st, ("eng@corp.com",)) == []
    assert st.get_user("eng@corp.com")["is_admin"] is False


def test_only_inactive_admins_or_plain_users_count_as_empty():
    st = StateStore("sqlite")
    st.upsert_user("old@corp.com", "Old", True, "admin-ui")
    st.deactivate_user("old@corp.com")
    st.upsert_user("eng@corp.com", "Eng", False, "admin-ui")
    assert st.active_admin_count() == 0
    assert bootstrap_admins(st, ("eng@corp.com",)) == ["eng@corp.com"]
    assert st.get_user("eng@corp.com")["is_admin"] is True and st.active_admin_count() == 1
    assert [a["target"] for a in _boot_audits(st)] == ["eng@corp.com"]


def test_second_startup_with_variable_still_set_creates_nothing():
    st = StateStore("sqlite")
    assert bootstrap_admins(st, ("a@corp.com",)) == ["a@corp.com"]
    assert bootstrap_admins(st, ("a@corp.com", "b@corp.com")) == []
    assert st.get_user("b@corp.com") is None and len(_boot_audits(st)) == 1


def _prod_app(monkeypatch, st, admins):
    monkeypatch.setattr(main, "build_state", lambda settings, pg_pool=None: st)
    mock = MockAnalytics(seed.build())
    s = Settings(demo_mode=False, warehouse_id="wh", static_dir="/nonexistent", bootstrap_admins=admins)
    return Client(TestClient(main.create_app(s, analytics_factory=lambda t: mock)))


def test_prod_startup_bootstraps_empty_store_and_audit_is_visible(monkeypatch, caplog):
    st = StateStore("sqlite")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        c = _prod_app(monkeypatch, st, ("boot@corp.com",))
    assert any(BANNER in m for m in _warnings(caplog))  # logged at startup, before any request
    me = c.get("/api/me", user="boot@corp.com").json()
    assert me["known"] and me["is_admin"] and set(me["dams"]) == {"CRD", "BFK", "HLD", "PHR", "STC"}
    entries = c.get("/api/admin/audit", user="boot@corp.com").json()["entries"]
    assert [e["target"] for e in entries if e["action"] == "bootstrap_admin"] == ["boot@corp.com"]


def test_listed_email_gets_no_rights_per_request_when_admins_exist(monkeypatch):
    st = StateStore("sqlite")
    st.upsert_user("owner@corp.com", "Owner", True, "admin-ui")
    c = _prod_app(monkeypatch, st, ("late@corp.com",))
    me = c.get("/api/me", user="late@corp.com").json()
    assert me["known"] is False and me["is_admin"] is False and me["dams"] == []
    assert c.get("/api/admin/audit", user="late@corp.com").status_code == 403
    assert st.get_user("late@corp.com") is None and _boot_audits(st) == []


def test_removed_admin_is_not_restored_by_variable(monkeypatch):
    """Previously the env var overrode is_admin on every request; now only app_user decides."""
    st = StateStore("sqlite")
    c = _prod_app(monkeypatch, st, ("boot@corp.com",))
    st.upsert_user("owner@corp.com", "Owner", True, "boot@corp.com")
    st.upsert_user("boot@corp.com", "Boot", False, "owner@corp.com")  # demoted in the Admin UI
    assert c.get("/api/me", user="boot@corp.com").json()["is_admin"] is False


def test_demo_seed_admins_mean_bootstrap_is_ignored(demo_settings, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        app = main.create_app(replace(demo_settings, bootstrap_admins=("boot@corp.com",)))
    assert app.state.store.get_user("boot@corp.com") is None
    assert any("ignored" in m for m in _warnings(caplog))


@pytest.mark.parametrize("raw,expected", [("", ()), ("x@y.org", ("x@y.org",)), (" X@y.org ,, z@w.org", ("x@y.org", "z@w.org"))])
def test_env_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("BOOTSTRAP_ADMINS", raw)
    assert Settings().bootstrap_admins == expected
