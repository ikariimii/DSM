"""v1.7 §7 item 21 (app SCHEMA_CONTRACT; App Dev 1770505): bulk DSSMP re-version. rule_reversion CHECKs, the chain
authority engine (dam_safety.rule_reversion_authority.chain_problems, run by verify_review_chain), the publisher's
acceptance of cause 'dssmp_reversion' publications on a rule_reversion_approve record, ref.v_dssmp_affected_rules /
ref.dssmp_affected_rules (C). SQLite renders + PostgreSQL (DAM_TEST_PG) for the CHECKs, the view and the MERGE.
Synthetic data only."""
import copy
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import ref_change_publish as rp  # noqa: E402
from dam_safety import rule_reversion_authority as rra  # noqa: E402
import verify_review_chain as vj  # noqa: E402

D = "SYN-DAM-01"
CDSE, ADMIN, NOBODY = "cdse@example.org", "admin@example.org", "nobody@example.org"
AUTH = [{"dam_id": D, "principal": CDSE, "granted_ts": "2026-01-01T00:00:00Z", "revoked_ts": None}]
DSSMP = {"DS-OLD": {"dam_id": D, "status": "superseded", "effective_from": "2025-01-01T00:00:00Z",
                    "effective_to": "2026-10-01T00:00:00Z"},
         "DS-NEW": {"dam_id": D, "status": "approved", "effective_from": "2026-10-01T00:00:00Z", "effective_to": None},
         "DS-ODD": {"dam_id": D, "status": "approved", "effective_from": "2026-10-01T00:00:00Z", "effective_to": None}}
V1 = {"threshold_id": "T-1", "threshold_version": 1, "dam_id": D, "instrument_id": "I-1", "sensor_id": None,
      "pfm_id": "PFM-1", "dssmp_version_id": "DS-OLD", "tier": "AL1", "level": 1, "level_name": "ALERT",
      "limit_type": "magnitude", "direction": "upper", "value": 10.0, "unit": "m", "rate_window": None,
      "deadband": 0.2, "persistence_on": 2, "persistence_off": 3, "condition_expr": None, "basis_text": None,
      "required_actions": "notify", "eap_level_id": None, "set_by": "seed", "effective_from": "2026-02-01T00:00:00Z",
      "effective_to": None, "is_current": True, "cdse_ack_required": True}
NOW = "2026-11-09T18:00:00Z"
PID, PUB = "RRP-1", "PUB-1"


def record(seq, action, who=CDSE, pid=PID, signed="2026-11-09T16:00:00Z", h=None, canon=2, roles="cdse", **cv):
    decision = rra.ACTIONS.get(action, "approve")
    body = {"proposal_id": pid, "to_dssmp_version_id": "DS-NEW", "proposed_by": ADMIN, "state": "applied",
            "publications": [{"threshold_id": "T-1", "publication_id": PUB, "threshold_version": 2}], **cv}
    return {"review_id": f"R{seq}", "dam_id": D, "subject_type": "rule_reversion", "subject_id": pid,
            "decision": decision, "comment": "why", "reviewer_id": f"id-{who}", "reviewer_email": who,
            "signed_ts": signed, "action_type": action, "original_value": "{}", "corrected_value": json.dumps(body),
            "chain_id": D, "chain_seq": seq, "record_hash": h or f"{seq:064d}", "canon_version": canon,
            "actor_app_role": roles}


PROPOSE = record(1, "rule_reversion_propose", ADMIN, signed="2026-11-09T15:00:00Z", roles="admin")
APPROVE = record(2, "rule_reversion_approve")


def outbox(rec=APPROVE, requested_at="2026-11-09T16:00:01Z", row_kw=None, pay_kw=None, **ins_kw):
    ins = {**V1, "threshold_version": 2, "dssmp_version_id": "DS-NEW", "effective_from": "2026-11-09T16:00:00+00:00",
           "set_by": CDSE, **ins_kw}
    pay = {"kind": rp.KIND, "threshold_id": "T-1", "dam_id": D, "supersedes_version": 1, "cause": rra.CAUSE,
           "reversion_proposal_id": PID, "insert": ins, "approval_record_hash": rec["record_hash"], **(pay_kw or {})}
    return {"pub_id": PUB, "kind": rp.KIND, "dam_id": D, "proposal_id": PID, "requested_by": CDSE,
            "requested_at": requested_at, "status": "pending", "payload": json.dumps(pay), **(row_kw or {})}


def ctx(records=(PROPOSE, APPROVE), second=False, now=NOW, auth=AUTH):
    return {"records_by_hash": {r["record_hash"]: r for r in records}, "authority": auth, "now": now,
            "require_second_approver": second}


def one(row, reversion="default", current=(V1,)):
    [d] = rp.plan([row], list(current), DSSMP, ctx() if reversion == "default" else reversion)
    return d


# ---------------------------------------------------------------- publisher
def test_reversion_publication_is_applied_on_its_cdse_approval():
    d = one(outbox())
    assert (d["decision"], d["status"]) == ("applied", "applied"), d["message"]
    assert d["insert"]["dssmp_version_id"] == "DS-NEW" and d["insert"]["cdse_ack_required"] is True
    assert d["close"] == {"threshold_id": "T-1", "threshold_version": 1, "effective_to": d["insert"]["effective_from"],
                          "is_current": False}


@pytest.mark.parametrize("kw,code", [
    (dict(row_kw={"dam_id": "SYN-DAM-02"}), "payload_invalid"),
    (dict(pay_kw={"cause": "dssmp_upgrade"}), "payload_invalid"),
    (dict(pay_kw={"reversion_proposal_id": "RRP-9"}), "reversion_approval_mismatch"),
    (dict(row_kw={"proposal_id": "RRP-9"}), "reversion_approval_mismatch"),
    (dict(row_kw={"pub_id": "PUB-9"}), "reversion_approval_mismatch"),
    (dict(set_by=ADMIN), "reversion_approval_mismatch"),
    (dict(dssmp_version_id="DS-ODD"), "reversion_approval_mismatch"),
    (dict(value=11.0), "reversion_fields_changed"),
    (dict(cdse_ack_required=False), "reversion_fields_changed"),
    (dict(dssmp_version_id="DS-OLD"), "dssmp_superseded"),
    (dict(rec=PROPOSE), "reversion_approval_wrong_action"),
    (dict(pay_kw={"approval_record_hash": "f" * 64}, requested_at="2026-11-09T10:00:00Z"),
     "reversion_approval_missing"),
])
def test_reversion_publication_refusals(kw, code):
    d = one(outbox(**kw))
    assert d["status"] == "rejected_invalid" and code in d["codes"], d["message"]


def test_not_authorized_self_approval_and_fail_closed_without_context():
    d = one(outbox(), ctx(auth=[]))
    assert d["codes"] == ["reversion_not_authorized"]
    late = [{**AUTH[0], "revoked_ts": "2026-11-09T12:00:00Z"}]                  # revoked before signed_ts
    assert one(outbox(), ctx(auth=late))["codes"] == ["reversion_not_authorized"]
    own = record(2, "rule_reversion_approve", proposed_by=CDSE)
    assert one(outbox(own), ctx((PROPOSE, own)))["status"] == "applied"           # switch off: allowed
    d = one(outbox(own), ctx((PROPOSE, own), second=True))
    assert d["codes"] == ["reversion_not_authorized"] and "self-approval" in d["message"]
    assert one(outbox(), None)["codes"] == ["reversion_approval_missing"]       # no context: fail closed


def test_missing_record_defers_within_the_wait_then_refuses():
    row = outbox(pay_kw={"approval_record_hash": "f" * 64}, requested_at="2026-11-09T17:30:00Z")
    d = one(row)
    assert (d["decision"], d["status"]) == ("deferred", None)
    assert one(row, ctx(now="2026-11-09T19:00:00Z"))["codes"] == ["reversion_approval_missing"]


def test_flag_publications_without_cause_are_unchanged():
    row = outbox(cdse_ack_required=False, set_by="cdse@example.org", dssmp_version_id="DS-NEW")
    p = json.loads(row["payload"])
    for k in ("cause", "reversion_proposal_id", "approval_record_hash"):
        p.pop(k)
    row["payload"] = json.dumps(p)
    assert one(row, None)["status"] == "applied"


def test_rerun_after_merge_is_already_applied():
    d = one(outbox())
    assert one(outbox(), current=(d["insert"],))["decision"] == "already_applied"


def test_job_reads_the_context_only_for_reversion_rows():
    import publish_ref_changes as job
    flag = {"payload": json.dumps({"kind": rp.KIND})}
    assert job.reversion_context(None, "c", [flag], False) is None
    src = open(os.path.join(ROOT, "src", "jobs", "publish_ref_changes.py")).read()
    assert "plan(pending, current, dssmp, reversion_context(spark, a.catalog, pending, second))" in src
    jobs = open(os.path.join(ROOT, "resources", "jobs.yml")).read()
    i = jobs.index("python_file: ../src/jobs/publish_ref_changes.py")
    assert "--cdse_flag_require_second_approver" in jobs[i:i + 400]


# ---------------------------------------------------------------- chain authority
def codes(p):
    return sorted(x["code"] for x in p)


def test_chain_valid_propose_by_admin_and_approve_by_cdse():
    assert rra.chain_problems([PROPOSE, APPROVE], AUTH, require_second_approver=False) == []
    rej = record(2, "rule_reversion_reject")
    assert rra.chain_problems([PROPOSE, rej], AUTH, require_second_approver=False) == []
    by_cdse = record(1, "rule_reversion_propose", CDSE, roles="viewer")      # CDSE authority from the view
    assert rra.chain_problems([by_cdse], AUTH, require_second_approver=False) == []


def test_chain_refusals():
    p = lambda recs, **kw: codes(rra.chain_problems(recs, AUTH, require_second_approver=kw.get("second", False)))  # noqa: E731
    assert p([record(1, "rule_reversion_propose", NOBODY, roles="engineer,viewer")]) == \
        ["rule_reversion_propose_no_authority"]
    assert p([record(1, "rule_reversion_propose", NOBODY, canon=None, roles=None)]) == \
        ["rule_reversion_propose_role_unverified"]
    assert p([record(1, "rule_reversion_propose", NOBODY, roles="cdse")]) == ["rule_reversion_propose_no_authority"]
    assert p([PROPOSE, record(2, "rule_reversion_approve", ADMIN, roles="admin")]) == \
        ["rule_reversion_approve_not_cdse"]
    assert p([PROPOSE, record(2, "rule_reversion_reject", NOBODY)]) == ["rule_reversion_reject_not_cdse"]
    assert p([APPROVE]) == ["rule_reversion_proposal_missing"]
    assert p([PROPOSE, APPROVE, record(3, "rule_reversion_reject")]) == ["rule_reversion_proposal_missing"]
    own = record(1, "rule_reversion_propose", CDSE)
    assert p([own, record(2, "rule_reversion_approve", proposed_by=CDSE)]) == []
    assert p([own, record(2, "rule_reversion_approve", proposed_by=CDSE)], second=True) == \
        ["rule_reversion_self_approval"]
    bad = record(2, "rule_reversion_approve", proposal_id="RRP-X")
    assert p([PROPOSE, bad]) == ["rule_reversion_subject_mismatch"]


def test_verify_job_reports_reversion_errors():
    errs, _ = vj.authority_failures([PROPOSE, record(2, "rule_reversion_approve", NOBODY)], AUTH,
                                    require_second_approver=False)
    assert [e["code"] for e in errs] == ["rule_reversion_approve_not_cdse"]


def test_every_code_is_tested():
    src = open(__file__).read()
    assert all(f'"{c}"' in src for c in list(rra.CODES) + list(rra.PUB_CODES))


# ---------------------------------------------------------------- CHECKs and the affected-rules view
def _code(sql):
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


DDL08 = _code(open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read())
DDL10B = _code(open(os.path.join(ROOT, "sql", "ddl", "10b_reviewer_authority.sql")).read())
MIG_V17 = _code(open(os.path.join(ROOT, "sql", "migrations", "v1_7_003_dssmp_registration.sql")).read())
# v1.8: the latest migration that re-creates the review_record CHECKs (v1_8_001 adds subject dam_lifecycle) must
# carry the DDL bodies; v1_7_003 keeps its own (older) bodies, which still accept the three rule_reversion actions.
MIG = _code(open(os.path.join(ROOT, "sql", "migrations", "v1_8_001_dam_lifecycle.sql")).read())


def _ck(name, sql=DDL08):
    return re.search(rf"ADD CONSTRAINT {name}\s+CHECK \((.*?)\);", sql, re.S).group(1)


RCOLS = ("review_id", "dam_id", "subject_type", "subject_id", "decision", "comment", "action_type", "original_value",
         "corrected_value", "chain_id", "chain_seq", "prev_hash", "record_hash")
GOOD = [dict(action_type=a, decision=d) for a, d in rra.ACTIONS.items()]
BAD = [dict(action_type="rule_reversion_approve", decision="reject"), dict(action_type=None),
       dict(action_type="rule_reversion_apply"), dict(comment=" "), dict(subject_type="rule_reversions"),
       dict(action_type="rule_reversion_propose", decision="propose", subject_type="dam")]


def _row(**kw):
    r = {"review_id": uuid.uuid4().hex, "dam_id": D, "subject_type": "rule_reversion", "subject_id": PID,
         "decision": "approve", "comment": "why", "action_type": "rule_reversion_approve", "original_value": "{}",
         "corrected_value": "{}", "chain_id": D, "chain_seq": 1, "prev_hash": "0" * 64, "record_hash": "a" * 64, **kw}
    return tuple(r[c] for c in RCOLS)


def test_checks_in_ddl_and_migration_are_identical_and_accept_only_the_three_actions():
    for n in ("ck_review_record_subject_decision", "ck_review_record_chain"):
        assert _ck(n) == _ck(n, MIG)
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE r ({', '.join(RCOLS)}, CHECK ({_ck('ck_review_record_subject_decision')}), "
                f"CHECK ({_ck('ck_review_record_chain')}))")
    for kw in GOOD:
        con.execute(f"INSERT INTO r VALUES ({','.join('?' * len(RCOLS))})", _row(**kw))
    for kw in BAD:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(f"INSERT INTO r VALUES ({','.join('?' * len(RCOLS))})", _row(**kw))


def _body(kind, name):
    pat = (rf"CREATE OR REPLACE VIEW {re.escape(name)}\b.*?\bAS\n(.*?);" if kind == "view" else
           rf"CREATE OR REPLACE FUNCTION {re.escape(name)}\(.*?\nRETURN (.*?);")
    return re.search(pat, DDL10B, re.S).group(1)


AFFECTED_ROWS = [
    {**V1},                                                                     # on DS-OLD -> superseded
    {**V1, "threshold_id": "T-2", "dssmp_version_id": "DS-NEW"},                # on the version in force
    {**V1, "threshold_id": "T-3", "dssmp_version_id": None},                    # no version
    {**V1, "threshold_id": "T-4", "is_current": False},                         # history row
    {**V1, "threshold_id": "T-5", "dam_id": "SYN-DAM-02"}]                      # dam without a version in force


def _expected():
    return [("SYN-DAM-01", "T-1", "DS-OLD", "DS-NEW", "dssmp_superseded"),
            ("SYN-DAM-01", "T-3", None, "DS-NEW", "dssmp_missing"),
            ("SYN-DAM-02", "T-5", "DS-OLD", None, "dssmp_superseded")]


def test_affected_rules_view_and_function_in_sqlite():
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE threshold ({', '.join(rp.THRESHOLD_COLS)})")
    con.execute("CREATE TABLE dssmp_version (dssmp_version_id, dam_id, status, effective_from, effective_to)")
    for r in AFFECTED_ROWS:
        con.execute(f"INSERT INTO threshold VALUES ({','.join('?' * len(rp.THRESHOLD_COLS))})",
                    tuple(r[c] for c in rp.THRESHOLD_COLS))
    for k, v in DSSMP.items():
        if k != "DS-ODD":
            con.execute("INSERT INTO dssmp_version VALUES (?,?,?,?,?)", (k, v["dam_id"], v["status"],
                        v["effective_from"], v["effective_to"]))
    sub = lambda b: (b.replace("ref.", "").replace("current_timestamp()", "'2026-11-09T18:00:00Z'")  # noqa: E731
                     .replace("= true", "= 1").replace("p_dam_id", "?").replace("p_to_dssmp_version_id", "?"))
    got = con.execute("SELECT dam_id, threshold_id, dssmp_version_id, current_dssmp_version_id, reason FROM ("
                      + sub(_body("view", "ref.v_dssmp_affected_rules")) + ") ORDER BY 1, 2").fetchall()
    assert got == _expected()
    fn = con.execute(sub(_body("function", "ref.dssmp_affected_rules")) + " ORDER BY 2", (D, "DS-NEW")).fetchall()
    assert [r[1] for r in fn] == ["T-1", "T-3"]


# ---------------------------------------------------------------- PostgreSQL
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")
PGTYPES = {"threshold_version": "INT", "level": "INT", "value": "DOUBLE PRECISION", "deadband": "DOUBLE PRECISION",
           "persistence_on": "INT", "persistence_off": "INT", "effective_from": "TIMESTAMPTZ",
           "effective_to": "TIMESTAMPTZ", "is_current": "BOOLEAN", "cdse_ack_required": "BOOLEAN"}


def _psql(conn, sql):
    return subprocess.run(["psql", conn, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True)


def _lit(v):
    import datetime as dt
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dt.datetime):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


@pytest.fixture()
def pg():
    db = f"rrv_{uuid.uuid4().hex[:8]}"
    assert _psql(PG, f"CREATE DATABASE {db}").returncode == 0
    conn = f"{PG} dbname={db}"
    cols = ", ".join(f"{c} {PGTYPES.get(c, 'TEXT')}" for c in rp.THRESHOLD_COLS)
    r = _psql(conn, f"CREATE TABLE threshold ({cols}, PRIMARY KEY (threshold_id, threshold_version)); "
                    f"CREATE TABLE src ({cols}, merge_action TEXT, merge_version INT, supersedes_version INT, "
                    f"pub_id TEXT); CREATE TABLE dssmp_version (dssmp_version_id TEXT, dam_id TEXT, status TEXT, "
                    f"effective_from TIMESTAMPTZ, effective_to TIMESTAMPTZ)")
    assert r.returncode == 0, r.stderr
    yield conn
    _psql(PG, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _insert(conn, table, rows, cols):
    for r in rows:
        q = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(_lit(r.get(c)) for c in cols)})"
        assert _psql(conn, q).returncode == 0


@live
def test_pg_checks_reversion_merge_and_affected_view(pg):
    # CHECKs exactly as in 08
    cols = ", ".join(f"{c} {'BIGINT' if c == 'chain_seq' else 'TEXT'}" for c in RCOLS)
    r = _psql(pg, f"CREATE TABLE rr ({cols}, CONSTRAINT a CHECK ({_ck('ck_review_record_subject_decision')}), "
                  f"CONSTRAINT b CHECK ({_ck('ck_review_record_chain')}))")
    assert r.returncode == 0, r.stderr
    for kw in GOOD:
        assert _psql(pg, f"INSERT INTO rr VALUES ({', '.join(_lit(v) for v in _row(**kw))})").returncode == 0
    for kw in BAD:
        assert _psql(pg, f"INSERT INTO rr VALUES ({', '.join(_lit(v) for v in _row(**kw))})").returncode != 0
    # the reversion publication through the publisher's MERGE: v1 closed, v2 on the new DSSMP, flag carried over
    _insert(pg, "threshold", [V1], rp.THRESHOLD_COLS)
    ds = rp.plan([outbox()], [V1], DSSMP, ctx())
    _insert(pg, "src", rp.merge_source_rows(ds), rp.THRESHOLD_COLS + ("merge_action", "merge_version",
                                                                      "supersedes_version", "pub_id"))
    for _ in range(2):
        assert _psql(pg, rp.merge_sql("threshold", "src")).returncode == 0
    got = _psql(pg, "SELECT threshold_version || ':' || dssmp_version_id || ':' || is_current || ':' || "
                    "cdse_ack_required FROM threshold ORDER BY 1").stdout.split()
    assert got == ["1:DS-OLD:false:true", "2:DS-NEW:true:true"]
    # the affected-rules view on PG (after the re-version T-1 is no longer affected)
    _insert(pg, "threshold", AFFECTED_ROWS[1:], rp.THRESHOLD_COLS)
    _insert(pg, "dssmp_version", [{"dssmp_version_id": k, **v} for k, v in DSSMP.items() if k != "DS-ODD"],
            ("dssmp_version_id", "dam_id", "status", "effective_from", "effective_to"))
    body = _body("view", "ref.v_dssmp_affected_rules").replace("ref.", "").replace("current_timestamp()",
                                                                                   "current_timestamp")
    out = _psql(pg, f"SELECT dam_id || ':' || threshold_id || ':' || reason FROM ({body}) v ORDER BY 1")
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["SYN-DAM-01:T-3:dssmp_missing", "SYN-DAM-02:T-5:dssmp_superseded"]
