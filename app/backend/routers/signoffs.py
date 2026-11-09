"""QA-039 v2: the per-dam hash-chained audit of reviewer actions - shared append helper and verify endpoints.

GET /api/audit/signoffs/verify               admin: every chain
GET /api/dams/{dam_id}/audit/signoffs/verify engineer+ on that dam (the dam ACL): that dam's chain only
Both recompute the whole chain and report ok, or the first broken chain_seq and the reason."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import audit_chain as ac
from ..deps import Ctx, ctx, require, require_admin, store
from ..repos.state import is_unique_violation

router = APIRouter()
log = logging.getLogger(__name__)
VERIFY_MIN_ROLE = "engineer"


def now_utc() -> str:
    """Server UTC timestamp (never client-supplied)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def rationale_of(text: str | None) -> str:
    r = (text or "").strip()
    if not r:
        raise HTTPException(422, "A non-empty reviewer rationale is required")
    return r


def reviewer_identity(c: Ctx, request: Request) -> dict:
    """SSO identity from Databricks Apps auth: X-Forwarded-User id, X-Forwarded-Email and the unverified 'sub' of
    x-forwarded-access-token (identity only). Demo personas (no proxy) are labelled 'demo:<email>'."""
    rid = c.user_id or (f"demo:{c.email}" if request.app.state.settings.demo_mode else "")
    if not rid:  # pragma: no cover - deps.identity already returns 401 outside demo mode
        raise HTTPException(401, "No authenticated user (X-Forwarded-User missing)")
    return {"reviewer_id": rid, "reviewer_email": c.email or None, "reviewer_sub": c.token_sub}


def cdse_principals(an, dam_id: str) -> frozenset[str]:
    """Principals that ARE the dam's CDSE: ref.dam.cdse_id plus direct ref.project_membership role CDSE grants for
    exactly this dam, read in live mode only through ref.v_dam_reviewer_authority (not '*', not group names - those
    cannot be resolved to a person here). Fails closed: an
    analytics source without the lookup, or a lookup error, yields no CDSE."""
    fn = getattr(an, "cdse_principals", None)
    if fn is None:
        return frozenset()
    try:
        return frozenset(p.strip().lower() for p in (fn(dam_id) or ()) if p and p.strip())
    except Exception:  # noqa: BLE001 - never elevate on a failed lookup
        log.warning("CDSE lookup failed for dam %s; treating caller as non-CDSE", dam_id)
        return frozenset()


def is_cdse(c: Ctx, dam_id: str, an) -> bool:
    """The caller is the dam's CDSE: their forwarded user id or (proxy-supplied) e-mail matches a CDSE principal.
    App roles (dso, dam_admin, platform admin) never make a caller CDSE (FERC Ch.14 App K 14-K-4)."""
    principals = cdse_principals(an, dam_id)
    ids = {x.strip().lower() for x in (c.user_id, c.email) if x and x.strip()}
    return bool(principals & ids)


def review_level(c: Ctx, dam_id: str, an=None) -> str:
    """CDSE only for the dam's named CDSE; otherwise the role's level (engineer - no elevation for dso/admins)."""
    if an is not None and is_cdse(c, dam_id, an):
        return ac.CDSE_LEVEL
    return ac.ROLE_LEVEL["admin" if c.is_admin else c.role(dam_id)]


def actor_app_role(c: Ctx, dam_id: str, an) -> str:
    """The acting user's RAW app roles on this dam at the time of the action, derived server-side from the stored
    grants (never from the request body): 'admin' for a platform admin, their stored per-dam role (viewer / engineer /
    dso / dam_admin) and 'cdse' when they are the dam's named CDSE (is_cdse; fails closed). Sorted, comma-joined."""
    roles = set()
    if c.is_admin:
        roles.add("admin")
    own = (c.roles or {}).get(dam_id)
    if own in ac.ACTOR_ROLES:
        roles.add(own)
    if is_cdse(c, dam_id, an):
        roles.add("cdse")
    if not roles:  # pragma: no cover - every chained action requires dam access first
        raise HTTPException(403, f"{c.email} has no app role on dam {dam_id}")
    return ",".join(sorted(roles))


def append(st, request: Request, base: dict, also=None, *, c: Ctx, an, also_row=None, check=None) -> dict:
    """Append one canon-2 record: actor_app_role and canon_version are set HERE from the server-side identity,
    overriding anything in `base` (which is itself built server-side; request bodies never carry them).
    QA-056: every chained write takes the per-dam lock first (lock order dam -> chain head), then `check(q)` re-runs
    the route's state checks inside the same transaction (an HTTPException there aborts it: nothing is written).
    A unique violation (the DB belt-and-braces indexes) is a 409 concurrent_write, with nothing written either."""
    signer = getattr(request.app.state, "signer", None) or ac.NoopSigner()
    base = {**base, ac.ACTOR_ROLE_FIELD: actor_app_role(c, base["dam_id"], an), ac.CANON_FIELD: ac.CANON_VERSION}
    try:
        return st.append_chained(base["dam_id"], ac.GENESIS, ac.finalizer(base, signer), also, also_row=also_row,
                                 lock_key=base["dam_id"], check=check)
    except Exception as e:
        if is_unique_violation(e):
            raise HTTPException(409, {"code": "concurrent_write",
                                      "message": "a concurrent request already recorded this; nothing was written "
                                                 "(reload and retry)"}) from e
        raise


def _verify(st, request: Request, chain_id: str) -> dict:
    signer = getattr(request.app.state, "signer", None) or ac.NoopSigner()
    return ac.verify_chain(st.chain_rows(chain_id), st.chain_head(chain_id), signer)


def _owner_refusal(st):
    """QA-058 residual: the owner-table startup check, re-run per verification (None = fine / sqlite demo)."""
    return st.verify_owner_tables() if hasattr(st, "verify_owner_tables") else None


@router.get("/api/audit/signoffs/verify")
def verify_all(request: Request, c: Ctx = Depends(ctx), st=Depends(store)):
    require_admin(c)
    # QA-058 residual (as QA-053 re-applies the signer guard): re-run the startup owner / trigger check, so a trigger
    # disabled or an owner changed AFTER startup is reported. Fail closed: no chain is verified, each reports the code.
    refused = _owner_refusal(st)
    if refused is not None:
        chains = [{"chain_id": cid, "records": None, "ok": False, "first_broken_seq": None, "reason": refused.code,
                   "detail": str(refused)} for cid in st.chain_ids()]
        return {"ok": False, "reason": refused.code, "detail": str(refused), "chains": chains, "genesis": ac.GENESIS,
                "scope": "per-dam", "verified_utc": now_utc()}
    chains = [_verify(st, request, cid) for cid in st.chain_ids()]
    return {"ok": all(x["ok"] for x in chains), "chains": chains, "genesis": ac.GENESIS, "scope": "per-dam",
            "verified_utc": now_utc()}


@router.get("/api/dams/{dam_id}/audit/signoffs/verify")
def verify_dam(dam_id: str, request: Request, c: Ctx = Depends(ctx), st=Depends(store)):
    require(c, dam_id, VERIFY_MIN_ROLE)
    refused = _owner_refusal(st)
    if refused is not None:            # QA-058 residual: same fail-closed re-check as verify_all
        return {"chain_id": dam_id, "records": None, "ok": False, "first_broken_seq": None, "reason": refused.code,
                "detail": str(refused), "genesis": ac.GENESIS, "scope": "per-dam", "verified_utc": now_utc()}
    res = _verify(st, request, dam_id)
    res["chain_id"] = dam_id
    return {**res, "genesis": ac.GENESIS, "scope": "per-dam", "verified_utc": now_utc()}
