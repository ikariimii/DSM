"""QA-039 v2: the per-dam hash-chained audit of reviewer actions - shared append helper and verify endpoints.

GET /api/audit/signoffs/verify               admin: every chain
GET /api/dams/{dam_id}/audit/signoffs/verify engineer+ on that dam (the dam ACL): that dam's chain only
Both recompute the whole chain and report ok, or the first broken chain_seq and the reason."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import audit_chain as ac
from ..deps import Ctx, ctx, require, require_admin, store

router = APIRouter()
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


def review_level(c: Ctx, dam_id: str) -> str:
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
