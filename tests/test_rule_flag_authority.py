"""§7.13 (app 6f7a1d5): live alarm-rule cdse_ack_required edits. UC row CHECK = shape only (subject alarm_rule,
propose|apply|reject, the three action types, non-blank rationale); authority = engine, against
ref.v_dam_reviewer_authority at signed_ts, one distinct failure code per violation. Synthetic data only."""
import json
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import audit_chain as ac  # noqa: E402
from dam_safety import rule_flag_authority as rfa  # noqa: E402
import verify_review_chain as vj  # noqa: E402

D, D2 = "SYN-DAM-01", "SYN-DAM-02"
CDSE, CDSE2 = "cdse1@example.org", "cdse2@example.org"
ADMIN, DAMADMIN, ENG = "admin@example.org", "damadmin@example.org", "eng@example.org"
AUTH = [{"dam_id": D, "principal": CDSE, "granted_ts": None, "revoked_ts": None},              # DAM_CDSE_ID
        {"dam_id": D2, "principal": CDSE2, "granted_ts": "2026-01-01T00:00:00Z", "revoked_ts": None},
        {"dam_id": D, "principal": "old.cdse@example.org", "granted_ts": "2025-01-01T00:00:00Z",
         "revoked_ts": "2026-06-01T00:00:00Z"}]
ROLES = {(D, ADMIN): "admin", (D, DAMADMIN): "dam_admin", (D, ENG): "engineer", (D, CDSE2): "engineer"}
roles = lambda dam, who, at: ROLES.get((dam, who))
_seq = iter(range(1, 10_000))


def rec(action, who, cv=None, dam=D, level=None, ts="2026-11-09T15:00:00Z", comment="synthetic rationale", decision=None):
    return {"review_id": f"RS-{next(_seq)}", "dam_id": dam, "chain_id": dam, "chain_seq": next(_seq),
            "subject_type": "alarm_rule", "subject_id": "T-1", "action_type": f"alarm_rule_flag_{action}",
            "decision": decision or action, "comment": comment, "reviewer_id": who, "reviewer_email": who,
            "review_level": level or ("CDSE" if who == CDSE else "engineer"), "signed_ts": ts,
            "corrected_value": json.dumps(cv or {})}


def codes(recs, app_roles=roles):
    return [p["code"] for p in rfa.rule_flag_authority_problems(recs, AUTH, app_roles)]


# ------------------------------------------------------------------ allowed paths
def test_cdse_applies_directly_without_a_proposal():
    assert codes([rec("apply", CDSE, {"proposal_id": "RFP-1", "proposed_by": CDSE})]) == []


def test_cdse_applies_own_proposal():
    p = rec("propose", CDSE, {"proposal_id": "RFP-2"})
    assert codes([p, rec("apply", CDSE, {"proposal_id": "RFP-2", "proposed_by": CDSE})]) == []


@pytest.mark.parametrize("who", [ADMIN, DAMADMIN])
def test_admins_propose_and_cdse_applies_or_rejects(who):
    p = rec("propose", who, {"proposal_id": "RFP-3"})
    assert codes([p, rec("apply", CDSE, {"proposal_id": "RFP-3", "proposed_by": who})]) == []
    p = rec("propose", who, {"proposal_id": "RFP-4"})
    assert codes([p, rec("reject", CDSE, {"proposal_id": "RFP-4", "proposed_by": who})]) == []


# ------------------------------------------------------------------ violations, one code each
@pytest.mark.parametrize("who,code", [(ADMIN, "apply_by_admin"), (DAMADMIN, "apply_by_admin"),
                                      (ENG, "apply_not_cdse"), (CDSE2, "apply_cdse_of_other_dam")])
def test_apply_without_cdse_authority(who, code):
    assert codes([rec("apply", who, {"proposal_id": "RFP-5", "proposed_by": who})]) == [code]


def test_admin_trying_to_apply_their_own_proposal():
    p = rec("propose", ADMIN, {"proposal_id": "RFP-6"})
    assert codes([p, rec("apply", ADMIN, {"proposal_id": "RFP-6", "proposed_by": ADMIN})]) == ["apply_by_admin"]


@pytest.mark.parametrize("who,code", [(ENG, "reject_not_cdse"), (ADMIN, "reject_by_admin"),
                                      (DAMADMIN, "reject_by_admin"), (CDSE2, "reject_cdse_of_other_dam")])
def test_reject_without_cdse_authority(who, code):
    """includes the non-CDSE engineer trying to reject"""
    p = rec("propose", DAMADMIN, {"proposal_id": "RFP-7"})
    assert codes([p, rec("reject", who, {"proposal_id": "RFP-7", "proposed_by": DAMADMIN})]) == [code]


def test_engineer_cannot_propose_and_unknown_role_is_a_warning():
    assert codes([rec("propose", ENG, {"proposal_id": "RFP-8"})]) == ["propose_no_authority"]
    probs = rfa.rule_flag_authority_problems([rec("propose", ADMIN, {"proposal_id": "RFP-9"})], AUTH, None)
    assert [(p["code"], p["severity"]) for p in probs] == [("propose_role_unverified", "warn")]


def test_without_app_roles_an_admin_apply_is_still_an_error():
    assert codes([rec("apply", ADMIN, {"proposed_by": ADMIN})], app_roles=None) == ["apply_not_cdse"]


def test_apply_or_reject_of_someone_elses_unrecorded_proposal():
    assert codes([rec("apply", CDSE, {"proposal_id": "RFP-X", "proposed_by": DAMADMIN})]) == ["apply_proposal_missing"]
    assert codes([rec("reject", CDSE, {"proposal_id": "RFP-Y", "proposed_by": DAMADMIN})]) == ["reject_proposal_missing"]


def test_authority_is_taken_at_decision_time():
    old = "old.cdse@example.org"
    assert codes([rec("apply", old, {"proposed_by": old}, ts="2026-03-01T00:00:00Z")]) == []
    assert codes([rec("apply", old, {"proposed_by": old}, ts="2026-07-01T00:00:00Z")]) == ["apply_not_cdse"]


def test_cdse_level_claim_rationale_and_decision_shape():
    assert codes([rec("propose", DAMADMIN, {"proposal_id": "RFP-Z"}, level="CDSE")]) == ["cdse_level_without_authority"]
    assert codes([rec("apply", CDSE, {"proposed_by": CDSE}, comment="  ")]) == ["rationale_missing"]
    assert codes([rec("apply", CDSE, {"proposed_by": CDSE}, decision="approve")]) == ["decision_action_mismatch"]


def test_every_code_is_tested_and_has_a_severity():
    src = open(__file__).read()
    for c, sev in rfa.CODES.items():
        assert sev in ("error", "warn") and f'"{c}"' in src, c


def test_verify_job_fails_on_authority_errors_only():
    errs, warns = vj.authority_failures([rec("apply", ADMIN, {"proposed_by": ADMIN}),
                                         rec("propose", ADMIN, {"proposal_id": "RFP-W"})], AUTH)
    assert [e["code"] for e in errs] == ["apply_not_cdse"] and [w["code"] for w in warns] == ["propose_role_unverified"]


# ------------------------------------------------------------------ UC row CHECK: shape only, no authority
def _check(name):
    ddl = open(os.path.join(ROOT, "sql", "ddl", "08_audit.sql")).read()
    return re.search(rf"ADD CONSTRAINT {name} CHECK \((.*?)\);\n", ddl, re.S).group(1)


COLS = ("dam_id", "subject_type", "decision", "comment", "action_type", "original_value", "corrected_value",
        "chain_id", "chain_seq", "prev_hash", "record_hash", "reviewer_id")


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.execute(f"CREATE TABLE t ({', '.join(COLS)}, CHECK ({_check('ck_review_record_subject_decision')}), "
              f"CHECK ({_check('ck_review_record_chain')}))")
    return c


def _ins(db, **kw):
    good = dict(dam_id=D, subject_type="alarm_rule", decision="apply", comment="why", action_type="alarm_rule_flag_apply",
                original_value="{}", corrected_value="{}", chain_id=D, chain_seq=1, prev_hash="0" * 64,
                record_hash="f" * 64, reviewer_id=ADMIN)
    good.update(kw)
    db.execute(f"INSERT INTO t VALUES ({','.join('?' * len(COLS))})", tuple(good[c] for c in COLS))


def test_uc_check_accepts_the_three_alarm_rule_actions_whoever_signs(db):
    for d in ("propose", "apply", "reject"):
        _ins(db, decision=d, action_type=f"alarm_rule_flag_{d}", reviewer_id=ADMIN)   # authority is NOT a CHECK
    assert "reviewer_id" not in _check("ck_review_record_chain") + _check("ck_review_record_subject_decision")


@pytest.mark.parametrize("bad", [dict(decision="approve", action_type="alarm_rule_flag_apply"),
                                 dict(decision="apply", action_type="alarm_rule_flag_reject"),
                                 dict(action_type=None), dict(comment=" "), dict(comment=None),
                                 dict(subject_type="alarm_event", decision="apply"),
                                 dict(action_type="alarm_rule_flag_delete")])
def test_uc_check_refuses_bad_alarm_rule_rows(db, bad):
    with pytest.raises(sqlite3.IntegrityError):
        _ins(db, **bad)


def test_engine_action_types_match_the_app():
    assert ac.RULE_FLAG_ACTION_TYPES == ("alarm_rule_flag_propose", "alarm_rule_flag_apply", "alarm_rule_flag_reject")
    assert set(ac.RULE_FLAG_ACTION_TYPES) <= set(ac.ACTION_TYPES)
    app = open(os.path.join(ROOT, "app", "backend", "audit_chain.py")).read()
    assert 'RULE_FLAG_ACTION_TYPES = ("alarm_rule_flag_propose", "alarm_rule_flag_apply", "alarm_rule_flag_reject")' in app
