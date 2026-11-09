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

App roles (v1.7, canon v2; App Dev c797151, app SCHEMA_CONTRACT §7 item 14): a canon-2 record carries
`actor_app_role`, the actor's raw app roles on the dam at signed_ts (sorted comma-joined viewer|engineer|dso|dam_admin|
admin|cdse), derived server-side and covered by record_hash. For canon-2 records that value is the role evidence:
a non-CDSE proposer without dam_admin/admin is `propose_no_authority` (error), and an apply/reject by an actor holding
dam_admin/admin without CDSE authority is `apply_by_admin` / `reject_by_admin`. A 'cdse' token in actor_app_role never
grants anything here: CDSE authority comes from ref.v_dam_reviewer_authority only. An actor who is both the dam's CDSE
(per UC) and admin may apply/reject (CDSE authority decides; same as the app's is_cdse gate).
Canon-1 records (canon_version NULL, before c797151) have no role: the optional `app_roles(dam_id, principal, at)`
callable is used, and without it a non-CDSE proposal stays `propose_role_unverified` (warn) and a non-CDSE
apply/reject is `apply_not_cdse` / `reject_not_cdse` (still an error: CDSE authority alone decides).
Independence caveat: actor_app_role is recorded by the app (DA decision recorded in docs/VERIFICATION.md).
actor_app_role is EVIDENCE ONLY (Genie 2026-11-09): it only adds failure codes (apply_by_admin / reject_by_admin,
propose_no_authority); it never grants authority. Authority always comes from ref.v_dam_reviewer_authority at signed_ts.

Second approver (PROVISIONAL, same setting and parsing as the app, App Dev f0dd9c8 backend/config.py):
CDSE_FLAG_REQUIRE_SECOND_APPROVER, default false. When true, an alarm_rule_flag_apply whose corrected_value.proposal_id
references a proposal recorded earlier in the chain fails with `apply_by_proposer` if the applier is that proposal's
proposer (same reviewer_id, or same email case-insensitively; proposer taken from the propose record and from
corrected_value.proposed_by). When false nothing changes. The setting is not in the chain, so the verifier applies the
CURRENT setting to every record; canon and chain fields are unchanged. Not covered (not asked): a direct apply (no
recorded proposal) while the setting is on, which the app refuses (second_approver_required).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Callable, Iterable, List, Mapping, Optional

from dam_safety.audit_chain import ACTOR_ROLE_FIELD, actor_role_problem, actor_roles, canon_of

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
    "apply_by_proposer": "error",           # CDSE_FLAG_REQUIRE_SECOND_APPROVER on: applier == proposer of the proposal
}
SECOND_APPROVER_SETTING = "CDSE_FLAG_REQUIRE_SECOND_APPROVER"


def second_approver_required(environ=None) -> bool:
    """Read CDSE_FLAG_REQUIRE_SECOND_APPROVER exactly as the app's config._bool does: unset -> False (default);
    otherwise true only for 1 / true / yes / on (case-insensitive, surrounding whitespace ignored)."""
    v = (os.environ if environ is None else environ).get(SECOND_APPROVER_SETTING)
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "on"}
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
                                 app_roles: Optional[Callable[[str, str, dt.datetime], Optional[str]]] = None,
                                 require_second_approver: Optional[bool] = None) -> List[dict]:
    """Check every alarm_rule record. Returns [{review_id, dam_id, chain_seq, action_type, code, severity, detail}]
    (empty = all authorised). `authority`: ref.v_dam_reviewer_authority rows (dam_id, principal, granted_ts,
    revoked_ts). `app_roles(dam_id, principal_lower, signed_ts)` -> 'dam_admin' | 'admin' | other role | None."""
    authority = list(authority)
    second = second_approver_required() if require_second_approver is None else bool(require_second_approver)
    recs = sorted((r for r in records if r.get("subject_type") == "alarm_rule"),
                  key=lambda r: (str(r.get("chain_id")), int(r.get("chain_seq") or 0)))
    proposals = {}
    out = []

    def bad(r, code, detail):
        out.append({"review_id": r.get("review_id"), "dam_id": r.get("dam_id"), "chain_seq": r.get("chain_seq"),
                    "action_type": r.get("action_type"), "code": code, "severity": CODES[code], "detail": detail})

    def role_of(r):
        """'admin' | 'dam_admin' | another role | None (unknown). canon 2: from the hashed actor_app_role ('cdse' is
        ignored: UC authority decides CDSE); canon 1: from app_roles, if given."""
        if canon_of(r) == 2 and actor_role_problem(r.get(ACTOR_ROLE_FIELD)) is None:
            roles = set(actor_roles(r.get(ACTOR_ROLE_FIELD))) - {"cdse"}
            if not roles:
                return "cdse_claim_only"     # app saw only a CDSE grant; UC did not confirm it
        elif app_roles is None:
            return None
        else:
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
        if second and verb == "apply" and (dam, pid) in proposals:
            prop = proposals[(dam, pid)]
            proposer_ids = _who(prop) | ({proposer} if proposer else set())
            if proposer_ids & _who(r):
                bad(r, "apply_by_proposer", f"{SECOND_APPROVER_SETTING} is on: proposal {pid!r} was applied by its "
                    f"own proposer (seq {prop.get('chain_seq')})")
    return out
