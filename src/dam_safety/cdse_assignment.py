"""v1.7 (Genie 2026-11-09 (B); app SCHEMA_CONTRACT §7 item 20): CDSE bootstrap and change for a dam, as signed
chain records (audit.review_record / Lakebase review_signoff, subject_type 'dam', subject_id = chain_id = dam_id),
re-checked by the engine and applied (src/jobs/apply_cdse_assignments.py, from the app outbox dam_app.dam_cdse_outbox)
to the SCD2 history ref.dam_cdse_assignment (migration v1_7_003) that ref.v_dam_reviewer_authority reads.

Actions (action_type -> decision; names fixed by app §7 item 20):
  dam_cdse_assign          -> assign   platform admin sets the FIRST CDSE, only while the dam has no CDSE and is not
                                       live (bootstrap)
  dam_cdse_change_propose  -> propose  platform admin proposes a new CDSE for a dam that has one
  dam_cdse_change_approve  -> approve  a CURRENT CDSE of the dam approves that proposal; only then does it apply
  dam_cdse_change_reject   -> reject   a CURRENT CDSE of the dam rejects it
No admin-only override after go-live: a dam_cdse_assign on a dam that has a CDSE or is live is refused, and a change
needs a current CDSE's approve record.

Record shape (the app's, dam_cdse.py): corrected_value JSON {dam_id, kind (assign|change), previous_cdse_id,
new_cdse_id, change_reason_code, proposed_by, change_id, proposal_id (= change_id), state[, outbox_id]};
original_value JSON {cdse_id: the CDSE before (null for assign)}; non-blank comment (rationale). change_reason_code
is REQUIRED, one of REASON_CODES (app enum).

Authority inputs (all at the record's signed_ts; actor_app_role is never authority):
  platform admin  ref.project_membership role ADMIN on dam_id '*' (granted_ts <= t < revoked_ts)
  current CDSE    the designated CDSE: the ref.dam_cdse_assignment row open just before t (effective_from < t <=
                  effective_to, or open; a dam with no assignment row falls back to its legacy ref.dam.cdse_id), or a
                  ref.project_membership CDSE grant for the dam in force at t (as the app's is_cdse; an assignment
                  that starts AT t is the record's own and never counts)
  live            app §7 item 20: a dam with no ref.dam row is not live. With a row: live iff live_since (first
                  ref.threshold effective_from) <= t when known, else live unless its status is pre-live
                  (NULL | onboarding | draft | pending | planned).
Self-approval (proposer approving) is refused only when CDSE_FLAG_REQUIRE_SECOND_APPROVER is on (PROVISIONAL, same
switch as the app and the alarm-rule rules).
Codes (CODES; all errors):
  dam_cdse_rationale_missing, dam_cdse_decision_action_mismatch, dam_cdse_subject_mismatch,
  dam_cdse_reason_code_missing, dam_cdse_cdse_id_missing,
  dam_cdse_assign_not_platform_admin, dam_cdse_assign_cdse_exists, dam_cdse_assign_dam_live,
  dam_cdse_change_propose_not_platform_admin, dam_cdse_change_propose_no_current_cdse,
  dam_cdse_change_{approve|reject}_not_current_cdse, dam_cdse_change_{approve|reject}_proposal_missing,
  dam_cdse_change_approve_proposal_mismatch, dam_cdse_change_self_approval.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Iterable, List, Mapping, Optional

from dam_safety.rule_flag_authority import _utc, _who, second_approver_required

SUBJECT = "dam"
ACTIONS = {"dam_cdse_assign": "assign", "dam_cdse_change_propose": "propose", "dam_cdse_change_approve": "approve",
           "dam_cdse_change_reject": "reject"}
REASON_CODES = ("initial_appointment", "resignation", "retirement", "reassignment", "incapacity",
                "regulatory_direction", "correction")
APPLYING = ("dam_cdse_assign", "dam_cdse_change_approve")
PRE_LIVE = ("", "onboarding", "draft", "pending", "planned")
LEGACY_ACTION = "dam_cdse_legacy"
ASSIGNMENT_COLS = ("assignment_id", "dam_id", "cdse_id", "effective_from", "effective_to", "action_type",
                   "reason_code", "assigned_by", "proposed_by", "proposal_record_hash", "approval_record_hash")
CODES = (
    "dam_cdse_rationale_missing", "dam_cdse_decision_action_mismatch", "dam_cdse_subject_mismatch",
    "dam_cdse_reason_code_missing", "dam_cdse_cdse_id_missing",
    "dam_cdse_assign_not_platform_admin", "dam_cdse_assign_cdse_exists", "dam_cdse_assign_dam_live",
    "dam_cdse_change_propose_not_platform_admin", "dam_cdse_change_propose_no_current_cdse",
    "dam_cdse_change_approve_not_current_cdse", "dam_cdse_change_approve_proposal_missing",
    "dam_cdse_change_reject_not_current_cdse", "dam_cdse_change_reject_proposal_missing",
    "dam_cdse_change_approve_proposal_mismatch", "dam_cdse_change_self_approval",
)


def _cv(rec, field="corrected_value") -> dict:
    try:
        v = json.loads(rec.get(field) or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _t(v) -> Optional[dt.datetime]:
    return None if v in (None, "") else _utc(v)


def _norm(p) -> str:
    return str(p or "").strip().lower()


def is_platform_admin(rec, admins: Iterable[Mapping]) -> bool:
    who, at = _who(rec), _utc(rec["signed_ts"])
    for a in admins:
        if _norm(a.get("principal")) in who and str(a.get("dam_id", "*")) == "*" \
                and str(a.get("role", "ADMIN")).upper() == "ADMIN":
            g, r = _t(a.get("granted_ts")), _t(a.get("revoked_ts"))
            if (g is None or g <= at) and (r is None or r > at):
                return True
    return False


def current_cdse(dam_id, at, assignments: Iterable[Mapping], legacy: Mapping) -> Optional[str]:
    """The dam's designated CDSE just before `at` (an assignment that starts exactly at `at` is the record's own)."""
    rows = [a for a in assignments if a.get("dam_id") == dam_id]
    if not rows:
        return legacy.get(dam_id) or None
    for a in rows:
        f, e = _t(a.get("effective_from")), _t(a.get("effective_to"))
        if (f is None or f < at) and (e is None or e >= at):
            return a.get("cdse_id")
    return None


def is_live(dam_id, at, dam_state: Mapping) -> bool:
    st = dam_state.get(dam_id)
    if st is None:                            # no ref.dam row: not live (app §7 item 20)
        return False
    since = _t(st.get("live_since"))
    if since is not None:                     # a known go-live time decides (no retroactive refusal)
        return since <= at
    return _norm(st.get("status")) not in PRE_LIVE


def membership_cdse_at(rec, memberships: Iterable[Mapping]) -> bool:
    who, at = _who(rec), _utc(rec["signed_ts"])
    for m in memberships:
        if m.get("dam_id") == rec.get("dam_id") and _norm(m.get("principal")) in who \
                and str(m.get("role", "CDSE")).upper() == "CDSE":
            g, r = _t(m.get("granted_ts")), _t(m.get("revoked_ts"))
            if (g is None or g <= at) and (r is None or r > at):
                return True
    return False


def evaluate(records: Iterable[Mapping], assignments: Iterable[Mapping], legacy: Mapping, admins: Iterable[Mapping],
             dam_state: Mapping, memberships: Iterable[Mapping] = (), require_second_approver=None) -> tuple:
    """Replays every subject_type 'dam' record in chain order. Returns (problems, new_rows): problems as
    [{review_id, dam_id, chain_seq, action_type, code, severity, detail}]; new_rows = the assignment rows a valid
    assign / change_approve adds that are not yet in `assignments` (by approval_record_hash), each with 'closes' =
    the assignment_id it ends (None: first) and, for a legacy dam, a legacy row first."""
    assignments = [dict(a) for a in assignments]
    admins, memberships = list(admins), list(memberships)
    second = second_approver_required() if require_second_approver is None else bool(require_second_approver)
    have = {a.get("approval_record_hash") for a in assignments if a.get("approval_record_hash")}
    recs = sorted((r for r in records if r.get("subject_type") == SUBJECT),
                  key=lambda r: (str(r.get("chain_id")), int(r.get("chain_seq") or 0)))
    proposals, used, out, new_rows = {}, set(), [], []

    def bad(r, code, detail):
        out.append({"review_id": r.get("review_id"), "dam_id": r.get("dam_id"), "chain_seq": r.get("chain_seq"),
                    "action_type": r.get("action_type"), "code": code, "severity": "error", "detail": detail})

    for r in recs:
        n0 = len(out)
        act, dam, cv, at = r.get("action_type"), r.get("dam_id"), _cv(r), _utc(r["signed_ts"])
        if not str(r.get("comment") or "").strip():
            bad(r, "dam_cdse_rationale_missing", "every dam CDSE record needs a non-blank rationale")
        verb = ACTIONS.get(act)
        if verb is None or verb != r.get("decision"):
            bad(r, "dam_cdse_decision_action_mismatch", f"action_type {act!r} with decision {r.get('decision')!r}")
            continue
        if str(r.get("subject_id") or "") != str(dam) or cv.get("dam_id") not in (None, dam) \
                or str(r.get("chain_id") or dam) != str(dam):
            bad(r, "dam_cdse_subject_mismatch", f"subject_id {r.get('subject_id')!r} / corrected_value dam "
                f"{cv.get('dam_id')!r} / chain {r.get('chain_id')!r} vs record dam {dam!r}")
        if str(cv.get("change_reason_code") or "") not in REASON_CODES:
            bad(r, "dam_cdse_reason_code_missing", f"change_reason_code {cv.get('change_reason_code')!r} is not one "
                f"of {', '.join(REASON_CODES)}")
        new = _norm(cv.get("new_cdse_id"))
        if not new:
            bad(r, "dam_cdse_cdse_id_missing", "corrected_value.new_cdse_id (the new CDSE) is required")
        cur = current_cdse(dam, at, assignments, legacy)
        admin = is_platform_admin(r, admins)
        proposal = None
        if verb == "assign":
            if not admin:
                bad(r, "dam_cdse_assign_not_platform_admin", "only a platform admin (ref.project_membership ADMIN on "
                    "'*') may set the first CDSE")
            if cur or any(m.get("dam_id") == dam and str(m.get("role", "CDSE")).upper() == "CDSE"
                          and (_t(m.get("granted_ts")) is None or _t(m.get("granted_ts")) <= at)
                          and (_t(m.get("revoked_ts")) is None or _t(m.get("revoked_ts")) > at) for m in memberships):
                bad(r, "dam_cdse_assign_cdse_exists", f"{dam} already has a CDSE ({cur or 'membership'}); use "
                    "dam_cdse_change_propose + dam_cdse_change_approve")
            if is_live(dam, at, dam_state):
                bad(r, "dam_cdse_assign_dam_live", f"{dam} is live at signed_ts; no admin-only CDSE assignment after "
                    "go-live")
        elif verb == "propose":
            if not admin:
                bad(r, "dam_cdse_change_propose_not_platform_admin", "only a platform admin may propose a CDSE change")
            if not cur:
                bad(r, "dam_cdse_change_propose_no_current_cdse", f"{dam} has no designated CDSE; a first CDSE is a "
                    "dam_cdse_assign (only before go-live)")
            if len(out) == n0 and cv.get("proposal_id"):
                proposals[(dam, str(cv["proposal_id"]))] = r
            continue
        else:
            is_cur = (cur and _norm(cur) in _who(r)) or membership_cdse_at(r, memberships)
            if not is_cur:
                bad(r, f"dam_cdse_change_{verb}_not_current_cdse", f"signer is not a current CDSE of {dam} "
                    f"(designated: {cur!r})")
            key = (dam, str(cv.get("proposal_id") or ""))
            proposal = proposals.get(key)
            if proposal is None or key in used:
                bad(r, f"dam_cdse_change_{verb}_proposal_missing", f"proposal {cv.get('proposal_id')!r} is not a "
                    "valid unconsumed dam_cdse_change_propose earlier in the chain")
            elif verb == "approve":
                pcv = _cv(proposal)
                if _norm(pcv.get("new_cdse_id")) != new or \
                        _norm(pcv.get("previous_cdse_id")) != _norm(cv.get("previous_cdse_id")):
                    bad(r, "dam_cdse_change_approve_proposal_mismatch", "new_cdse_id / previous_cdse_id differ from "
                        "the proposal's")
                if second and _who(proposal) & _who(r):
                    bad(r, "dam_cdse_change_self_approval", "CDSE_FLAG_REQUIRE_SECOND_APPROVER is on: the proposer "
                        "may not approve their own CDSE change")
            if len(out) == n0:
                used.add(key)
            if verb == "reject":
                continue
        if len(out) > n0:
            continue
        # valid assign / approve: the new designated CDSE from signed_ts on
        prev = next((a for a in assignments if a.get("dam_id") == dam and a.get("effective_to") is None), None)
        rows = []
        if prev is None and cur:                          # legacy dam: keep its pre-v1.7 CDSE as history
            prev = {"assignment_id": f"legacy:{dam}", "dam_id": dam, "cdse_id": cur, "effective_from": None,
                    "effective_to": at, "action_type": LEGACY_ACTION, "reason_code": "legacy_ref_dam_cdse_id",
                    "assigned_by": None, "proposed_by": None, "proposal_record_hash": None, "approval_record_hash": None}
            rows.append({**prev, "closes": None, "for_record": r["record_hash"]})
            assignments.append(prev)
            closes = None
        else:
            closes = prev["assignment_id"] if prev else None
            if prev:
                prev["effective_to"] = at
        row = {"assignment_id": r["record_hash"], "dam_id": dam, "cdse_id": str(cv.get("new_cdse_id")).strip(),
               "effective_from": at, "effective_to": None, "action_type": act,
               "reason_code": cv.get("change_reason_code"),
               "assigned_by": r.get("reviewer_email") or r.get("reviewer_id"),
               "proposed_by": (proposal.get("reviewer_email") or proposal.get("reviewer_id")) if proposal else None,
               "proposal_record_hash": proposal.get("record_hash") if proposal else None,
               "approval_record_hash": r["record_hash"]}
        assignments.append(dict(row))
        rows.append({**row, "closes": closes, "for_record": r["record_hash"]})
        if r["record_hash"] not in have:
            new_rows.extend(rows)
    return out, new_rows


def merge_source_rows(new_rows: Iterable[Mapping]) -> List[dict]:
    """'insert' rows for every new assignment (legacy history row included) and a 'close' row for the one it ends.
    supersedes_id = the open assignment the plan expected (stale re-check inside the MERGE)."""
    out = []
    for r in new_rows:
        base = {c: r.get(c) for c in ASSIGNMENT_COLS}
        if r["action_type"] == LEGACY_ACTION:
            out.append({**base, "merge_action": "insert", "merge_key": r["assignment_id"], "supersedes_id": None})
            continue
        out.append({**base, "merge_action": "insert", "merge_key": r["assignment_id"], "supersedes_id": r["closes"]})
        if r["closes"]:
            out.append({**base, "merge_action": "close", "merge_key": r["closes"], "supersedes_id": r["closes"]})
    return out


def merge_sql(target: str, source: str) -> str:
    """ONE conditional MERGE: rows are used only while the dam's open assignment is still supersedes_id (NULL: none
    open); a close sets only effective_to of that row; existing rows are never otherwise changed."""
    cols = ", ".join(ASSIGNMENT_COLS)
    vals = ", ".join(f"s.{c}" for c in ASSIGNMENT_COLS)
    return (
        f"MERGE INTO {target} AS t\n"
        f"USING (SELECT s.* FROM {source} AS s WHERE\n"
        f"         (s.supersedes_id IS NULL AND NOT EXISTS (SELECT 1 FROM {target} AS c WHERE c.dam_id = s.dam_id\n"
        f"            AND c.effective_to IS NULL))\n"
        f"      OR (s.supersedes_id IS NOT NULL AND EXISTS (SELECT 1 FROM {target} AS c WHERE c.dam_id = s.dam_id\n"
        f"            AND c.assignment_id = s.supersedes_id AND c.effective_to IS NULL))) AS s\n"
        f"ON t.assignment_id = s.merge_key\n"
        f"WHEN MATCHED AND s.merge_action = 'close' AND t.effective_to IS NULL\n"
        f"  THEN UPDATE SET effective_to = s.effective_from\n"
        f"WHEN NOT MATCHED AND s.merge_action = 'insert'\n"
        f"  THEN INSERT ({cols}) VALUES ({vals})")


def dam_sync_sql(dam_table: str, assignment_table: str) -> str:
    """Keep the denormalized ref.dam.cdse_id equal to the open assignment (authority reads the assignment table)."""
    return (f"MERGE INTO {dam_table} AS t\n"
            f"USING (SELECT dam_id, cdse_id FROM {assignment_table} WHERE effective_to IS NULL) AS s\n"
            f"ON t.dam_id = s.dam_id\n"
            f"WHEN MATCHED AND (t.cdse_id IS NULL OR t.cdse_id <> s.cdse_id) THEN UPDATE SET cdse_id = s.cdse_id")


__all__ = ["SUBJECT", "ACTIONS", "REASON_CODES", "CODES", "membership_cdse_at", "ASSIGNMENT_COLS", "LEGACY_ACTION", "evaluate", "current_cdse", "is_live",
           "is_platform_admin", "merge_source_rows", "merge_sql", "dam_sync_sql"]
