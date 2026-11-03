from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app
from conftest import ADMIN, DSO, ENG, NOBODY, VIEWER, Client


def test_health(rc):
    assert rc.get("/api/health", user=None).json() == {"ok": True, "demo_mode": True}


def test_demo_fallback_identity(rc):
    me = rc.get("/api/me", user=None).json()
    assert me["email"] == ADMIN and me["is_admin"] and me["demo_mode"]
    assert {u["email"] for u in me["demo_users"]} >= {ENG, VIEWER, DSO, NOBODY}
    me = rc.tc.get("/api/me", headers={"x-demo-user": VIEWER}).json()
    assert me["email"] == VIEWER and me["dams"] == ["BFK"] and me["roles"] == {"BFK": "viewer"}


def test_forwarded_email_wins_over_demo_header(rc):
    me = rc.tc.get("/api/me", headers={"x-forwarded-email": "Engineer.CRD@demo.local", "x-demo-user": ADMIN}).json()
    assert me["email"] == ENG and not me["is_admin"]


def _prod_app(**kw):
    from backend.repos.analytics import MockAnalytics
    from backend import seed
    mock = MockAnalytics(seed.build())
    s = Settings(demo_mode=False, warehouse_id="wh", static_dir="/nonexistent", **kw)
    return create_app(s, pg_pool=object(), analytics_factory=lambda t: mock)


def test_prod_requires_forwarded_email(monkeypatch):
    from backend.repos import state
    monkeypatch.setattr(state.StateStore, "init_schema", lambda self: None)
    app = _prod_app()
    tc = TestClient(app)
    assert tc.get("/api/me").status_code == 401
    # demo header is ignored outside demo mode
    assert tc.get("/api/me", headers={"x-demo-user": ADMIN}).status_code == 401


def test_config_check_refuses_unsafe():
    import pytest
    with pytest.raises(RuntimeError):
        Settings(demo_mode=False, warehouse_id="").check()
    Settings(demo_mode=False, warehouse_id="abc").check()
    Settings(demo_mode=True).check()


def test_config_defaults(monkeypatch):
    for k in ("DEMO_MODE", "BOOTSTRAP_ADMINS", "DAM_CATALOG"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert s.demo_mode is False and s.bootstrap_admins == () and s.catalog == "dam_dev"
    monkeypatch.setenv("DEMO_MODE", "yes")
    monkeypatch.setenv("BOOTSTRAP_ADMINS", " A@x.org, b@y.org ,")
    s = Settings()
    assert s.demo_mode and s.bootstrap_admins == ("a@x.org", "b@y.org")
    assert s.uc("silver", "v_reading_current") == "`dam_dev`.`silver`.`v_reading_current`"
    monkeypatch.setenv("DEMO_MODE", "maybe")
    assert Settings().demo_mode is False


def test_unknown_user_has_no_access(rc):
    me = rc.get("/api/me", user="stranger@x.org").json()
    assert me["known"] is False and me["dams"] == [] and me["can_onboard"] is False
    assert rc.get("/api/portfolio", user="stranger@x.org").json()["dams"] == []
    assert rc.get("/api/dams/CRD", user="stranger@x.org").status_code == 403


def test_portfolio_scoped_by_acl(rc):
    assert [d["dam_id"] for d in rc.get("/api/portfolio").json()["dams"]] == ["CRD", "BFK", "HLD", "PHR", "STC"]
    assert {d["dam_id"] for d in rc.get("/api/portfolio", user=ENG).json()["dams"]} == {"CRD", "HLD"}
    assert {d["dam_id"] for d in rc.get("/api/portfolio", user=DSO).json()["dams"]} == {"PHR", "STC"}
    assert rc.get("/api/portfolio", user=NOBODY).json()["dams"] == []


def test_portfolio_counts(rc):
    crd = next(d for d in rc.get("/api/portfolio").json()["dams"] if d["dam_id"] == "CRD")
    assert crd["n_unverified"] >= 1  # CRD-SM-1 SETTLEMENT has no test vectors
    assert crd["max_active_level"] == 3 and crd["max_active_level_name"] == "Act"
    assert crd["alarms_open"] >= 1


def test_dam_detail(rc):
    r = rc.get("/api/dams/CRD", user=ENG).json()
    assert r["site"]["dam_id"] == "CRD" and r["my_role"] == "engineer"
    assert r["structures"] and r["pfms"]
    for i in r["instruments"]:
        lt = i["latest"]
        if lt:
            assert "display_status" in lt and "qa_flag" in lt and "cal_version" in lt
            if lt["display_status"] != "VERIFIED":
                assert lt["corrected_value"] is None and lt["eng_value"] is None
    sm = next(i for i in r["instruments"] if i["instrument_id"] == "CRD-SM-1")
    assert sm["latest"]["display_status"] == "UNVERIFIED_FORMULA"
    assert rc.get("/api/dams/NOPE").status_code == 404
    assert rc.get("/api/dams/CRD", user=VIEWER).status_code == 403


def test_formulas_list(rc):
    fs = {f["formula_id"]: f for f in rc.get("/api/formulas").json()["formulas"]}
    assert fs["VW_PIEZO"]["verified"] and fs["VW_PIEZO"]["app_check"]["verified"]
    assert fs["SETTLEMENT"]["verified"] is False


def test_unknown_route_404(rc):
    assert rc.get("/api/nope").status_code == 404


def test_identity_keyed_on_forwarded_user(monkeypatch):
    from backend import main
    from backend.repos import state
    store = state.StateStore("sqlite")
    store.upsert_user("eng@x.org", "Eng", False, "admin")
    store.set_role("eng@x.org", "CRD", "engineer", "admin")
    monkeypatch.setattr(main, "build_state", lambda settings, pg_pool=None: store)
    app = _prod_app()
    tc = TestClient(app)
    tok = "SECRET-OBO-TOKEN"
    h = {"x-forwarded-user": "1234@5678", "x-forwarded-email": "Eng@x.org", "x-forwarded-access-token": tok}
    me = tc.get("/api/me", headers=h).json()
    assert me["email"] == "eng@x.org" and tok not in str(me)
    assert store.get_user("eng@x.org")["user_id"] == "1234@5678"   # bound on first sign-in
    # email changes at the IdP: identity follows X-Forwarded-User, email is display only
    me2 = tc.get("/api/me", headers={**h, "x-forwarded-email": "renamed@x.org"}).json()
    assert me2["email"] == "eng@x.org" and me2.get("roles", {}).get("CRD", "engineer") == "engineer"
    # a different user presenting the bound email gets no grants
    other = tc.get("/api/me", headers={"x-forwarded-user": "9999@5678", "x-forwarded-email": "eng@x.org"}).json()
    assert other["is_admin"] is False and not other.get("roles")
    # email alone (no X-Forwarded-User) is refused outside demo mode
    assert tc.get("/api/me", headers={"x-forwarded-email": "eng@x.org"}).status_code == 401


def test_token_never_logged_or_in_ctx_repr(caplog, rc):
    import logging
    from backend.deps import Ctx
    caplog.set_level(logging.DEBUG)
    tok = "SECRET-OBO-TOKEN-XYZ"
    h = {"x-forwarded-user": "uid-1", "x-forwarded-email": ADMIN, "x-forwarded-access-token": tok}
    for url in ("/api/me", "/api/dams", "/api/alarms", "/api/does-not-exist"):
        r = rc.tc.get(url, headers=h)
        assert tok not in r.text
    assert tok not in caplog.text
    assert tok not in repr(Ctx(email="a", token=tok))
