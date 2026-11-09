"""QA-056 (live PostgreSQL, the app as the RESTRICTED login role on the DA's lakebase/001 split-ownership schema):
concurrent state-checked chained writes. Exactly one request wins (200); the loser gets 409 and writes NOTHING (no
chain record, no workflow row, no outbox row). Two layers, each tested on its own:
  1. per-dam pg_advisory_xact_lock(DAM_LOCK_NS, hashtext('dam_app.dam:'||dam)) taken first in every chained write,
     then the route's state checks re-run inside the transaction (then the chain head lock: order dam -> chain);
  2. unique partial indexes (belt and braces): app-created on app-owned tables, DA-created on owner-owned ones.
A barrier just before the dam lock makes both requests finish their pre-transaction reads before either writes, so
the in-transaction re-check (not the early read) decides. Demo loads are stubbed to 'pending' (the app role has no
UPDATE on the owner-owned outboxes, by design)."""
import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import ADMIN, CDSE, Client
from fastapi.testclient import TestClient
from test_lakebase_owner_live import LB001, _psql, _store, lakebase  # noqa: F401  (fixture)

from backend import seed
from backend.main import create_app
from backend.repos import state as state_mod
from backend.routers import dam_cdse

NEW_A, NEW_B = "cdse.race.a@demo.local", "cdse.race.b@demo.local"


@pytest.fixture()
def live(lakebase, demo_settings, monkeypatch):  # noqa: F811
    su, conn = lakebase
    st, pool = _store(conn)
    pool.resize(min_size=1, max_size=8)
    for email, name, is_admin in seed.SEED_USERS:
        st.upsert_user(email, name, is_admin, "seed")
    for email, dam, role in seed.SEED_ROLES:
        st.set_role(email, dam, role, "seed")
    app = create_app(dataclasses.replace(demo_settings))
    app.state.store = st
    an = app.state.analytics_factory(None)
    for fn in ("load_cdse_outbox", "load_dssmp_outbox"):
        monkeypatch.setattr(an, fn, lambda row: ("pending", None, None))
    try:
        yield {"c": Client(TestClient(app)), "st": st, "an": an, "su": su}
    finally:
        pool.close()


def _overlap(monkeypatch, st, n=2):
    """Both requests reach the transaction (pre-reads done) before either takes the dam lock."""
    barrier = threading.Barrier(n, timeout=20)
    real = st.lock_dam

    def lock_dam(q, dam_id):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        real(q, dam_id)
    monkeypatch.setattr(st, "lock_dam", lock_dam)


def _race(fns):
    with ThreadPoolExecutor(len(fns)) as ex:
        return list(ex.map(lambda f: f(), fns))


def _codes(rs):
    return sorted(r.status_code for r in rs)


def _assign(c, who):
    return lambda: c.post("/api/dams/NEW/cdse/assign", user=ADMIN, json={
        "cdse_id": who, "reason_code": "initial_appointment", "rationale": "race"})


# ------------------------------------------------------------------ 1. lock + in-transaction re-check
def test_concurrent_first_cdse_assign_one_200_one_409_nothing_written_by_the_loser(live, monkeypatch):
    c, st = live["c"], live["st"]
    _overlap(monkeypatch, st)
    rs = _race([_assign(c, NEW_A), _assign(c, NEW_B)])
    assert _codes(rs) == [200, 409], [r.text for r in rs]
    loser = next(r for r in rs if r.status_code == 409)
    assert loser.json()["detail"]["code"] == "cdse_assign_pending"
    assert [x["action_type"] for x in st.chain_rows("NEW")] == ["dam_cdse_assign"]
    assert len(st.rows("dam_cdse_change", "proposed_at", dam_id="NEW")) == 1
    (o,) = st.rows("dam_cdse_outbox", "outbox_id", dam_id="NEW")
    assert o["new_cdse_id"] == next(r for r in rs if r.status_code == 200).json()["new_cdse_id"]


def test_assign_race_decided_inside_the_transaction_even_without_the_early_check(live, monkeypatch):
    c, st = live["c"], live["st"]
    monkeypatch.setattr(dam_cdse, "_open_assignment", lambda st, dam_id: None)   # pre-transaction read says "free"
    _overlap(monkeypatch, st)
    rs = _race([_assign(c, NEW_A), _assign(c, NEW_B)])
    assert _codes(rs) == [200, 409]
    assert len(st.chain_rows("NEW")) == 1 and len(st.rows("dam_cdse_outbox", "outbox_id", dam_id="NEW")) == 1


def test_concurrent_cdse_change_proposals(live, monkeypatch):
    c, st = live["c"], live["st"]
    _overlap(monkeypatch, st)
    rs = _race([lambda w=w: c.post("/api/dams/CRD/cdse/changes", user=ADMIN, json={
        "cdse_id": w, "reason_code": "retirement", "rationale": "race"}) for w in (NEW_A, NEW_B)])
    assert _codes(rs) == [200, 409] and next(r for r in rs if r.status_code == 409).json()["detail"]["code"] == \
        "cdse_change_pending"
    assert [x["action_type"] for x in st.chain_rows("CRD")] == ["dam_cdse_change_propose"]
    assert len(st.rows("dam_cdse_change", "proposed_at", dam_id="CRD", status="pending")) == 1


def test_concurrent_cdse_change_approvals(live, monkeypatch):
    c, st = live["c"], live["st"]
    cid = c.post("/api/dams/CRD/cdse/changes", user=ADMIN, json={
        "cdse_id": NEW_A, "reason_code": "retirement", "rationale": "x"}).json()["change_id"]
    _overlap(monkeypatch, st)
    rs = _race([lambda: c.post(f"/api/dams/CRD/cdse/changes/{cid}/approve", user=CDSE, json={"rationale": "ok"})] * 2)
    assert _codes(rs) == [200, 409]
    assert [x["action_type"] for x in st.chain_rows("CRD")] == ["dam_cdse_change_propose", "dam_cdse_change_approve"]
    assert len(st.rows("dam_cdse_outbox", "outbox_id", dam_id="CRD")) == 1


def _dssmp(c, vid="DSSMP-CRD-RACE"):
    return lambda: c.post("/api/dams/CRD/dssmp-registrations/direct", user=CDSE, json={
        "dssmp_version_id": vid, "version_no": 2, "effective_from": "2026-11-01", "change_reason": "r",
        "source_ref": "s", "document_uri": "https://docs.example/d.pdf", "document_sha256": "ab" * 32,
        "rationale": "race"})


def test_concurrent_dssmp_direct_registrations_one_pending_outbox_row_per_version(live, monkeypatch):
    c, st = live["c"], live["st"]
    _overlap(monkeypatch, st)
    rs = _race([_dssmp(c), _dssmp(c)])
    assert _codes(rs) == [200, 409], [r.text for r in rs]
    assert next(r for r in rs if r.status_code == 409).json()["detail"]["code"] == "dssmp_registration_open"
    assert [x["action_type"] for x in st.chain_rows("CRD")] == ["dssmp_version_approve"]
    assert len(st.dssmp_outbox(dam_id="CRD")) == 1


def test_concurrent_dssmp_approvals_of_one_registration(live, monkeypatch):
    c, st = live["c"], live["st"]
    rid = c.post("/api/dams/CRD/dssmp-registrations", user=ADMIN, json={
        "dssmp_version_id": "DSSMP-CRD-RACE2", "version_no": 2, "effective_from": "2026-11-01", "change_reason": "r",
        "source_ref": "s", "document_uri": "https://docs.example/d.pdf", "document_sha256": "ab" * 32,
        "rationale": "p"}).json()["registration_id"]
    _overlap(monkeypatch, st)
    rs = _race([lambda: c.post(f"/api/dams/CRD/dssmp-registrations/{rid}/approve", user=CDSE,
                               json={"rationale": "ok"})] * 2)
    assert _codes(rs) == [200, 409]
    assert [x["action_type"] for x in st.chain_rows("CRD")] == ["dssmp_version_propose", "dssmp_version_approve"]
    assert len(st.dssmp_outbox(dam_id="CRD")) == 1


def test_concurrent_cdse_flag_applies_queue_one_n_plus_1(live, monkeypatch):
    c, st, an = live["c"], live["st"], live["an"]
    rule = next(t for t in an.thresholds("CRD") if not t.get("cdse_ack_required"))
    tid = rule["threshold_id"]
    _overlap(monkeypatch, st)
    rs = _race([lambda: c.post(f"/api/dams/CRD/alarm-rules/{tid}/cdse-flag", user=CDSE,
                               json={"cdse_ack_required": True, "rationale": "race"})] * 2)
    assert _codes(rs) == [200, 409], [r.text for r in rs]
    assert next(r for r in rs if r.status_code == 409).json()["detail"]["code"] == "flag_stale"
    assert [x["action_type"] for x in st.chain_rows("CRD")] == ["alarm_rule_flag_apply"]
    pubs = [p for p in st.ref_publications("CRD") if p["payload"]["threshold_id"] == tid]
    assert len(pubs) == 1


def test_concurrent_reversion_proposals(live, monkeypatch):
    c, st, an = live["c"], live["st"], live["an"]
    an.d["dssmp_versions"].append({"dssmp_version_id": "DSSMP-CRD-2027", "dam_id": "CRD", "version_no": 2,
                                   "effective_from": "2026-11-01T00:00:00Z", "effective_to": None, "status": "approved"})
    _overlap(monkeypatch, st)
    rs = _race([lambda: c.post("/api/dams/CRD/rule-reversions", user=CDSE, json={
        "to_dssmp_version_id": "DSSMP-CRD-2027", "rationale": "race"})] * 2)
    assert _codes(rs) == [200, 409]
    assert [x["action_type"] for x in st.chain_rows("CRD")] == ["rule_reversion_propose"]
    assert len(st.rows("rule_reversion_proposal", "proposed_at", dam_id="CRD")) == 1


def test_appends_on_two_dams_and_the_same_dam_do_not_deadlock(live):
    c, st = live["c"], live["st"]
    calls = []
    for n in range(4):
        calls.append(lambda n=n: c.post("/api/dams/CRD/cdse/changes", user=ADMIN, json={
            "cdse_id": f"cdse.dl{n}@demo.local", "reason_code": "retirement", "rationale": "x"}))
        calls.append(lambda n=n: c.post("/api/dams/HLD/cdse/changes", user=ADMIN, json={
            "cdse_id": f"cdse.dl{n}@demo.local", "reason_code": "retirement", "rationale": "x"}))
    rs = _race(calls)
    assert all(r.status_code in (200, 409) for r in rs), [r.text for r in rs]
    for d in ("CRD", "HLD"):
        assert len(st.chain_rows(d)) == 1 and len(st.rows("dam_cdse_change", "proposed_at", dam_id=d)) == 1


def test_dam_lock_is_two_key_advisory_distinct_from_the_init_lock(live):
    st, su = live["st"], live["su"]
    with st.transaction() as q:
        st.lock_dam(q, "CRD")
        out = _psql(su, "-At", "-c", "SELECT classid, objsubid FROM pg_locks WHERE locktype = 'advisory' "
                                     "AND granted")
    assert f"{state_mod.DAM_LOCK_NS}|2" in out.split()          # (int4, int4) form: objsubid 2
    assert state_mod.DAM_LOCK_NS not in (state_mod.INIT_LOCK_KEY, 4702046)   # init lock; DA chain-lock class


# ------------------------------------------------------------------ 2. belt and braces: the unique indexes alone
def _no_lock_no_check(monkeypatch, st):
    """Disable layer 1 (lock + in-transaction re-check) and force overlap AFTER the re-check point."""
    barrier = threading.Barrier(2, timeout=20)
    monkeypatch.setattr(st, "lock_dam", lambda q, d: None)
    real = st.append_chained

    def append_chained(*a, check=None, **kw):
        def gate(q):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real(*a, check=gate, **kw)
    monkeypatch.setattr(st, "append_chained", append_chained)


def test_unique_index_alone_refuses_a_second_pending_change_on_the_app_owned_table(live, monkeypatch):
    c, st = live["c"], live["st"]
    _no_lock_no_check(monkeypatch, st)
    rs = _race([lambda w=w: c.post("/api/dams/CRD/cdse/changes", user=ADMIN, json={
        "cdse_id": w, "reason_code": "retirement", "rationale": "race"}) for w in (NEW_A, NEW_B)])
    assert _codes(rs) == [200, 409]
    assert next(r for r in rs if r.status_code == 409).json()["detail"]["code"] == "concurrent_write"
    assert len(st.chain_rows("CRD")) == 1


def test_owner_outbox_index_from_the_da_patch_refuses_a_second_assign(live, monkeypatch):
    c, st, su = live["c"], live["st"], live["su"]
    name, table, cols = next(g for g in state_mod.UNIQUE_GUARDS if g[1] == "dam_cdse_outbox")
    _psql(su, "-c", f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON dam_app.{table} {cols}")   # = DA patch
    monkeypatch.setattr(dam_cdse, "_open_assignment", lambda st, dam_id: None)
    _no_lock_no_check(monkeypatch, st)
    rs = _race([_assign(c, NEW_A), _assign(c, NEW_B)])
    assert _codes(rs) == [200, 409]
    assert next(r for r in rs if r.status_code == 409).json()["detail"]["code"] == "concurrent_write"
    assert len(st.chain_rows("NEW")) == 1 and len(st.rows("dam_cdse_outbox", "outbox_id", dam_id="NEW")) == 1
    assert len(st.rows("dam_cdse_change", "proposed_at", dam_id="NEW")) == 1


def test_app_creates_the_guards_only_on_tables_it_owns(live):
    out = _psql(live["su"], "-At", "-c", "SELECT c.relname || ':' || i.relname FROM pg_index x JOIN pg_class i ON "
                                         "i.oid = x.indexrelid JOIN pg_class c ON c.oid = x.indrelid JOIN pg_namespace n "
                                         "ON n.oid = c.relnamespace WHERE n.nspname = 'dam_app' AND x.indisunique")
    have = set(out.split())
    lb001 = LB001.read_text()
    for name, table, _ in state_mod.UNIQUE_GUARDS + ((state_mod.PUB_GUARD[0], state_mod.PUB_GUARD[1], None),):
        owner_owned = table in ("dam_cdse_outbox", "dssmp_registration_outbox", "ref_change_publication")
        # app-owned: the app creates it; owner-owned: present exactly when the DA's lakebase/001 carries it
        # (tools/patches/qa056_unique_guards.patch), never created by the app (it has no DDL rights there)
        assert (f"{table}:{name}" in have) is (name in lb001 if owner_owned else True), (table, name)
