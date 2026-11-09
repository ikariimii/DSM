"""v1.6 ROLE-LEVEL (Genie-confirmed): CDSE review level and the CDSE acknowledgement belong ONLY to the principal named
by ref.dam.cdse_id (FERC Ch.14 App K 14-K-4) or a direct ref.project_membership CDSE grant for that dam. engineer ->
engineer; dso, dam_admin and platform admin get no review elevation (their user/role admin powers are unchanged).
HIGH = alarm level 2 (Evaluate / ACTION), CRITICAL = level 3 (Act); level 1 (Verify / ALERT) stays engineer+.
Existing chain records written under the earlier proposal keep their stored review_level and still verify."""
import json

import pytest
from conftest import ADMIN, CDSE, DSO, ENG, VIEWER

from backend import audit_chain as ac
from backend.routers import alarms as alarms_router
from backend.routers import signoffs as sg

DAMADMIN = "damadmin.crd@demo.local"
LEVELS = {2: "HIGH", 3: "CRITICAL"}


@pytest.fixture()
def crd(c):
    """CRD with one user per app role: viewer, engineer (ENG), dso, dam_admin, platform admin, and the named CDSE."""
    for email, role in ((VIEWER, "viewer"), (DSO, "dso"), (DAMADMIN, "dam_admin")):
        assert c.put("/api/admin/roles", user=ADMIN, json={"email": email, "dam_id": "CRD", "role": role}).status_code == 200
    return c


def _events(c, user, dam="CRD"):
    return c.get(f"/api/alarms?dam_id={dam}", user=user).json()["alarms"]


def _at_level(c, level, user=CDSE, dam="CRD"):
    return next(a for a in _events(c, user, dam) if a["level"] == level and a["state"] == "open")


def _chain(c, dam="CRD"):
    return c.tc.app.state.store.chain_rows(dam)


def _acks(c, dam="CRD"):
    return [e for e in c.get(f"/api/admin/audit?dam_id={dam}").json()["entries"] if e["action"] == "alarm_ack"]


def test_role_level_mapping_has_no_cdse_for_any_app_role():
    assert ac.ROLE_LEVEL == {"engineer": "engineer", "dso": "engineer", "dam_admin": "engineer", "admin": "engineer"}
    assert ac.CDSE_LEVEL == "CDSE" and "CDSE" not in ac.ROLE_LEVEL.values()
    assert alarms_router.CDSE_ACK_MIN_LEVEL == 2


@pytest.mark.parametrize("level", sorted(LEVELS), ids=LEVELS.get)
@pytest.mark.parametrize("user", [VIEWER, ENG, DSO, DAMADMIN, ADMIN], ids=["viewer", "engineer", "dso", "dam_admin", "admin"])
def test_non_cdse_role_cdse_ack_is_403_and_writes_nothing(crd, user, level):
    ev = _at_level(crd, level)
    seen = next(a for a in _events(crd, user) if a["event_id"] == ev["event_id"])
    assert seen["requires_cdse"] and not seen["is_cdse"] and not seen["can_ack"]      # UI hides the control
    assert crd.get(f"/api/alarms/{ev['event_id']}", user=user).json()["can_ack"] is False
    r = crd.post(f"/api/alarms/{ev['event_id']}/ack", user=user, json={"rationale": "I reviewed it"})
    assert r.status_code == 403, r.text
    assert "CDSE" in r.json()["detail"]
    assert _chain(crd) == [] and crd.tc.app.state.store.chain_ids() == []
    assert crd.tc.app.state.store.ack_states([ev["event_id"]]).get(ev["event_id"]) is None
    assert _acks(crd) == []
    assert _at_level(crd, level)["event_id"] == ev["event_id"]                       # still open


def test_admin_cdse_ack_refused_on_high_and_critical_with_no_chain_record(crd):
    """The headline case: a non-CDSE platform admin cannot perform the CDSE acknowledgement on HIGH or CRITICAL."""
    for level in (2, 3):
        eid = _at_level(crd, level)["event_id"]
        assert crd.post(f"/api/alarms/{eid}/ack", user=ADMIN, json={"rationale": "admin override"}).status_code == 403
    assert _chain(crd) == [] and _acks(crd) == []
    v = crd.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()
    assert v["ok"] is True and v["records"] == 0


@pytest.mark.parametrize("level", sorted(LEVELS), ids=LEVELS.get)
def test_cdse_user_acks_high_and_critical_with_cdse_level(crd, level):
    ev = _at_level(crd, level)
    assert ev["requires_cdse"] and ev["is_cdse"] and ev["can_ack"]
    r = crd.post(f"/api/alarms/{ev['event_id']}/ack", user=CDSE, json={"rationale": "CDSE reviewed; EAP not triggered"})
    assert r.status_code == 200 and r.json()["state"] == "acknowledged"
    (row,) = _chain(crd)
    assert (row["action_type"], row["review_level"], row["reviewer_email"], row["reviewer_id"]) == \
        ("alarm_ack", "CDSE", CDSE, f"uid-{CDSE}")
    assert json.loads(row["original_value"])["level"] == level
    # close stays dso+ (a dam power, not a review elevation): the dso's close is recorded at engineer level
    r = crd.post(f"/api/alarms/{ev['event_id']}/close", user=DSO, json={"reason": "field check OK"})
    assert r.status_code == 200
    assert [x["review_level"] for x in _chain(crd)] == ["CDSE", "engineer"]
    assert crd.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()["ok"]


def test_level1_ack_stays_engineer_plus_without_elevation(crd):
    """Unflagged level-1 rules: engineer+ (dam_admin on CRD, platform admin on HLD), recorded at engineer level."""
    crd_l1 = [a["event_id"] for a in _events(crd, ENG) if a["level"] == 1 and a["state"] == "open"
              and not a["requires_cdse"]]
    hld_l1 = [a["event_id"] for a in _events(crd, ADMIN, "HLD") if a["level"] == 1 and not a["requires_cdse"]]
    assert crd_l1 and hld_l1
    assert crd.post(f"/api/alarms/{crd_l1[0]}/ack", user=VIEWER, json={"rationale": "x"}).status_code == 403
    for eid, user, dam in ((crd_l1[0], DAMADMIN, "CRD"), (hld_l1[0], ADMIN, "HLD")):
        assert next(a for a in _events(crd, user, dam) if a["event_id"] == eid)["can_ack"]
        assert crd.post(f"/api/alarms/{eid}/ack", user=user, json={"rationale": "verified"}).status_code == 200
    assert [r["review_level"] for r in _chain(crd)] == ["engineer"]
    assert [r["review_level"] for r in _chain(crd, "HLD")] == ["engineer"]


def test_cdse_of_dam_a_is_refused_on_dam_b(crd):
    """cdse.crd is CRD's CDSE and an engineer on HLD: on an HLD HIGH/CRITICAL alarm they are not the CDSE."""
    an = crd.tc.app.state.analytics_factory(None)
    tmpl = next(e for e in an.d["alarm_events"] if e["event_id"] == _at_level(crd, 3)["event_id"])
    for lvl in (2, 3):
        an.d["alarm_events"].append(dict(tmpl, event_id=f"EV-HLD-L{lvl}", dam_id="HLD", instrument_id="HLD-IN-1",
                                         sensor_id="HLD-IN-1", level=lvl))
    for lvl in (2, 3):
        seen = next(a for a in _events(crd, CDSE, "HLD") if a["event_id"] == f"EV-HLD-L{lvl}")
        assert not seen["is_cdse"] and not seen["can_ack"]
        r = crd.post(f"/api/alarms/EV-HLD-L{lvl}/ack", user=CDSE, json={"rationale": "I am CDSE (of CRD)"})
        assert r.status_code == 403 and "HLD" in r.json()["detail"]
    assert _chain(crd, "HLD") == [] and crd.tc.app.state.store.chain_ids() == []
    # and their own HLD level-1 engineer work is recorded at engineer level, not CDSE
    l1 = next(a for a in _events(crd, CDSE, "HLD") if a["level"] == 1)["event_id"]
    assert crd.post(f"/api/alarms/{l1}/ack", user=CDSE, json={"rationale": "verified"}).status_code == 200
    assert [r["review_level"] for r in _chain(crd, "HLD")] == ["engineer"]


def test_cdse_membership_grant_exact_dam_active_and_person_only(crd):
    an = crd.tc.app.state.analytics_factory(None)
    eid = _at_level(crd, 3)["event_id"]
    mem = an.d.setdefault("project_membership", [])
    # '*' (all dams), a revoked grant, another dam's grant and a group name do not make ENG the CDSE of CRD
    mem += [dict(principal=ENG, dam_id="*", role="CDSE", revoked_ts=None),
            dict(principal=ENG, dam_id="CRD", role="CDSE", revoked_ts="2026-01-01T00:00:00Z"),
            dict(principal=ENG, dam_id="HLD", role="CDSE", revoked_ts=None),
            dict(principal="dam-safety-engineers", dam_id="CRD", role="CDSE", revoked_ts=None),
            dict(principal=ENG, dam_id="CRD", role="ENGINEER", revoked_ts=None)]
    assert crd.post(f"/api/alarms/{eid}/ack", user=ENG, json={"rationale": "x"}).status_code == 403
    assert _chain(crd) == []
    mem.append(dict(principal=ENG.upper(), dam_id="CRD", role="cdse", revoked_ts=None))
    assert crd.post(f"/api/alarms/{eid}/ack", user=ENG, json={"rationale": "acting CDSE"}).status_code == 200
    assert [r["review_level"] for r in _chain(crd)] == ["CDSE"]


def test_cdse_lookup_fails_closed(crd, monkeypatch):
    an = crd.tc.app.state.analytics_factory(None)
    eid = _at_level(crd, 2)["event_id"]

    def boom(dam_id):
        raise RuntimeError("ref.dam unavailable")
    monkeypatch.setattr(an, "cdse_principals", boom, raising=False)
    assert sg.cdse_principals(an, "CRD") == frozenset()
    assert crd.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "x"}).status_code == 403
    assert sg.cdse_principals(object(), "CRD") == frozenset()          # source without the lookup
    assert _chain(crd) == []


def test_cdse_review_level_on_instrument_reviews(crd):
    """Corrections/bias reviews by the dam's CDSE carry CDSE; an admin's carry engineer (no elevation)."""
    from test_audit_chain import IID, _rec
    sid = crd.get(f"/api/instruments/{IID}/incl/surveys").json()["surveys"][-1]["survey_id"]
    an = crd.tc.app.state.analytics_factory(None)
    an.d.setdefault("incl_corrections", []).extend([
        _rec("D1", sid, correction_type="depth", detection_method="engineer_judgement"),
        _rec("D2", sid, correction_type="depth", detection_method="engineer_judgement")])
    url = f"/api/instruments/{IID}/incl/surveys/{sid}/corrections/%s/review"
    assert crd.post(url % "D1", user=ADMIN, json={"decision": "approve", "rationale": "ok"}).status_code == 200
    assert crd.post(url % "D2", user=CDSE, json={"decision": "reject", "rationale": "no"}).status_code == 200
    assert [r["review_level"] for r in _chain(crd)] == ["engineer", "CDSE"]


def test_admin_powers_unchanged(crd):
    """dam_admin / platform admin keep user and role administration; only review elevation was removed."""
    assert crd.put("/api/admin/roles", user=DAMADMIN, json={"email": ENG, "dam_id": "CRD", "role": "dso"}).status_code == 200
    assert crd.put("/api/admin/roles", user=DAMADMIN, json={"email": ENG, "dam_id": "BFK", "role": "dso"}).status_code == 403
    assert crd.post("/api/admin/users", user=ADMIN, json={"email": "new@demo.local", "display_name": "N",
                                                        "is_admin": False}).status_code == 200
    assert crd.put("/api/admin/roles", user=ADMIN, json={"email": ENG, "dam_id": "CRD", "role": "engineer"}).status_code == 200


def test_pre_change_chain_still_verifies_and_only_new_records_change(crd, monkeypatch):
    """Records written under the earlier proposal (dso/dam_admin/admin -> CDSE) keep their stored review_level and
    the chain still verifies (hash recomputed from stored content); new records use the confirmed mapping."""
    old_map = {"engineer": "engineer", "dso": "CDSE", "dam_admin": "CDSE", "admin": "CDSE"}
    monkeypatch.setattr(sg, "review_level", lambda c, dam_id, an=None: old_map["admin" if c.is_admin else c.role(dam_id)])
    monkeypatch.setattr(alarms_router, "needs_cdse_ack", lambda ev, flagged=None: False)          # pre-change: engineer+ acks
    l1 = [a["event_id"] for a in _events(crd, ENG) if a["level"] == 1 and not a["requires_cdse"]]
    hi = _at_level(crd, 2)["event_id"]
    for eid, user in ((l1[0], ADMIN), (hi, DAMADMIN)):
        assert crd.post(f"/api/alarms/{eid}/ack", user=user, json={"rationale": "pre-change ack"}).status_code == 200
    assert crd.post(f"/api/alarms/{l1[0]}/close", user=DSO, json={"reason": "pre-change close"}).status_code == 200
    old = _chain(crd)
    assert [(r["reviewer_email"], r["review_level"]) for r in old] == [(ADMIN, "CDSE"), (DAMADMIN, "CDSE"), (DSO, "CDSE")]
    monkeypatch.undo()                                                                # the v1.6 server
    v = crd.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()
    assert v["ok"] and v["records"] == 3
    crit = _at_level(crd, 3)["event_id"]
    assert crd.post(f"/api/alarms/{crit}/ack", user=ADMIN, json={"rationale": "post-change"}).status_code == 403
    assert crd.post(f"/api/alarms/{crit}/ack", user=CDSE, json={"rationale": "post-change"}).status_code == 200
    assert crd.post(f"/api/alarms/{hi}/close", user=DAMADMIN, json={"reason": "post-change"}).status_code == 200
    rows = _chain(crd)
    assert [r["review_level"] for r in rows] == ["CDSE", "CDSE", "CDSE", "CDSE", "engineer"]
    assert [r["record_hash"] for r in rows[:3]] == [r["record_hash"] for r in old]      # old records untouched
    v = crd.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()
    assert v["ok"] and v["records"] == 5
    assert ac.verify_chain(rows, crd.tc.app.state.store.chain_head("CRD"))["ok"]


# ------------------------------------------------------------------ live lookup: ref.v_dam_reviewer_authority only
class _FakeSql:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.sql = rows or [], error, []

    def __call__(self, statement, params=None):
        self.sql.append((statement, params))
        if self.error:
            raise self.error
        return self.rows


def _sql_analytics(fake, monkeypatch):
    from backend.config import Settings
    from backend.repos import analytics as an_mod
    a = an_mod.SqlAnalytics(Settings(demo_mode=False, warehouse_id="w", catalog="dam_prod"), "tok")
    monkeypatch.setattr(a, "q", fake)
    monkeypatch.setattr(an_mod.SqlAnalytics, "_authority_missing_logged", False)
    return a


def test_live_cdse_lookup_reads_only_the_reviewer_authority_view(monkeypatch):
    fake = _FakeSql(rows=[{"principal": CDSE}, {"principal": "dam-safety-engineers"}, {"principal": None}])
    a = _sql_analytics(fake, monkeypatch)
    assert a.cdse_principals("CRD") == [CDSE, "dam-safety-engineers"]
    (stmt, params), = fake.sql
    assert "v_dam_reviewer_authority" in stmt and params == {"dam_id": "CRD"}
    low = stmt.lower()
    assert "ref`.`dam`" not in low and ".dam " not in low and "project_membership" not in low and "ref.dam " not in low
    assert "revoked_ts is null or revoked_ts > current_timestamp()" in low
    assert "granted_ts is null or granted_ts <= current_timestamp()" in low
    # a group principal never matches a person
    class C:
        user_id, email = f"uid-{ENG}", ENG
    assert sg.is_cdse(C, "CRD", a) is False


def test_live_cdse_lookup_fails_closed_and_logs_once_when_the_view_is_missing(monkeypatch, caplog):
    import logging
    err = RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] The table or view `dam_prod`.`ref`.`v_dam_reviewer_authority` "
                       "cannot be found.")
    a = _sql_analytics(_FakeSql(error=err), monkeypatch)
    caplog.set_level(logging.WARNING)
    assert [a.cdse_principals("CRD") for _ in range(3)] == [[], [], []]

    class C:
        user_id, email = f"uid-{CDSE}", CDSE
    assert sg.is_cdse(C, "CRD", a) is False
    assert len([r for r in caplog.records if "v_dam_reviewer_authority is missing" in r.getMessage()]) == 1


def test_live_cdse_lookup_other_errors_fail_closed_in_signoffs(monkeypatch):
    a = _sql_analytics(_FakeSql(error=RuntimeError("warehouse timeout")), monkeypatch)
    with pytest.raises(RuntimeError):
        a.cdse_principals("CRD")

    class C:
        user_id, email = f"uid-{CDSE}", CDSE
    assert sg.is_cdse(C, "CRD", a) is False and sg.review_level.__name__ == "review_level"


def test_live_designated_cdse_reads_one_value_from_the_authority_view(monkeypatch):
    fake = _FakeSql(rows=[{"cdse_id": CDSE}, {"cdse_id": CDSE.upper()}])
    a = _sql_analytics(fake, monkeypatch)
    assert a.designated_cdse("CRD") == CDSE
    (stmt, params), = fake.sql
    low = stmt.lower()
    assert "v_dam_reviewer_authority" in low and params == {"dam_id": "CRD"} and "membership_cdse" not in low
    assert "'dam_cdse_assignment', 'dam_cdse_id'" in low and "ref`.`dam`" not in low
    assert _sql_analytics(_FakeSql(rows=[]), monkeypatch).designated_cdse("CRD") is None
    with pytest.raises(RuntimeError, match="designated CDSE"):              # ambiguous -> callers fail closed
        _sql_analytics(_FakeSql(rows=[{"cdse_id": "a@x"}, {"cdse_id": "b@x"}]), monkeypatch).designated_cdse("CRD")


def test_live_go_live_readiness_reads_the_da_view(monkeypatch):
    row = {"dam_id": "CRD", "ready": False, "has_current_dssmp": True,
           "blocking_reason": "no_cdse_in_reviewer_authority"}
    fake = _FakeSql(rows=[row])
    a = _sql_analytics(fake, monkeypatch)
    assert a.go_live_readiness("CRD") == row
    (stmt, params), = fake.sql
    assert "v_dam_go_live_readiness" in stmt and "blocking_reason" in stmt and params == {"dam_id": "CRD"}
    assert _sql_analytics(_FakeSql(rows=[]), monkeypatch).go_live_readiness("CRD") is None
    with pytest.raises(RuntimeError):
        _sql_analytics(_FakeSql(error=RuntimeError("down")), monkeypatch).go_live_readiness("CRD")


def test_live_dssmp_affected_rules_calls_the_da_function(monkeypatch):
    fake = _FakeSql(rows=[{"threshold_id": "T1", "threshold_version": 1, "dssmp_version_id": None,
                           "cdse_ack_required": False}])
    a = _sql_analytics(fake, monkeypatch)
    assert a.dssmp_affected_rules("CRD", "D2")[0]["threshold_id"] == "T1"
    (stmt, params), = fake.sql
    assert "dssmp_affected_rules`(:dam_id, :to)" in stmt and params == {"dam_id": "CRD", "to": "D2"}
