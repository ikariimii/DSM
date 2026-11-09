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


def append(st, request: Request, base: dict, also=None) -> dict:
    signer = getattr(request.app.state, "signer", None) or ac.NoopSigner()
    return st.append_chained(base["dam_id"], ac.GENESIS, ac.finalizer(base, signer), also)


def _verify(st, request: Request, chain_id: str) -> dict:
    signer = getattr(request.app.state, "signer", None) or ac.NoopSigner()
    return ac.verify_chain(st.chain_rows(chain_id), st.chain_head(chain_id), signer)


@router.get("/api/audit/signoffs/verify")
def verify_all(request: Request, c: Ctx = Depends(ctx), st=Depends(store)):
    require_admin(c)
    chains = [_verify(st, request, cid) for cid in st.chain_ids()]
    return {"ok": all(x["ok"] for x in chains), "chains": chains, "genesis": ac.GENESIS, "scope": "per-dam",
            "verified_utc": now_utc()}


@router.get("/api/dams/{dam_id}/audit/signoffs/verify")
def verify_dam(dam_id: str, request: Request, c: Ctx = Depends(ctx), st=Depends(store)):
    require(c, dam_id, VERIFY_MIN_ROLE)
    res = _verify(st, request, dam_id)
    res["chain_id"] = dam_id
    return {**res, "genesis": ac.GENESIS, "scope": "per-dam", "verified_utc": now_utc()}
