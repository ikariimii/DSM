"""QA-039 v2 / QA-040 adversarial checks (QA, 2026-11-02 EST) on App Dev's app/v1.5-audit-chain branch.

Independent of app/tests: the record hash is re-derived here from SCHEMA text (sha256 of canonical JSON of
CHAIN_FIELDS, engine canonical_value), the chain is attacked through raw SQL like a privileged actor would, and
identity / ACL / 409 rules are exercised through the HTTP endpoints in both demo and non-demo (proxy) mode.
No restricted data is read: everything here is synthetic seed data."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from backend import audit_chain as ac  # noqa: E402
from backend import main as app_main  # noqa: E402
from backend import seed  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.repos import contract as ct  # noqa: E402
from backend.repos.analytics import MockAnalytics  # noqa: E402
from backend.repos.state import StateStore  # noqa: E402
from dam_safety.inclinometer.corrections import canonical_value as engine_canonical_value  # noqa: E402

ADMIN, ENG, VIEWER, DSO, NOBODY = ("admin@demo.local", "engineer.crd@demo.local", "viewer.bfk@demo.local",
                                   "dso.phr@demo.local", "nobody@demo.local")
IID = "CRD-IN-1"
BODY = {"decision": "approve", "rationale": "QA adversarial: independent check of the proposal"}
SPEC_CHAIN_FIELDS = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
                     "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash", "action_type",
                     "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id", "chain_seq",
                     "prev_hash")


def _h(user, uid=None, **extra):
    h = {} if user is None else {"x-forwarded-user": uid or f"uid-{user.lower()}", "x-forwarded-email": user}
    return {**h, **extra}


def _rec(rid, sid, **kw):
    base = {k: None for k in ct.INCL_CORRECTION_FIELDS}
    base.update(record_id=rid, dam_id="CRD", instrument_id=IID, survey_id=sid, axis="A", applied=False,
                timestamp_utc="2026-11-02T00:00:00Z", user="engine:reduce_inclinometers", before_value=12.5,
                after_value=11.9, parameter_name="c", auto_value=-0.6)
    return {**base, **kw}


_STAT = ("rule=QA021-v2;ZA=-6.0000;abs_dD=6.0000;abs_bs=10.0000;crosscheck_verdict=SUSPECT;crosscheck_diff=4.0;"
         "crosscheck_tol=1.0;status=PROPOSED;requires_review=True")


def _inject(an, sid):
    an.d.setdefault("incl_corrections", []).extend([
        _rec("B1", sid, detection_method="between_set_bias_qc", detection_statistic=_STAT),
        _rec("D1", sid, correction_type="depth", detection_method="engineer_judgement", override_value=-0.5),
        _rec("D2", sid, correction_type="depth", detection_method="engineer_judgement", applied=True),   # qa-negative-fixture: applied, no review -> 409
        _rec("D3", sid, correction_type="depth", detection_method="engineer_judgement", applied=True,  # qa-negative-fixture: approved, not open -> 409
             reviewed_by="rev", reviewed_utc="2026-11-02T01:00:00Z", review_id="RS-X"),               # approved
        _rec("D5", sid, correction_type="depth", detection_method="engineer_judgement", superseded_by="D6"),
        _rec("D4", sid, correction_type="depth", detection_method="engineer_judgement", user=ENG),
        _rec("D7", sid, correction_type="depth", detection_method="engineer_judgement", user="  Engineer.CRD@Demo.Local "),
        _rec("D8", sid, correction_type="depth", detection_method="engineer_judgement", user=f"uid-{ENG}"),
    ])


def _sid(tc, user=ADMIN):
    return tc.get(f"/api/instruments/{IID}/incl/surveys", headers=_h(user)).json()["surveys"][-1]["survey_id"]


@pytest.fixture()
def demo(tmp_path):
    app = app_main.create_app(Settings(demo_mode=True, static_dir=str(tmp_path / "nodist")))
    tc = TestClient(app)
    sid = _sid(tc)
    _inject(app.state.analytics_factory(None), sid)
    yield tc, app, sid
    app.state.store._conn.close()


@pytest.fixture()
def prod(tmp_path, monkeypatch):
    """Non-demo app exactly as deployed (identity from the Apps proxy headers), with the Lakebase store swapped for a
    seeded sqlite StateStore (same class, same DDL and append-only triggers) so requests can run offline."""
    monkeypatch.setattr(StateStore, "init_schema", lambda self: None)
    mock = MockAnalytics(seed.build())
    app = app_main.create_app(Settings(demo_mode=False, warehouse_id="wh", static_dir=str(tmp_path / "nodist")),
                              pg_pool=object(), analytics_factory=lambda t: mock)
    monkeypatch.undo()
    st = StateStore("sqlite")
    for email, name, is_admin in seed.SEED_USERS:
        st.upsert_user(email, name, is_admin, "seed")
    for email, dam, role in seed.SEED_ROLES:
        st.set_role(email, dam, role, "seed")
    app.state.store = st
    tc = TestClient(app)
    sid = _sid(tc)
    _inject(mock, sid)
    yield tc, app, sid
    st._conn.close()


def _rv(tc, sid, rid, headers, kind="corrections", body=BODY, iid=IID):
    return tc.post(f"/api/instruments/{iid}/incl/surveys/{sid}/{kind}/{rid}/review", headers=headers, json=body)


def _crd_event(tc):
    al = tc.get("/api/alarms?dam_id=CRD", headers=_h(ENG)).json()["alarms"]
    return next(a for a in al if a["dam_id"] == "CRD")


def _three(tc, sid):
    r = [_rv(tc, sid, "B1", _h(ENG), "bias-proposals"), _rv(tc, sid, "D1", _h(ENG)),
         _rv(tc, sid, "D1", _h(ADMIN), body={"decision": "reject", "rationale": "second opinion"})]
    assert [x.status_code for x in r] == [200] * 3, [x.text for x in r]
    return r


def _unguard(st):
    for t in ("t_noupdate_review_signoff", "t_nodelete_review_signoff"):
        st.execute(f"DROP TRIGGER IF EXISTS {t}")


def _verify(tc, user=ENG, dam="CRD"):
    return tc.get(f"/api/dams/{dam}/audit/signoffs/verify", headers=_h(user))


# ============================================================ QA-040 end to end
def test_qa040_no_uppercase_ZA_anywhere_in_inclinometer_api_json(demo):
    tc, app, sid = demo
    seen = 0
    for iid in ("CRD-IN-1", "HLD-IN-1", "PHR-IN-1"):
        r = tc.get(f"/api/instruments/{iid}/incl/surveys", headers=_h(ADMIN))
        assert r.status_code == 200
        sids = [s["survey_id"] for s in r.json()["surveys"]]
        p = tc.get(f"/api/instruments/{iid}/incl/profile", params={"survey_ids": ",".join(sids)}, headers=_h(ADMIN))
        assert p.status_code == 200, p.text
        for body in (r.text, p.text):
            seen += body.count('"za"')
            assert not re.search(r"\bZA\b|\bZB\b", body), (iid, re.findall(r".{40}\bZ[AB]\b.{40}", body)[:3])
    assert seen >= 1, "the injected B1 proposal's evidence must be served (else this scan proves nothing)"
    prop = json.dumps(ct.bias_proposal(_rec("B1", sid, detection_method="between_set_bias_qc", detection_statistic=_STAT)))
    assert '"za": -6.0' in prop and not re.search(r"\bZ[AB]\b", prop)


# ============================================================ hash chain: independent re-derivation
def _my_hash(row):
    doc = {f: (int(row[f]) if f == "chain_seq" else engine_canonical_value(row[f])) for f in SPEC_CHAIN_FIELDS}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def test_chain_fields_match_the_contract_and_hash_rederives_independently(demo):
    tc, app, sid = demo
    assert tuple(ac.CHAIN_FIELDS) == SPEC_CHAIN_FIELDS
    assert set(ac.HASH_EXCLUDED) == {"record_hash", "signature", "sig_alg", "key_id"}
    _three(tc, sid)
    rows = sorted(app.state.store.chain_rows("CRD"), key=lambda r: int(r["chain_seq"]))
    assert [int(r["chain_seq"]) for r in rows] == [1, 2, 3]
    prev = "0" * 64
    for r in rows:
        assert r["chain_id"] == "CRD" == r["dam_id"] and r["prev_hash"] == prev
        assert re.fullmatch(r"[0-9a-f]{64}", r["record_hash"]) and r["record_hash"] == _my_hash(r)
        prev = r["record_hash"]
    head = app.state.store.chain_head("CRD")
    assert int(head["chain_seq"]) == 3 and head["head_hash"] == prev
    # identity in the record is the forwarded user id, never only the e-mail; server UTC time
    assert {r["reviewer_id"] for r in rows} == {f"uid-{ENG}", f"uid-{ADMIN}"}
    assert all(r["signed_ts"].endswith("+00:00") for r in rows)
    # a different dam gets its own chain from genesis, and does not touch CRD's
    eid = next(a for a in tc.get("/api/alarms?dam_id=PHR", headers=_h(DSO)).json()["alarms"])["event_id"]
    assert tc.post(f"/api/alarms/{eid}/ack", headers=_h(DSO), json={"rationale": "checked"}).status_code == 200
    phr = app.state.store.chain_rows("PHR")
    assert len(phr) == 1 and phr[0]["prev_hash"] == "0" * 64 and int(phr[0]["chain_seq"]) == 1
    assert _verify(tc).json()["ok"] and _verify(tc, DSO, "PHR").json()["ok"]


@pytest.mark.parametrize("field", [f for f in SPEC_CHAIN_FIELDS if f not in ("chain_seq", "prev_hash", "chain_id")])
def test_tampering_any_hashed_field_is_detected_at_that_seq(demo, field):
    tc, app, sid = demo
    _three(tc, sid)
    st = app.state.store
    with pytest.raises(Exception, match="append-only"):
        st.execute(f"UPDATE review_signoff SET {field} = %s WHERE chain_id='CRD' AND chain_seq = 2", ("tampered",))
    _unguard(st)
    st.execute(f"UPDATE review_signoff SET {field} = %s WHERE chain_id='CRD' AND chain_seq = 2", ("tampered",))
    v = _verify(tc).json()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "hash_mismatch"), v
    assert tc.get("/api/audit/signoffs/verify", headers=_h(ADMIN)).json()["ok"] is False


def test_link_and_ordering_attacks_are_detected(demo):
    tc, app, sid = demo
    _three(tc, sid)
    st = app.state.store
    _unguard(st)
    rows = sorted(st.chain_rows("CRD"), key=lambda r: int(r["chain_seq"]))
    # 1. relink seq 2 to a wrong prev (record_hash recomputed so only the link is wrong)
    st.execute("UPDATE review_signoff SET prev_hash = %s WHERE chain_seq = 2", ("f" * 64,))
    v = _verify(tc).json()
    assert (v["first_broken_seq"], v["reason"]) == (2, "prev_mismatch"), v
    st.execute("UPDATE review_signoff SET prev_hash = %s WHERE chain_seq = 2", (rows[0]["record_hash"],))
    assert _verify(tc).json()["ok"]
    # 2. first record not at genesis
    st.execute("UPDATE review_signoff SET prev_hash = %s WHERE chain_seq = 1", ("1" * 64,))
    assert _verify(tc).json()["reason"] == "prev_mismatch"
    st.execute("UPDATE review_signoff SET prev_hash = %s WHERE chain_seq = 1", ("0" * 64,))
    # 3. swap seq numbers of 2 and 3 (reorder)
    st.execute("UPDATE review_signoff SET chain_seq = 99 WHERE chain_seq = 2")
    st.execute("UPDATE review_signoff SET chain_seq = 2 WHERE chain_seq = 3")
    st.execute("UPDATE review_signoff SET chain_seq = 3 WHERE chain_seq = 99")
    v = _verify(tc).json()
    assert not v["ok"] and v["first_broken_seq"] == 2 and v["reason"] in ("prev_mismatch", "hash_mismatch"), v
    st.execute("UPDATE review_signoff SET chain_seq = 99 WHERE chain_seq = 2")
    st.execute("UPDATE review_signoff SET chain_seq = 2 WHERE chain_seq = 3")
    st.execute("UPDATE review_signoff SET chain_seq = 3 WHERE chain_seq = 99")
    assert _verify(tc).json()["ok"]
    # 4. move a record to another dam's chain (chain_id is hashed)
    st.execute("UPDATE review_signoff SET chain_id = 'HLD' WHERE chain_seq = 3 AND dam_id = 'CRD'")
    v = _verify(tc).json()
    assert (v["ok"], v["reason"]) == (False, "head_mismatch"), v
    st.execute("UPDATE review_signoff SET chain_id = 'CRD' WHERE chain_seq = 3 AND dam_id = 'CRD'")
    # 5. delete a middle record -> gap; delete the tail -> head_mismatch; delete the head row -> head_missing
    st.execute("DELETE FROM review_signoff WHERE chain_seq = 3")
    assert _verify(tc).json()["reason"] == "head_mismatch"
    st.execute("DELETE FROM review_signoff WHERE chain_seq = 1")
    v = _verify(tc).json()
    assert (v["first_broken_seq"], v["reason"]) == (1, "gap"), v


def test_head_row_deletion_and_forged_unlinked_append_are_detected(demo):
    tc, app, sid = demo
    _three(tc, sid)
    st = app.state.store
    st.execute("DELETE FROM review_chain_head WHERE chain_id = 'CRD'")  # head is not append-only (it is the lock)
    v = _verify(tc).json()
    assert (v["ok"], v["reason"]) == (False, "head_missing"), v


def test_full_recompute_by_privileged_actor_is_a_documented_limit(demo):
    """Known limitation (not a code defect): an unkeyed chain is tamper-evident only against actors who cannot rewrite
    every later record and the head. app/SCHEMA_CONTRACT.md §5a says tampering is "all detected" without this caveat
    (QA-045 doc item)."""
    tc, app, sid = demo
    _three(tc, sid)
    st = app.state.store
    _unguard(st)
    rows = sorted(st.chain_rows("CRD"), key=lambda r: int(r["chain_seq"]))
    prev = "0" * 64
    for r in rows:
        r = {**r, "prev_hash": prev, "decision": "approve" if int(r["chain_seq"]) == 3 else r["decision"]}
        h = _my_hash(r)
        st.execute("UPDATE review_signoff SET decision=%s, prev_hash=%s, record_hash=%s WHERE chain_id='CRD' AND "
                   "chain_seq=%s", (r["decision"], prev, h, int(r["chain_seq"])))
        prev = h
    st.execute("UPDATE review_chain_head SET head_hash=%s WHERE chain_id='CRD'", (prev,))
    assert _verify(tc).json()["ok"] is True   # undetectable without a key: logged as a doc gap under QA-045


# ============================================================ append-only
def test_append_only_in_the_database_and_no_mutating_routes(demo):
    tc, app, sid = demo
    _three(tc, sid)
    st = app.state.store
    for sql in ("UPDATE review_signoff SET comment = 'x'", "DELETE FROM review_signoff",
                "UPDATE review_signoff SET record_hash = prev_hash WHERE chain_seq = 3"):
        with pytest.raises(Exception, match="append-only"):
            st.execute(sql)
    row = dict(sorted(st.chain_rows("CRD"), key=lambda r: int(r["chain_seq"]))[1])
    cols = list(row)
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE (chain_id, chain_seq): no second record at a used seq
        st._conn.execute(f"INSERT INTO review_signoff ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [{**row, "review_id": "RS-FORGED"}[c] for c in cols])
    assert len(st.chain_rows("CRD")) == 3 and _verify(tc).json()["ok"]
    # no HTTP route edits or deletes a sign-off / chain record
    for r in app.routes:
        p, methods = getattr(r, "path", ""), getattr(r, "methods", set()) or set()
        if re.search(r"signoff|/review$|/ack$|/close$|audit", p):
            assert methods <= {"GET", "POST", "HEAD"}, (p, methods)
    # the Postgres path installs the same guard (row-level BEFORE UPDATE OR DELETE trigger)
    src = (ROOT / "app" / "backend" / "repos" / "state.py").read_text()
    assert "BEFORE UPDATE OR DELETE ON" in src and 'APPEND_ONLY = ("review_signoff",)' in src


# ============================================================ identity: 401 outside demo mode
_ENDPOINTS = [("get", "/api/audit/signoffs/verify", None), ("get", "/api/dams/CRD/audit/signoffs/verify", None),
              ("post", "/api/instruments/CRD-IN-1/incl/surveys/{sid}/corrections/D1/review", BODY),
              ("post", "/api/instruments/CRD-IN-1/incl/surveys/{sid}/bias-proposals/B1/review", BODY)]


@pytest.mark.parametrize("headers", [{}, {"x-forwarded-user": ""}, {"x-forwarded-user": "   "},
                                     {"x-demo-user": ADMIN}, {"x-forwarded-email": ADMIN},
                                     {"x-forwarded-email": ADMIN, "x-demo-user": ADMIN, "x-forwarded-user": " \t"}],
                         ids=["none", "empty", "blank", "demo-header", "email-only", "blank+email+demo"])
def test_prod_missing_or_blank_forwarded_user_is_401_and_writes_nothing(prod, headers):
    tc, app, sid = prod
    eid = _crd_event(tc)["event_id"]
    for method, url, body in _ENDPOINTS + [("post", f"/api/alarms/{eid}/ack", {"rationale": "x"}),
                                           ("post", f"/api/alarms/{eid}/close", {"reason": "x"})]:
        r = getattr(tc, method)(url.format(sid=sid), headers=headers, **({"json": body} if body else {}))
        assert r.status_code == 401, (url, r.status_code, r.text)
    assert app.state.store.chain_ids() == []


def test_prod_spoofed_identities_are_refused_and_write_nothing(prod):
    tc, app, sid = prod
    st = app.state.store
    # legitimate first sign-in binds ENG's email row to uid-ENG
    assert _verify(tc, ENG).status_code == 200
    spoofs = [_h("attacker@evil.example", "uid-attacker"),                 # unknown user
              _h(ENG, "uid-attacker"),                                       # victim's e-mail, new id (ENG bound)
              _h(ADMIN, "uid-attacker2"),                                    # admin e-mail (bound by the fixture)
              {"x-forwarded-user": "uid-attacker3", "x-forwarded-email": "", "x-demo-user": ADMIN}]
    out = [(tc.get("/api/dams/CRD/audit/signoffs/verify", headers=h).status_code, _rv(tc, sid, "D1", h).status_code,
            tc.get("/api/audit/signoffs/verify", headers=h).status_code) for h in spoofs]
    # an unknown id, or a new id presenting an already-bound e-mail, never inherits roles (403, not 401: the proxy
    # authenticated *someone*; they are just not authorised)
    assert out == [(403, 403, 403)] * 4, out
    assert st.chain_ids() == []
    # Proxy-trust model (observation, QA.md): a granted e-mail row that has never signed in is bound by the first id
    # that presents it. In Databricks Apps only the proxy sets these headers, so this needs a proxy bypass.
    assert tc.get("/api/dams/PHR/audit/signoffs/verify", headers=_h(DSO, "uid-first-presenter")).status_code == 200
    assert tc.get("/api/dams/PHR/audit/signoffs/verify", headers=_h(DSO)).status_code == 403   # real DSO locked out


def test_prod_reviewer_identity_is_forwarded_user_not_client_supplied(prod):
    tc, app, sid = prod
    body = {**BODY, "reviewer_id": "someone-else", "reviewer_email": "x@y", "signed_ts": "1999-01-01T00:00:00Z"}
    r = _rv(tc, sid, "D1", _h(ENG), body=body)
    assert r.status_code == 200, r.text
    row = app.state.store.chain_rows("CRD")[0]
    assert row["reviewer_id"] == f"uid-{ENG}" and row["reviewer_email"] == ENG
    assert not row["signed_ts"].startswith("1999") and row["review_level"] == "engineer"


# ============================================================ 403 author == reviewer
@pytest.mark.parametrize("mode", ["demo", "prod"])
def test_self_review_is_403_in_every_identity_form(request, mode):
    tc, app, sid = request.getfixturevalue(mode)
    for rid in ("D4", "D7", "D8"):   # author stored as e-mail, as padded mixed-case e-mail, and as the user id
        r = _rv(tc, sid, rid, _h(ENG))
        assert r.status_code == 403 and "author" in r.text, (rid, r.status_code, r.text)
        # the forwarded e-mail header is display-only: with only the id, the bound e-mail still matches
        if mode == "prod":
            r = _rv(tc, sid, rid, {"x-forwarded-user": f"uid-{ENG}"})
            assert r.status_code == 403, (rid, r.text)
            r = _rv(tc, sid, rid, {"x-forwarded-user": f"uid-{ENG}", "x-forwarded-email": "other@x.example"})
            assert r.status_code == 403, (rid, r.text)
    assert app.state.store.chain_ids() == []
    assert _rv(tc, sid, "D4", _h(ADMIN)).status_code == 200   # an independent reviewer may


# ============================================================ 409 not an open proposal
@pytest.mark.parametrize("mode", ["demo", "prod"])
def test_non_open_records_are_409_and_write_nothing(request, mode):
    tc, app, sid = request.getfixturevalue(mode)
    an = app.state.analytics_factory(None)
    an.d["incl_corrections"].append(_rec("B2", sid, detection_method="between_set_bias_qc",
                                         detection_statistic=_STAT.replace("status=PROPOSED", "status=NO_PROPOSAL")))
    for kind, rid in (("corrections", "D2"), ("corrections", "D3"), ("corrections", "D5"),
                      ("bias-proposals", "B2")):
        r = _rv(tc, sid, rid, _h(ADMIN), kind)
        assert r.status_code in (409, 404), (rid, r.status_code, r.text)
        if rid != "B2":
            assert r.status_code == 409, (rid, r.text)
    assert app.state.store.chain_ids() == []
    for bad in ({"decision": "applied", "rationale": "x"}, {"decision": "approve", "rationale": "   "},
                {"decision": "approve"}):
        assert _rv(tc, sid, "D1", _h(ADMIN), body=bad).status_code == 422
    assert _rv(tc, sid, "NOPE", _h(ADMIN)).status_code == 404
    assert _rv(tc, "OTHER", "D1", _h(ADMIN)).status_code == 404
    assert app.state.store.chain_ids() == []


# ============================================================ read / write ACL by dam
@pytest.mark.parametrize("mode", ["demo", "prod"])
def test_verify_and_review_acl_by_dam(request, mode):
    tc, app, sid = request.getfixturevalue(mode)
    st = app.state.store
    exp = {(ENG, "CRD"): 200, (ENG, "HLD"): 200, (ENG, "BFK"): 403, (ENG, "PHR"): 403,
           (VIEWER, "BFK"): 403, (VIEWER, "CRD"): 403, (DSO, "PHR"): 200, (DSO, "STC"): 200, (DSO, "CRD"): 403,
           (NOBODY, "CRD"): 403, (ADMIN, "CRD"): 200, (ADMIN, "BFK"): 200, (ENG, "NO-SUCH-DAM"): 403}
    got = {(u, d): _verify(tc, u, d).status_code for (u, d) in exp}
    assert got == exp
    assert {u: tc.get("/api/audit/signoffs/verify", headers=_h(u)).status_code
            for u in (ADMIN, ENG, VIEWER, DSO, NOBODY)} == {ADMIN: 200, ENG: 403, VIEWER: 403, DSO: 403, NOBODY: 403}
    # writes: engineer on another dam's inclinometer, viewers and no-access users cannot review
    phr_sid = tc.get("/api/instruments/PHR-IN-1/incl/surveys", headers=_h(ADMIN)).json()["surveys"][-1]["survey_id"]
    assert _rv(tc, phr_sid, "D1", _h(ENG), iid="PHR-IN-1").status_code == 403
    for u in (VIEWER, NOBODY):
        assert _rv(tc, sid, "D1", _h(u)).status_code == 403
    assert st.chain_ids() == []
    # a dam verify reveals only that dam's chain status, no record content
    _three(tc, sid)
    v = _verify(tc, ENG).json()
    assert v["chain_id"] == "CRD" and "rows" not in v and "records_detail" not in v
    assert "QA adversarial" not in json.dumps(v) and "second opinion" not in json.dumps(v)


# ============================================================ demo mode can never reach prod
def test_demo_mode_never_constructs_prod_clients_even_with_prod_env(tmp_path, monkeypatch):
    for k, v in {"DAM_CATALOG": "dam_prod", "PGHOST": "prod-lakebase.invalid", "PGDATABASE": "databricks_postgres",
                 "PGUSER": "sp", "DATABRICKS_WAREHOUSE_ID": "prodwh", "DATABRICKS_HOST": "https://prod.invalid"}.items():
        monkeypatch.setenv(k, v)

    def boom(*a, **k):
        raise AssertionError("demo mode touched a production client")
    monkeypatch.setattr(app_main, "SqlAnalytics", boom)
    monkeypatch.setattr(app_main, "lakebase_pool", boom)
    s = Settings(demo_mode=True, static_dir=str(tmp_path))
    assert s.catalog == "dam_prod"
    app = app_main.create_app(s)
    tc = TestClient(app)
    assert app.state.store.kind == "sqlite" and isinstance(app.state.analytics_factory("tok"), MockAnalytics)
    for url in ("/api/me", f"/api/instruments/{IID}/incl/surveys", "/api/dams/CRD/audit/signoffs/verify"):
        assert tc.get(url, headers=_h(ENG)).status_code == 200, url
    app.state.store._conn.close()


def test_every_bundle_target_deploys_with_demo_mode_off():
    app_yml = yaml.safe_load((ROOT / "resources" / "app.yml").read_text())
    apps = app_yml["resources"]["apps"]
    for name, a in apps.items():
        env = {e["name"]: e.get("value") for e in (a.get("config") or {}).get("env", [])}
        assert str(env.get("DEMO_MODE", "false")).lower() == "false", name
    app_yaml = yaml.safe_load((ROOT / "app" / "app.yaml").read_text())
    env = {e["name"]: e.get("value") for e in app_yaml.get("env", [])}
    assert str(env.get("DEMO_MODE", "false")).lower() == "false"
    dbx = (ROOT / "databricks.yml").read_text()
    assert not re.search(r"DEMO_MODE\W+(true|1|yes)", dbx, re.I)


@pytest.mark.xfail(strict=True, reason="QA-041: Settings.check() accepts DEMO_MODE=true with DAM_CATALOG=dam_prod")
def test_settings_refuse_demo_mode_against_the_prod_catalog():
    with pytest.raises(RuntimeError):
        Settings(demo_mode=True, catalog="dam_prod").check()


# ============================================================ latent: key signatures
class _HmacSigner:
    alg, key_id = "HS256-test", "k1"

    def sign(self, record_hash):
        return hashlib.sha256(b"k1|" + record_hash.encode()).hexdigest()

    def verify(self, record_hash, signature, key_id):
        return signature == self.sign(record_hash)


def _signed_store():
    st = StateStore("sqlite")
    signer = _HmacSigner()
    base = lambda n: {"review_id": f"RS-{n}", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": f"E{n}",  # noqa: E731
                      "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "uid-1",
                      "reviewer_pe_license": None, "signed_ts": "2026-11-02T00:00:00.000000+00:00",
                      "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
                      "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}"}
    for n in range(3):
        st.append_chained("CRD", ac.GENESIS, ac.finalizer(base(n), signer))
    return st, signer


def test_signed_chain_detects_a_forged_signature():
    st, signer = _signed_store()
    v = ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"), signer)
    assert v["ok"] and v["signed_records"] == 3
    _unguard(st)
    st.execute("UPDATE review_signoff SET signature = %s WHERE chain_seq = 2", ("0" * 64,))
    v = ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"), signer)
    assert (v["first_broken_seq"], v["reason"]) == (2, "signature_invalid")
    st._conn.close()


# QA-045a closed (App Dev 6cb95c9 / 113060c, verified 2026-11-09): with a real Signer, signature/sig_alg/key_id
# are mandatory even though they sit outside record_hash. Strict xfail removed; adversarial coverage below.
class _KeyAwareSigner(_HmacSigner):
    """Rejects a mismatched key_id (the base HMAC helper only checked the signature bytes)."""
    key_id = "k1"

    def verify(self, record_hash, signature, key_id):
        return key_id == self.key_id and super().verify(record_hash, signature, key_id)


def _rows_head(st, signer):
    return st.chain_rows("CRD"), st.chain_head("CRD"), signer


def test_signed_chain_detects_a_stripped_signature():
    st, signer = _signed_store()
    _unguard(st)
    st.execute("UPDATE review_signoff SET signature = NULL, sig_alg = NULL, key_id = NULL WHERE chain_seq = 2")
    v = ac.verify_chain(*_rows_head(st, signer))
    st._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "signature_downgrade")
    assert v["signatures_required"] is True


@pytest.mark.parametrize("blank", [None, "", "   ", chr(9)])
@pytest.mark.parametrize("cols", [("signature",), ("sig_alg",), ("key_id",), ("signature", "sig_alg", "key_id")])
def test_qa045a_blank_or_missing_signature_fields_fail(cols, blank):
    st, signer = _signed_store()
    _unguard(st)
    st.execute(f"UPDATE review_signoff SET {', '.join(c + ' = %s' for c in cols)} WHERE chain_seq = 2",
               tuple(blank for _ in cols))
    v = ac.verify_chain(*_rows_head(st, signer))
    st._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "signature_downgrade")
    assert all(c in v["detail"] for c in cols)


def test_qa045a_borrowed_signature_from_another_row_fails():
    st, signer = _signed_store()
    rows = {int(r["chain_seq"]): r for r in st.chain_rows("CRD")}
    _unguard(st)
    st.execute("UPDATE review_signoff SET signature = %s WHERE chain_seq = 2", (rows[1]["signature"],))
    v = ac.verify_chain(*_rows_head(st, signer))
    st._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "signature_invalid")


def test_qa045a_truncated_signature_fails():
    st, signer = _signed_store()
    rows = {int(r["chain_seq"]): r for r in st.chain_rows("CRD")}
    _unguard(st)
    st.execute("UPDATE review_signoff SET signature = %s WHERE chain_seq = 2", (rows[2]["signature"][:-4],))
    v = ac.verify_chain(*_rows_head(st, signer))
    st._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "signature_invalid")


def test_qa045a_wrong_key_id_fails():
    st, _ = _signed_store()
    signer = _KeyAwareSigner()
    # Re-sign with the key-aware helper so key_id is checked on verify.
    st2 = StateStore("sqlite")
    base = lambda n: {"review_id": f"RS-{n}", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": f"E{n}",
                      "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "uid-1",
                      "reviewer_pe_license": None, "signed_ts": "2026-11-02T00:00:00.000000+00:00",
                      "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
                      "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}"}
    for n in range(3):
        st2.append_chained("CRD", ac.GENESIS, ac.finalizer(base(n), signer))
    _unguard(st2)
    st2.execute("UPDATE review_signoff SET key_id = %s WHERE chain_seq = 2", ("k-other",))
    v = ac.verify_chain(st2.chain_rows("CRD"), st2.chain_head("CRD"), signer)
    st._conn.close(); st2._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "signature_invalid")


def test_qa045a_signature_key_removed_from_row_dict_fails():
    st, signer = _signed_store()
    rows = [dict(r) for r in st.chain_rows("CRD")]
    head = st.chain_head("CRD")
    for f in ("signature", "sig_alg", "key_id"):
        rows[1].pop(f, None)
    v = ac.verify_chain(rows, head, signer)
    st._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "signature_downgrade")
    assert "signature" in v["detail"]


def test_qa045a_unsigned_row_inserted_mid_chain_fails():
    st, signer = _signed_store()
    # Append an unsigned record after signed ones (Noop finalizer), then verify under the real signer.
    base = {"review_id": "RS-99", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": "E99",
            "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "uid-1",
            "reviewer_pe_license": None, "signed_ts": "2026-11-02T00:00:00.000000+00:00",
            "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
            "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}"}
    st.append_chained("CRD", ac.GENESIS, ac.finalizer(base))  # NoopSigner -> unsigned
    v = ac.verify_chain(*_rows_head(st, signer))
    st._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 4, "signature_downgrade")


def test_qa045a_verify_with_signer_unset_cannot_silently_accept_claimed_signatures():
    """Downgrade attack: rows claim signatures, but verification is invoked without a real signer.
    NoopSigner cannot vouch for any signature, so every claimed signature fails; this is not a bypass."""
    st, _ = _signed_store()
    for signer in (None, ac.NoopSigner()):
        v = ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"), signer)
        assert v["ok"] is False and v["reason"] == "signature_invalid"
        assert v.get("signatures_required") is False
    st._conn.close()


def test_qa045a_demo_noop_path_still_accepts_unsigned_and_rejects_tampered_hash():
    """Documented demo/no-signer path: unsigned chains verify; content/prev tampering still breaks the hash chain."""
    st = StateStore("sqlite")
    base = lambda n: {"review_id": f"RS-{n}", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": f"E{n}",
                      "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "uid-1",
                      "reviewer_pe_license": None, "signed_ts": "2026-11-02T00:00:00.000000+00:00",
                      "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
                      "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}"}
    for n in range(3):
        st.append_chained("CRD", ac.GENESIS, ac.finalizer(base(n)))  # Noop default
    v = ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"), None)
    assert v["ok"] and v["signed_records"] == 0 and v["signatures_required"] is False
    _unguard(st)
    st.execute("UPDATE review_signoff SET comment = %s WHERE chain_seq = 2", ("tampered",))
    v = ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"), None)
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "hash_mismatch")
    st._conn.close()


def test_qa045a_hash_chain_checks_not_weakened_under_real_signer():
    st, signer = _signed_store()
    _unguard(st)
    st.execute("UPDATE review_signoff SET comment = %s WHERE chain_seq = 2", ("tampered",))
    v = ac.verify_chain(*_rows_head(st, signer))
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "hash_mismatch")
    st.execute("UPDATE review_signoff SET prev_hash = %s WHERE chain_seq = 3", ("f" * 64,))
    # restore comment so hash of seq2 is valid again, then break prev on seq3
    # (comment already tampered; re-write from a fresh store for a clean prev_mismatch)
    st._conn.close()
    st, signer = _signed_store()
    _unguard(st)
    st.execute("UPDATE review_signoff SET prev_hash = %s WHERE chain_seq = 3", ("f" * 64,))
    v = ac.verify_chain(*_rows_head(st, signer))
    st._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 3, "prev_mismatch")


def test_qa045a_noop_cannot_be_swapped_in_to_accept_a_stripped_prod_chain():
    """Stripping all signatures and verifying with NoopSigner succeeds (demo path), but that path is only
    the default when no real signer is configured. A prod catalog that installs a real signer still fails
    the stripped chain. Settings.demo_mode against dam_prod remains separately refused (QA-041 xfail)."""
    st, signer = _signed_store()
    _unguard(st)
    st.execute("UPDATE review_signoff SET signature = NULL, sig_alg = NULL, key_id = NULL")
    assert ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"), ac.NoopSigner())["ok"] is True
    v = ac.verify_chain(st.chain_rows("CRD"), st.chain_head("CRD"), signer)
    st._conn.close()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 1, "signature_missing")
    assert ac.is_enforcing(signer) and not ac.is_enforcing(ac.NoopSigner())


# QA-045b closed (App Dev 4014e4f + DA 98fab6c / bd2cf29; live PG 17.11 verified 2026-11-09):
# statement-level BEFORE TRUNCATE triggers on append-only tables. Strict xfail removed.
def test_postgres_guard_also_refuses_truncate():
    src = (ROOT / "app" / "backend" / "repos" / "state.py").read_text()
    assert re.search(r"BEFORE TRUNCATE ON \{s\}\{t\}", src) or re.search(
        r"BEFORE TRUNCATE ON \{\{s\}\}\{t\}", src) or "BEFORE TRUNCATE ON" in src
    assert "t_app_notrunc_" in src and "FOR EACH STATEMENT" in src
    assert "app_forbid_mutation" in src
    lb = (ROOT / "lakebase" / "001_app_schema.sql").read_text()
    for t in ("review_signoff", "exceedance_status", "manual_reading"):
        assert re.search(rf"BEFORE TRUNCATE ON {t}\s+FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation\(\)", lb)


# ============================================================ demo-path approval_status vs gold view / engine
@pytest.mark.xfail(strict=True, reason="QA-046: contract.approval_status demo fallback compares reviewed_by to user "
                                       "case-sensitively; gold.incl_correction_status and the engine use lower()")
def test_demo_approval_status_treats_case_variant_self_review_as_invalid():
    view = (ROOT / "sql" / "ddl" / "11_gold_views.sql").read_text()
    assert "lower(r.reviewer_id) <> lower(c.`user`)" in view
    with pytest.raises(ct.DataIntegrityError):
        ct.approval_status(dict(applied=True, reviewed_by="Eng@X.org", user="eng@x.org",  # qa-negative-fixture
                                reviewed_utc="2026-11-02T00:00:00Z", review_id="RS-1"))
