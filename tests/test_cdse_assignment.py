"""v1.7 (B) CDSE bootstrap and change (dam_safety.cdse_assignment): engine authority rules, the UC CHECKs on
audit.review_record and ref.dam_cdse_assignment (executed in SQLite), ref.v_dam_reviewer_authority on the assignment
history and ref.v_dam_go_live_readiness (SQLite), verify_review_chain wiring, the dam_ref_publish task order, and the
assignment MERGE executed on PostgreSQL when DAM_TEST_PG is set. Synthetic principals only."""
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
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import cdse_assignment as ca  # noqa: E402
import verify_review_chain as vj  # noqa: E402

D = "SYN-NEW"
ADMIN, ADMIN2, CDSE1, CDSE2, NOBODY = "admin@x.org", "admin2@x.org", "cdse1@x.org", "cdse2@x.org", "eng@x.org"
ADMINS = [{"principal": ADMIN, "dam_id": "*", "role": "ADMIN", "granted_ts": "2026-01-01T00:00:00Z", "revoked_ts": None},
          {"principal": ADMIN2, "dam_id": "*", "role": "ADMIN", "granted_ts": "2026-01-01T00:00:00Z", "revoked_ts": None},
          {"principal": "old-admin@x.org", "dam_id": "*", "role": "ADMIN", "granted_ts": "2026-01-01T00:00:00Z",
           "revoked_ts": "2026-02-01T00:00:00Z"},
          {"principal": "dam-admin@x.org", "dam_id": D, "role": "ADMIN", "granted_ts": "2026-01-01T00:00:00Z",
           "revoked_ts": None}]
PRE_LIVE = {D: {"status": "onboarding", "live_since": None}}
LIVE = {D: {"status": "ACTIVE", "live_since": "2026-05-01T00:00:00Z"}}


def rec(seq, act, who, ts, cdse=CDSE1, reason="initial_appointment", pid=None, prev=None, **kw):
    """The app's record shape (app/backend/routers/dam_cdse.py _snap)."""
    cv = {"dam_id": D, "kind": "assign" if act == "dam_cdse_assign" else "change", "previous_cdse_id": prev,
          "new_cdse_id": cdse, "change_reason_code": reason, "proposed_by": who, "change_id": pid or f"DCC-{seq}",
          "proposal_id": pid or f"DCC-{seq}", "state": "approved"}
    cv.update(kw.pop("cv", {}))
    r = {"review_id": f"R{seq}", "dam_id": D, "chain_id": D, "chain_seq": seq, "subject_type": "dam", "subject_id": D,
         "action_type": act, "decision": ca.ACTIONS.get(act, "assign"), "comment": "why", "reviewer_id": who,
         "reviewer_email": who, "signed_ts": ts, "record_hash": f"{seq:064x}", "corrected_value": json.dumps(cv),
         "original_value": json.dumps({"cdse_id": prev})}
    r.update(kw)
    return r


def ev(records, assignments=(), legacy=None, dam_state=None, admins=ADMINS, memberships=(), second=False):
    return ca.evaluate(records, list(assignments), legacy or {}, admins, PRE_LIVE if dam_state is None else dam_state,
                       memberships, require_second_approver=second)


def codes(probs):
    return sorted(p["code"] for p in probs)


BOOT = rec(1, "dam_cdse_assign", ADMIN, "2026-03-01T00:00:00Z")
PROP = rec(2, "dam_cdse_change_propose", ADMIN, "2026-06-01T00:00:00Z", cdse=CDSE2, reason="reassignment", pid="P1",
           prev=CDSE1)
APPR = rec(3, "dam_cdse_change_approve", CDSE1, "2026-06-02T00:00:00Z", cdse=CDSE2, reason="reassignment", pid="P1",
           prev=CDSE1, cv={"proposed_by": ADMIN})


# ---------------------------------------------------------------- the four required cases
def test_bootstrap_allowed_for_a_platform_admin_on_a_dam_without_cdse_before_go_live():
    probs, rows = ev([BOOT])
    assert probs == []
    assert [(r["cdse_id"], r["action_type"], r["closes"], r["approval_record_hash"]) for r in rows] == \
        [(CDSE1, "dam_cdse_assign", None, BOOT["record_hash"])]
    assert rows[0]["assignment_id"] == BOOT["record_hash"] and rows[0]["reason_code"] == "initial_appointment"


def test_bootstrap_refused_when_a_cdse_exists():
    assert codes(ev([BOOT, rec(2, "dam_cdse_assign", ADMIN2, "2026-03-05T00:00:00Z", cdse=CDSE2)])[0]) == \
        ["dam_cdse_assign_cdse_exists"]
    probs, rows = ev([BOOT], legacy={D: "legacy@x.org"})                    # pre-v1.7 ref.dam.cdse_id counts
    assert codes(probs) == ["dam_cdse_assign_cdse_exists"] and rows == []


@pytest.mark.parametrize("state", [LIVE, {D: {"status": "active", "live_since": None}}])
def test_bootstrap_refused_when_the_dam_is_live(state):
    late = rec(1, "dam_cdse_assign", ADMIN, "2026-06-01T00:00:00Z")
    probs, rows = ev([late], dam_state=state)
    assert codes(probs) == ["dam_cdse_assign_dam_live"] and rows == []


def test_admin_only_change_after_go_live_is_refused():
    applied = ev([BOOT], dam_state=LIVE)[1]                                 # bootstrap 2026-03 < live 2026-05: ok
    assert ev([BOOT], dam_state=LIVE)[0] == []
    # (1) a second admin assign after go-live
    p, r = ev([BOOT, rec(2, "dam_cdse_assign", ADMIN, "2026-06-01T00:00:00Z", cdse=CDSE2)], applied, dam_state=LIVE)
    assert codes(p) == ["dam_cdse_assign_cdse_exists", "dam_cdse_assign_dam_live"] and r == []
    # (2) a proposal alone changes nothing
    p, r = ev([BOOT, PROP], applied, dam_state=LIVE)
    assert p == [] and r == []
    # (3) the admin approving their own proposal (not the current CDSE), with and without the second-approver switch
    own = rec(3, "dam_cdse_change_approve", ADMIN, "2026-06-02T00:00:00Z", cdse=CDSE2, reason="reassignment", pid="P1",
              prev=CDSE1)
    p, r = ev([BOOT, PROP, own], applied, dam_state=LIVE)
    assert codes(p) == ["dam_cdse_change_approve_not_current_cdse"] and r == []
    p, r = ev([BOOT, PROP, own], applied, dam_state=LIVE, second=True)
    assert codes(p) == ["dam_cdse_change_approve_not_current_cdse", "dam_cdse_change_self_approval"] and r == []


def test_proposal_plus_current_cdse_approval_is_allowed_and_closes_the_previous_assignment():
    applied = ev([BOOT], dam_state=LIVE)[1]
    assignments = [{k: v for k, v in x.items() if k != "closes"} for x in applied]
    p, r = ev([BOOT, PROP, APPR], assignments, dam_state=LIVE)
    assert p == []
    assert [(x["cdse_id"], x["action_type"], x["closes"], x["proposal_record_hash"], x["proposed_by"]) for x in r] == \
        [(CDSE2, "dam_cdse_change_approve", BOOT["record_hash"], PROP["record_hash"], ADMIN)]
    # after the change CDSE1 is no longer current: a second change needs CDSE2
    now = assignments + [{k: v for k, v in x.items() if k != "closes"} for x in r]
    now[0]["effective_to"] = r[0]["effective_from"]
    p2, _ = ev([BOOT, PROP, APPR,
                rec(4, "dam_cdse_change_propose", ADMIN, "2026-07-01T00:00:00Z", cdse="c3@x.org", reason="retirement",
                    pid="P2", prev=CDSE2),
                rec(5, "dam_cdse_change_approve", CDSE1, "2026-07-02T00:00:00Z", cdse="c3@x.org", reason="retirement",
                    pid="P2", prev=CDSE2)],
               now, dam_state=LIVE)
    assert codes(p2) == ["dam_cdse_change_approve_not_current_cdse"]
    # re-verifying the applied history raises nothing and adds nothing (idempotent)
    assert ev([BOOT, PROP, APPR], now, dam_state=LIVE) == ([], [])


def test_change_on_a_legacy_dam_keeps_the_legacy_cdse_as_history():
    prop = rec(2, "dam_cdse_change_propose", ADMIN, "2026-06-01T00:00:00Z", cdse=CDSE2, reason="reassignment", pid="P1",
               prev="legacy@x.org")
    appr = rec(3, "dam_cdse_change_approve", "legacy@x.org", "2026-06-02T00:00:00Z", cdse=CDSE2,
               reason="reassignment", pid="P1", prev="legacy@x.org")
    p, r = ev([prop, appr], legacy={D: "legacy@x.org"}, dam_state=LIVE)
    assert p == []
    assert [(x["assignment_id"], x["cdse_id"], x["effective_from"], str(x["effective_to"])[:10], x["closes"])
            for x in r] == [(f"legacy:{D}", "legacy@x.org", None, "2026-06-02", None),
                            (appr["record_hash"], CDSE2, r[1]["effective_from"], "None", None)]
    src = ca.merge_source_rows(r)
    assert [(s["merge_action"], s["supersedes_id"]) for s in src] == [("insert", None), ("insert", None)]


# ---------------------------------------------------------------- the other codes
@pytest.mark.parametrize("r,code", [
    (rec(1, "dam_cdse_assign", NOBODY, "2026-03-01T00:00:00Z"), "dam_cdse_assign_not_platform_admin"),
    (rec(1, "dam_cdse_assign", "old-admin@x.org", "2026-03-01T00:00:00Z"), "dam_cdse_assign_not_platform_admin"),
    (rec(1, "dam_cdse_assign", "dam-admin@x.org", "2026-03-01T00:00:00Z"), "dam_cdse_assign_not_platform_admin"),
    (rec(1, "dam_cdse_assign", ADMIN, "2026-03-01T00:00:00Z", reason=None), "dam_cdse_reason_code_missing"),
    (rec(1, "dam_cdse_assign", ADMIN, "2026-03-01T00:00:00Z", reason="initial_assignment"),
     "dam_cdse_reason_code_missing"),
    (rec(1, "dam_cdse_assign", ADMIN, "2026-03-01T00:00:00Z", cdse=" "), "dam_cdse_cdse_id_missing"),
    (rec(1, "dam_cdse_assign", ADMIN, "2026-03-01T00:00:00Z", comment=" "), "dam_cdse_rationale_missing"),
    (rec(1, "dam_cdse_assign", ADMIN, "2026-03-01T00:00:00Z", decision="approve"), "dam_cdse_decision_action_mismatch"),
    (rec(1, "dam_cdse_override", ADMIN, "2026-03-01T00:00:00Z"), "dam_cdse_decision_action_mismatch"),
    (rec(1, "dam_cdse_assign", ADMIN, "2026-03-01T00:00:00Z", subject_id="OTHER"), "dam_cdse_subject_mismatch"),
    (rec(1, "dam_cdse_assign", ADMIN, "2026-03-01T00:00:00Z", cv={"dam_id": "OTHER"}), "dam_cdse_subject_mismatch"),
    (rec(1, "dam_cdse_change_propose", NOBODY, "2026-06-01T00:00:00Z", pid="P"),
     "dam_cdse_change_propose_not_platform_admin"),
    (rec(1, "dam_cdse_change_propose", ADMIN, "2026-06-01T00:00:00Z", pid="P"), "dam_cdse_change_propose_no_current_cdse"),
])
def test_refusal_codes(r, code):
    probs, rows = ev([r])
    assert code in codes(probs) and rows == []


def test_approve_needs_a_matching_unconsumed_proposal():
    applied = [{k: v for k, v in x.items() if k != "closes"} for x in ev([BOOT])[1]]
    no_prop = rec(3, "dam_cdse_change_approve", CDSE1, "2026-06-02T00:00:00Z", cdse=CDSE2, reason="reassignment", pid="P9")
    assert codes(ev([BOOT, no_prop], applied)[0]) == ["dam_cdse_change_approve_proposal_missing"]
    wrong = rec(3, "dam_cdse_change_approve", CDSE1, "2026-06-02T00:00:00Z", cdse="c9@x.org", reason="reassignment", pid="P1",
                prev=CDSE1)
    assert codes(ev([BOOT, PROP, wrong], applied)[0]) == ["dam_cdse_change_approve_proposal_mismatch"]
    bad_prop = rec(2, "dam_cdse_change_propose", NOBODY, "2026-06-01T00:00:00Z", cdse=CDSE2, reason="reassignment", pid="P1")
    assert "dam_cdse_change_approve_proposal_missing" in codes(ev([BOOT, bad_prop, APPR], applied)[0])
    twice = rec(4, "dam_cdse_change_approve", CDSE2, "2026-06-03T00:00:00Z", cdse=CDSE2, reason="reassignment", pid="P1")
    assert codes(ev([BOOT, PROP, APPR, twice], applied)[0]) == ["dam_cdse_change_approve_proposal_missing"]


def test_every_code_is_tested():
    src = open(__file__).read()
    assert all(f'"{c}"' in src for c in ca.CODES)
    assert ca.ACTIONS == {"dam_cdse_assign": "assign", "dam_cdse_change_propose": "propose",
                          "dam_cdse_change_approve": "approve", "dam_cdse_change_reject": "reject"}
    assert ca.REASON_CODES == ("initial_appointment", "resignation", "retirement", "reassignment", "incapacity",
                               "regulatory_direction", "correction")


def test_verify_review_chain_runs_the_rules_and_fails_closed_without_context():
    bad = rec(1, "dam_cdse_assign", NOBODY, "2026-03-01T00:00:00Z")
    errs, _ = vj.authority_failures([bad], [], cdse_context=([], {}, ADMINS, PRE_LIVE, []))
    assert [e["code"] for e in errs] == ["dam_cdse_assign_not_platform_admin"]
    errs, _ = vj.authority_failures([BOOT], [])
    assert [e["code"] for e in errs] == ["dam_cdse_context_missing"]
    assert vj.authority_failures([BOOT], [], cdse_context=([], {}, ADMINS, PRE_LIVE, [])) == ([], [])
    src = open(os.path.join(ROOT, "src", "jobs", "verify_review_chain.py")).read()
    assert "cdse_context=cdse_context" in src and "load_context(spark, a.catalog)" in src


def test_task_order_in_dam_ref_publish():
    jobs = yaml.safe_load(open(os.path.join(ROOT, "resources", "jobs.yml")))["resources"]["jobs"]
    tasks = jobs["dam_ref_publish"]["tasks"]
    assert [t["task_key"] for t in tasks] == ["apply_cdse_assignments", "load_dssmp_registrations",
                                               "publish_ref_changes"]
    assert tasks[1]["depends_on"] == [{"task_key": "apply_cdse_assignments"}]


# ---------------------------------------------------------------- UC CHECKs and views, executed in SQLite
def _code(sql):
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


DDL = {f: open(os.path.join(ROOT, "sql", "ddl", f)).read() for f in ("01_ref_hierarchy.sql", "08_audit.sql",
                                                                       "10b_reviewer_authority.sql")}


def _ck(name):
    return re.search(rf"ADD CONSTRAINT {name}\s+CHECK \((.*?)\);", _code("\n".join(DDL.values())), re.S).group(1)


def test_review_record_checks_accept_the_three_actions_and_refuse_others():
    con = sqlite3.connect(":memory:")
    cols = ("review_id", "dam_id", "subject_type", "subject_id", "decision", "comment", "action_type", "original_value",
            "corrected_value", "chain_id", "chain_seq", "prev_hash", "record_hash")
    con.execute(f"CREATE TABLE r ({', '.join(cols)}, CHECK ({_ck('ck_review_record_subject_decision')}), "
                f"CHECK ({_ck('ck_review_record_chain')}))")

    def ins(**kw):
        r = {"review_id": uuid.uuid4().hex, "dam_id": D, "subject_type": "dam", "subject_id": D, "decision": "assign",
             "comment": "why", "action_type": "dam_cdse_assign", "original_value": "{}", "corrected_value": "{}",
             "chain_id": D, "chain_seq": 1, "prev_hash": "0" * 64, "record_hash": "a" * 64, **kw}
        con.execute(f"INSERT INTO r VALUES ({','.join('?' * len(cols))})", tuple(r[c] for c in cols))
    ins()
    ins(action_type="dam_cdse_change_propose", decision="propose")
    ins(action_type="dam_cdse_change_approve", decision="approve")
    ins(action_type="dam_cdse_change_reject", decision="reject")
    for kw in (dict(decision="approve"), dict(action_type=None), dict(subject_id="OTHER"), dict(comment=" "),
               dict(action_type="dam_cdse_override"), dict(action_type="dam_cdse_change_approve", decision="assign"),
               dict(action_type="dam_cdse_change_reject", decision="approve")):
        with pytest.raises(sqlite3.IntegrityError):
            ins(**kw)


def test_assignment_check():
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE a ({', '.join(ca.ASSIGNMENT_COLS)}, CHECK ({_ck('ck_dam_cdse_assignment')}))")

    def ins(**kw):
        r = {"assignment_id": "h1", "dam_id": D, "cdse_id": CDSE1, "effective_from": "2026-03-01", "effective_to": None,
             "action_type": "dam_cdse_assign", "reason_code": "initial_assignment", "assigned_by": ADMIN,
             "proposed_by": None, "proposal_record_hash": None, "approval_record_hash": "h1", **kw}
        con.execute(f"INSERT INTO a VALUES ({','.join('?' * len(ca.ASSIGNMENT_COLS))})",
                    tuple(r[c] for c in ca.ASSIGNMENT_COLS))
    ins()
    ins(assignment_id="h2", approval_record_hash="h2", action_type="dam_cdse_change_approve", proposed_by=ADMIN,
        proposal_record_hash="p")
    ins(assignment_id="legacy:D", action_type="dam_cdse_legacy", effective_from=None, effective_to="2026-03-01",
        approval_record_hash=None)
    for kw in (dict(cdse_id=" "), dict(reason_code=" "), dict(action_type="dam_cdse_override"),
               dict(approval_record_hash=None), dict(assignment_id="other"), dict(effective_to="2026-01-01"),
               dict(action_type="dam_cdse_change_approve", proposal_record_hash=None),
               dict(action_type="dam_cdse_legacy", effective_to=None, approval_record_hash=None)):
        with pytest.raises(sqlite3.IntegrityError):
            ins(**{"assignment_id": "x", "approval_record_hash": "x", **kw} if "assignment_id" not in kw else kw)


def _view_body(name):
    m = re.search(rf"CREATE OR REPLACE VIEW {re.escape(name)}\b.*?\bAS\n(.*?);", _code(DDL["10b_reviewer_authority.sql"]),
                  re.S)
    return m.group(1)


def _views():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.create_function("dam_row_filter", 1, lambda d: 1)
    con.executescript(f"""
      CREATE TABLE dam (dam_id TEXT, cdse_id TEXT);
      CREATE TABLE dam_cdse_assignment ({', '.join(ca.ASSIGNMENT_COLS)});
      CREATE TABLE project_membership (principal TEXT, dam_id TEXT, role TEXT, granted_by TEXT, granted_ts TEXT,
                                       revoked_ts TEXT);
      CREATE TABLE dssmp_version (dssmp_version_id TEXT, dam_id TEXT, status TEXT, effective_from TEXT,
                                  effective_to TEXT);
      INSERT INTO dam VALUES ('NEW', NULL), ('BOOT', '{CDSE1}'), ('LEG', 'legacy@x.org'), ('CHG', '{CDSE2}'),
                             ('GONE', NULL);
      INSERT INTO dam_cdse_assignment (assignment_id, dam_id, cdse_id, effective_from, effective_to, action_type) VALUES
        ('h1', 'BOOT', '{CDSE1}', '2026-03-01 00:00:00', NULL, 'dam_cdse_assign'),
        ('h2', 'CHG', '{CDSE1}', '2026-03-01 00:00:00', '2026-06-02 00:00:00', 'dam_cdse_assign'),
        ('h3', 'CHG', '{CDSE2}', '2026-06-02 00:00:00', NULL, 'dam_cdse_change_approve'),
        ('h4', 'GONE', '{CDSE1}', '2026-03-01 00:00:00', '2026-04-01 00:00:00', 'dam_cdse_assign');
      INSERT INTO dam_cdse_assignment (assignment_id, dam_id, cdse_id, effective_from, effective_to, action_type) VALUES
        ('h5', 'ONLY', '{CDSE1}', '2026-10-01 00:00:00', NULL, 'dam_cdse_assign');   -- not live: no ref.dam row
      INSERT INTO dssmp_version VALUES ('DS1', 'BOOT', 'approved', '2026-01-01 00:00:00', NULL);
    """)
    sub = lambda b: (b.replace("ref.dam_row_filter(", "dam_row_filter(").replace("ref.project_membership", "project_membership")  # noqa: E731
                     .replace("ref.dam_cdse_assignment", "dam_cdse_assignment").replace("ref.dam ", "dam ")
                     .replace("ref.dssmp_version", "dssmp_version")
                     .replace("ref.v_dam_reviewer_authority", "v_dam_reviewer_authority")
                     .replace("CAST(NULL AS TIMESTAMP)", "NULL").replace("current_timestamp()", "'2026-11-09 12:00:00'"))
    con.execute("CREATE TEMP VIEW v_dam_reviewer_authority AS " + sub(_view_body("ref.v_dam_reviewer_authority")))
    con.execute("CREATE TEMP VIEW v_dam_go_live_readiness AS " + sub(_view_body("ref.v_dam_go_live_readiness")))
    return con


def test_authority_view_reads_the_history_and_legacy_only_without_assignments():
    rows = sorted((r["dam_id"], r["principal"], r["authority_source"], r["granted_ts"], r["revoked_ts"])
                  for r in _views().execute("SELECT * FROM v_dam_reviewer_authority"))
    assert rows == [
        ("BOOT", CDSE1, "DAM_CDSE_ASSIGNMENT", "2026-03-01 00:00:00", None),
        ("CHG", CDSE1, "DAM_CDSE_ASSIGNMENT", "2026-03-01 00:00:00", "2026-06-02 00:00:00"),
        ("CHG", CDSE2, "DAM_CDSE_ASSIGNMENT", "2026-06-02 00:00:00", None),
        ("GONE", CDSE1, "DAM_CDSE_ASSIGNMENT", "2026-03-01 00:00:00", "2026-04-01 00:00:00"),
        ("LEG", "legacy@x.org", "DAM_CDSE_ID", None, None),
        ("ONLY", CDSE1, "DAM_CDSE_ASSIGNMENT", "2026-10-01 00:00:00", None)]               # CHG's ref.dam.cdse_id not used twice


def test_go_live_readiness_fails_for_a_dam_without_a_cdse_in_force():
    got = {r["dam_id"]: (r["ready"], r["has_current_dssmp"], r["blocking_reason"])
           for r in _views().execute("SELECT * FROM v_dam_go_live_readiness")}
    assert got == {"NEW": (0, 0, "no_cdse_in_reviewer_authority"), "GONE": (0, 0, "no_cdse_in_reviewer_authority"),
                   "BOOT": (1, 1, None), "LEG": (1, 0, None), "CHG": (1, 0, None), "ONLY": (1, 0, None)}
    fn = _code(DDL["10b_reviewer_authority.sql"])
    assert "CREATE OR REPLACE FUNCTION ref.assert_dam_go_live_ready(p_dam_id STRING)" in fn
    assert "raise_error(concat('dam_go_live_not_ready: '" in fn


def test_migration_creates_the_table_exactly_as_the_ddl():
    pat = r"CREATE TABLE IF NOT EXISTS ref\.dam_cdse_assignment \(.*?\n\);"
    mig = open(os.path.join(ROOT, "sql", "migrations", "v1_7_003_dssmp_registration.sql")).read()
    m, d = re.search(pat, mig, re.S), re.search(pat, DDL["01_ref_hierarchy.sql"], re.S)
    assert m and d and m.group(0) == d.group(0)
    cols = re.findall(r"^\s+(\w+)\s+(?:STRING|TIMESTAMP)", d.group(0), re.M)
    assert tuple(cols) == ca.ASSIGNMENT_COLS


# ---------------------------------------------------------------- the MERGE on PostgreSQL
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")


def _psql(conn, sql):
    return subprocess.run(["psql", conn, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True)


def _lit(v):
    import datetime as dt
    if v is None:
        return "NULL"
    return "'" + (v.isoformat() if isinstance(v, dt.datetime) else str(v)).replace("'", "''") + "'"


@live
def test_assignment_merge_and_dam_sync_on_postgres():
    db = f"cdse_{uuid.uuid4().hex[:8]}"
    assert _psql(PG, f"CREATE DATABASE {db}").returncode == 0
    conn = f"{PG} dbname={db}"
    T = {"effective_from": "TIMESTAMPTZ", "effective_to": "TIMESTAMPTZ"}
    cols = ", ".join(f"{c} {T.get(c, 'TEXT')}" for c in ca.ASSIGNMENT_COLS)
    try:
        assert _psql(conn, f"CREATE TABLE a ({cols}, PRIMARY KEY (assignment_id)); "
                           f"CREATE TABLE s ({cols}, merge_action TEXT, merge_key TEXT, supersedes_id TEXT); "
                           f"CREATE TABLE dam (dam_id TEXT PRIMARY KEY, cdse_id TEXT); "
                           f"INSERT INTO dam VALUES ('{D}', NULL)").returncode == 0
        SRC = ca.ASSIGNMENT_COLS + ("merge_action", "merge_key", "supersedes_id")

        def run(rows):
            assert _psql(conn, "DELETE FROM s").returncode == 0
            for r in ca.merge_source_rows(rows):
                assert _psql(conn, f"INSERT INTO s ({', '.join(SRC)}) VALUES "
                                   f"({', '.join(_lit(r.get(c)) for c in SRC)})").returncode == 0
            for sql in (ca.merge_sql("a", "s"), ca.dam_sync_sql("dam", "a")):
                r = _psql(conn, sql)
                assert r.returncode == 0, r.stderr

        def state():
            return _psql(conn, "SELECT cdse_id || ':' || coalesce(to_char(effective_to AT TIME ZONE 'UTC', "
                               "'YYYY-MM-DD'), '-') FROM a ORDER BY effective_from").stdout.split() + \
                _psql(conn, "SELECT 'dam=' || coalesce(cdse_id, '-') FROM dam").stdout.split()

        boot = ev([BOOT], dam_state=LIVE)[1]
        run(boot)
        assert state() == [f"{CDSE1}:-", f"dam={CDSE1}"]
        run(boot)                                                             # idempotent
        assert state() == [f"{CDSE1}:-", f"dam={CDSE1}"]
        assignments = [{k: v for k, v in x.items() if k != "closes"} for x in boot]
        change = ev([BOOT, PROP, APPR], assignments, dam_state=LIVE)[1]
        stale = copy.deepcopy(change)
        stale[0]["closes"] = "not-the-open-one"
        run(stale)                                                            # stale: nothing written
        assert state() == [f"{CDSE1}:-", f"dam={CDSE1}"]
        run(change)
        assert state() == [f"{CDSE1}:2026-06-02", f"{CDSE2}:-", f"dam={CDSE2}"]
    finally:
        _psql(PG, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


# ---------------------------------------------------------------- §7 item 20 additions: reject, memberships, not live
def test_no_ref_dam_row_means_not_live_and_reject_consumes_the_proposal():
    assert ev([BOOT], dam_state={}) == ev([BOOT])                            # app: no ref.dam row = not live
    applied = [{k: v for k, v in x.items() if k != "closes"} for x in ev([BOOT])[1]]
    rej = rec(3, "dam_cdse_change_reject", CDSE1, "2026-06-02T00:00:00Z", cdse=CDSE2, reason="reassignment", pid="P1",
              prev=CDSE1)
    p, r = ev([BOOT, PROP, rej], applied)
    assert p == [] and r == []
    p, _ = ev([BOOT, PROP, rej, APPR], applied)                             # rejected proposal cannot be approved
    assert codes(p) == ["dam_cdse_change_approve_proposal_missing"]
    p, _ = ev([BOOT, PROP, rec(3, "dam_cdse_change_reject", NOBODY, "2026-06-02T00:00:00Z", pid="P1", prev=CDSE1)],
              applied)
    assert codes(p) == ["dam_cdse_change_reject_not_current_cdse"]
    p, _ = ev([BOOT, rec(3, "dam_cdse_change_reject", CDSE1, "2026-06-02T00:00:00Z", pid="P7", prev=CDSE1)], applied)
    assert codes(p) == ["dam_cdse_change_reject_proposal_missing"]


def test_a_membership_cdse_may_decide_but_the_new_cdse_never_approves_its_own_appointment():
    applied = [{k: v for k, v in x.items() if k != "closes"} for x in ev([BOOT])[1]]
    deputy = [{"principal": "deputy@x.org", "dam_id": D, "role": "CDSE", "granted_ts": "2026-04-01T00:00:00Z",
               "revoked_ts": None}]                                       # granted after the bootstrap
    by_deputy = rec(3, "dam_cdse_change_approve", "deputy@x.org", "2026-06-02T00:00:00Z", cdse=CDSE2,
                    reason="reassignment", pid="P1", prev=CDSE1)
    assert ev([BOOT, PROP, by_deputy], applied, memberships=deputy)[0] == []
    by_new = rec(3, "dam_cdse_change_approve", CDSE2, "2026-06-02T00:00:00Z", cdse=CDSE2, reason="reassignment",
                 pid="P1", prev=CDSE1)
    assert codes(ev([BOOT, PROP, by_new], applied)[0]) == ["dam_cdse_change_approve_not_current_cdse"]
    assert codes(ev([rec(1, "dam_cdse_assign", ADMIN, "2026-05-01T00:00:00Z")], memberships=deputy)[0]) == \
        ["dam_cdse_assign_cdse_exists"]                                   # a membership CDSE blocks a bootstrap


# ---------------------------------------------------------------- the load plan (dam_safety.cdse_load)
from dam_safety import cdse_load as cl  # noqa: E402

NOW = "2026-11-09T18:00:00Z"


def ob(r, kind="assign", **kw):
    cv = json.loads(r["corrected_value"])
    row = {"outbox_id": f"DCO-{r['chain_seq']}", "dam_id": D, "kind": kind, "previous_cdse_id": cv["previous_cdse_id"],
           "new_cdse_id": cv["new_cdse_id"], "change_reason_code": cv["change_reason_code"], "proposed_by": ADMIN,
           "approved_by": r["reviewer_email"], "approved_at": r["signed_ts"], "approval_record_hash": r["record_hash"],
           "status": "pending", "reason_code": None, "message": None, "processed_at": None}
    row.update(kw)
    return row


def load(pending, records, assignments=(), dam_cdse=None, dam_state=None, legacy=None, now=NOW):
    return cl.plan(pending, records, list(assignments), legacy or {}, ADMINS,
                   PRE_LIVE if dam_state is None else dam_state, dam_cdse or {}, now, (), False)


def test_load_assign_then_change_and_audit_rows():
    d = load([ob(BOOT)], [BOOT])[0]
    assert (d["decision"], d["status"], d["reason_code"]) == ("applied", "loaded", None)
    assert [x["assignment_id"] for x in d["rows"]] == [BOOT["record_hash"]]
    applied = [{k: v for k, v in x.items() if k not in ("closes", "for_record")} for x in d["rows"]]
    assert load([ob(BOOT)], [BOOT], applied)[0]["decision"] == "already_applied"
    c = load([ob(APPR, "change")], [BOOT, PROP, APPR], applied, dam_state=LIVE)[0]
    assert c["status"] == "loaded" and [(x["closes"], x["cdse_id"]) for x in c["rows"]] == [(BOOT["record_hash"], CDSE2)]
    a = cl.audit_rows([d, c], "run-1", NOW)
    assert [(x["event_id"], x["event_type"], x["subject_id"]) for x in a] == [
        (f"cdse_load:DCO-1:loaded", "CDSE_ASSIGNMENT_LOAD", D), (f"cdse_load:DCO-3:loaded", "CDSE_ASSIGNMENT_LOAD", D)]
    assert json.loads(a[1]["payload"])["approval_record_hash"] == APPR["record_hash"]
    assert cl.outbox_updates([d], "T") == [("loaded", None, d["message"], "T", "DCO-1")]


def test_load_refusals_cdse_exists_and_cdse_stale():
    d = load([ob(BOOT)], [BOOT], dam_cdse={D: "someone@x.org"})[0]
    assert (d["status"], d["reason_code"]) == ("rejected", "cdse_exists")
    applied = [{k: v for k, v in x.items() if k not in ("closes", "for_record")} for x in load([ob(BOOT)], [BOOT])[0]["rows"]]
    other = rec(9, "dam_cdse_assign", ADMIN2, "2026-03-02T00:00:00Z", cdse=CDSE2)
    assert load([ob(other)], [BOOT, other], applied)[0]["reason_code"] == "cdse_exists"
    stale = ob(APPR, "change", previous_cdse_id="former@x.org")
    s = load([stale], [BOOT, PROP, APPR], applied, dam_state=LIVE)[0]
    assert (s["status"], s["reason_code"]) == ("rejected", "approval_record_mismatch")   # row disagrees with record
    rec_stale = rec(3, "dam_cdse_change_approve", CDSE1, "2026-06-02T00:00:00Z", cdse=CDSE2, reason="reassignment",
                    pid="P1", prev="former@x.org")
    prop_stale = rec(2, "dam_cdse_change_propose", ADMIN, "2026-06-01T00:00:00Z", cdse=CDSE2, reason="reassignment",
                     pid="P1", prev="former@x.org")
    s = load([ob(rec_stale, "change")], [BOOT, prop_stale, rec_stale], applied, dam_state=LIVE)[0]
    assert (s["status"], s["reason_code"]) == ("rejected", "cdse_stale")
    ok = load([ob(APPR, "change")], [BOOT, PROP, APPR], applied, dam_state=LIVE)
    cl.mark_merge_skipped(ok, set())
    assert (ok[0]["status"], ok[0]["reason_code"]) == ("rejected", "cdse_stale")


@pytest.mark.parametrize("kw,code", [
    (dict(kind="other"), "invalid_value"), (dict(change_reason_code="initial_assignment"), "invalid_value"),
    (dict(approval_record_hash="short"), "invalid_value"), (dict(new_cdse_id=None), "missing_required_column"),
    (dict(new_cdse_id="evil@x.org"), "approval_record_mismatch"), (dict(approved_by=NOBODY), "approval_record_mismatch"),
    (dict(kind="change", previous_cdse_id=CDSE1), "approval_record_wrong_action"),
    (dict(approval_record_hash="e" * 64), "approval_record_missing")])
def test_load_row_and_record_refusals(kw, code):
    d = load([ob(BOOT, **kw)], [BOOT])[0]
    assert (d["status"], d["reason_code"]) == ("rejected", code), d["message"]


def test_load_defers_within_the_wait_and_refuses_unauthorized_records():
    d = load([ob(BOOT, approval_record_hash="e" * 64)], [BOOT], now="2026-03-01T00:30:00Z")[0]
    assert d["status"] is None and d["decision"] == "deferred"
    bad = rec(1, "dam_cdse_assign", NOBODY, "2026-03-01T00:00:00Z")
    d = load([ob(bad, approved_by=NOBODY)], [bad])[0]
    assert d["reason_code"] == "approval_not_authorized" and "dam_cdse_assign_not_platform_admin" in d["message"]
    live = load([ob(BOOT)], [BOOT], dam_state={D: {"status": "ACTIVE", "live_since": "2026-02-01T00:00:00Z"}})[0]
    assert live["reason_code"] == "approval_not_authorized" and "dam_cdse_assign_dam_live" in live["message"]


def test_every_load_code_is_tested():
    src = open(__file__).read()
    assert all(f'"{c}"' in src for c in cl.CODES)


def test_job_sql_and_wiring():
    import apply_cdse_assignments as job
    assert job.UPDATE_SQL.split(" SET ")[1].split(" WHERE ")[0] == \
        "status = %s, reason_code = %s, message = %s, processed_at = %s"
    assert "FROM dam_cdse_outbox WHERE status = 'pending'" in job.OUTBOX_SQL
    src = open(os.path.join(ROOT, "src", "jobs", "apply_cdse_assignments.py")).read()
    order = [src.index(x) for x in ("merge_sql(tgt", "mark_merge_skipped(", "dam_sync_sql(", "audit_insert_sql(",
                                    "cur.execute(UPDATE_SQL")]
    assert order == sorted(order)


@live
def test_live_audit_insert_is_idempotent_and_outbox_flow_as_publisher(tmp_path):
    db = f"cdl_{uuid.uuid4().hex[:8]}"
    app, pub = f"cdl_app_{db[-8:]}", f"cdl_pub_{db[-8:]}"
    assert _psql(PG, f"CREATE DATABASE {db}").returncode == 0
    assert _psql(PG, f"CREATE ROLE {app} LOGIN; CREATE ROLE {pub} LOGIN").returncode == 0
    conn = f"{PG} dbname={db}"
    try:
        cols = ", ".join(f"{c} {'TIMESTAMPTZ' if c.endswith('_ts') else 'TEXT'}" for c in cl.EVENT_COLS)
        assert _psql(conn, f"CREATE TABLE event_log ({cols}); CREATE TABLE src ({cols})").returncode == 0
        d = load([ob(BOOT)], [BOOT])
        for r in cl.audit_rows(d, "run-1", NOW):
            vals = ", ".join(_lit(r[c]) for c in cl.EVENT_COLS)
            assert _psql(conn, f"INSERT INTO src VALUES ({vals})").returncode == 0
        for _ in range(2):
            assert _psql(conn, cl.audit_insert_sql("event_log", "src")).returncode == 0
        assert _psql(conn, "SELECT count(*) || ':' || min(event_type) FROM event_log").stdout.split() == \
            ["1:CDSE_ASSIGNMENT_LOAD"]
        # Lakebase: the app's outbox row, read and decided by the job's own SQL as the publisher login
        env = dict(os.environ, PGOPTIONS=f"-c dam.app_role={app} -c dam.publisher_login={pub}")
        r = subprocess.run(["psql", conn, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f",
                            os.path.join(ROOT, "lakebase", "001_app_schema.sql")], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        row = ob(BOOT)
        cols = [c for c in cl.OUTBOX_COLS]
        assert _psql(f"{conn} user={app}", f"INSERT INTO dam_app.dam_cdse_outbox ({', '.join(cols)}) VALUES "
                                            f"({', '.join(_lit(row[c]) for c in cols)})").returncode == 0
        import psycopg
        from psycopg.rows import dict_row
        import apply_cdse_assignments as job
        with psycopg.connect(f"{conn} user={pub}") as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute("SET search_path TO dam_app")
            cur.execute(job.OUTBOX_SQL)
            pending = cur.fetchall()
            ds = load(pending, [BOOT])
            for u in cl.outbox_updates(ds, NOW):
                cur.execute(job.UPDATE_SQL, u)
            c.commit()
        assert _psql(conn, "SELECT status FROM dam_app.dam_cdse_outbox").stdout.split() == ["loaded"]
    finally:
        _psql(PG, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        _psql(PG, f"DROP ROLE IF EXISTS {app}")
        _psql(PG, f"DROP ROLE IF EXISTS {pub}")
