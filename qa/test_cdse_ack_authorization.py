"""QA-047: CDSE acknowledgement authorization (independent of app/tests/test_cdse_roles.py).

Derived from app/backend/routers/alarms.py (CDSE_ACK_MIN_LEVEL=2, ack()), routers/signoffs.py
(is_cdse / cdse_principals / review_level), SCHEMA_CONTRACT v1.6 ROLE-LEVEL, and FERC Ch.14 App K 14-K-4:
the CDSE is a named person (ref.dam.cdse_id / direct membership), not an app role. Level ≥ 2 ack is
CDSE-only; everyone else including dso/dam_admin/admin gets 403 with no rows written.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

from backend import audit_chain as ac  # noqa: E402
from backend import main as app_main  # noqa: E402
from backend import seed  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.repos.analytics import MockAnalytics  # noqa: E402
from backend.repos.state import StateStore  # noqa: E402

# Demo personas (seed): cdse.crd@demo.local is CDSE of CRD only; engineer on HLD. App roles never confer CDSE.
ADMIN = "admin@demo.local"
ENG = "engineer.crd@demo.local"
DSO_CRD = "dso.crd@demo.local"          # granted in fixture
DAM_ADMIN_CRD = "damadmin.crd@demo.local"
CDSE_CRD = "cdse.crd@demo.local"
BODY = {"rationale": "QA-047 independent CDSE-ack authorization check"}


def _h(email, user_id=None, **extra):
    """Proxy headers. Demo mode resolves by email when user_id is omitted."""
    uid = user_id if user_id is not None else f"uid-{email.lower()}"
    return {"x-forwarded-user": uid, "x-forwarded-email": email, **extra}


def _snap(st, dam="CRD"):
    """Comparable snapshots of every table the ack path can write."""
    return {
        "ack": st.execute("SELECT * FROM {s}alarm_ack ORDER BY event_id"),
        "signoff": st.execute(
            "SELECT review_id, dam_id, subject_id, review_level, decision, action_type, chain_seq, record_hash "
            "FROM {s}review_signoff WHERE dam_id = %s ORDER BY chain_seq, review_id", (dam,)),
        "head": st.execute("SELECT * FROM {s}review_chain_head WHERE chain_id = %s", (dam,)),
        "audit": st.execute(
            "SELECT actor, action, dam_id, target FROM {s}audit_log WHERE dam_id = %s ORDER BY id", (dam,)),
        "comments": st.execute(
            "SELECT event_id, author, body FROM {s}alarm_comment ORDER BY id"),
    }


def _open_by_level(an, dam, level):
    evs = [e for e in an.alarm_events([dam]) if e.get("level") == level]
    assert evs, f"no level-{level} alarm for {dam} in seed/fixture"
    return evs[0]["event_id"]


def _inject_alarm(an, *, event_id, dam_id, level, instrument_id=None):
    """Add a synthetic open alarm episode to the in-memory analytics payload."""
    inst = instrument_id or f"{dam_id}-PZ-QA"
    row = dict(event_id=event_id, dam_id=dam_id, instrument_id=inst, sensor_id=inst,
               threshold_id=None, threshold_version=None, level=level, limit_type="upper",
               direction="above", value=999.0, unit="m", event_ts="2026-11-02T00:00:00Z",
               message=f"QA-047 synthetic L{level}", machine_state="ACTION_UNACK" if level >= 2 else "ALERT_UNACK",
               raise_reading_id=None, state="open")
    an.d.setdefault("alarm_events", []).append(row)
    return event_id


@pytest.fixture()
def demo(tmp_path):
    """Demo app with extra CRD dso/dam_admin personas for role-elevation negative cases."""
    app = app_main.create_app(Settings(demo_mode=True, static_dir=str(tmp_path / "nodist")))
    st = app.state.store
    for email, name in ((DSO_CRD, "QA DSO CRD"), (DAM_ADMIN_CRD, "QA Dam Admin CRD")):
        st.upsert_user(email, name, False, "qa047")
    st.set_role(DSO_CRD, "CRD", "dso", "qa047")
    st.set_role(DAM_ADMIN_CRD, "CRD", "dam_admin", "qa047")
    # Ensure CDSE user exists (seed already has them on fresh demo; re-bind roles for clarity).
    if st.get_user(CDSE_CRD) is None:
        st.upsert_user(CDSE_CRD, "Chris CDSE", False, "qa047")
        st.set_role(CDSE_CRD, "CRD", "engineer", "qa047")
        st.set_role(CDSE_CRD, "HLD", "engineer", "qa047")
    tc = TestClient(app)
    yield tc, app
    st._conn.close()


def _an(app):
    return app.state.analytics_factory(None)


# --------------------------------------------------------------------------- negatives: non-CDSE on L2/L3
@pytest.mark.parametrize("level", [2, 3])
@pytest.mark.parametrize("email,label", [
    (ENG, "engineer"),
    (DSO_CRD, "dso"),
    (DAM_ADMIN_CRD, "dam_admin"),
    (ADMIN, "platform_admin"),
])
def test_non_cdse_role_gets_403_on_level_2_or_3_and_writes_nothing(demo, level, email, label):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    eid = _open_by_level(an, "CRD", level)
    before = _snap(st, "CRD")
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(email), json=BODY)
    assert r.status_code == 403, (label, level, r.status_code, r.text)
    assert "CDSE" in r.json()["detail"] or "CDSE" in r.text
    assert _snap(st, "CRD") == before, f"{label} L{level}: a table changed after 403"


def test_cdse_of_dam_a_cannot_ack_level_2_on_dam_b(demo):
    """cdse.crd@demo.local is CDSE of CRD and only engineer on HLD; HLD's CDSE is cdse.hld@demo.local."""
    tc, app = demo
    st = app.state.store
    an = _an(app)
    eid = _inject_alarm(an, event_id="EV-QA047-HLD-L2", dam_id="HLD", level=2)
    before_hld = _snap(st, "HLD")
    before_crd = _snap(st, "CRD")
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(CDSE_CRD), json=BODY)
    assert r.status_code == 403, r.text
    assert _snap(st, "HLD") == before_hld
    assert _snap(st, "CRD") == before_crd


# --------------------------------------------------------------------------- principal matching (case / whitespace) — also bears on QA-046
@pytest.mark.parametrize("email", [
    "CDSE.CRD@DEMO.LOCAL",
    "Cdse.Crd@Demo.Local",
    "  cdse.crd@demo.local  ",
])
def test_cdse_principal_match_is_case_and_whitespace_insensitive_on_email(demo, email):
    tc, app = demo
    an = _an(app)
    eid = _open_by_level(an, "CRD", 2)
    # Use a distinct open L2 each time: first call consumes EV-00002; pick an open one dynamically.
    open_l2 = [e["event_id"] for e in an.alarm_events(["CRD"])
               if e.get("level") == 2 and (app.state.store.ack_states([e["event_id"]]).get(e["event_id"]) or {}).get("state", "open") == "open"]
    if not open_l2:
        eid = _inject_alarm(an, event_id=f"EV-QA047-CASE-{abs(hash(email)) % 10**6}", dam_id="CRD", level=2)
    else:
        eid = open_l2[0]
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(email), json=BODY)
    assert r.status_code == 200, (email, r.status_code, r.text)
    row = app.state.store.execute(
        "SELECT review_level FROM {s}review_signoff WHERE subject_id = %s ORDER BY chain_seq DESC LIMIT 1",
        (eid,))[0]
    assert row["review_level"] == "CDSE"


def test_cdse_principal_match_on_forwarded_user_id_casefold(demo):
    """is_cdse also matches X-Forwarded-User (strip+lower) against cdse_id."""
    tc, app = demo
    an = _an(app)
    # Point CRD's cdse_id at a user-id style principal; keep email different so only user_id matches.
    an.d["dam_cdse"] = {**an.d["dam_cdse"], "CRD": "UID-CDSE-CRD"}
    eid = _inject_alarm(an, event_id="EV-QA047-UID", dam_id="CRD", level=3)
    r = tc.post(f"/api/alarms/{eid}/ack",
                headers=_h("someone.else@demo.local", user_id="  Uid-Cdse-Crd  "), json=BODY)
    # someone.else may lack ACL — seed them as engineer on CRD
    app.state.store.upsert_user("someone.else@demo.local", "Other", False, "qa047")
    app.state.store.set_role("someone.else@demo.local", "CRD", "engineer", "qa047")
    r = tc.post(f"/api/alarms/{eid}/ack",
                headers=_h("someone.else@demo.local", user_id="  Uid-Cdse-Crd  "), json=BODY)
    assert r.status_code == 200, r.text
    assert app.state.store.execute(
        "SELECT review_level FROM {s}review_signoff WHERE subject_id = %s", (eid,))[0]["review_level"] == "CDSE"


# --------------------------------------------------------------------------- fail closed
def test_missing_cdse_id_fails_closed_with_403(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    an.d["dam_cdse"] = {**an.d["dam_cdse"], "CRD": None}
    an.d["project_membership"] = []
    eid = _inject_alarm(an, event_id="EV-QA047-NOCDSE", dam_id="CRD", level=2)
    before = _snap(st, "CRD")
    # Even the former CDSE persona must fail — cdse_id is gone.
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(CDSE_CRD), json=BODY)
    assert r.status_code == 403, r.text
    assert _snap(st, "CRD") == before


def test_unknown_cdse_id_fails_closed_with_403(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    an.d["dam_cdse"] = {**an.d["dam_cdse"], "CRD": "nobody-named-this@example.invalid"}
    eid = _inject_alarm(an, event_id="EV-QA047-UNK", dam_id="CRD", level=3)
    before = _snap(st, "CRD")
    for email in (CDSE_CRD, ENG, ADMIN, DSO_CRD):
        r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(email), json=BODY)
        assert r.status_code == 403, (email, r.text)
    assert _snap(st, "CRD") == before


def test_analytics_lookup_error_fails_closed_with_403(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    eid = _inject_alarm(an, event_id="EV-QA047-ERR", dam_id="CRD", level=2)
    before = _snap(st, "CRD")

    def boom(_dam_id):
        raise RuntimeError("QA-047 forced analytics failure")

    an.cdse_principals = boom  # type: ignore[method-assign]
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(CDSE_CRD), json=BODY)
    assert r.status_code == 403, r.text
    assert _snap(st, "CRD") == before


# --------------------------------------------------------------------------- level 1 unchanged; success paths; chain verifies
def test_level_1_ack_still_allowed_for_engineer_and_records_engineer_review_level(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    eid = _open_by_level(an, "CRD", 1)
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(ENG), json=BODY)
    assert r.status_code == 200, r.text
    row = st.execute("SELECT review_level, action_type, decision FROM {s}review_signoff WHERE subject_id = %s",
                     (eid,))[0]
    assert row["review_level"] == "engineer"
    assert row["action_type"] == "alarm_ack" and row["decision"] == "acknowledge"
    ack = st.ack_states([eid])[eid]
    assert ack["state"] == "acknowledged"


def test_real_cdse_ack_succeeds_with_review_level_cdse_and_chain_verifies(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    eid = _inject_alarm(an, event_id="EV-QA047-OK", dam_id="CRD", level=3)
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(CDSE_CRD), json=BODY)
    assert r.status_code == 200, r.text
    row = st.execute(
        "SELECT review_level, action_type, decision, record_hash, chain_seq FROM {s}review_signoff "
        "WHERE subject_id = %s", (eid,))[0]
    assert row["review_level"] == "CDSE"
    assert row["action_type"] == "alarm_ack"
    assert st.ack_states([eid])[eid]["state"] == "acknowledged"
    v = tc.get("/api/dams/CRD/audit/signoffs/verify", headers=_h(ENG))
    assert v.status_code == 200 and v.json()["ok"] is True, v.text
    # Independent re-verify
    assert ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"))["ok"] is True


def test_list_flags_requires_cdse_and_can_ack_for_ui(demo):
    tc, app = demo
    r = tc.get("/api/alarms?dam_id=CRD", headers=_h(ENG))
    assert r.status_code == 200
    alarms = r.json()["alarms"]
    for a in alarms:
        if (a.get("level") or 0) >= 2 and a.get("state") == "open":
            assert a["requires_cdse"] is True
            assert a["is_cdse"] is False  # engineer is not CDSE
            assert a["can_ack"] is False
    r2 = tc.get("/api/alarms?dam_id=CRD", headers=_h(CDSE_CRD))
    for a in r2.json()["alarms"]:
        if (a.get("level") or 0) >= 2 and a.get("state") == "open":
            assert a["is_cdse"] is True and a["can_ack"] is True


# =========================================================================== c4a384c / 0e79376 extensions
# cdse_ack_required (c4a384c): flagged rule needs CDSE at ANY level; OR of episode snapshot / view flag / current rule.
# View-based live lookup (0e79376): SqlAnalytics.cdse_principals; missing/empty/error → fail closed. Demo MockAnalytics
# unchanged; these cases exercise signoffs.cdse_principals + a stub analytics object.


class _StubAn:
    """Minimal analytics for is_cdse / cdse_principals edge cases (not MockAnalytics seed)."""

    def __init__(self, principals=None, error=None, missing=False):
        self._principals = principals
        self._error = error
        self._missing = missing

    def cdse_principals(self, dam_id):
        if self._missing:
            # Mimic TABLE_OR_VIEW_NOT_FOUND path: raise an Exception that SqlAnalytics would catch;
            # for the router we call signoffs.cdse_principals which wraps any Exception as empty.
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND: v_dam_reviewer_authority")
        if self._error is not None:
            raise self._error
        return list(self._principals or [])


def test_cdse_principals_empty_view_means_nobody_is_cdse():
    from backend.routers import signoffs as sg
    from backend.deps import Ctx
    an = _StubAn(principals=[])
    c = Ctx(email=CDSE_CRD, user_id="uid-cdse", is_admin=False, roles={"CRD": "engineer"})
    assert sg.cdse_principals(an, "CRD") == frozenset()
    assert sg.is_cdse(c, "CRD", an) is False


def test_cdse_principals_lookup_error_fails_closed():
    from backend.routers import signoffs as sg
    from backend.deps import Ctx
    an = _StubAn(error=RuntimeError("warehouse blew up"))
    c = Ctx(email=CDSE_CRD, user_id="uid-cdse", is_admin=False, roles={"CRD": "engineer"})
    assert sg.cdse_principals(an, "CRD") == frozenset()  # exception → empty
    assert sg.is_cdse(c, "CRD", an) is False


def test_cdse_principals_missing_view_fails_closed():
    from backend.routers import signoffs as sg
    from backend.deps import Ctx
    an = _StubAn(missing=True)
    c = Ctx(email=CDSE_CRD, user_id="uid-cdse", is_admin=False, roles={"CRD": "engineer"})
    assert sg.cdse_principals(an, "CRD") == frozenset()
    assert sg.is_cdse(c, "CRD", an) is False


def test_duplicate_conflicting_cdse_principals_still_match_intersection_only():
    """View returning multiple principals: caller must match one of them; a non-listed person is not CDSE.
    Conflicting rows that include both the real CDSE and an impostor still authorize the real CDSE (set membership);
    an impostor who is also listed would be authorized — that is a data defect, not an app bypass. Documented here."""
    from backend.routers import signoffs as sg
    from backend.deps import Ctx
    an = _StubAn(principals=["cdse.crd@demo.local", "impostor@demo.local"])
    real = Ctx(email="cdse.crd@demo.local", user_id="u1", is_admin=False, roles={"CRD": "engineer"})
    fake = Ctx(email="engineer.crd@demo.local", user_id="u2", is_admin=False, roles={"CRD": "engineer"})
    assert sg.is_cdse(real, "CRD", an) is True
    assert sg.is_cdse(fake, "CRD", an) is False


def test_flagged_level_1_requires_cdse_ack(demo):
    """c4a384c: cdse_ack_required on a level-1 episode forces CDSE ack (engineer gets 403)."""
    tc, app = demo
    st = app.state.store
    an = _an(app)
    eid = _inject_alarm(an, event_id="EV-QA047-FLAG-L1", dam_id="CRD", level=1)
    # Mark the episode as flagged at open (snapshot column OR-ed by rule_flag)
    for e in an.d["alarm_events"]:
        if e["event_id"] == eid:
            e["cdse_ack_required_at_open"] = True
            e["cdse_ack_required"] = True
    before = _snap(st, "CRD")
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(ENG), json=BODY)
    assert r.status_code == 403, r.text
    assert _snap(st, "CRD") == before
    r2 = tc.post(f"/api/alarms/{eid}/ack", headers=_h(CDSE_CRD), json=BODY)
    assert r2.status_code == 200, r2.text
    assert st.execute("SELECT review_level FROM {s}review_signoff WHERE subject_id = %s",
                      (eid,))[0]["review_level"] == "CDSE"


def test_unflagged_level_1_still_engineer(demo):
    tc, app = demo
    an = _an(app)
    eid = _inject_alarm(an, event_id="EV-QA047-NOFLAG-L1", dam_id="CRD", level=1)
    for e in an.d["alarm_events"]:
        if e["event_id"] == eid:
            e["cdse_ack_required_at_open"] = False
            e.pop("cdse_ack_required", None)
    r = tc.post(f"/api/alarms/{eid}/ack", headers=_h(ENG), json=BODY)
    assert r.status_code == 200, r.text


def test_needs_cdse_ack_semantics_unit():
    from backend.routers import alarms as al
    assert al.needs_cdse_ack({"level": 1}) is False
    assert al.needs_cdse_ack({"level": 2}) is True
    assert al.needs_cdse_ack({"level": 3}) is True
    assert al.needs_cdse_ack({"level": 1, "cdse_ack_required": True}) is True
    assert al.needs_cdse_ack({"level": 1}, flagged=True) is True
    assert al.needs_cdse_ack({"level": 1}, flagged=False) is False
    assert al.needs_cdse_ack({"level": 2}, flagged=False) is True  # level still wins
