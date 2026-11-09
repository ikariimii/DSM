"""QA-048+: live-dam cdse_ack_required edit control (independent of app/tests/test_alarm_rule_flag_edits.py).

Derived from app/backend/routers/alarm_rules.py + FERC Ch.14 App K / SCHEMA_CONTRACT v1.6 §5a:
admins propose; only the dam's CDSE applies/approves; rationale required; one chained record per action;
apply appends threshold version N+1 (never UPDATE/DELETE prior rows); stale → 409; open-episode snapshot OR.
"""
from __future__ import annotations

import copy
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from backend.repos.state import StateStore  # noqa: E402

ADMIN = "admin@demo.local"
ENG = "engineer.crd@demo.local"
DAM_ADMIN = "damadmin.crd@demo.local"
CDSE_CRD = "cdse.crd@demo.local"
# HLD has its own CDSE principal with no login; cdse.crd is only engineer there
RULE = "CRD-IN-1-T2"  # unflagged L2 rule on CRD (T1 is already flagged in seed)


def _h(email, user_id=None):
    return {"x-forwarded-user": user_id if user_id is not None else f"uid-{email.lower()}",
            "x-forwarded-email": email}


def _snap_tables(st, dam="CRD"):
    return {
        "proposals": st.execute(
            "SELECT proposal_id, status, base_threshold_version, new_flag, decided_by "
            "FROM {s}alarm_rule_flag_proposal WHERE dam_id = %s ORDER BY proposal_id", (dam,)),
        "pubs": st.execute(
            "SELECT pub_id, status, kind FROM {s}ref_change_publication WHERE dam_id = %s ORDER BY pub_id", (dam,)),
        "signoff": st.execute(
            "SELECT review_id, action_type, decision, review_level, subject_id, chain_seq, record_hash "
            "FROM {s}review_signoff WHERE dam_id = %s ORDER BY chain_seq, review_id", (dam,)),
        "head": st.execute("SELECT * FROM {s}review_chain_head WHERE chain_id = %s", (dam,)),
        "audit": st.execute(
            "SELECT actor, action, target FROM {s}audit_log WHERE dam_id = %s ORDER BY id", (dam,)),
    }


def _threshold_rows(an, tid):
    """Every stored version row for a threshold_id (including superseded), raw from the demo dataset."""
    return [copy.deepcopy(t) for t in an.d["thresholds"] if t["threshold_id"] == tid]


@pytest.fixture()
def demo(tmp_path):
    app = app_main.create_app(Settings(demo_mode=True, static_dir=str(tmp_path / "nodist")))
    st = app.state.store
    st.upsert_user(DAM_ADMIN, "QA Dam Admin CRD", False, "qa048")
    st.set_role(DAM_ADMIN, "CRD", "dam_admin", "qa048")
    if st.get_user(CDSE_CRD) is None:
        st.upsert_user(CDSE_CRD, "Chris CDSE", False, "qa048")
        st.set_role(CDSE_CRD, "CRD", "engineer", "qa048")
        st.set_role(CDSE_CRD, "HLD", "engineer", "qa048")
    tc = TestClient(app)
    yield tc, app
    st._conn.close()


def _an(app):
    return app.state.analytics_factory(None)


def _propose(tc, email, tid=RULE, flag=True, rationale="QA propose", dam="CRD"):
    return tc.post(f"/api/dams/{dam}/alarm-rules/{tid}/cdse-flag/proposals",
                   headers=_h(email), json={"cdse_ack_required": flag, "rationale": rationale})


def _approve(tc, email, pid, dam="CRD", rationale="QA approve"):
    return tc.post(f"/api/dams/{dam}/alarm-rules/proposals/{pid}/approve",
                   headers=_h(email), json={"rationale": rationale})


def _reject(tc, email, pid, dam="CRD", rationale="QA reject"):
    return tc.post(f"/api/dams/{dam}/alarm-rules/proposals/{pid}/reject",
                   headers=_h(email), json={"rationale": rationale})


def _direct(tc, email, tid=RULE, flag=True, rationale="QA apply", dam="CRD"):
    return tc.post(f"/api/dams/{dam}/alarm-rules/{tid}/cdse-flag",
                   headers=_h(email), json={"cdse_ack_required": flag, "rationale": rationale})


# --------------------------------------------------------------------------- authorization / 403 writes nothing
@pytest.mark.parametrize("email", [ENG, DAM_ADMIN, ADMIN])
def test_non_cdse_cannot_apply_direct_and_writes_nothing(demo, email):
    tc, app = demo
    st = app.state.store
    before = _snap_tables(st)
    rows_before = _threshold_rows(_an(app), RULE)
    r = _direct(tc, email, flag=True)
    assert r.status_code == 403, (email, r.text)
    assert _snap_tables(st) == before
    assert _threshold_rows(_an(app), RULE) == rows_before


@pytest.mark.parametrize("email", [ENG])
def test_engineer_cannot_propose(demo, email):
    tc, app = demo
    st = app.state.store
    before = _snap_tables(st)
    r = _propose(tc, email, flag=True)
    assert r.status_code == 403, r.text
    assert _snap_tables(st) == before


def test_admin_and_dam_admin_may_propose_but_not_approve(demo):
    tc, app = demo
    st = app.state.store
    for email in (ADMIN, DAM_ADMIN):
        tid = RULE
        # ensure flag is false so propose to true works
        an = _an(app)
        cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == tid)
        if cur.get("cdse_ack_required"):
            assert _direct(tc, CDSE_CRD, tid=tid, flag=False, rationale="reset").status_code == 200
        r = _propose(tc, email, tid=tid, flag=True, rationale=f"propose by {email}")
        assert r.status_code == 200, (email, r.text)
        pid = r.json()["proposal_id"]
        before = _snap_tables(st)
        rows_before = _threshold_rows(an, tid)
        a = _approve(tc, email, pid)
        assert a.status_code == 403, (email, a.text)
        assert _snap_tables(st) == before
        assert _threshold_rows(_an(app), tid) == rows_before
        # clean: CDSE reject so next iteration can propose again
        assert _reject(tc, CDSE_CRD, pid, rationale="cleanup").status_code == 200


def test_cross_dam_cdse_cannot_approve_or_apply_on_other_dam(demo):
    """cdse.crd is CDSE of CRD only; HLD's CDSE is cdse.hld@demo.local (no login)."""
    tc, app = demo
    st = app.state.store
    an = _an(app)
    # pick an HLD rule
    hld_rules = an.thresholds("HLD")
    assert hld_rules, "seed must have HLD thresholds"
    tid = hld_rules[0]["threshold_id"]
    # grant dam_admin on HLD so we can propose as admin path, then CDSE-of-CRD tries to approve
    st.set_role(DAM_ADMIN, "HLD", "dam_admin", "qa048")
    flag = not bool(hld_rules[0].get("cdse_ack_required"))
    p = _propose(tc, DAM_ADMIN, tid=tid, flag=flag, dam="HLD", rationale="HLD propose")
    assert p.status_code == 200, p.text
    pid = p.json()["proposal_id"]
    before = _snap_tables(st, "HLD")
    rows_before = _threshold_rows(an, tid)
    assert _approve(tc, CDSE_CRD, pid, dam="HLD").status_code == 403
    assert _direct(tc, CDSE_CRD, tid=tid, flag=flag, dam="HLD").status_code == 403
    assert _snap_tables(st, "HLD") == before
    assert _threshold_rows(_an(app), tid) == rows_before


# --------------------------------------------------------------------------- SoD: CDSE self-approval
@pytest.mark.xfail(strict=True, reason="QA-048: SoD gap — alarm_rules.approve allows the dam's CDSE to approve "
                                       "their own proposal (Genie-accepted in 6f7a1d5); FERC two-person intent "
                                       "would refuse self-approval")
def test_cdse_cannot_approve_own_proposal_sod(demo):
    tc, app = demo
    # reset RULE to unflagged
    an = _an(app)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    if cur.get("cdse_ack_required"):
        assert _direct(tc, CDSE_CRD, flag=False, rationale="reset").status_code == 200
    p = _propose(tc, CDSE_CRD, flag=True, rationale="CDSE proposes")
    assert p.status_code == 200, p.text
    pid = p.json()["proposal_id"]
    r = _approve(tc, CDSE_CRD, pid, rationale="CDSE self-approve")
    assert r.status_code == 403, r.text  # expected under two-person SoD; today returns 200


def test_admin_propose_then_approve_is_403(demo):
    tc, app = demo
    an = _an(app)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    if cur.get("cdse_ack_required"):
        assert _direct(tc, CDSE_CRD, flag=False, rationale="reset").status_code == 200
    p = _propose(tc, ADMIN, flag=True, rationale="admin proposes")
    assert p.status_code == 200, p.text
    before = _snap_tables(app.state.store)
    r = _approve(tc, ADMIN, p.json()["proposal_id"])
    assert r.status_code == 403
    assert _snap_tables(app.state.store) == before


# --------------------------------------------------------------------------- rationale 422
@pytest.mark.parametrize("rationale", [None, "", "   ", "\t"])
def test_missing_rationale_422_no_writes(demo, rationale):
    tc, app = demo
    st = app.state.store
    before = _snap_tables(st)
    rows_before = _threshold_rows(_an(app), RULE)
    body = {"cdse_ack_required": True}
    if rationale is not None:
        body["rationale"] = rationale
    r = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag/proposals", headers=_h(ADMIN), json=body)
    assert r.status_code == 422, r.text
    assert _snap_tables(st) == before
    assert _threshold_rows(_an(app), RULE) == rows_before


# --------------------------------------------------------------------------- apply: N+1, append-only, one chain record, verify
def test_apply_appends_n_plus_1_never_updates_prior_rows_one_chain_record(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    # start from known unflagged
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    if cur.get("cdse_ack_required"):
        assert _direct(tc, CDSE_CRD, flag=False, rationale="reset").status_code == 200
        cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    n = int(cur.get("threshold_version") or 1)
    prior_rows = _threshold_rows(an, RULE)
    prior_frozen = copy.deepcopy(prior_rows)
    before_chain = len(st.execute("SELECT * FROM {s}review_signoff WHERE dam_id = %s", ("CRD",)))
    r = _direct(tc, CDSE_CRD, flag=True, rationale="CDSE direct apply")
    assert r.status_code == 200, r.text
    assert r.json()["new_threshold_version"] == n + 1
    after_rows = _threshold_rows(an, RULE)
    # every prior row byte-identical (append-only); plus exactly one new version
    assert after_rows[:-1] == prior_frozen or all(
        any(a == p for a in after_rows) for p in prior_frozen)
    for p in prior_frozen:
        assert p in after_rows, "prior threshold row was mutated or removed"
    assert any(int(t.get("threshold_version") or 1) == n + 1 and t.get("cdse_ack_required") is True
               for t in after_rows)
    # contiguous latest
    assert int(next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)["threshold_version"]) == n + 1
    signoffs = st.execute(
        "SELECT action_type, decision, review_level FROM {s}review_signoff WHERE dam_id = %s ORDER BY chain_seq",
        ("CRD",))
    assert len(signoffs) == before_chain + 1
    assert signoffs[-1]["action_type"] == "alarm_rule_flag_apply"
    assert signoffs[-1]["review_level"] == "CDSE"
    v = tc.get("/api/dams/CRD/audit/signoffs/verify", headers=_h(ENG))
    assert v.status_code == 200 and v.json()["ok"] is True, v.text
    assert ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"))["ok"] is True


def test_propose_then_cdse_approve_one_record_each_and_n_plus_1(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    if cur.get("cdse_ack_required"):
        assert _direct(tc, CDSE_CRD, flag=False, rationale="reset").status_code == 200
        cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    n = int(cur.get("threshold_version") or 1)
    prior = copy.deepcopy(_threshold_rows(an, RULE))
    p = _propose(tc, DAM_ADMIN, flag=True, rationale="dam_admin proposes")
    assert p.status_code == 200, p.text
    pid = p.json()["proposal_id"]
    mid = len(st.execute("SELECT * FROM {s}review_signoff WHERE dam_id=%s", ("CRD",)))
    a = _approve(tc, CDSE_CRD, pid, rationale="CDSE approves admin proposal")
    assert a.status_code == 200, a.text
    assert a.json()["new_threshold_version"] == n + 1
    after = _threshold_rows(an, RULE)
    for row in prior:
        assert row in after
    actions = [r["action_type"] for r in st.execute(
        "SELECT action_type FROM {s}review_signoff WHERE dam_id=%s ORDER BY chain_seq", ("CRD",))]
    assert actions[-2:] == ["alarm_rule_flag_propose", "alarm_rule_flag_apply"]
    assert len(actions) >= mid + 1
    assert ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"))["ok"]


# --------------------------------------------------------------------------- stale proposal 409
def test_stale_proposal_409_no_write_version_unchanged(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    if cur.get("cdse_ack_required"):
        assert _direct(tc, CDSE_CRD, flag=False, rationale="reset").status_code == 200
    p = _propose(tc, ADMIN, flag=True, rationale="will go stale")
    assert p.status_code == 200, p.text
    pid = p.json()["proposal_id"]
    # intervening direct apply moves the version
    assert _direct(tc, CDSE_CRD, flag=True, rationale="intervening").status_code == 200
    n_after = int(next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)["threshold_version"])
    before = _snap_tables(st)
    rows_before = copy.deepcopy(_threshold_rows(an, RULE))
    r = _approve(tc, CDSE_CRD, pid, rationale="approve stale")
    assert r.status_code == 409, r.text
    assert "stale" in r.text.lower() or "changed" in r.text.lower()
    assert _snap_tables(st) == before
    assert _threshold_rows(an, RULE) == rows_before
    assert int(next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)["threshold_version"]) == n_after


# --------------------------------------------------------------------------- open-episode protection
def test_clearing_flag_does_not_alter_open_episode_snapshot(demo):
    tc, app = demo
    an = _an(app)
    # Find an open CRD episode that was snapshotted with the flag true (seed: CRD-IN-1-T1 flagged)
    eps = [e for e in an.d["alarm_events"]
           if e["dam_id"] == "CRD" and e.get("cdse_ack_required_at_open")]
    assert eps, "seed should have at least one episode with snapshot flag"
    snap_before = copy.deepcopy(eps[0]["cdse_ack_required_at_open"])
    tid = "CRD-IN-1-T1"
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == tid)
    if not cur.get("cdse_ack_required"):
        assert _direct(tc, CDSE_CRD, tid=tid, flag=True, rationale="ensure flagged").status_code == 200
    # clear the current rule flag
    assert _direct(tc, CDSE_CRD, tid=tid, flag=False, rationale="clear flag").status_code == 200
    # episode snapshot unchanged
    eps_after = [e for e in an.d["alarm_events"] if e["event_id"] == eps[0]["event_id"]][0]
    assert eps_after["cdse_ack_required_at_open"] == snap_before
    # ack rule still ORs snapshot: level-1 flagged episode still requires CDSE
    from backend.routers import alarms as al
    assert al.rule_flag(an, eps_after) is True


# --------------------------------------------------------------------------- demo case sensitivity (QA-046 adjacent)
def test_cdse_email_casefold_can_apply(demo):
    tc, app = demo
    an = _an(app)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    target = not bool(cur.get("cdse_ack_required"))
    r = _direct(tc, "CDSE.CRD@DEMO.LOCAL", flag=target, rationale="casefold apply")
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- concurrent double-approve (sqlite serialization; live PG separately)
def test_double_approve_race_only_one_succeeds(demo):
    tc, app = demo
    st = app.state.store
    an = _an(app)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    if cur.get("cdse_ack_required"):
        assert _direct(tc, CDSE_CRD, flag=False, rationale="reset").status_code == 200
    p = _propose(tc, ADMIN, flag=True, rationale="race propose")
    pid = p.json()["proposal_id"]
    results = []

    def one():
        # each thread needs its own client hitting the same app/store
        c = TestClient(app)
        results.append(_approve(c, CDSE_CRD, pid, rationale="race approve").status_code)

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1, results
    assert results.count(409) == 3, results
    props = st.flag_proposal(pid)
    assert props["status"] == "applied"
    # only one apply chain record for this proposal
    applies = st.execute(
        "SELECT * FROM {s}review_signoff WHERE dam_id=%s AND action_type=%s AND corrected_value LIKE %s",
        ("CRD", "alarm_rule_flag_apply", f"%{pid}%"))
    assert len(applies) == 1


# --------------------------------------------------------------------------- live PG 17: race + append-only triggers
import os
import uuid as _uuid

_PG = os.environ.get("DAM_TEST_PG")
_pg_mark = pytest.mark.skipif(not _PG, reason="set DAM_TEST_PG to run against live PostgreSQL")


def _pg_store():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    schema = f"qa048_{_uuid.uuid4().hex[:10]}"
    pool = ConnectionPool(_PG, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
    st = StateStore("postgres", schema=schema, pg_pool=pool, retries=1)
    return st, pool, schema, psycopg


def _pg_drop(pool, schema):
    with pool.connection() as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    pool.close()


def _flag_row(n, action="alarm_rule_flag_apply", decision="apply"):
    return {
        "review_id": f"RS-{n}-{_uuid.uuid4().hex[:8]}", "dam_id": "CRD", "subject_type": "alarm_rule",
        "subject_id": RULE, "review_level": "CDSE", "decision": decision, "comment": f"live-pg-{n}",
        "reviewer_id": "uid-cdse", "reviewer_pe_license": None,
        "signed_ts": "2026-11-09T00:00:00.000000+00:00", "signature_hash": "a" * 64,
        "action_type": action, "reviewer_email": CDSE_CRD, "reviewer_sub": None,
        "original_value": "{}", "corrected_value": "{}",
    }


@_pg_mark
def test_live_pg_append_only_triggers_refuse_update_delete_truncate_on_flag_chain_rows():
    """After writing an alarm_rule_flag_* chain row, PG 17 append-only triggers still refuse mutation."""
    st, pool, schema, psycopg = _pg_store()
    try:
        st.append_chained("CRD", ac.GENESIS, ac.finalizer(_flag_row(0)))
        before = st.execute("SELECT * FROM {s}review_signoff ORDER BY chain_seq")
        assert len(before) == 1 and before[0]["action_type"] == "alarm_rule_flag_apply"
        for stmt in ("UPDATE {s}review_signoff SET comment = 'tampered'",
                     "DELETE FROM {s}review_signoff",
                     "TRUNCATE {s}review_signoff"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                st.execute(stmt)
        assert st.execute("SELECT * FROM {s}review_signoff ORDER BY chain_seq") == before
        assert ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"))["ok"]
        from backend.repos.state import APPEND_ONLY
        for t in APPEND_ONLY:
            rows = st.execute(
                "SELECT tgname, tgtype FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = %s::regclass "
                "AND tgname LIKE 't_app_%%' ORDER BY tgname", (f'"{st.schema}".{t}',))
            names = [r["tgname"] for r in rows]
            assert names == sorted([f"t_app_noupd_{t}", f"t_app_notrunc_{t}"])
            trunc = next(r["tgtype"] for r in rows if "notrunc" in r["tgname"])
            assert trunc & 32 and not trunc & 1
    finally:
        _pg_drop(pool, schema)


@_pg_mark
def test_live_pg_double_approve_race_only_one_applied():
    """Concurrent decide_flag_proposal under chain-head FOR UPDATE: exactly one applied, others ValueError/409."""
    st, pool, schema, _ = _pg_store()
    try:
        pid = f"RFP-{_uuid.uuid4().hex}"
        with st.transaction() as q:
            st.insert_flag_proposal(q, {
                "proposal_id": pid, "dam_id": "CRD", "threshold_id": RULE, "base_threshold_version": 1,
                "base_flag": False, "new_flag": True, "proposed_by": ADMIN, "proposed_by_id": "uid-admin",
                "proposed_at": "2026-11-09T00:00:00.000000+00:00", "rationale": "race seed",
                "status": "pending"})
        barrier = threading.Barrier(4)
        outcomes = []

        def race(i):
            barrier.wait()
            try:
                def also(q):
                    st.decide_flag_proposal(q, pid, "applied", CDSE_CRD, "uid-cdse",
                                            "2026-11-09T00:00:01.000000+00:00", f"race-{i}",
                                            pub_id=str(_uuid.uuid4()), new_version=2)
                st.append_chained("CRD", ac.GENESIS, ac.finalizer(_flag_row(i)), also)
                outcomes.append(200)
            except ValueError as e:
                outcomes.append(409 if "pending" in str(e) else repr(e))
            except Exception as e:  # noqa: BLE001
                outcomes.append(repr(e))

        threads = [threading.Thread(target=race, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count(200) == 1, outcomes
        assert outcomes.count(409) == 3, outcomes
        prop = st.flag_proposal(pid)
        assert prop["status"] == "applied" and prop["new_threshold_version"] == 2
        applies = st.execute(
            "SELECT * FROM {s}review_signoff WHERE action_type = %s ORDER BY chain_seq",
            ("alarm_rule_flag_apply",))
        assert len(applies) == 1
        assert ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"))["ok"]
    finally:
        _pg_drop(pool, schema)


# --------------------------------------------------------------------------- SCHEMA_CONTRACT §7.13 / QA-049 (closed on b3aeecd)
def test_uc_ddl_and_engine_accept_flag_chain_action_types():
    """§7.13 landed in v1_6_003 + ddl/08 + engine RULE_FLAG_ACTION_TYPES: extract/UC accept 6f7a1d5 flag rows;
    unknown action types still rejected."""
    import re
    import sqlite3

    from dam_safety.audit_chain import ACTION_TYPES as ENGINE_ACTIONS, RULE_FLAG_ACTION_TYPES

    root = Path(__file__).resolve().parents[1]
    # Prefer the widen migration; fall back to current ddl/08 (fresh installs)
    sources = [
        (root / "sql/migrations/v1_6_003_alarm_rule_flag.sql").read_text(),
        (root / "sql/ddl/08_audit.sql").read_text(),
    ]
    for a in RULE_FLAG_ACTION_TYPES:
        assert a in ENGINE_ACTIONS
    for src in sources:
        subj = re.search(r"ck_review_record_subject_decision CHECK \((.*?)\);", src, re.S).group(1)
        chain = re.search(r"ck_review_record_chain CHECK \((.*?)\);", src, re.S).group(1)
        assert "alarm_rule" in subj
        assert "'propose'" in subj and "'apply'" in subj and "'reject'" in subj
        for a in RULE_FLAG_ACTION_TYPES:
            assert a in chain
        db = sqlite3.connect(":memory:")
        # CHECKs (post-v1_7_003) may reference subject_id; include it so the SQLite render is faithful
        db.execute(
            f"CREATE TABLE t (subject_type TEXT, subject_id TEXT, decision TEXT, comment TEXT, action_type TEXT, "
            f"original_value TEXT, corrected_value TEXT, chain_id TEXT, dam_id TEXT, chain_seq INT, "
            f"prev_hash TEXT, record_hash TEXT, CHECK ({subj}), CHECK ({chain}))")
        for d, at in (("propose", "alarm_rule_flag_propose"), ("apply", "alarm_rule_flag_apply"),
                      ("reject", "alarm_rule_flag_reject")):
            db.execute("INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("alarm_rule", "T1", d, "why", at, "{}", "{}", "CRD", "CRD", 1, "0" * 64, "c" * 64))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("alarm_rule", "T1", "apply", "why", "alarm_rule_flag_delete", "{}", "{}", "CRD", "CRD", 1,
                        "0" * 64, "c" * 64))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("alarm_rule", "T1", "apply", "  ", "alarm_rule_flag_apply", "{}", "{}", "CRD", "CRD", 1,
                        "0" * 64, "c" * 64))
        db.close()
    # extract accepts a flag apply row end-to-end (required cols present → no raise)
    import lakebase_extract as lx
    row = {"review_id": "RS-FLAG-1", "dam_id": "CRD", "subject_type": "alarm_rule", "subject_id": "T1",
           "review_level": "CDSE", "decision": "apply", "comment": "why", "reviewer_id": "uid",
           "reviewer_pe_license": None, "signed_ts": "2026-11-09T00:00:00+00:00", "signature_hash": "a" * 64,
           "action_type": "alarm_rule_flag_apply", "reviewer_email": "cdse@x", "reviewer_sub": None,
           "original_value": "{}", "corrected_value": "{}", "chain_id": "CRD", "chain_seq": 1,
           "prev_hash": "0" * 64, "record_hash": "b" * 64, "signature": None, "sig_alg": None, "key_id": None}
    [rec] = lx.review_rows_to_records([row])
    assert rec["action_type"] == "alarm_rule_flag_apply" and rec["subject_type"] == "alarm_rule"
