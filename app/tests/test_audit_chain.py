"""QA-039 v2: per-dam hash chain over every reviewer action (bias / data-correction reviews, alarm ack / close)."""
import base64
import contextlib
import copy
import json
import logging
import sqlite3
import sys
import threading

import pytest

from backend import audit_chain as ac
from backend.repos import contract as ct
from backend.repos.state import StateStore
from conftest import ADMIN, CDSE, DSO, ENG, VIEWER

IID = "CRD-IN-1"


# ------------------------------------------------------------------ helpers
_OPEN = []


def _store(**kw):
    st = StateStore("sqlite", **kw)
    _OPEN.append(st)
    return st


@pytest.fixture(autouse=True)
def _close_stores():
    yield
    while _OPEN:
        _OPEN.pop()._conn.close()


def _base(dam="CRD", n=0, **kw):
    b = {"review_id": f"RS-{dam}-{n}", "dam_id": dam, "subject_type": "alarm_event", "subject_id": f"EV-{n}",
         "review_level": "engineer", "decision": "acknowledge", "comment": f"rationale {n}", "reviewer_id": "uid-1",
         "reviewer_pe_license": None, "signed_ts": "2026-11-02T00:00:00.000000+00:00", "signature_hash": "c" * 64,
         "action_type": "alarm_ack", "reviewer_email": "a@x", "reviewer_sub": "sub-1",
         "original_value": ac.canon_json({"value": 1.5, "unit": "m", "state": "open"}),
         "corrected_value": ac.canon_json({"state": "acknowledged"})}
    return {**b, **kw}


def _chain(st, k=4, dam="CRD"):
    return [st.append_chained(dam, ac.GENESIS, ac.finalizer(_base(dam, i))) for i in range(k)]


def _unguard(st):
    """Simulate a privileged actor bypassing the app (drops the append-only triggers on the sqlite file)."""
    for t in ("t_noupdate_review_signoff", "t_nodelete_review_signoff"):
        st.execute(f"DROP TRIGGER IF EXISTS {t}")


def _verify(st, dam="CRD", signer=None):
    return ac.verify_chain(st.chain_rows(dam), st.chain_head(dam), signer)


def _jwt(claims):
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.SIGNATUREPART-secret"


# ------------------------------------------------------------------ building / verifying
def test_chain_building_links_genesis_and_head():
    st = _store()
    rows = _chain(st, 3)
    assert [r["chain_seq"] for r in rows] == [1, 2, 3] and rows[0]["prev_hash"] == ac.GENESIS == "0" * 64
    assert rows[1]["prev_hash"] == rows[0]["record_hash"] and rows[2]["prev_hash"] == rows[1]["record_hash"]
    assert all(r["chain_id"] == "CRD" and r["signature"] is None and r["sig_alg"] is None for r in rows)
    head = st.chain_head("CRD")
    assert (head["chain_seq"], head["head_hash"]) == (3, rows[2]["record_hash"])
    v = _verify(st)
    assert v["ok"] and v["records"] == 3 and v["head_seq"] == 3 and v["first_broken_seq"] is None
    # chains are per dam: a second dam starts at genesis again
    other = _chain(st, 1, dam="BFK")[0]
    assert other["chain_seq"] == 1 and other["prev_hash"] == ac.GENESIS and st.chain_ids() == ["BFK", "CRD"]
    assert ac.verify_chain([], None) ["ok"]  # empty chain is trivially intact


def test_record_hash_covers_every_chain_field_but_not_signature():
    row = ac.finalizer(_base())(1, ac.GENESIS)
    for f in ac.CHAIN_FIELDS:
        changed = {**row, f: (row[f] + 1) if isinstance(row[f], int) else f"{row[f]}x"}
        assert ac.record_hash(changed) != row["record_hash"], f
    assert ac.record_hash({**row, "signature": "s", "sig_alg": "a", "key_id": "k"}) == row["record_hash"]
    assert ac.record_hash({**row, "chain_seq": "1"}) == row["record_hash"]  # text vs int seq from the DB


@pytest.mark.parametrize("col,val", [("comment", "edited"), ("original_value", '{"value":9}'),
                                     ("reviewer_id", "uid-2"), ("signed_ts", "2026-11-03T00:00:00+00:00"),
                                     ("signature_hash", "d" * 64)])
def test_tamper_detected_as_hash_mismatch(col, val):
    st = _store()
    _chain(st, 4)
    _unguard(st)
    st.execute(f"UPDATE review_signoff SET {col} = %s WHERE chain_seq = 2", (val,))
    v = _verify(st)
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "hash_mismatch")


def test_tamper_with_recomputed_hash_breaks_next_link():
    st = _store()
    rows = _chain(st, 4)
    _unguard(st)
    forged = {**rows[1], "comment": "forged"}
    st.execute("UPDATE review_signoff SET comment = %s, record_hash = %s WHERE chain_seq = 2",
               ("forged", ac.record_hash(forged)))
    v = _verify(st)
    assert (v["first_broken_seq"], v["reason"]) == (3, "prev_mismatch")


def test_delete_middle_is_a_gap_and_delete_tail_is_a_head_mismatch():
    st = _store()
    _chain(st, 4)
    _unguard(st)
    st.execute("DELETE FROM review_signoff WHERE chain_seq = 2")
    v = _verify(st)
    assert (v["first_broken_seq"], v["reason"]) == (2, "gap")
    st2 = _store()
    _chain(st2, 4)
    _unguard(st2)
    st2.execute("DELETE FROM review_signoff WHERE chain_seq = 4")
    v = _verify(st2)
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 4, "head_mismatch")
    st2.execute("DELETE FROM review_chain_head")
    assert _verify(st2)["reason"] == "head_missing"


def test_head_altered_or_first_record_not_at_genesis():
    st = _store()
    rows = _chain(st, 2)
    st.execute("UPDATE review_chain_head SET head_hash = %s", ("e" * 64,))
    assert _verify(st)["reason"] == "head_mismatch"
    bad = {**rows[0], "prev_hash": "1" * 64}
    bad["record_hash"] = ac.record_hash(bad)
    v = ac.verify_chain([bad], {"chain_seq": 1, "head_hash": bad["record_hash"]})
    assert (v["first_broken_seq"], v["reason"]) == (1, "prev_mismatch") and "GENESIS" in v["detail"]
    dup = ac.verify_chain([rows[0], {**rows[1], "chain_seq": 1}], None)
    assert dup["reason"] in ("gap", "prev_mismatch") and not dup["ok"]


def test_reorder_detected():
    st = _store()
    _chain(st, 4)
    _unguard(st)
    st.execute("UPDATE review_signoff SET chain_seq = -2 WHERE chain_seq = 2")
    st.execute("UPDATE review_signoff SET chain_seq = 2 WHERE chain_seq = 3")
    st.execute("UPDATE review_signoff SET chain_seq = 3 WHERE chain_seq = -2")
    v = _verify(st)
    assert (v["ok"], v["first_broken_seq"]) == (False, 2) and v["reason"] in ("hash_mismatch", "prev_mismatch")


def test_db_refuses_update_delete_duplicate_seq_and_blank_rationale():
    st = _store()
    rows = _chain(st, 2)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.execute("UPDATE review_signoff SET comment = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.execute("DELETE FROM review_signoff")
    cols = StateStore.SIGNOFF_COLS
    ins = f"INSERT INTO review_signoff ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE (chain_id, chain_seq)
        st.execute(ins, tuple({**rows[1], "review_id": "RS-other"}.get(c) for c in cols))
    with pytest.raises(sqlite3.IntegrityError):  # CHECK: rationale non-empty
        st.execute(ins, tuple({**rows[1], "review_id": "RS-blank", "chain_seq": 9, "comment": "   "}.get(c)
                              for c in cols))


def test_failed_side_effect_rolls_back_the_record():
    st = _store()
    _chain(st, 1)

    def boom(q):
        raise ValueError("state changed")
    with pytest.raises(ValueError):
        st.append_chained("CRD", ac.GENESIS, ac.finalizer(_base(n=7)), boom)
    assert len(st.chain_rows("CRD")) == 1 and st.chain_head("CRD")["chain_seq"] == 1 and _verify(st)["ok"]


def test_concurrent_appends_two_instances_same_db(tmp_path):
    db = str(tmp_path / "state.db")
    a, b = _store(sqlite_path=db), _store(sqlite_path=db)
    errors = []

    def work(store, tag):
        try:
            for i in range(25):
                store.append_chained("CRD", ac.GENESIS, ac.finalizer(_base(n=f"{tag}{i}")))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=work, args=(s, t)) for s, t in ((a, "a"), (b, "b"), (a, "c"), (b, "d"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    rows = a.chain_rows("CRD")
    assert [r["chain_seq"] for r in rows] == list(range(1, 101))
    assert _verify(b)["ok"] and _verify(a)["head_seq"] == 100


# ------------------------------------------------------------------ postgres path (fake pool)
class _Cur:
    def __init__(self, log):
        self.log, self.description, self._rows = log, None, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.log.append(sql)
        head = "SELECT chain_seq, head_hash FROM" in sql
        self.description = [("x",)] if sql.lstrip().startswith("SELECT") else None
        self._rows = [{"chain_seq": 4, "head_hash": "f" * 64}] if head else []

    def fetchall(self):
        return self._rows


class _Pool:
    def __init__(self):
        self.log, self.borrowed, self.committed, self.rolled_back = [], 0, 0, 0

    @contextlib.contextmanager
    def connection(self):
        self.borrowed += 1
        try:
            yield self
            self.committed += 1
        except BaseException:
            self.rolled_back += 1
            raise

    def cursor(self):
        return _Cur(self.log)


def test_postgres_append_serializes_on_head_row_in_one_transaction():
    pool = _Pool()
    st = StateStore("postgres", schema="dam_app", pg_pool=pool)
    ddl = "\n".join(pool.log)
    assert '"dam_app".review_chain_head' in ddl and "ADD COLUMN IF NOT EXISTS chain_seq BIGINT" in ddl
    assert 'CREATE UNIQUE INDEX IF NOT EXISTS review_signoff_chain_seq ON "dam_app".review_signoff (chain_id, chain_seq)' in ddl
    assert "t_app_noupd_review_signoff" in ddl
    pool.log.clear()
    n = pool.borrowed
    row = st.append_chained("CRD", ac.GENESIS, ac.finalizer(_base()), lambda q: q("UPDATE x SET y = 1"))
    assert pool.borrowed == n + 1 and pool.committed >= 1          # one connection = one transaction
    assert row["chain_seq"] == 5 and row["prev_hash"] == "f" * 64
    kinds = [s.split()[0] + (" FOR UPDATE" if "FOR UPDATE" in s else "") for s in pool.log]
    assert kinds == ["INSERT", "SELECT FOR UPDATE", "INSERT", "UPDATE", "UPDATE"]
    assert "ON CONFLICT (chain_id) DO NOTHING" in pool.log[0] and '"dam_app".review_chain_head' in pool.log[1]
    rb = pool.rolled_back
    with pytest.raises(ValueError):
        st.append_chained("CRD", ac.GENESIS, ac.finalizer(_base()), lambda q: (_ for _ in ()).throw(ValueError("x")))
    assert pool.rolled_back == rb + 1


# ------------------------------------------------------------------ signer hook
class _FakeSigner:
    alg, key_id = "test-hmac", "k1"

    def sign(self, h):
        return "sig:" + h[::-1]

    def verify(self, h, sig, key_id):
        return key_id == "k1" and sig == "sig:" + h[::-1]


def test_pluggable_signer():
    st = _store()
    rows = [st.append_chained("CRD", ac.GENESIS, ac.finalizer(_base(n=i), _FakeSigner())) for i in range(2)]
    assert rows[0]["signature"].startswith("sig:") and (rows[0]["sig_alg"], rows[0]["key_id"]) == ("test-hmac", "k1")
    v = _verify(st, signer=_FakeSigner())
    assert v["ok"] and v["signed_records"] == 2
    v = _verify(st)  # NoopSigner cannot vouch for a signature
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 1, "signature_invalid")
    noop = ac.NoopSigner()
    assert noop.sign("h") is None and noop.verify("h", "s", None) is False


# ------------------------------------------------------------------ QA-045a: signatures mandatory with a real signer
def _signed(st, k=3, signer=None):
    return [st.append_chained("CRD", ac.GENESIS, ac.finalizer(_base(n=i), signer or _FakeSigner())) for i in range(k)]


@pytest.mark.parametrize("cols", [("signature", "sig_alg", "key_id"), ("signature",), ("sig_alg",), ("key_id",)])
@pytest.mark.parametrize("blank", [None, "", "  "])
@pytest.mark.parametrize("seq,reason", [(1, "signature_missing"), (2, "signature_downgrade"),
                                        (3, "signature_downgrade")])
def test_stripped_or_blank_signature_fields_fail_with_real_signer(cols, blank, seq, reason):
    st = _store()
    _signed(st)
    _unguard(st)
    st.execute(f"UPDATE review_signoff SET {', '.join(c + ' = %s' for c in cols)} WHERE chain_seq = %s",
               tuple([blank] * len(cols)) + (seq,))
    v = _verify(st, signer=_FakeSigner())
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, seq, reason)
    assert all(c in v["detail"] for c in cols) and v["signatures_required"] is True


def test_unsigned_chain_fails_under_real_signer():
    st = _store()
    _chain(st, k=2)  # written with the no-op default
    v = _verify(st, signer=_FakeSigner())
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 1, "signature_missing")


def test_downgrade_unsigned_append_after_signed_records_fails():
    st = _store()
    _signed(st, k=2)
    st.append_chained("CRD", ac.GENESIS, ac.finalizer(_base(n=9)))  # later record written with NoopSigner
    v = _verify(st, signer=_FakeSigner())
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 3, "signature_downgrade")
    assert "first signed seq 1" in v["detail"]


@pytest.mark.parametrize("col,val", [("signature", "sig:forged"), ("key_id", "k2"), ("sig_alg", "other-alg")])
def test_nonverifying_signature_metadata_fails(col, val):
    st = _store()
    _signed(st)
    _unguard(st)
    st.execute(f"UPDATE review_signoff SET {col} = %s WHERE chain_seq = 2", (val,))
    v = _verify(st, signer=_FakeSigner())
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "signature_invalid")


def test_real_signer_accepts_fully_signed_chain_and_noop_default_unchanged():
    st = _store()
    _signed(st)
    v = _verify(st, signer=_FakeSigner())
    assert v["ok"] and v["signed_records"] == 3 and v["signatures_required"] is True
    plain = _store()
    _chain(plain, k=3)
    for signer in (None, ac.NoopSigner()):
        v = _verify(plain, signer=signer)
        assert v["ok"] and v["signed_records"] == 0 and v["signatures_required"] is False
    _unguard(st)  # no-op default: a stripped record passes as before; a remaining signature still cannot be vouched for
    st.execute("UPDATE review_signoff SET signature = NULL, sig_alg = NULL, key_id = NULL WHERE chain_seq = 1")
    v = _verify(st)
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "signature_invalid")
    assert not ac.is_enforcing(None) and not ac.is_enforcing(ac.NoopSigner()) and ac.is_enforcing(_FakeSigner())


# ------------------------------------------------------------------ identity
def test_token_sub_edge_cases():
    assert ac.token_sub(_jwt({"sub": "abc-123"})) == "abc-123"
    assert ac.token_sub(_jwt({"sub": 42})) == "42"
    for bad in (None, "", "opaque-token", "a.b", "a.!!!.c", _jwt({"nosub": 1}), _jwt({"sub": ""}),
                _jwt(["sub"]), "x." + base64.urlsafe_b64encode(b"not json").decode() + ".y"):
        assert ac.token_sub(bad) is None, bad


# ------------------------------------------------------------------ endpoints
def _crd_event(c, user=ENG):
    al = c.get("/api/alarms?dam_id=CRD", user=user).json()["alarms"]
    return next(a for a in al if a["instrument_id"] == "CRD-PZ-2")


def _rec(rid, sid, **kw):
    base = {k: None for k in ct.INCL_CORRECTION_FIELDS}
    base.update(record_id=rid, dam_id="CRD", instrument_id=IID, survey_id=sid, axis="A", applied=False,
                timestamp_utc="2026-11-02T00:00:00Z", user="engine:reduce_inclinometers", before_value=12.5,
                after_value=11.9, parameter_name="c", auto_value=-0.6)
    return {**base, **kw}


@pytest.fixture()
def recs(c):
    sid = c.get(f"/api/instruments/{IID}/incl/surveys").json()["surveys"][-1]["survey_id"]
    an = c.tc.app.state.analytics_factory(None)
    stat = ("rule=QA021-v2;ZA=-6.0000;abs_dD=6.0000;abs_bs=6.0000;crosscheck_verdict=AGREE;status=PROPOSED;"
            "requires_review=True")
    an.d.setdefault("incl_corrections", []).extend([
        _rec("B1", sid, detection_method="between_set_bias_qc", detection_statistic=stat),
        _rec("D1", sid, correction_type="depth", detection_method="engineer_judgement", override_value=-0.5),
        _rec("D2", sid, correction_type="depth", detection_method="engineer_judgement", applied=True),
        _rec("D3", sid, correction_type="depth", detection_method="engineer_judgement", applied=True,
             reviewed_by="rev", reviewed_utc="2026-11-02T01:00:00Z"),
        _rec("D4", sid, correction_type="depth", detection_method="engineer_judgement", user=ENG)])
    return c, sid, an


def test_all_reviewer_actions_land_in_one_verifiable_dam_chain(recs):
    c, sid, an = recs
    st = c.tc.app.state.store
    r1 = c.post(f"/api/instruments/{IID}/incl/surveys/{sid}/bias-proposals/B1/review", user=ENG,
                json={"decision": "approve", "rationale": "AGREE across 6 sets"})
    r2 = c.post(f"/api/instruments/{IID}/incl/surveys/{sid}/corrections/D1/review", user=ENG,
                json={"decision": "reject", "rationale": "depth offset not supported by casing log"})
    eid = _crd_event(c)["event_id"]
    r3 = c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "spike after rainfall; checking"})
    r4 = c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "field check OK"})
    assert [r.status_code for r in (r1, r2, r3, r4)] == [200] * 4, [r.text for r in (r1, r2, r3, r4)]
    rows = st.chain_rows("CRD")
    assert [(r["chain_seq"], r["action_type"], r["decision"]) for r in rows] == [
        (1, "bias_review", "approve"), (2, "data_correction_review", "reject"), (3, "alarm_ack", "acknowledge"),
        (4, "alarm_close", "close")]
    # v1.6 ROLE-LEVEL: only the dam's named CDSE records CDSE; the platform admin's close is "engineer" (no elevation)
    assert [r["review_level"] for r in rows] == ["engineer", "engineer", "CDSE", "engineer"]
    assert all(r["reviewer_id"] and r["reviewer_email"] and r["signed_ts"].endswith("+00:00") for r in rows)
    assert r3.json()["state"] == "acknowledged" and r3.json()["chain_seq"] == 3
    assert r4.json()["state"] == "closed" and r4.json()["chain_seq"] == 4
    v = c.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()
    assert v["ok"] and v["records"] == 4 and v["scope"] == "per-dam" and v["genesis"] == ac.GENESIS
    allv = c.get("/api/audit/signoffs/verify", user=ADMIN).json()
    assert allv["ok"] and [x["chain_id"] for x in allv["chains"]] == ["CRD"]
    # tamper (privileged bypass) -> both endpoints report the first broken seq
    _unguard(st)
    st.execute("UPDATE review_signoff SET corrected_value = %s WHERE chain_seq = 2", ('{"state":"approved"}',))
    v = c.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 2, "hash_mismatch")
    assert c.get("/api/audit/signoffs/verify", user=ADMIN).json()["ok"] is False


def test_original_values_are_retained_never_overwritten(recs):
    c, sid, an = recs
    st = c.tc.app.state.store
    c.post(f"/api/instruments/{IID}/incl/surveys/{sid}/corrections/D1/review", user=ENG,
           json={"decision": "approve", "rationale": "offset confirmed by survey"})
    ev = _crd_event(c)
    c.post(f"/api/alarms/{ev['event_id']}/ack", user=CDSE, json={"rationale": "reviewed"})
    c.post(f"/api/alarms/{ev['event_id']}/close", user=ADMIN, json={"reason": "resolved"})
    rows = st.chain_rows("CRD")
    orig = json.loads(rows[0]["original_value"])
    corr = json.loads(rows[0]["corrected_value"])
    assert orig["before_value"] == 12.5 and corr["override_value"] == -0.5 and corr["after_value"] == 11.9
    assert corr["applied_by_app"] is False
    src = next(x for x in an.d["incl_corrections"] if x["record_id"] == "D1")
    assert src["before_value"] == 12.5 and src["applied"] is False and src.get("reviewed_by") is None
    ack_o, ack_n = json.loads(rows[1]["original_value"]), json.loads(rows[1]["corrected_value"])
    assert (ack_o["state"], ack_o["value"], ack_o["unit"], ack_o["level"]) == \
        ("open", ev["value"], ev["unit"], ev["level"])
    assert ack_n == {"state": "acknowledged", "measured_value_changed": False}
    close_o = json.loads(rows[2]["original_value"])
    assert close_o["state"] == "acknowledged" and close_o["value"] == ev["value"]
    # the earlier (ack) record still holds its original after the close: nothing was rewritten
    assert json.loads(st.chain_rows("CRD")[1]["original_value"]) == ack_o
    assert c.get(f"/api/alarms/{ev['event_id']}", user=ENG).json()["value"] == ev["value"]


@pytest.mark.parametrize("body", [None, {}, {"rationale": ""}, {"rationale": "   "}, {"comment": " \t "}])
def test_ack_requires_rationale(c, body):
    eid = _crd_event(c)["event_id"]
    kw = {} if body is None else {"json": body}
    assert c.post(f"/api/alarms/{eid}/ack", user=CDSE, **kw).status_code == 422
    assert c.tc.app.state.store.chain_rows("CRD") == []
    assert _crd_event(c)["state"] == "open"
    # the older `comment` field still works as the rationale
    assert c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"comment": "ok, checking"}).status_code == 200


def test_close_and_reviews_require_rationale(recs):
    c, sid, an = recs
    eid = _crd_event(c)["event_id"]
    c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "r"})
    assert c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "      "}).status_code == 422
    for path in ("bias-proposals/B1", "corrections/D1"):
        for r in ("", "   "):
            resp = c.post(f"/api/instruments/{IID}/incl/surveys/{sid}/{path}/review", user=ENG,
                          json={"decision": "approve", "rationale": r})
            assert resp.status_code == 422
    assert len(c.tc.app.state.store.chain_rows("CRD")) == 1  # only the ack


def test_correction_review_rules(recs):
    c, sid, an = recs
    url = f"/api/instruments/{IID}/incl/surveys/{sid}/corrections/%s/review"
    j = {"decision": "approve", "rationale": "ok"}
    assert c.post(url % "NOPE", user=ENG, json=j).status_code == 404
    assert c.post(f"/api/instruments/{IID}/incl/surveys/OTHER/corrections/D1/review", user=ENG, json=j).status_code == 404
    r = c.post(url % "D2", user=ENG, json=j)
    assert r.status_code == 409 and "data-integrity" in r.text
    assert c.post(url % "D3", user=ENG, json=j).status_code == 409   # already approved
    assert c.post(url % "D4", user=ENG, json=j).status_code == 403   # own proposal
    assert c.post(url % "D1", user=VIEWER, json=j).status_code == 403
    assert c.post(url % "D1", user=ENG, json={**j, "decision": "maybe"}).status_code == 422
    # a bias record reviewed via the generic route is still typed as a bias review
    assert c.post(url % "B1", user=ENG, json=j).json()["action_type"] == "bias_review"


def test_verify_endpoint_acl(c):
    assert c.get("/api/audit/signoffs/verify", user=ENG).status_code == 403
    allv = c.get("/api/audit/signoffs/verify", user=ADMIN).json()
    assert (allv["ok"], allv["chains"], allv["genesis"]) == (True, [], ac.GENESIS)
    assert c.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()["ok"] is True
    assert c.get("/api/dams/CRD/audit/signoffs/verify", user=VIEWER).status_code == 403
    assert c.get("/api/dams/CRD/audit/signoffs/verify", user=DSO).status_code == 403
    assert c.get("/api/dams/BFK/audit/signoffs/verify", user=VIEWER).status_code == 403  # viewer < engineer
    assert c.get("/api/dams/BFK/audit/signoffs/verify", user=ENG).status_code == 403
    assert c.get("/api/dams/BFK/audit/signoffs/verify", user=ADMIN).status_code == 200


def test_token_is_never_logged_stored_or_returned(c, caplog):
    caplog.set_level(logging.DEBUG)
    tok = _jwt({"sub": "sso-sub-777", "email": ENG})
    h = {"x-forwarded-user": f"uid-{ENG}", "x-forwarded-email": ENG, "x-forwarded-access-token": tok}
    # ENG's own (level-1, engineer-ackable) alarm on CRD
    eid = next(a for a in c.get("/api/alarms?dam_id=CRD", user=ENG).json()["alarms"] if a["level"] == 1)["event_id"]
    r = c.tc.post(f"/api/alarms/{eid}/ack", headers=h, json={"rationale": "checking"})
    assert r.status_code == 200
    v = c.tc.get("/api/dams/CRD/audit/signoffs/verify", headers=h)
    st = c.tc.app.state.store
    rows = st.chain_rows("CRD")
    assert rows[0]["reviewer_sub"] == "sso-sub-777" and rows[0]["reviewer_id"] == f"uid-{ENG}"
    secret = tok.split(".")[2]
    dump = json.dumps([st.execute(f"SELECT * FROM {t}") for t in
                       ("review_signoff", "review_chain_head", "audit_log", "alarm_ack", "alarm_comment")], default=str)
    for blob in (dump, r.text, v.text, caplog.text, repr(rows)):
        assert tok not in blob and secret not in blob and tok.split(".")[1] not in blob


def test_state_race_inside_transaction_rolls_back(c, monkeypatch):
    """The workflow state is re-checked inside the append transaction: if another instance changed it after the
    pre-check, the request gets 409 and no chain record is written."""
    st = c.tc.app.state.store
    eid = _crd_event(c)["event_id"]
    assert c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "first"}).status_code == 200
    real = st.ack_states
    monkeypatch.setattr(st, "ack_states", lambda ids: {i: {**r, "state": "open"} for i, r in real(ids).items()})
    assert c.post(f"/api/alarms/{eid}/ack", user=CDSE, json={"rationale": "second"}).status_code == 409
    monkeypatch.undo()
    assert c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "done"}).status_code == 200
    monkeypatch.setattr(st, "ack_states", lambda ids: {i: {**r, "state": "acknowledged"} for i, r in real(ids).items()})
    assert c.post(f"/api/alarms/{eid}/close", user=ADMIN, json={"reason": "again"}).status_code == 409
    assert [r["action_type"] for r in st.chain_rows("CRD")] == ["alarm_ack", "alarm_close"]
    assert c.get("/api/dams/CRD/audit/signoffs/verify", user=ENG).json()["ok"]


# ------------------------------------------------------------------ v1.6 interop with the engine verifier
def test_app_hash_and_verify_match_engine_v16_vector():
    """The DA's tests/fixtures/review_chain_vector.json (v1.6, engine dam_safety.audit_chain) was generated with this
    module; the QA-045a signature rules must not change record_hash or the no-op verification of an unsigned chain."""
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "review_chain_vector.json"
    vec = json.loads(path.read_text(encoding="utf-8"))
    assert set(ac.CHAIN_FIELDS) | set(ac.HASH_EXCLUDED) == set(vec["rows"][0])
    for r in vec["rows"]:
        assert ac.record_hash(r) == r["record_hash"], r["review_id"]
    assert ac.verify_chain(vec["rows"], vec["head"])["ok"]
    v = ac.verify_chain(vec["rows"], vec["head"], _FakeSigner())  # unsigned UC rows never pass a real signer
    assert (v["ok"], v["first_broken_seq"], v["reason"]) == (False, 1, "signature_missing")


# ------------------------------------------------------------------ QA-045c: app verify_chain == engine verify_records
def _engine_ac():
    """The engine verifier (src/dam_safety/audit_chain.py), imported from the repo's src/ for this check only."""
    from pathlib import Path
    src = str(Path(__file__).resolve().parents[2] / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    import importlib
    return importlib.import_module("dam_safety.audit_chain")


class _VecHmacSigner:
    """TEST ONLY: the public HMAC key stored in tests/fixtures/review_chain_signed_vector.json (not a secret)."""
    def __init__(self, spec):
        self.alg, self.key_id, self._key = spec["alg"], spec["key_id"], spec["key_utf8"].encode("utf-8")

    def sign(self, record_hash):
        import hashlib
        import hmac
        return hmac.new(self._key, record_hash.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, record_hash, signature, key_id):
        import hmac
        return key_id == self.key_id and hmac.compare_digest(str(signature), self.sign(record_hash))


def _svec():
    from pathlib import Path
    return json.loads((Path(__file__).resolve().parents[2] / "tests" / "fixtures" /
                       "review_chain_signed_vector.json").read_text(encoding="utf-8"))


SVEC_CASES = sorted(_svec()["cases"])


@pytest.mark.parametrize("with_signer", [True, False], ids=["signer", "noop"])
@pytest.mark.parametrize("case", SVEC_CASES)
def test_app_and_engine_verifiers_agree_on_signed_vector(case, with_signer):
    vec = _svec()
    eng = _engine_ac()
    c = vec["cases"][case]
    signer = _VecHmacSigner(vec["signer"]) if with_signer else None
    verdict = lambda v: {"ok": v["ok"], "reason": v["reason"], "first_broken_seq": v["first_broken_seq"]}  # noqa: E731
    app_v = verdict(ac.verify_chain(copy.deepcopy(c["rows"]), c["head"], signer))
    eng_v = verdict(eng.verify_records(copy.deepcopy(c["rows"]), signer, c["head"]))
    assert app_v == eng_v == c["expected" if with_signer else "expected_noop_signer"]
    if case == "intact":
        assert all(ac.record_hash(r) == eng.record_hash(r) == r["record_hash"] for r in c["rows"])
