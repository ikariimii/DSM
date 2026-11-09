"""v1.7 (Genie 2026-11-09): who may propose / approve / reject a DSSMP version, re-checked by the engine on the UC copy
(audit.review_record, subject_type 'dssmp_version', ck_review_record_chain via migration v1_7_003). Same model as the
§7.13 alarm-rule rules (dam_safety.rule_flag_authority): authority comes ONLY from ref.v_dam_reviewer_authority at
signed_ts; a canon-2 record's actor_app_role is evidence only (it adds *_by_admin / propose_no_authority, never
grants).

Rules (Genie): the dam's CDSE registers / approves a new dam's DSSMP version before go-live; dam_admin may only propose.
* dssmp_version_propose: the dam's CDSE, or dam_admin / admin of that dam.
* dssmp_version_approve / dssmp_version_reject: CDSE authority for that dam_id at signed_ts. dam_admin / admin never.
* The CDSE may approve directly (no proposal) or approve their own proposal. A reject, or an approve naming someone
  else's proposal, must reference a proposal recorded earlier in the chain.
* subject_id = corrected_value.dssmp_version_id; an approve carries version_no, effective_from and document_sha256
  (the values the load job registers; dam_safety.dssmp_registration checks they match the outbox row).

Record shape proposed to App Dev (not yet in the app): subject_type 'dssmp_version', decision propose | approve |
reject, action_type dssmp_version_propose | _approve | _reject, corrected_value JSON {dssmp_version_id, dam_id,
version_no, effective_from, document_uri, document_sha256[, proposal_id, proposed_by]}, original_value JSON
{supersedes_dssmp_version_id}, non-blank comment (rationale).
"""
from __future__ import annotations

import json
import re
from typing import Callable, Iterable, List, Mapping, Optional

from dam_safety.audit_chain import ACTOR_ROLE_FIELD, actor_role_problem, actor_roles, canon_of
from dam_safety.rule_flag_authority import ADMIN_ROLES, _utc, _who, cdse_dams_at

SUBJECT = "dssmp_version"
ACTIONS = {"dssmp_version_propose": "propose", "dssmp_version_approve": "approve", "dssmp_version_reject": "reject"}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

CODES = {
    "dssmp_propose_no_authority": "error",       # role known: neither CDSE nor dam_admin / admin
    "dssmp_propose_role_unverified": "warn",     # non-CDSE proposer, no role evidence (canon 1 without app_roles)
    "dssmp_approve_not_cdse": "error",
    "dssmp_approve_by_admin": "error",
    "dssmp_approve_cdse_of_other_dam": "error",
    "dssmp_reject_not_cdse": "error",
    "dssmp_reject_by_admin": "error",
    "dssmp_reject_cdse_of_other_dam": "error",
    "dssmp_approve_proposal_missing": "error",
    "dssmp_reject_proposal_missing": "error",
    "dssmp_subject_mismatch": "error",           # subject_id / corrected_value.dssmp_version_id / dam disagree
    "dssmp_approval_fields_missing": "error",    # approve without version_no, effective_from or a sha256 document hash
    "dssmp_rationale_missing": "error",
    "dssmp_decision_action_mismatch": "error",
}


def _cv(rec) -> dict:
    try:
        v = json.loads(rec.get("corrected_value") or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _role(r, app_roles):
    if canon_of(r) == 2 and actor_role_problem(r.get(ACTOR_ROLE_FIELD)) is None:
        roles = set(actor_roles(r.get(ACTOR_ROLE_FIELD))) - {"cdse"}
        if not roles:
            return "cdse_claim_only"
    elif app_roles is None:
        return None
    else:
        roles = {app_roles(r["dam_id"], p, _utc(r["signed_ts"])) for p in _who(r)} - {None}
    return "admin" if "admin" in roles else ("dam_admin" if "dam_admin" in roles else (min(roles) if roles else None))


def dssmp_authority_problems(records: Iterable[Mapping], authority: Iterable[Mapping],
                             app_roles: Optional[Callable] = None) -> List[dict]:
    """Every dssmp_version record checked; [{review_id, dam_id, chain_seq, action_type, code, severity, detail}]."""
    authority = list(authority)
    recs = sorted((r for r in records if r.get("subject_type") == SUBJECT),
                  key=lambda r: (str(r.get("chain_id")), int(r.get("chain_seq") or 0)))
    proposals, out = {}, []

    def bad(r, code, detail):
        out.append({"review_id": r.get("review_id"), "dam_id": r.get("dam_id"), "chain_seq": r.get("chain_seq"),
                    "action_type": r.get("action_type"), "code": code, "severity": CODES[code], "detail": detail})

    for r in recs:
        act, dam, cv = r.get("action_type"), r.get("dam_id"), _cv(r)
        if not str(r.get("comment") or "").strip():
            bad(r, "dssmp_rationale_missing", "every dssmp_version record needs a non-blank rationale")
        verb = ACTIONS.get(act)
        if verb is None or verb != r.get("decision"):
            bad(r, "dssmp_decision_action_mismatch", f"action_type {act!r} with decision {r.get('decision')!r}")
            continue
        if (str(cv.get("dssmp_version_id") or "") != str(r.get("subject_id") or "")
                or (cv.get("dam_id") not in (None, dam))):
            bad(r, "dssmp_subject_mismatch", f"subject_id {r.get('subject_id')!r}, corrected_value "
                f"{cv.get('dssmp_version_id')!r} / dam {cv.get('dam_id')!r} vs record dam {dam!r}")
        dams = cdse_dams_at(r, authority)
        is_cdse = dam in dams
        if verb == "propose":
            if cv.get("proposal_id"):
                proposals[(dam, cv["proposal_id"])] = r
            if not is_cdse:
                role = _role(r, app_roles)
                if role is None:
                    bad(r, "dssmp_propose_role_unverified", "proposer is not the dam's CDSE and no app-role evidence")
                elif role not in ADMIN_ROLES:
                    bad(r, "dssmp_propose_no_authority", f"proposer role {role!r} on {dam}: needs CDSE, dam_admin "
                        "or admin")
            continue
        if not is_cdse:
            role = _role(r, app_roles)
            if role in ADMIN_ROLES:
                bad(r, f"dssmp_{verb}_by_admin", f"{role} may only propose; {verb} needs CDSE authority for {dam}")
            elif dams:
                bad(r, f"dssmp_{verb}_cdse_of_other_dam", f"CDSE of {sorted(map(str, dams))}, not of {dam}")
            else:
                bad(r, f"dssmp_{verb}_not_cdse", f"no CDSE authority for {dam} at signed_ts")
        pid = cv.get("proposal_id")
        proposer = str(cv.get("proposed_by") or "").strip().lower()
        direct_or_own = verb == "approve" and (not proposer or proposer in _who(r))
        if not direct_or_own and (dam, pid) not in proposals:
            bad(r, f"dssmp_{verb}_proposal_missing", f"proposal {pid!r} is not recorded earlier in the chain")
        if verb == "approve":
            try:
                ok = int(cv.get("version_no")) >= 1 and bool(_utc(cv.get("effective_from")))
            except (TypeError, ValueError):
                ok = False
            if not ok or not SHA_RE.match(str(cv.get("document_sha256") or "")):
                bad(r, "dssmp_approval_fields_missing", "approve must carry version_no >= 1, effective_from and a "
                    "lower-case hex sha256 document_sha256")
    return out


__all__ = ["SUBJECT", "ACTIONS", "CODES", "dssmp_authority_problems"]
