import contextlib
import importlib
import logging
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import formulas as fx
from backend import seed
from backend.common import hours_since, merge_ack
from backend.config import Settings
from backend.deps import Ctx
from backend.repos import analytics as an_mod
from backend.repos import state as st_mod
from backend.verify import INCL_KEYS, Gate

ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ verify gate
def _gate():
    return Gate([{"formula_id": "F", "formula_version": "1.0.0", "verified": True},
                 {"formula_id": "U", "formula_version": "1.0.0", "verified": False}],
                [{"calib_id": "C1", "approved_by": "eor"}, {"calib_id": "C2", "approved_by": None}])


@pytest.mark.parametrize("row,status", [
    ({"formula_id": "F", "formula_version": "1.0.0", "calib_id": "C1", "qa_flag": "OK"}, "VERIFIED"),
    ({"formula_id": "F", "formula_version": "1.0.0", "calib_id": "C1", "qa_flag": "SUSPECT"}, "VERIFIED"),
    ({"formula_id": "F", "formula_version": "1.0.0", "calib_id": "C1", "qa_flag": "bad"}, "QC_BAD"),
    ({"formula_id": "F", "formula_version": "1.0.0", "calib_id": "C1", "qa_flag": "MISSING"}, "MISSING"),
    ({"formula_id": "F", "formula_version": "1.0.0", "calib_id": "C2", "qa_flag": "OK"}, "UNAPPROVED_CALIBRATION"),
    ({"formula_id": "F", "formula_version": "1.0.0", "calib_id": None, "qa_flag": "OK"}, "UNAPPROVED_CALIBRATION"),
    ({"formula_id": "U", "formula_version": "1.0.0", "calib_id": "C1", "qa_flag": "OK"}, "UNVERIFIED_FORMULA"),
    ({"formula_id": "F", "formula_version": "2.0.0", "calib_id": "C1", "qa_flag": "OK"}, "UNVERIFIED_FORMULA"),
    ({"formula_id": None, "formula_version": None, "calib_id": "C1"}, "UNVERIFIED_FORMULA"),
])
def test_gate_status(row, status):
    r = _gate().apply({**row, "eng_value": 1.0, "corrected_value": 2.0, "raw_value": 3.0})
    assert r["display_status"] == status and r["raw_value"] == 3.0
    if status == "VERIFIED":
        assert r["eng_value"] == 1.0 and r["corrected_value"] == 2.0 and r["verified"]
    else:
        assert r["eng_value"] is None and r["corrected_value"] is None and not r["verified"]


def test_gate_incl_keys_and_events():
    g = _gate()
    r = g.apply_all([{"formula_id": "U", "formula_version": "1.0.0", "calib_id": "C1", "a_cum_mm": 5.0}],
                    INCL_KEYS)[0]
    assert r["a_cum_mm"] is None
    assert "proposed_bias" not in INCL_KEYS        # suppressed field, not merely gated (QA-020 decision)
    base = {"formula_id": "F", "formula_version": "1.0.0", "calib_id": "C1", "qa_flag": "OK", "value": 9.0}
    assert g.apply_event(dict(base))["value"] == 9.0
    assert g.apply_event({**base, "calib_id": "C2"})["display_status"] == "UNAPPROVED_CALIBRATION"
    # no calibration lineage on the event -> withheld (accuracy over speed)
    assert g.apply_event({**base, "calib_id": None})["display_status"] == "LINEAGE_INCOMPLETE"
    e = g.apply_event({**base, "qa_flag": "BAD", "message": "X 9.0 mm > L1 limit 8.0 mm", "level": 1,
                       "rule": "value_above", "instrument_id": "X"})
    assert e["value"] is None and e["display_status"] == "QC_BAD"
    assert "9.0" not in e["message"] and "withheld" in e["message"]
    assert g.apply_event({**base, "message": "keep 9.0"})["message"] == "keep 9.0"
    assert g.apply_event({**base, "formula_id": None})["value"] is None


# ------------------------------------------------------------------ formulas
def test_all_registered_vectors_pass_except_unsourced():
    # SETTLEMENT and LEVEL_DATUM have no published source (engine: LEVEL_DATUM verified=False) -> unverified.
    assert fx.verified_formulas() == {k for k in fx.REGISTRY if k[0] not in ("SETTLEMENT", "LEVEL_DATUM")}
    lv = fx.run_vectors("LEVEL_DATUM", "1.0.0")
    assert lv["verified"] is False and lv["results"] == []
    assert ("SETTLEMENT_DELTA", "1.0.0") in fx.verified_formulas()
    assert fx.run_vectors("SETTLEMENT", "1.0.0")["verified"] is False
    assert fx.run_vectors("NOPE", "1")["known"] is False


def test_user_vectors_and_errors():
    r = fx.run_vectors("LEVEL_DATUM", "1.0.0", [{"inputs": {"datum_elev_m": 1, "reading_m": 1}, "expected": 3}])
    assert r["verified"] is False and r["results"][-1]["pass"] is False
    r = fx.run_vectors("LEVEL_DATUM", "1.0.0", [{"inputs": {}, "expected": 3}])
    assert r["results"][-1]["error"].startswith("KeyError")
    assert fx.get_formula("VW_PIEZO", 1) is None  # versions are strings ("1.0.0"), like the engine


def test_vw_piezo_paths():
    p = {"G": -0.1795, "R0": 9082, "K": 0.1, "T0": 20, "tip_elev_m": 100}
    o = fx.vw_piezo(p, 6749, T1=25)
    assert o["pressure_kpa"] == pytest.approx(418.8 + 0.5, abs=0.05) and o["qa_flag"] == "OK"
    assert o["piezometric_elev_m"] == pytest.approx(100 + o["pressure_kpa"] / fx.GAMMA_W_KPA_PER_M)
    o = fx.vw_piezo({"method": "poly", "A": -7.113e-7, "B": -0.1694, "R0": 9082}, 6749)
    assert o["pressure_kpa"] == pytest.approx(421.5, abs=0.05) and "piezometric_elev_m" not in o
    o = fx.vw_piezo({"G": -0.1795, "R0": 9082, "vented": False, "S0": 101.3, "F": 1.0}, 6749)
    assert o["qa_flag"] == "SUSPECT" and o["qa_reason"] == "NO_BARO"
    o = fx.vw_piezo({"G": -0.1795, "R0": 9082, "vented": False, "S0": 101.3}, 6749, S1=103.3)
    assert o["pressure_kpa"] == pytest.approx(418.8 - 2.0, abs=0.05)
    o = fx.vw_piezo({"G": 0.1795, "R0": 9082}, 6749)
    assert "BELOW_TIP" in o["qa_reason"]


def test_other_formulas():
    o = fx.standpipe_dtw({"top_elev_m": 200, "tip_elev_m": 190}, 12)
    assert o["qa_reason"] == "DRY" and o["piezometric_elev_m"] == 188
    assert fx.standpipe_dtw({"top_elev_m": 200, "tip_elev_m": 150}, -1)["qa_reason"] == "NEG_DEPTH"
    assert fx.weir_vnotch_90_cfs(0) == 0.0
    assert fx.weir_vnotch_90_lps(0.3048) == pytest.approx(2.49 * 28.316846592)
    assert fx.vw_disp({"G": 0.01211, "R0": 4200, "K": 0.01, "T0": 20}, 6000, 30) == pytest.approx(21.798 + 0.1)
    assert fx.vw_disp({"method": "poly", "A": 4.1548e-8, "B": 0.01173, "R0": 4200}, 6000) == pytest.approx(21.88, abs=0.005)
    assert fx.digits_from_hz(2828) == pytest.approx(7997.6, abs=0.1)
    assert fx.digits_from_period_us(354) == pytest.approx(7980, abs=1)
    assert fx.thermistor_3k_c(3000) == pytest.approx(25.0, abs=0.1)
    assert fx.inc_checksum(359, -339) == 20


def test_incl_reduction_checksum_and_stability():
    base = {0.5: (0.0, 0.0), 1.0: (0.0, 0.0), 1.5: (0.0, 0.0)}
    rows = [{"depth_m": 0.5, "a0": 110, "a180": -90, "b0": 0, "b180": 0},
            {"depth_m": 1.0, "a0": 10, "a180": -10, "b0": 0, "b180": 0},
            {"depth_m": 1.5, "a0": 50, "a180": -30, "b0": 0, "b180": 0}]
    prof = fx.reduce_inclinometer_profile(rows, base, 500.0, 25000.0, stable_from_m=1.0, chk_tol=5)
    assert [r["depth_m"] for r in prof] == [0.5, 1.0, 1.5]
    assert prof[0]["a_cum_mm"] == pytest.approx(sum(r["a_inc_mm"] for r in prof))
    # checksums 20, 0, 20 -> median 20; the 1.0 m row deviates by 20 > tol 5
    assert [r["qc_checksum_ok"] for r in prof] == [True, False, True]
    # internal-only proposal (kept for QA reference tests; never surfaced by the app)
    assert prof[0]["proposed_bias"]["a_bias_mm_per_interval"] == pytest.approx((prof[1]["a_inc_mm"] + prof[2]["a_inc_mm"]) / 2)
    assert all(r["qc_checksum_ok"] for r in fx.reduce_inclinometer_profile(rows, base, 500.0, 25000.0))


# -------------------------------------------------------------- parity / oracle
def _load(path: Path, name: str):
    if not path.exists():
        pytest.skip(f"{path} not present")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_qa_reference_oracle():
    qa = _load(ROOT / "qa" / "reference_examples.py", "qa_ref")
    case = {c[0]: c for c in qa.CASES}
    assert fx.vw_linear(-0.1795, 6749, 9082) == pytest.approx(case["PZ-03 linear P=G(R1-R0)"][3], abs=0.05)
    assert fx.vw_poly(-7.113e-7, -0.1694, 6749, 9082) == pytest.approx(case["PZ-06 polynomial P"][3], abs=0.05)
    assert fx.vw_poly_C(-7.113e-7, -0.1694, 9082) == pytest.approx(case["PZ-05 polynomial C"][3], abs=0.05)
    assert fx.thermistor_3k_c(9796) == pytest.approx(0.0, abs=0.1)
    assert fx.inc_deviation(24, 359, -339, 20000) == pytest.approx(0.4188, abs=5e-5)
    assert fx.vw_linear(0.01211, 6000, 4200) == pytest.approx(21.79, abs=0.01)
    assert fx.vw_poly(4.1548e-8, 0.01173, 6000, 4200) == pytest.approx(21.88, abs=0.005)
    assert fx.digits_from_period_us(354) == pytest.approx(8000, abs=25)
    assert fx.digits_from_hz(2828) == pytest.approx(8000, abs=5)
    assert 1.0 / fx.GAMMA_W_KPA_PER_M == pytest.approx(0.10197, abs=1e-4)


def test_parity_with_engine_registry():
    src = ROOT / "src"
    if not (src / "dam_safety" / "formulas" / "registry.py").exists():
        pytest.skip("engine not present")
    sys.path.insert(0, str(src))
    try:
        reg = importlib.import_module("dam_safety.formulas.registry")
    finally:
        sys.path.remove(str(src))
    for (fid, ver), f in reg.REGISTRY.items():
        assert fx.get_formula(fid, ver) is not None, f"app lacks engine formula {fid} {ver}"
        assert f.verified == fx.run_vectors(fid, ver)["verified"]
        for tv in f.test_vectors:
            if fid == "VW_PIEZO":
                p = tv["params"]
                got = fx.vw_piezo({**p}, tv["reading"]["raw_value"])["pressure_kpa"]
                assert got == pytest.approx(tv["expect"]["eng_value"], abs=tv["tol"])
                eng = reg.apply_formula(tv["reading"], {"formula_id": fid, "formula_version": ver, "params": p})
                assert got == pytest.approx(eng["eng_value"], abs=1e-6)
            elif fid == "STANDPIPE_DTW":
                eng = reg.apply_formula(tv["reading"], {"formula_id": fid, "formula_version": ver,
                                                        "params": tv["params"]})
                got = fx.standpipe_dtw({"top_elev_m": tv["params"]["top_el_m"], "tip_elev_m": -1e9},
                                       tv["reading"]["raw_value"])["piezometric_elev_m"]
                assert got == pytest.approx(eng["corrected_value"], abs=1e-9)
            elif fid == "SETTLEMENT_DELTA":
                raw = tv["raw"]
                got = [fx.REGISTRY[(fid, ver)].fn({"value": v, "value0": raw[0], "sign_mult": tv["sign_mult"]})
                       for v in raw]
                assert got == pytest.approx(tv["expect"], abs=tv["tol"])
            elif fid == "INCL_PROBE_2PASS":
                assert fx.inc_deviation(tv["L"], tv["a0"], tv["a180"], tv["K"]) == pytest.approx(tv["expect"]["dev"], abs=tv["tol"])
                assert fx.inc_checksum(tv["a0"], tv["a180"]) == tv["expect"]["checksum"]


# ------------------------------------------------------------------ seed
def test_seed_dataset():
    d = seed.build()
    assert [s["dam_id"] for s in d["sites"]] == ["CRD", "BFK", "HLD", "PHR", "STC"]
    for r in d["readings"]:
        assert r["formula_version"] == "1.0.0" or r["formula_id"] is None
    assert sum(r["qa_flag"] == "BAD" for r in d["readings"]) == 1
    assert {c["calib_id"] for c in d["calibrations"] if not c["approved_by"]} == {"STC-PZ-3-C1"}
    bfk = [c for c in d["calibrations"] if c["instrument_id"] == "BFK-PZ-1"]
    assert [c["is_current"] for c in bfk] == [False, True] and bfk[0]["effective_to"] == bfk[1]["effective_from"]
    for e in d["alarm_events"]:  # BAD/MISSING never drive alarms
        assert e["qa_flag"] in (None, "OK", "SUSPECT")


# ------------------------------------------------------------------ helpers
def test_common_helpers():
    assert hours_since(None) is None
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert hours_since("2026-01-01T00:00:00Z", now) == 24
    ev = merge_ack([{"event_id": "a", "level": 2}, {"event_id": "b", "level": 9}],
                   {"a": {"state": "closed", "acknowledged_by": "x", "acknowledged_at": "t", "closed_by": "y",
                          "closed_at": "t2", "close_reason": "r"}})
    assert ev[0]["state"] == "closed" and ev[0]["level_name"] == "Evaluate"
    assert ev[1]["state"] == "open" and ev[1]["acknowledged_by"] is None


def test_ctx():
    c = Ctx(email="a", token=None, is_admin=False, roles={"X": "engineer"})
    assert c.can("X", "viewer") and c.can("X", "engineer") and not c.can("X", "dso") and not c.can("Y")
    assert c.dams(["X", "Y"]) == ["X"]
    a = Ctx(email="b", token=None, is_admin=True, roles={})
    assert a.role("Z") == "dam_admin" and a.dams(["X", "Y"]) == ["X", "Y"]


# ------------------------------------------------------------------ state store
@pytest.fixture()
def st():
    return st_mod.StateStore("sqlite")


def test_state_users_roles(st):
    st.upsert_user("A@x.org", "A", False, "t")
    with pytest.raises(ValueError):
        st.set_role("a@x.org", "CRD", "boss", "t")
    st.set_role("a@x.org", "CRD", "engineer", "t")
    st.set_role("a@x.org", "CRD", "dso", "t")
    assert st.roles_for("a@x.org") == {"CRD": "dso"}
    assert st.users_for_dam("CRD")[0]["role"] == "dso"
    st.set_role("a@x.org", "CRD", None, "t")
    assert st.roles_for("a@x.org") == {}
    st.set_role("a@x.org", "BFK", "viewer", "t")
    st.deactivate_user("a@x.org")
    assert st.get_user("a@x.org")["active"] is False and st.roles_for("a@x.org") == {}
    assert st.get_user("nobody") is None


def test_state_alarms(st):
    with pytest.raises(ValueError):
        st.close_alarm("E1", "CRD", "u", "r")
    st.acknowledge("E1", "CRD", "u")
    with pytest.raises(ValueError):
        st.acknowledge("E1", "CRD", "u")
    st.close_alarm("E1", "CRD", "v", "reason")
    with pytest.raises(ValueError):
        st.close_alarm("E1", "CRD", "v", "reason")
    s = st.ack_states(["E1", "E2"])
    assert s["E1"]["state"] == "closed" and "E2" not in s
    assert st.ack_states([]) == {}


def test_state_drafts_reports_audit(st):
    d = st.create_draft("u", {"details": {"dam_id": "N"}})
    assert d["proposed_dam_id"] == "N" and st.list_drafts("u")[0]["data"]["details"]["dam_id"] == "N"
    assert st.list_drafts("other") == [] and len(st.list_drafts()) == 1
    assert st.get_draft("nope") is None
    st.update_draft(d["draft_id"], {"x": 1}, "published")
    assert st.get_draft(d["draft_id"])["status"] == "published"
    pub = st.publish(d["draft_id"], "N", {"a": 1}, "u")
    assert st.publications()[0]["pub_id"] == pub["pub_id"]
    rep = st.save_report("N", "s", "e", "u", "# md")
    assert st.get_report(rep["report_id"])["content_md"] == "# md" and st.get_report("x") is None
    st.audit("u", "act", "N", "t", {"k": 1})
    st.audit("u", "act2")
    assert st.audit_entries("N")[0]["detail"] == {"k": 1}
    assert st.audit_entries()[0]["detail"] == {}


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.conn.log.append(sql)
        if self.conn.fail:
            e = self.conn.fail.pop(0)
            raise e
        self.description = [("x",)] if sql.startswith("SELECT") else None
        # QA-058: answer the startup probe like a provisioned Lakebase (owner-owned table, ENABLE ALWAYS trigger)
        self._rows = ([{"owner": "dam_lakebase_owner"}] if "pg_get_userbyid(c.relowner)" in sql else
                      [{"tgenabled": "A"}] if "tgenabled" in sql else [{"x": 1}])

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, log, fail):
        self.log, self.fail, self.closed = log, fail, False

    def cursor(self):
        return FakeCursor(self)


class FakePool:
    """Stands in for psycopg_pool.ConnectionPool: connection() is a context manager."""

    def __init__(self, conn):
        self.conn, self.borrowed = conn, 0

    @contextlib.contextmanager
    def connection(self):
        self.borrowed += 1
        yield self.conn


class OperationalError(Exception):
    pass


def test_state_postgres_path(monkeypatch):
    sleeps = []
    monkeypatch.setattr(st_mod.time, "sleep", sleeps.append)
    log, fail = [], []
    pool = FakePool(FakeConn(log, fail))
    st = st_mod.StateStore("postgres", schema="dam_app", pg_pool=pool, retries=3)
    assert log[0] == "SELECT pg_advisory_xact_lock(%s)"            # QA-045c: init = one locked transaction
    assert log[1].startswith("SELECT NOT pg_has_role(current_user, nspowner")    # v1.7: schema owner probe
    assert log[2] == 'CREATE SCHEMA IF NOT EXISTS "dam_app"'
    assert not any(x.startswith("DROP TRIGGER") for x in log)    # CREATE OR REPLACE TRIGGER, no unguarded window
    assert any('"dam_app".app_user' in s for s in log) and any("BIGSERIAL" in s for s in log)
    # QA-058: no DDL on the owner's rule_reversion_proposal; its owner and transition trigger are verified instead
    assert not any("rule_reversion_proposal" in s for s in log if not s.startswith("SELECT"))
    assert any("pg_get_userbyid(c.relowner)" in s for s in log) and any("tgenabled" in s for s in log)
    assert st.execute("SELECT 1") == [{"x": 1}]
    # scale-to-zero wake-up / HA failover: retried with backoff on a fresh pooled connection
    fail.extend([OperationalError("server closed the connection"), OperationalError("terminating connection")])
    n = pool.borrowed
    assert st.execute("SELECT 1") == [{"x": 1}] and pool.borrowed == n + 3 and sleeps == [0.25, 0.5]
    fail.append(ValueError("syntax"))  # not a connection error: no retry
    with pytest.raises(ValueError):
        st.execute("SELECT 1")
    fail.extend([OperationalError("down")] * 3)  # retries exhausted
    with pytest.raises(OperationalError):
        st.execute("SELECT 1")
    assert st_mod.StateStore("postgres", pg_pool=FakePool(FakeConn([], []))).prefix == ""
    with pytest.raises(ValueError, match="pg_pool"):
        st_mod.StateStore("postgres")


class FakeCred:
    def __init__(self, token, expire_time):
        self.token, self.expire_time = token, expire_time


class FakeW:
    def __init__(self, creds):
        self.calls, self._creds = [], list(creds)
        self.postgres = SimpleNamespace(generate_database_credential=self._gen)

    def _gen(self, endpoint):
        self.calls.append(endpoint)
        return self._creds.pop(0)


EP = "projects/dam-safety-dev/branches/production/endpoints/primary"


def test_lakebase_credentials_refresh_from_expire_time():
    from google.protobuf.timestamp_pb2 import Timestamp
    now = {"t": 1_000_000.0}
    ts = Timestamp(seconds=1_000_000 + 3600)
    w = FakeW([FakeCred("tok1", ts), FakeCred("tok2", "1970-01-12T14:46:40Z"), FakeCred("tok3", None),
               FakeCred("", None)])
    c = st_mod.LakebaseCredentials(EP, client=w, clock=lambda: now["t"])
    assert c.token() == "tok1" and c.token() == "tok1" and w.calls == [EP]      # cached
    now["t"] += 3600 - 299                                                     # inside 5-min margin
    assert c.token() == "tok2" and len(w.calls) == 2                           # re-minted
    # RFC 3339 text expire_time = 1_000_000 + 0 -> already expired -> next call re-mints
    assert c.token() == "tok3" and len(w.calls) == 3
    assert c._exp == now["t"] + st_mod.DEFAULT_TOKEN_LIFETIME_S              # missing expire_time -> 1 h
    c.invalidate()
    with pytest.raises(RuntimeError, match="no token"):
        c.token()
    assert st_mod._expire_epoch(datetime(2026, 1, 1), 0) == datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    assert st_mod._expire_epoch("garbage", 5.0) == 5.0 + 3600
    for bad in ("", "dam-safety-dev", "projects/p/branches/b"):
        with pytest.raises(ValueError, match="ENDPOINT_NAME"):
            st_mod.LakebaseCredentials(bad)


def test_lakebase_credentials_never_uses_workspace_oauth_token(monkeypatch):
    import databricks.sdk as sdk

    class W(FakeW):
        def __init__(self):
            super().__init__([FakeCred("db-cred", None)])
            self.config = SimpleNamespace(oauth_token=lambda: pytest.fail("workspace OAuth token used"))

    monkeypatch.setattr(sdk, "WorkspaceClient", W)
    assert st_mod.LakebaseCredentials(EP).token() == "db-cred"


def test_lakebase_conninfo_uses_injected_env():
    env = {"PGHOST": "ep-1.db", "PGDATABASE": "databricks_postgres", "PGUSER": "sp-uuid", "PGPORT": "5433",
           "PGSSLMODE": "verify-full", "PGAPPNAME": "dam-safety-prod"}
    kw = st_mod.lakebase_conninfo(env)
    assert kw["host"] == "ep-1.db" and kw["port"] == 5433 and kw["user"] == "sp-uuid"
    assert kw["sslmode"] == "verify-full" and kw["application_name"] == "dam-safety-prod"
    assert kw["keepalives"] == 1 and kw["keepalives_idle"] == 30 and "password" not in kw
    d = st_mod.lakebase_conninfo({"PGHOST": "h", "PGDATABASE": "d", "PGUSER": "u"})
    assert d["port"] == 5432 and d["sslmode"] == "require" and d["application_name"] == "dam-safety-app"
    with pytest.raises(RuntimeError, match="PGHOST, PGUSER"):
        st_mod.lakebase_conninfo({"PGDATABASE": "d"})


def test_lakebase_pool_mints_credential_per_new_connection(monkeypatch):
    import psycopg
    import psycopg_pool
    env = {"PGHOST": "h", "PGDATABASE": "db", "PGUSER": "sp", "ENDPOINT_NAME": EP}
    w = FakeW([FakeCred("t1", None), FakeCred("t2", None)])
    creds = st_mod.LakebaseCredentials(EP, client=w)
    made = {}

    class Pool:
        check_connection = psycopg_pool.ConnectionPool.check_connection

        def __init__(self, **kw):
            made.update(kw)

    st_mod.lakebase_pool(env=env, creds=creds, pool_cls=Pool)
    assert made["kwargs"]["host"] == "h" and made["kwargs"]["keepalives"] == 1 and "password" not in made["kwargs"]
    assert made["check"] is Pool.check_connection and made["open"] is True
    cls = made["connection_class"]
    assert issubclass(cls, psycopg.Connection)
    seen = []
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(lambda c, ci="", **kw: seen.append(kw) or "conn"))
    assert cls.connect("", host="h") == "conn" and seen[-1]["password"] == "t1"

    def refuse(c, ci="", **kw):
        raise psycopg.OperationalError("password authentication failed")

    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(refuse))
    with pytest.raises(psycopg.OperationalError):
        cls.connect("")
    assert creds._tok is None  # invalidated so the next connection re-mints
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(lambda c, ci="", **kw: seen.append(kw) or "c2"))
    assert cls.connect("") == "c2" and seen[-1]["password"] == "t2"
    # default path: real psycopg_pool class + ENDPOINT_NAME from env (pool not opened against a server)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", Pool)
    st_mod.lakebase_pool(env=env)
    assert made["name"] == "dam-lakebase"


def test_lakebase_logs_never_contain_token(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(st_mod.time, "sleep", lambda s: None)
    w = FakeW([FakeCred("SECRET-DB-TOKEN", None)])
    st_mod.LakebaseCredentials(EP, client=w).token()
    log_, fail = [], [OperationalError("closed SECRET?")]
    st = st_mod.StateStore("postgres", pg_pool=FakePool(FakeConn(log_, [])))
    st._pool.conn.fail.extend(fail)
    st.execute("SELECT 1")
    assert caplog.records and "SECRET-DB-TOKEN" not in caplog.text


# ------------------------------------------------------------------ SQL analytics
class SqlCur:
    def __init__(self, rec, rows):
        self.rec, self.rows = rec, rows
        self.description = [("a",), ("ts",), ("params",), ("d",)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.rec.append((sql, params))

    def fetchall(self):
        return self.rows


class SqlConn(SqlCur):
    def cursor(self):
        return SqlCur(self.rec, self.rows)


@pytest.fixture()
def sql(monkeypatch):
    rec, kw = [], {}
    rows = [("x", datetime(2026, 1, 1, 6), [("G", 1.0)], Decimal("1.5"))]
    from databricks import sql as dbsql
    monkeypatch.setattr(dbsql, "connect", lambda **k: kw.update(k) or SqlConn(rec, rows))
    from databricks.sdk import core
    monkeypatch.setattr(core, "Config", lambda: SimpleNamespace(host="https://ws.example.com/", authenticate=lambda: {}))
    return rec, kw


def test_sql_analytics_obo_and_queries(sql):
    rec, kw = sql
    s = Settings(demo_mode=False, warehouse_id="wh1", catalog="dam")
    a = an_mod.SqlAnalytics(s, "user-token")
    rows = a.sites()
    assert kw["access_token"] == "user-token" and kw["server_hostname"] == "ws.example.com"
    assert kw["http_path"] == "/sql/1.0/warehouses/wh1"
    assert rows == [{"a": "x", "ts": "2026-01-01T06:00:00Z", "params": {"G": 1.0}, "d": 1.5}]
    assert "`dam`.`ref`.`site`" in rec[-1][0]
    a.structures("CRD"); a.instruments("CRD"); a.calibrations("I"); a.calibrations_for_dam("CRD")
    a.formulas(); a.thresholds("CRD"); a.thresholds("CRD", "I"); a.latest_readings("CRD")
    a.readings("I", None, "2026-01-01", limit=5); a.incl_surveys("I"); a.incl_profile("I", "S"); a.pfms("CRD")
    assert a.instrument("I")["a"] == "x" and a.alarm_event("E")["a"] == "x"
    assert a.alarm_events([]) == []
    a.alarm_events(["CRD", "HLD"], since="2026-01-01")
    sqls = " ".join(r[0] for r in rec)
    for t in ("gold`.`instrument_latest", "ops`.`reading", "ops`.`alarm_episode", "ref`.`pfm_instrument", "silver`.`incl_reduced",
              "ref`.`threshold", "ref`.`pfm"):
        assert t in sqls
    assert rec[-1][1]["dams"] == '["CRD", "HLD"]'
    assert all("{" not in r[0] for r in rec)  # every value is a bound parameter
    assert any(r[1].get("lim") == 5 and r[1]["start"] is None for r in rec)


def test_sql_analytics_requires_user_token(sql):
    """§6 / §7.3: gold is read only on behalf of the signed-in user; no service-principal fallback."""
    from fastapi import HTTPException
    rec, kw = sql
    with pytest.raises(HTTPException) as e:
        an_mod.SqlAnalytics(Settings(demo_mode=False, warehouse_id="w"), None).sites()
    assert e.value.status_code == 401 and "credentials_provider" not in kw


def test_mock_analytics_edges():
    m = an_mod.MockAnalytics(seed.build())
    assert m.instrument("nope") is None and m.alarm_event("nope") is None
    assert m.incl_profile("CRD-IN-1", "nope") == []
    sid = m.incl_surveys("CRD-IN-1")[-1]["survey_id"]
    assert m.incl_profile("CRD-IN-1", sid) == m.incl_profile("CRD-IN-1", sid)  # cached
    # seed calibration has stable_from_m=24.0, but the app must not compute or return a bias proposal
    assert all("proposed_bias" not in r for r in m.incl_profile("CRD-IN-1", sid))
    assert m.alarm_events(["CRD"], since="2999-01-01") == []
    assert len(m.readings("CRD-PZ-2", None, None, limit=3)) == 3


# ------------------------------------------------------------------ main / SPA
def test_spa_serving(tmp_path):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets" / "a.js").write_text("js")
    (tmp_path / "favicon.svg").write_text("svg")
    c = TestClient(create_app(Settings(demo_mode=True, static_dir=str(tmp_path))))
    assert c.get("/").text == "<html>app</html>"
    assert c.get("/dams/CRD").text == "<html>app</html>"  # client-side route
    assert c.get("/assets/a.js").text == "js" and c.get("/favicon.svg").text == "svg"
    assert c.get("/api/does-not-exist").status_code == 404
    assert c.get("/../../etc/passwd").text == "<html>app</html>"


def test_prod_app_wiring(monkeypatch):
    from backend import main
    monkeypatch.setattr(main, "lakebase_pool", lambda: "pool")
    made = {}

    class FakeStore:
        def __init__(self, kind, schema="", pg_pool=None, **kw):
            made.update(kind=kind, schema=schema, pg=pg_pool)

    monkeypatch.setattr(main, "StateStore", FakeStore)
    app = main.create_app(Settings(demo_mode=False, warehouse_id="w", static_dir="/nonexistent"))
    assert made == {"kind": "postgres", "schema": "dam_app", "pg": "pool"}
    assert isinstance(app.state.analytics_factory("tok"), an_mod.SqlAnalytics)
    assert app.state.analytics_factory("tok").user_token == "tok"
    with pytest.raises(RuntimeError):
        main.create_app(Settings(demo_mode=False, warehouse_id=""))


def test_mock_incl_profile_without_installation_in_force():
    """v1.5 §5.5: constants come from the ref.incl_installation row valid at survey time; none -> no reduction."""
    d = seed.build()
    for row in d["incl_installations"]:
        if row["instrument_id"] == "CRD-IN-1":
            row["effective_from"] = "2999-01-01T00:00:00Z"
    m = an_mod.MockAnalytics(d)
    sid = m.incl_surveys("CRD-IN-1")[-1]["survey_id"]
    assert m.incl_profile("CRD-IN-1", sid) == []  # never reduce with constants that were not in force
    d2 = seed.build()
    d2["incl_installations"].append({**d2["incl_installations"][0], "effective_to": None})   # overlap
    assert an_mod.MockAnalytics(d2).incl_profile("CRD-IN-1", sid) == []


def test_seed_v15_incl_lineage_and_alarm_rows():
    """v1.5 demo CSV shape: calib_id NULL + installation_version (row at survey time), real reading_ids,
    metric_value filled on every reading-driven episode."""
    d = seed.build()
    m = an_mod.MockAnalytics(d)
    sv = m.incl_surveys("CRD-IN-1")
    rows = d["incl_installations"]
    for s in sv:
        prof = m.incl_profile("CRD-IN-1", s["survey_id"])
        want = seed.installation_at(rows, "CRD-IN-1", s["survey_ts"])["effective_from"]
        assert prof and {(r["calib_id"], r["cal_version"], r["installation_version"]) for r in prof} == \
            {(None, None, want)}
    # the survey on the SCD2 boundary is reduced with the second (half-open) version
    edge = [s for s in sv if s["survey_ts"] == rows[1]["effective_from"] and rows[1]["instrument_id"] == "CRD-IN-1"]
    assert edge and m.incl_profile("CRD-IN-1", edge[0]["survey_id"])[0]["installation_version"] == rows[1]["effective_from"]
    survey_ids = {s["survey_id"] for s in d["incl_surveys"]}
    reading_ids = {r["reading_id"] for r in d["readings"]}
    for e in d["alarm_events"]:
        if e["rule"] == "no_data":
            continue
        assert e["metric_value"] == e["value"] is not None and "PEAK" not in e["reading_id"]
        if e["formula_id"] == "INCL_PROBE_2PASS":
            assert e["calib_id"] is None and e["reading_id"] in survey_ids and e["installation_version"]
        else:
            assert e["reading_id"] in reading_ids


def test_incl_reduction_against_dmm_demo_hole45():
    """Production reduction vs DGSI DMM DEMO hole 45 published table (PDF p99-102), transcribed by the
    Inclinometer Specialist and re-verified by QA (QA.md 2026-11-02 18:40). L = 24 in, K = 20000."""
    import csv
    p = Path("/workspace/dam-refs/datasets/inclinometers/vendor_manual_samples/sinco_dmm_demo_hole45_A.csv")
    if not p.exists():
        pytest.skip("reference dataset not present")
    rows = list(csv.DictReader(line for line in p.open() if not line.startswith("#")))
    base = {float(r["depth_ft"]): (fx.inc_deviation(24, int(r["init_A0"]), int(r["init_A180"]), 20000), 0.0)
            for r in rows}
    cur = [{"depth_m": float(r["depth_ft"]), "a0": int(r["cur_A0"]), "a180": int(r["cur_A180"]), "b0": 0, "b180": 0}
           for r in rows]
    prof = {x["depth_m"]: x for x in fx.reduce_inclinometer_profile(cur, base, 24.0, 20000.0)}
    assert len(prof) == 30
    for r in rows:
        x = prof[float(r["depth_ft"])]
        assert x["a_inc_mm"] == pytest.approx(float(r["pub_incr_disp_in"]), abs=1e-4)
        assert x["a_cum_mm"] == pytest.approx(float(r["pub_cum_disp_in"]), abs=1e-4)
        assert fx.inc_deviation(24, int(r["cur_A0"]), int(r["cur_A180"]), 20000) == \
            pytest.approx(float(r["pub_cur_incr_dev_in"]), abs=5.1e-5)


def test_vw_sign_mult_offset_and_explicit_c():
    lin = {"method": "linear", "G": 0.1795, "R0": 9082}
    assert fx.vw_gauge({**lin, "sign_mult": -1}, 6749) == pytest.approx(418.77, abs=0.01)
    assert fx.vw_gauge(lin, 6749) == pytest.approx(-418.77, abs=0.01)
    assert fx.vw_gauge({**lin, "sign_mult": "-1"}, 6749) == pytest.approx(418.77, abs=0.01)
    for bad in (0, 2, -1.5, "x", None):
        with pytest.raises(ValueError):
            fx.check_sign_mult(bad)
    poly = {"method": "poly", "A": -7.113e-7, "B": -0.1694, "R0": 9082}
    c = fx.vw_poly_C(poly["A"], poly["B"], 9082)
    assert fx.vw_gauge({**poly, "C": c}, 6749) == pytest.approx(fx.vw_gauge(poly, 6749))
    assert fx.vw_gauge({**poly, "C": float("nan")}, 6749) == pytest.approx(fx.vw_gauge(poly, 6749))
    base = fx.vw_piezo({**lin, "sign_mult": -1}, 6749)["pressure_kpa"]
    assert fx.vw_piezo({**lin, "sign_mult": -1, "offset": 1.5}, 6749)["pressure_kpa"] == pytest.approx(base + 1.5)
    assert fx.vw_disp({"G": 0.01211, "R0": 4200, "sign_mult": -1}, 6000) == pytest.approx(-21.798)


def test_onboarding_sign_mult_checks():
    from backend import onboarding as ob
    draft = {"instruments": [{"instrument_id": "X-PZ-1", "structure_id": "S", "instrument_type": "piezometer",
                              "formula_id": "VW_PIEZO", "formula_version": "1.0.0", "eng_unit": "m"}],
             "calibrations": [{"instrument_id": "X-PZ-1", "cal_version": 1,
                               "sign_mult": 3, "offset": "x",
                               "params": {"method": "linear", "G": -0.18, "R0": 1, "T0": 1, "K": 0,
                                          "tip_elev_m": 1}}]}
    msgs = [i["message"] for i in ob.validate(draft, set(), fx.formula_catalog() if hasattr(fx, "formula_catalog") else [])
            if i["step"] == "calibrations"]
    assert any("sign_mult must be +1 or -1" in m for m in msgs)
    assert any("store |G| with sign_mult -1" in m for m in msgs)
    assert any("offset must be numeric" in m for m in msgs)
    # §10 Q3: sign_mult / offset in params is an error; method must be linear|poly
    draft["calibrations"][0].update(sign_mult=-1, offset=0, params={"method": "cubic", "G": 0.18, "sign_mult": -1,
                                                                     "offset": 1, "R0": 1, "T0": 1, "K": 0})
    msgs = [i["message"] for i in ob.validate(draft, set()) if i["step"] == "calibrations"]
    assert any("sign_mult belongs in the calibration's sign_mult field" in m for m in msgs)
    assert any("offset belongs in the calibration's offset field" in m for m in msgs)
    assert any("method must be one of linear|poly" in m for m in msgs)


def test_contract_reading_maps_ferc_view_and_erroneous_mark():
    from backend.repos import contract as ct
    r = ct.reading({"reading_ts": "t", "value": 1.0, "unit": "kPa", "qc_flag": "OK", "qc_reason": None,
                    "is_erroneous": False})
    assert (r["ts_utc"], r["corrected_value"], r["corrected_unit"], r["qa_flag"]) == ("t", 1.0, "kPa", "OK")
    r = ct.reading({"reading_ts": "t", "value": 1.0, "unit": "kPa", "qc_flag": "SUSPECT", "qc_reason": "SPIKE",
                    "is_erroneous": True, "erroneous_reason": "probe swap"})
    assert r["qa_flag"] == "BAD" and r["qa_reason"] == "SPIKE,ERRONEOUS_MARK,probe swap"   # §3 comma-joined


