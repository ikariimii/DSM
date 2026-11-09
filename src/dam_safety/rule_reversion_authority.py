"""v1.7 bulk re-version of rules under a superseded DSSMP version (app SCHEMA_CONTRACT §7 item 21; App Dev 1770505,
app/backend/routers/rule_reversion.py). Two checks on the UC side:

1. chain_problems: every subject_type 'rule_reversion' record of audit.review_record (subject_id = proposal_id)
   - rule_reversion_propose: the dam's CDSE (ref.v_dam_reviewer_authority at signed_ts) or dam_admin / admin of the dam
     (canon-2 actor_app_role evidence; canon 1 without evidence -> warn); actor_app_role never grants CDSE authority.
   - rule_reversion_approve / _reject: CDSE authority for the dam at signed_ts; the proposal must be recorded earlier in
     the chain and still undecided; with CDSE_FLAG_REQUIRE_SECOND_APPROVER the proposer may not approve it.
2. publication_problems: a ref.threshold.new_version publication whose payload carries cause 'dssmp_reversion' is
   authorised by a rule_reversion_approve record (payload approval_record_hash) instead of an alarm-rule flag apply:
   the record must be that action for the same dam and proposal (row proposal_id = payload reversion_proposal_id =
   subject_id = corrected_value.proposal_id), the target version (corrected_value.to_dssmp_version_id = the new
   version's dssmp_version_id), list this publication (publication_id = pub_id, threshold_id, threshold_version), be
   signed by a principal holding CDSE authority for the dam at signed_ts (set_by = that reviewer), and the new version
   may differ from the current one ONLY in dssmp_version_id, threshold_version, effective_from and set_by.
   A record not yet copied to UC defers the row for APPROVAL_WAIT, then refuses it. Any other cause is payload_invalid.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Iterable, List, Mapping, Optional

from dam_safety.audit_chain import ACTOR_ROLE_FIELD, actor_roles, canon_of
from dam_safety.rule_flag_authority import cdse_dams_at, second_approver_required

SUBJECT = "rule_reversion"
CAUSE = "dssmp_reversion"
ACTIONS = {"rule_reversion_propose": "propose", "rule_reversion_approve": "approve", "rule_reversion_reject": "reject"}
APPROVAL_WAIT = dt.timedelta(hours=1)
# columns a re-version may change (everything else must equal the current version; effective_to / is_current are
# fixed for every new version by ref_change_publish.column_problems)
REVERSION_MAY_CHANGE = ("dssmp_version_id", "threshold_version", "effective_from", "set_by", "effective_to",
                        "is_current")

CODES = {   # chain codes -> severity
    "rule_reversion_propose_no_authority": "error",
    "rule_reversion_propose_role_unverified": "warn",
    "rule_reversion_approve_not_cdse": "error",
    "rule_reversion_reject_not_cdse": "error",
    "rule_reversion_proposal_missing": "error",       # decision on a proposal not recorded (or already decided)
    "rule_reversion_self_approval": "error",          # only with CDSE_FLAG_REQUIRE_SECOND_APPROVER
    "rule_reversion_subject_mismatch": "error",       # subject_id != corrected_value.proposal_id
}
PUB_CODES = ("reversion_approval_missing", "reversion_approval_wrong_action", "reversion_approval_mismatch",
             "reversion_not_authorized", "reversion_fields_changed")


def _cv(rec) -> dict:
    try:
        v = json.loads(rec.get("corrected_value") or "")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _l(v) -> str:
    return str(v or "").strip().lower()


def _who(rec) -> set:
    return {_l(rec.get("reviewer_id")), _l(rec.get("reviewer_email"))} - {""}


def chain_problems(records: Iterable[Mapping], authority: Iterable[Mapping], app_roles=None,
                   require_second_approver: Optional[bool] = None) -> List[dict]:
    authority = list(authority)
    second = second_approver_required() if require_second_approver is None else bool(require_second_approver)
    recs = sorted((r for r in records if r.get("subject_type") == SUBJECT),
                  key=lambda r: (str(r.get("chain_id")), int(r.get("chain_seq") or 0)))
    open_props, out = {}, []

    def bad(r, code, detail):
        out.append({"review_id": r.get("review_id"), "dam_id": r.get("dam_id"), "chain_seq": r.get("chain_seq"),
                    "action_type": r.get("action_type"), "code": code, "severity": CODES[code], "detail": detail})

    for r in recs:
        act, dam, cv = r.get("action_type"), r.get("dam_id"), _cv(r)
        pid = r.get("subject_id")
        if cv.get("proposal_id") != pid:
            bad(r, "rule_reversion_subject_mismatch", f"subject_id {pid!r} != corrected_value.proposal_id "
                f"{cv.get('proposal_id')!r}")
        is_cdse = dam in cdse_dams_at(r, authority)
        if act == "rule_reversion_propose":
            if not is_cdse:
                roles = actor_roles(r.get(ACTOR_ROLE_FIELD)) if canon_of(r) == 2 else (
                    app_roles(dam, r.get("reviewer_email") or r.get("reviewer_id"), r.get("signed_ts"))
                    if app_roles else None)
                if roles is None:
                    bad(r, "rule_reversion_propose_role_unverified", "non-CDSE proposer, no app-role evidence")
                elif not set(roles) & {"dam_admin", "admin"}:
                    bad(r, "rule_reversion_propose_no_authority", f"roles {sorted(roles)} hold no propose right")
            open_props[(dam, pid)] = r
        elif act in ("rule_reversion_approve", "rule_reversion_reject"):
            verb = ACTIONS[act]
            if not is_cdse:
                bad(r, f"rule_reversion_{verb}_not_cdse", f"{r.get('reviewer_email')} held no CDSE authority for "
                    f"{dam} at {r.get('signed_ts')}")
            prop = open_props.pop((dam, pid), None)
            if prop is None:
                bad(r, "rule_reversion_proposal_missing", f"proposal {pid!r} not recorded earlier or already decided")
            elif verb == "approve" and second and (_who(prop) & _who(r) or _l(cv.get("proposed_by")) in _who(r)):
                bad(r, "rule_reversion_self_approval", f"{r.get('reviewer_email')} proposed {pid}")
    return out


def _utc(v):
    if v in (None, ""):
        return None
    d = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)


def publication_problems(row: Mapping, payload: Mapping, insert: Mapping, current: Optional[Mapping],
                         records_by_hash: Mapping[str, Mapping], authority: Iterable[Mapping], now,
                         normalise, require_second_approver: Optional[bool] = None):
    """Returns 'defer' or a list of '<code>: <detail>' (empty = authorised). `normalise` = the publisher's typed
    ref.threshold row builder (compares typed values)."""
    h = payload.get("approval_record_hash")
    rec = records_by_hash.get(h) if isinstance(h, str) else None
    if rec is None:
        req = _utc(row.get("requested_at"))
        if req is not None and _utc(now) - req < APPROVAL_WAIT:
            return "defer"
        return [f"reversion_approval_missing: approval record {h!r} not in audit.review_record"]
    if rec.get("action_type") != "rule_reversion_approve" or rec.get("subject_type") != SUBJECT:
        return [f"reversion_approval_wrong_action: {rec.get('action_type')!r}/{rec.get('subject_type')!r} is not "
                f"rule_reversion_approve/{SUBJECT}"]
    cv, out = _cv(rec), []
    pid = payload.get("reversion_proposal_id")
    pubs = [p for p in (cv.get("publications") or []) if isinstance(p, dict)]
    checks = [
        (rec.get("dam_id") == row.get("dam_id") == payload.get("dam_id"), "dam_id"),
        (pid is not None and row.get("proposal_id") == pid == rec.get("subject_id") == cv.get("proposal_id"),
         "proposal_id"),
        (cv.get("to_dssmp_version_id") == insert.get("dssmp_version_id"), "to_dssmp_version_id"),
        (any(p.get("publication_id") == row.get("pub_id") and p.get("threshold_id") == insert.get("threshold_id")
             and str(p.get("threshold_version")) == str(insert.get("threshold_version")) for p in pubs),
         "publication not listed in the approval"),
        (_l(insert.get("set_by")) in _who(rec), "set_by != approver"),
    ]
    for ok, what in checks:
        if not ok:
            out.append(f"reversion_approval_mismatch: {what}")
    if rec.get("dam_id") not in cdse_dams_at(rec, authority):
        out.append(f"reversion_not_authorized: {rec.get('reviewer_email')} held no CDSE authority for "
                   f"{rec.get('dam_id')} at {rec.get('signed_ts')}")
    second = second_approver_required() if require_second_approver is None else bool(require_second_approver)
    if second and _l(cv.get("proposed_by")) in _who(rec):
        out.append(f"reversion_not_authorized: self-approval ({rec.get('reviewer_email')} proposed {pid})")
    if current is not None:
        a, b = normalise(insert), normalise(current)
        changed = sorted(c for c in a if c not in REVERSION_MAY_CHANGE and a[c] != b.get(c))
        if changed:
            out.append(f"reversion_fields_changed: {changed} differ from v{current.get('threshold_version')}")
    return out


__all__ = ["ACTIONS", "APPROVAL_WAIT", "CAUSE", "CODES", "PUB_CODES", "REVERSION_MAY_CHANGE", "SUBJECT",
           "chain_problems", "publication_problems"]
