"""QA-039 canon-2 / §7 item 14 (independent of app/tests/test_actor_app_role.py).

Re-derives hashes from app/SCHEMA_CONTRACT.md §7 item 14 text only. The App Dev vector
app/tests/fixtures/review_chain_canon2_vector.json is a cross-check — if the spec and the vector (or the
code) disagree, that is a defect, not a reason to retune the helper.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "jobs"))

from backend import audit_chain as app_ac  # noqa: E402
from backend import main as app_main  # noqa: E402
from backend.config import Settings  # noqa: E402
from dam_safety import audit_chain as eng_ac  # noqa: E402
from dam_safety.inclinometer.corrections import canonical_value as engine_canonical_value  # noqa: E402

# ---- SPEC (§7 item 14) — do not import CANON_FIELDS from app code for the helper ----
SPEC_V1 = ("review_id", "dam_id", "subject_type", "subject_id", "review_level", "decision", "comment",
           "reviewer_id", "reviewer_pe_license", "signed_ts", "signature_hash", "action_type",
           "reviewer_email", "reviewer_sub", "original_value", "corrected_value", "chain_id", "chain_seq",
           "prev_hash")
SPEC_ROLE, SPEC_CANON = "actor_app_role", "canon_version"
SPEC_V2 = SPEC_V1 + (SPEC_ROLE, SPEC_CANON)
SPEC_ROLES = ("viewer", "engineer", "dso", "dam_admin", "admin", "cdse")
VECTOR = ROOT / "app/tests/fixtures/review_chain_canon2_vector.json"

ADMIN, ENG, CDSE = "admin@demo.local", "engineer.crd@demo.local", "cdse.crd@demo.local"
RULE = "CRD-IN-1-T2"


def _spec_canon_of(row):
    v = row.get(SPEC_CANON)
    if v is None or (isinstance(v, str) and str(v).strip() == ""):
        return 1
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def spec_record_hash(row: dict) -> str:
    canon = _spec_canon_of(row)
    fields = SPEC_V1 if canon == 1 else SPEC_V2
    doc = {}
    for f in fields:
        if f == "chain_seq":
            doc[f] = None if row.get(f) is None else int(row[f])
        elif f == SPEC_CANON:
            doc[f] = canon
        else:
            doc[f] = engine_canonical_value(row.get(f))
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _h(email, **extra):
    return {"x-forwarded-user": f"uid-{email.lower()}", "x-forwarded-email": email, **extra}


@pytest.fixture()
def demo(tmp_path):
    app = app_main.create_app(Settings(demo_mode=True, static_dir=str(tmp_path / "nodist")))
    st = app.state.store
    if st.get_user(CDSE) is None:
        st.upsert_user(CDSE, "Chris CDSE", False, "qa050")
        st.set_role(CDSE, "CRD", "engineer", "qa050")
    tc = TestClient(app)
    yield tc, app
    st._conn.close()


def _ack(tc, email=ENG):
    ev = tc.get("/api/alarms?dam_id=CRD", headers=_h(email)).json()["alarms"][0]["event_id"]
    # level-1 unflagged can be engineer; pick one engineer can ack or use CDSE for L2
    r = tc.post(f"/api/alarms/{ev}/ack", headers=_h(email), json={"rationale": "qa"})
    if r.status_code == 403:
        r = tc.post(f"/api/alarms/{ev}/ack", headers=_h(CDSE), json={"rationale": "qa"})
    assert r.status_code == 200, r.text
    return r


# --------------------------------------------------------------------------- spec vs vector cross-check
def test_spec_helper_matches_canon2_vector_byte_for_byte():
    vec = json.loads(VECTOR.read_text())
    assert tuple(vec["canon_fields"]["1"]) == SPEC_V1
    assert tuple(vec["canon_fields"]["2"]) == SPEC_V2
    assert tuple(vec["actor_roles"]) == SPEC_ROLES
    mismatches = []
    for r in vec["rows"]:
        got = spec_record_hash(r)
        if got != r["record_hash"]:
            mismatches.append((r["chain_seq"], r.get("canon_version"), got, r["record_hash"]))
    assert mismatches == [], f"SPEC helper disagrees with vector hashes: {mismatches}"


def test_spec_helper_matches_app_record_hash_on_vector_rows():
    vec = json.loads(VECTOR.read_text())
    for r in vec["rows"]:
        assert spec_record_hash(r) == app_ac.record_hash(r) == r["record_hash"]


def test_v1_vector_rows_unchanged_vs_pre_c797151_field_set():
    """Legacy canon-1 rows hash exactly SPEC_V1 — byte-for-byte stable vs pre-c797151."""
    vec = json.loads(VECTOR.read_text())
    v1 = [r for r in vec["rows"] if r.get("canon_version") in (None, "", 1)]
    assert len(v1) >= 2
    for r in v1:
        # force-absent canon fields
        bare = {k: v for k, v in r.items() if k not in (SPEC_ROLE, SPEC_CANON)}
        assert _spec_canon_of(bare) == 1
        assert spec_record_hash(bare) == r["record_hash"]
        # adding NULL canon fields must not change v1 hash
        with_nulls = {**bare, SPEC_ROLE: None, SPEC_CANON: None}
        assert spec_record_hash(with_nulls) == r["record_hash"]


# --------------------------------------------------------------------------- live app writes are canon-2
def test_new_records_are_canon2_with_server_role(demo):
    tc, app = demo
    _ack(tc, ENG)
    rows = sorted(app.state.store.chain_rows("CRD"), key=lambda r: int(r["chain_seq"]))
    assert rows and int(rows[-1]["canon_version"]) == 2
    assert rows[-1]["actor_app_role"] in ("engineer", "cdse,engineer", "admin", "admin,cdse") or \
        all(p in SPEC_ROLES for p in rows[-1]["actor_app_role"].split(","))
    assert rows[-1]["record_hash"] == spec_record_hash(rows[-1])
    assert app_ac.verify_chain(rows, app.state.store.chain_head("CRD"))["ok"]


# --------------------------------------------------------------------------- client cannot supply role / canon
@pytest.mark.parametrize("channel", ["body", "query", "header_role", "header_x_actor", "header_forwarded_role"])
def test_client_supplied_actor_app_role_and_canon_never_stored(demo, channel):
    tc, app = demo
    st = app.state.store
    # ensure CDSE can apply a flag flip
    an = app.state.analytics_factory(None)
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    target = not bool(cur.get("cdse_ack_required"))
    body = {"cdse_ack_required": target, "rationale": "client injection probe",
            "actor_app_role": "admin,cdse", "canon_version": 99}
    headers = _h(CDSE)
    url = f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag"
    if channel == "body":
        r = tc.post(url, headers=headers, json=body)
    elif channel == "query":
        r = tc.post(url + "?actor_app_role=admin&canon_version=99", headers=headers,
                    json={"cdse_ack_required": target, "rationale": "q"})
    elif channel == "header_role":
        r = tc.post(url, headers={**headers, "actor_app_role": "admin", "canon_version": "99"},
                    json={"cdse_ack_required": target, "rationale": "h"})
    elif channel == "header_x_actor":
        r = tc.post(url, headers={**headers, "x-actor-app-role": "admin", "x-canon-version": "99"},
                    json={"cdse_ack_required": target, "rationale": "h2"})
    else:
        r = tc.post(url, headers={**headers, "x-forwarded-role": "admin", "x-forwarded-actor-app-role": "admin"},
                    json={"cdse_ack_required": target, "rationale": "h3"})
    assert r.status_code == 200, (channel, r.text)
    row = sorted(st.chain_rows("CRD"), key=lambda r: int(r["chain_seq"]))[-1]
    assert int(row["canon_version"]) == 2
    # must be server-derived for CDSE user (engineer grant + cdse) — never the client forgery
    assert row["actor_app_role"] == "cdse,engineer"
    assert "99" not in str(row["canon_version"])
    # response must not echo client-supplied forged role as authority
    dump = json.dumps(r.json())
    assert "canon_version\": 99" not in dump


def test_spoofed_forwarded_email_cannot_mint_foreign_actor_role_without_grants(demo):
    tc, app = demo
    # viewer-looking email with no grants → 403 on apply; if somehow written, role wouldn't be admin
    r = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag",
                headers={"x-forwarded-user": "uid-spoof", "x-forwarded-email": "spoof@demo.local",
                         "x-forwarded-role": "admin", "actor_app_role": "admin"},
                json={"cdse_ack_required": True, "rationale": "spoof"})
    assert r.status_code in (401, 403, 404)


# --------------------------------------------------------------------------- adversarial verify reasons (app)
def _v2_row(seq, prev, role="engineer", canon=2, **over):
    base = {"review_id": f"RS-{seq}", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": f"E{seq}",
            "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "u",
            "reviewer_pe_license": None, "signed_ts": "2026-11-09T00:00:00.000000+00:00",
            "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
            "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}",
            "chain_id": "CRD", "chain_seq": seq, "prev_hash": prev,
            SPEC_ROLE: role, SPEC_CANON: canon, "signature": None, "sig_alg": None, "key_id": None}
    base.update(over)
    base["record_hash"] = spec_record_hash(base)
    return base


def _chain_v1_then_v2():
    r1 = _v2_row(1, app_ac.GENESIS, role=None, canon=None)
    # strip role/canon keys for pure v1
    r1 = {k: v for k, v in r1.items() if k not in (SPEC_ROLE, SPEC_CANON)}
    r1["record_hash"] = spec_record_hash(r1)
    r2 = _v2_row(2, r1["record_hash"], role="engineer", canon=2)
    return [r1, r2]


def test_mixed_v1_then_v2_verifies_on_app():
    rows = _chain_v1_then_v2()
    head = {"chain_id": "CRD", "chain_seq": 2, "head_hash": rows[-1]["record_hash"]}
    v = app_ac.verify_chain(rows, head)
    assert v["ok"] is True, v


def test_v2_then_v1_fails_canon_downgrade():
    r1 = _v2_row(1, app_ac.GENESIS, role="engineer", canon=2)
    r2 = _v2_row(2, r1["record_hash"], role=None, canon=None)
    r2 = {k: v for k, v in r2.items() if k not in (SPEC_ROLE, SPEC_CANON)}
    r2["record_hash"] = spec_record_hash(r2)
    head = {"chain_id": "CRD", "chain_seq": 2, "head_hash": r2["record_hash"]}
    v = app_ac.verify_chain([r1, r2], head)
    assert v["ok"] is False and v["reason"] == "canon_downgrade" and v["first_broken_seq"] == 2


def test_stripped_v2_presented_as_v1_is_downgrade_or_hash_mismatch():
    """Strip canon_version + actor_app_role from a v2 row without rehash → hash_mismatch;
    with rehash as v1 after a prior v2 → canon_downgrade."""
    r1 = _v2_row(1, app_ac.GENESIS, role="cdse", canon=2)
    r2 = _v2_row(2, r1["record_hash"], role="engineer", canon=2)
    stripped = {k: v for k, v in r2.items() if k not in (SPEC_ROLE, SPEC_CANON)}
    # no rehash
    v = app_ac.verify_chain([r1, stripped], {"chain_id": "CRD", "chain_seq": 2, "head_hash": stripped["record_hash"]})
    assert v["reason"] == "hash_mismatch"
    # rehash as v1
    stripped2 = {**stripped, "record_hash": spec_record_hash(stripped)}
    v2 = app_ac.verify_chain([r1, stripped2], {"chain_id": "CRD", "chain_seq": 2, "head_hash": stripped2["record_hash"]})
    assert v2["reason"] == "canon_downgrade"


def test_canon_downgrade_at_seq1_after_head_and_after_split():
    # seq1 as v1 after a synthetic prior v2_seen is impossible in one chain from genesis; instead: after split,
    # a new chain starting at v1 is ok; inserting v1 after v2 in same chain fails.
    r1 = _v2_row(1, app_ac.GENESIS, role="admin", canon=2)
    r2 = _v2_row(2, r1["record_hash"], role=None, canon=None)
    r2 = {k: v for k, v in r2.items() if k not in (SPEC_ROLE, SPEC_CANON)}
    r2["record_hash"] = spec_record_hash(r2)
    assert app_ac.verify_chain([r1, r2], {"chain_id": "CRD", "chain_seq": 2,
                                          "head_hash": r2["record_hash"]})["reason"] == "canon_downgrade"
    # fresh chain that is only v1 still ok (no prior v2)
    only = _v2_row(1, app_ac.GENESIS, role=None, canon=None)
    only = {k: v for k, v in only.items() if k not in (SPEC_ROLE, SPEC_CANON)}
    only["record_hash"] = spec_record_hash(only)
    assert app_ac.verify_chain([only], {"chain_id": "CRD", "chain_seq": 1, "head_hash": only["record_hash"]})["ok"]


def test_unknown_canon_version():
    r = _v2_row(1, app_ac.GENESIS, role="engineer", canon=3)
    v = app_ac.verify_chain([r], {"chain_id": "CRD", "chain_seq": 1, "head_hash": r["record_hash"]})
    assert v["reason"] == "canon_unknown"


@pytest.mark.parametrize("role,reason", [
    (None, "actor_role_missing"),
    ("", "actor_role_missing"),
    ("   ", "actor_role_missing"),
    ("superuser", "actor_role_invalid"),
    ("engineer,admin", "actor_role_invalid"),  # unsorted
    ("engineer,engineer", "actor_role_invalid"),  # not unique
    ("Engineer", "actor_role_invalid"),  # case
    (" engineer", "actor_role_invalid"),  # whitespace token
])
def test_missing_invalid_role_variants(role, reason):
    r = _v2_row(1, app_ac.GENESIS, role=role, canon=2)
    v = app_ac.verify_chain([r], {"chain_id": "CRD", "chain_seq": 1, "head_hash": r["record_hash"]})
    assert v["reason"] == reason, v


def test_forged_role_change_on_existing_record_is_hash_mismatch():
    r = _v2_row(1, app_ac.GENESIS, role="engineer", canon=2)
    forged = {**r, SPEC_ROLE: "admin"}
    v = app_ac.verify_chain([forged], {"chain_id": "CRD", "chain_seq": 1, "head_hash": r["record_hash"]})
    assert v["reason"] == "hash_mismatch"


# --------------------------------------------------------------------------- engine parity (expected gap per §7 item 14)
def test_engine_verify_records_parity_on_canon2_cases():
    """QA-050 closed: engine ≡ app on canon-2 including reason codes."""
    rows = _chain_v1_then_v2()
    head = {"chain_id": "CRD", "chain_seq": 2, "head_hash": rows[-1]["record_hash"]}
    a = app_ac.verify_chain(copy.deepcopy(rows), copy.deepcopy(head))
    e = eng_ac.verify_records(copy.deepcopy(rows), head=head)
    assert a["ok"] and e["ok"] and a.get("reason") == e.get("reason")
    # downgrade case
    r1 = _v2_row(1, app_ac.GENESIS, role="engineer", canon=2)
    r2 = _v2_row(2, r1["record_hash"], role=None, canon=None)
    r2 = {k: v for k, v in r2.items() if k not in (SPEC_ROLE, SPEC_CANON)}
    r2["record_hash"] = spec_record_hash(r2)
    head2 = {"chain_id": "CRD", "chain_seq": 2, "head_hash": r2["record_hash"]}
    a2 = app_ac.verify_chain([r1, r2], head2)
    e2 = eng_ac.verify_records([r1, r2], head=head2)
    assert a2["reason"] == e2["reason"] == "canon_downgrade"


def test_uc_ddl_and_extract_carry_actor_role_and_canon_version():
    """QA-051 closed: UC DDL + Lakebase schema + extract carry both columns end-to-end."""
    ddl = (ROOT / "sql/ddl/08_audit.sql").read_text()
    lake = (ROOT / "lakebase/001_app_schema.sql").read_text()
    import lakebase_extract as lx
    assert "actor_app_role" in ddl and "canon_version" in ddl
    assert "actor_app_role" in lake and "canon_version" in lake
    assert lx.CHAIN_COLS[-2:] == ("actor_app_role", "canon_version") or \
        ("actor_app_role" in lx.CHAIN_COLS and "canon_version" in lx.CHAIN_COLS)
    # extract passes canon_version as int
    row = _v2_row(1, "0" * 64, role="engineer", canon=2)
    row["signed_ts"] = "2026-11-09T00:00:00+00:00"
    [rec] = lx.review_rows_to_records([row])
    assert rec.get("actor_app_role") == "engineer" and int(rec["canon_version"]) == 2
    mig = (ROOT / "sql/migrations/v1_7_002_canon_v2.sql").read_text()
    assert "ADD COLUMNS" in mig and "actor_app_role" in mig and "canon_version" in mig
    assert "No data change" in mig or "never backfilled" in mig.lower()
    code = "\n".join(l for l in mig.splitlines() if not l.lstrip().startswith("--"))
    assert "UPDATE" not in code.upper() and "DELETE FROM" not in code.upper()


# --------------------------------------------------------------------------- go-live / N+1 required threshold columns
PUBLISH_REQUIRED = ("dssmp_version_id", "tier", "deadband", "persistence_on", "persistence_off")


@pytest.mark.parametrize("field", PUBLISH_REQUIRED)
@pytest.mark.parametrize("bad", [None, "", "  "])
def test_n_plus_1_missing_required_threshold_column_409_no_writes(demo, field, bad):
    tc, app = demo
    st = app.state.store
    an = app.state.analytics_factory(None)
    # mutate current rule in demo dataset
    for t in an.d["thresholds"]:
        if t["threshold_id"] == RULE and t.get("is_current", True):
            t[field] = bad
    before = st.execute("SELECT count(*) AS n FROM {s}review_signoff WHERE dam_id=%s", ("CRD",))[0]["n"]
    rows_before = [copy.deepcopy(t) for t in an.d["thresholds"] if t["threshold_id"] == RULE]
    cur = next(t for t in an.thresholds("CRD") if t["threshold_id"] == RULE)
    target = not bool(cur.get("cdse_ack_required"))
    r = tc.post(f"/api/dams/CRD/alarm-rules/{RULE}/cdse-flag", headers=_h(CDSE),
                json={"cdse_ack_required": target, "rationale": f"missing {field}"})
    assert r.status_code == 409, r.text
    body = r.text.lower()
    assert field in r.text
    # 478b7b5 mirrors DA dssmp_missing for the dssmp_version_id case; other fields keep "no default"/"backfill"
    assert ("no default" in body or "backfill" in body or "dssmp_missing" in body), r.text
    after = st.execute("SELECT count(*) AS n FROM {s}review_signoff WHERE dam_id=%s", ("CRD",))[0]["n"]
    assert after == before
    assert [copy.deepcopy(t) for t in an.d["thresholds"] if t["threshold_id"] == RULE] == rows_before


@pytest.mark.parametrize("field,wrong", [("deadband", "x"), ("persistence_on", "x"), ("persistence_off", -1),
                                         ("tier", ""), ("dssmp_version_id", None)])
def test_go_live_validate_rejects_bad_required_fields(demo, field, wrong):
    from backend import onboarding as ob
    row = {k: ("DSSMP-X", "AL1", 0.1, 2, 3)[i] for i, k in enumerate(PUBLISH_REQUIRED)}
    row.update({"instrument_id": "X", "level": 1, "limit_type": "upper", "value": 1, "unit": "mm",
                "cdse_ack_required": False, field: wrong})
    errs = ob.publish_field_problems(row)
    assert errs and any(field in e for e in errs), errs


def test_no_migration_adds_default_for_publish_required_columns():
    mig_dir = ROOT / "sql/migrations"
    hits = []
    for p in mig_dir.glob("*.sql"):
        text = p.read_text()
        for col in PUBLISH_REQUIRED:
            if col in text and "DEFAULT" in text.upper():
                # only flag if DEFAULT appears near the column
                for line in text.splitlines():
                    if col in line and "DEFAULT" in line.upper():
                        hits.append((p.name, line.strip()))
    assert hits == [], f"migrations must not DEFAULT the five NOT NULL publish columns: {hits}"
