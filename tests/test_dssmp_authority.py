"""v1.7 (Genie 2026-11-09): dssmp_version propose / approve / reject authority (dam_safety.dssmp_authority), the UC
row CHECK shape for those records (migration v1_7_003, sql/ddl/08) and the verify job wiring. Synthetic data only."""
import json
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import dssmp_authority as da  # noqa: E402
import verify_review_chain as vj  # noqa: E402

D, D2 = "SYN-DAM-01", "SYN-DAM-02"
CDSE, CDSE2 = "cdse1@example.org", "cdse2@example.org"
ADMIN, DAMADMIN, ENG = "admin@example.org", "damadmin@example.org", "eng@example.org"
AUTH = [{"dam_id": D, "principal": CDSE, "granted_ts": None, "revoked_ts": None},
        {"dam_id": D2, "principal": CDSE2, "granted_ts": "2026-01-01T00:00:00Z", "revoked_ts": None}]
ROLES = {(D, ADMIN): "admin", (D, DAMADMIN): "dam_admin", (D, ENG): "engineer", (D, CDSE2): "engineer"}
roles = lambda dam, who, at: ROLES.get((dam, who))
SHA = "ab" * 32
_seq = iter(range(1, 10_000))


def rec(verb, who, cv=None, dam=D, subject="DSSMP-1-V2", comment="synthetic rationale", decision=None,
        actor_role=None):
    cv = {"dssmp_version_id": subject, "dam_id": dam, "version_no": 2, "effective_from": "2026-12-01T00:00:00Z",
          "document_sha256": SHA, **(cv or {})}
    r = {"review_id": f"RS-{next(_seq)}", "dam_id": dam, "chain_id": dam, "chain_seq": next(_seq),
         "subject_type": "dssmp_version", "subject_id": subject, "action_type": f"dssmp_version_{verb}",
         "decision": decision or verb, "comment": comment, "reviewer_id": who, "reviewer_email": who,
         "review_level": "CDSE" if who == CDSE else "engineer", "signed_ts": "2026-11-09T15:00:00Z",
         "corrected_value": json.dumps(cv), "original_value": json.dumps({"supersedes_dssmp_version_id": None})}
    if actor_role:
        r.update(canon_version=2, actor_app_role=actor_role)
    return r


def codes(recs, app_roles=roles):
    return [p["code"] for p in da.dssmp_authority_problems(recs, AUTH, app_roles)]


def test_cdse_approves_directly_or_own_proposal():
    assert codes([rec("approve", CDSE)]) == []
    p = rec("propose", CDSE, {"proposal_id": "DP-1"})
    assert codes([p, rec("approve", CDSE, {"proposal_id": "DP-1", "proposed_by": CDSE})]) == []


@pytest.mark.parametrize("who", [DAMADMIN, ADMIN])
def test_dam_admin_and_admin_may_propose_and_the_cdse_decides(who):
    p = rec("propose", who, {"proposal_id": "DP-2"})
    assert codes([p, rec("approve", CDSE, {"proposal_id": "DP-2", "proposed_by": who})]) == []
    p = rec("propose", who, {"proposal_id": "DP-3"})
    assert codes([p, rec("reject", CDSE, {"proposal_id": "DP-3", "proposed_by": who})]) == []


@pytest.mark.parametrize("verb", ["approve", "reject"])
@pytest.mark.parametrize("who", [DAMADMIN, ADMIN])
def test_dam_admin_and_admin_may_never_approve_or_reject(verb, who):
    p = rec("propose", who, {"proposal_id": "DP-4"})
    assert codes([p, rec(verb, who, {"proposal_id": "DP-4", "proposed_by": who})]) == [f"dssmp_{verb}_by_admin"]


def test_canon2_actor_role_is_the_evidence_for_admin_without_app_roles():
    r = rec("approve", ADMIN, actor_role="admin,engineer")
    assert codes([r], app_roles=None) == ["dssmp_approve_by_admin"]
    r = rec("propose", ENG, {"proposal_id": "DP-5"}, actor_role="engineer")
    assert codes([r], app_roles=None) == ["dssmp_propose_no_authority"]
    r = rec("approve", ENG, actor_role="cdse")                 # a 'cdse' token never grants: UC authority decides
    assert codes([r], app_roles=None) == ["dssmp_approve_not_cdse"]


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_engineer_and_other_dams_cdse(verb):
    p = rec("propose", DAMADMIN, {"proposal_id": "DP-6"})
    assert codes([p, rec(verb, ENG, {"proposal_id": "DP-6", "proposed_by": DAMADMIN})]) == [f"dssmp_{verb}_not_cdse"]
    p = rec("propose", DAMADMIN, {"proposal_id": "DP-7"})
    got = codes([p, rec(verb, CDSE2, {"proposal_id": "DP-7", "proposed_by": DAMADMIN})])
    assert got == [f"dssmp_{verb}_cdse_of_other_dam"]


def test_proposal_codes():
    assert codes([rec("propose", ENG, {"proposal_id": "DP-8"})]) == ["dssmp_propose_no_authority"]
    assert codes([rec("propose", "x@example.org", {"proposal_id": "DP-9"})], app_roles=None) == \
        ["dssmp_propose_role_unverified"]
    assert codes([rec("approve", CDSE, {"proposal_id": "DP-X", "proposed_by": DAMADMIN})]) == \
        ["dssmp_approve_proposal_missing"]
    assert codes([rec("reject", CDSE, {"proposal_id": "DP-Y", "proposed_by": DAMADMIN})]) == \
        ["dssmp_reject_proposal_missing"]


def test_record_shape_codes():
    assert codes([rec("approve", CDSE, {"dssmp_version_id": "OTHER"})]) == ["dssmp_subject_mismatch"]
    assert codes([rec("approve", CDSE, {"dam_id": D2})]) == ["dssmp_subject_mismatch"]
    assert codes([rec("approve", CDSE, {"document_sha256": "not-a-hash"})]) == ["dssmp_approval_fields_missing"]
    assert codes([rec("approve", CDSE, {"version_no": None})]) == ["dssmp_approval_fields_missing"]
    assert codes([rec("approve", CDSE, comment=" ")]) == ["dssmp_rationale_missing"]
    assert codes([rec("approve", CDSE, decision="apply")]) == ["dssmp_decision_action_mismatch"]


def test_every_code_is_tested_and_has_a_severity():
    src = open(__file__).read()
    for c, sev in da.CODES.items():
        assert sev in ("error", "warn") and (f'"{c}"' in src or f"dssmp_{{verb}}_{c.split('_', 2)[2]}" in src), c


def test_verify_job_reports_dssmp_authority_errors():
    errs, warns = vj.authority_failures([rec("approve", ADMIN)], AUTH, app_roles=roles, require_second_approver=False)
    assert [e["code"] for e in errs] == ["dssmp_approve_by_admin"] and warns == []


# ---------------------------------------------------------------- UC row CHECK (executed in SQLite)
def _check(name):
    ddl = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    return re.search(rf"ADD CONSTRAINT {name} CHECK \((.*?)\);\n", ddl, re.S).group(1)


COLS = ("subject_type", "decision", "comment", "action_type", "original_value", "corrected_value", "chain_id",
        "chain_seq", "prev_hash", "record_hash", "dam_id", "subject_id")   # subject_id: v1.7 'dam' branch


def _db():
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE r ({', '.join(COLS)}, CHECK ({_check('ck_review_record_subject_decision')}), "
                f"CHECK ({_check('ck_review_record_chain')}))")
    return con


def _row(**kw):
    base = dict(subject_type="dssmp_version", decision="approve", comment="why", action_type="dssmp_version_approve",
                original_value="{}", corrected_value="{}", chain_id=D, chain_seq=1, prev_hash="0" * 64,
                record_hash="1" * 64, dam_id=D, subject_id="DS-2")
    base.update(kw)
    return tuple(base[c] for c in COLS)


@pytest.mark.parametrize("verb", ["propose", "approve", "reject"])
def test_uc_check_accepts_the_three_dssmp_actions(verb):
    _db().execute(f"INSERT INTO r VALUES ({','.join('?' * len(COLS))})",
                  _row(decision=verb, action_type=f"dssmp_version_{verb}"))


@pytest.mark.parametrize("kw", [dict(decision="apply"), dict(action_type="alarm_rule_flag_apply"),
                                dict(comment=" "), dict(action_type=None), dict(subject_type="alarm_rule")])
def test_uc_check_refuses_wrong_shapes(kw):
    with pytest.raises(sqlite3.IntegrityError):
        _db().execute(f"INSERT INTO r VALUES ({','.join('?' * len(COLS))})", _row(**kw))
