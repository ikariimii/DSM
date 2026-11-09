"""v1.6 §7.13 (app SCHEMA_CONTRACT §7 item 13, App Dev 6f7a1d5; rules confirmed by App Dev via Genie 2026-11-09):
who may propose / apply / reject a live alarm-rule `cdse_ack_required` change, re-checked by the engine on the UC copy
(audit.review_record, subject_type 'alarm_rule'). Deliberately NOT a UC row CHECK: authority depends on
ref.v_dam_reviewer_authority at decision time (signed_ts), which a row CHECK cannot read.

Rules
* alarm_rule_flag_apply / alarm_rule_flag_reject: the reviewer must hold CDSE authority for that dam_id at signed_ts
  (ref.v_dam_reviewer_authority principal = reviewer_id or reviewer_email, case-insensitive, granted_ts <= signed_ts <
  revoked_ts; the DAM_CDSE_ID row has no history, as in gold.v_review_level_check).
* dam_admin and platform admin may never apply or reject: an admin without CDSE authority gets its own code.
* alarm_rule_flag_propose: the dam's CDSE, or dam_admin / admin of that dam.
* The CDSE may apply directly (no proposal) or apply their own proposal; nothing forbids applier == proposer.
* An apply that names a proposal by someone else, or a reject, must reference a proposal recorded in the chain.
* review_level 'CDSE' on any alarm_rule record requires the same CDSE authority.

App roles (dam_admin / admin) are NOT in UC: they live in the app's Lakebase dam_role / app_user, without history,
and the app records review_level 'engineer' for them (ROLE_LEVEL). So `app_roles(dam_id, principal, at)` is optional;
without it a non-CDSE proposal is `propose_role_unverified` (severity 'warn': cannot be proven either way) and a
non-CDSE apply/reject is `apply_not_cdse` / `reject_not_cdse` (still an error: CDSE authority alone decides).
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Callable, Iterable, List, Mapping, Optional

ADMIN_ROLES = ("dam_admin", "admin")

# failure code -> severity ('error' fails the verify task, 'warn' is reported only)
CODES = {
    "apply_not_cdse": "error",              # apply by a principal with no CDSE authority for the dam (not an admin)
    "apply_by_admin": "error",              # apply by dam_admin / admin without CDSE authority
    "apply_cdse_of_other_dam": "error",     # apply by a CDSE of ANOTHER dam only
    "reject_not_cdse": "error",
    "reject_by_admin": "error",
    "reject_cdse_of_other_dam": "error",
    "propose_no_authority": "error",        # role known and it is neither CDSE nor dam_admin / admin
    "propose_role_unverified": "warn",      # non-CDSE proposer and no app-role evidence supplied
    "apply_proposal_missing": "error",      # apply of someone else's proposal that is not in the chain
    "reject_proposal_missing": "error",
    "cdse_level_without_authority": "error",
    "rationale_missing": "error",           # defence in depth (the UC CHECK also refuses it)
    "decision_action_mismatch": "error",
}
_DECISION = {"alarm_rule_flag_propose": "propose", "alarm_rule_flag_apply": "apply", "alarm_rule_flag_reject": "reject"}


def _utc(v) -> dt.datetime:
    d = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)


def _who(rec) -> set:
    return {str(x).strip().lower() for x in (rec.get("reviewer_id"), rec.get("reviewer_email")) if x and str(x).strip()}


def cdse_dams_at(rec, authority: Iterable[Mapping]) -> set:
    """dam_ids for which the record's reviewer held CDSE authority at its signed_ts."""
    who, at, out = _who(rec), _utc(rec["signed_ts"]), set()
    for a in authority:
        if str(a.get("principal") or "").strip().lower() not in who:
            continue
        g = a.get("granted_ts")
        r = a.get("revoked_ts")
        if (g in (None, "") or _utc(g) <= at) and (r in (None, "") or _utc(r) > at):
            out.add(a.get("dam_id"))
    return out


def _cv(rec) -> dict:
    try:
        v = json.loads(rec.get("corrected_value") or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def rule_flag_authority_problems(records: Iterable[Mapping], authority: Iterable[Mapping],
                                 app_roles: Optional[Callable[[str, str, dt.datetime], Optional[str]]] = None
                                 ) -> List[dict]:
    """Check every alarm_rule record. Returns [{review_id, dam_id, chain_seq, action_type, code, severity, detail}]
    (empty = all authorised). `authority`: ref.v_dam_reviewer_authority rows (dam_id, principal, granted_ts,
    revoked_ts). `app_roles(dam_id, principal_lower, signed_ts)` -> 'dam_admin' | 'admin' | other role | None."""
    authority = list(authority)
    recs = sorted((r for r in records if r.get("subject_type") == "alarm_rule"),
                  key=lambda r: (str(r.get("chain_id")), int(r.get("chain_seq") or 0)))
    proposals = {}
    out = []

    def bad(r, code, detail):
        out.append({"review_id": r.get("review_id"), "dam_id": r.get("dam_id"), "chain_seq": r.get("chain_seq"),
                    "action_type": r.get("action_type"), "code": code, "severity": CODES[code], "detail": detail})

    def role_of(r):
        if app_roles is None:
            return None
        roles = {app_roles(r["dam_id"], p, _utc(r["signed_ts"])) for p in _who(r)} - {None}
        return "admin" if "admin" in roles else ("dam_admin" if "dam_admin" in roles else (min(roles) if roles else None))

    for r in recs:
        act, dam = r.get("action_type"), r.get("dam_id")
        if not str(r.get("comment") or "").strip():
            bad(r, "rationale_missing", "every alarm_rule record needs a non-blank rationale")
        if _DECISION.get(act) != r.get("decision"):
            bad(r, "decision_action_mismatch", f"action_type {act!r} with decision {r.get('decision')!r}")
            continue
        dams = cdse_dams_at(r, authority)
        is_cdse = dam in dams
        cv = _cv(r)
        if str(r.get("review_level") or "").upper() == "CDSE" and not is_cdse:
            bad(r, "cdse_level_without_authority", f"review_level CDSE but no CDSE authority for {dam} at signed_ts")
        if act == "alarm_rule_flag_propose":
            if cv.get("proposal_id"):
                proposals[(dam, cv["proposal_id"])] = r
            if is_cdse:
                continue
            role = role_of(r)
            if role in ADMIN_ROLES:
                continue
            if role is None:
                bad(r, "propose_role_unverified", "proposer is not the dam's CDSE and no app-role evidence was given")
            else:
                bad(r, "propose_no_authority", f"proposer role {role!r} on {dam}: needs CDSE, dam_admin or admin")
            continue
        verb = "apply" if act == "alarm_rule_flag_apply" else "reject"
        if not is_cdse:
            role = role_of(r)
            if role in ADMIN_ROLES:
                bad(r, f"{verb}_by_admin", f"{role} may never {verb} (CDSE authority for {dam} required)")
            elif dams:
                bad(r, f"{verb}_cdse_of_other_dam", f"CDSE of {sorted(map(str, dams))}, not of {dam}")
            else:
                bad(r, f"{verb}_not_cdse", f"no CDSE authority for {dam} at signed_ts")
        pid = cv.get("proposal_id")
        proposer = str(cv.get("proposed_by") or "").strip().lower()
        direct_or_own = verb == "apply" and (not proposer or proposer in _who(r))
        if not direct_or_own and (dam, pid) not in proposals:
            bad(r, f"{verb}_proposal_missing", f"proposal {pid!r} is not recorded earlier in the chain")
    return out
