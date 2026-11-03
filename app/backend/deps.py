"""Request identity, per-dam access control and shared services."""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request

from .config import Settings
from .repos.state import ROLE_RANK, StateStore


@dataclass
class Ctx:
    email: str          # display / audit label only; never the identity key
    token: str | None = field(default=None, repr=False)  # OBO token; never logged, printed or returned
    is_admin: bool = False
    roles: dict[str, str] = field(default_factory=dict)
    known: bool = True
    user_id: str = ""   # X-Forwarded-User: the identity key

    @property
    def token_sub(self) -> str | None:
        """Unverified 'sub' claim of the forwarded access token (identity label only; the token is never stored)."""
        from .audit_chain import token_sub
        return token_sub(self.token)

    def role(self, dam_id: str) -> str | None:
        return "dam_admin" if self.is_admin else self.roles.get(dam_id)

    def can(self, dam_id: str, min_role: str = "viewer") -> bool:
        r = self.role(dam_id)
        return r is not None and ROLE_RANK[r] >= ROLE_RANK[min_role]

    def dams(self, all_dams: list[str]) -> list[str]:
        return list(all_dams) if self.is_admin else [d for d in all_dams if d in self.roles]


def store(request: Request) -> StateStore:
    return request.app.state.store


def identity(request: Request) -> tuple[str, str, str | None]:
    """(user_id, email, token). Identity is keyed on X-Forwarded-User (stable); X-Forwarded-Email is used
    for display and to bind a first sign-in to an admin-granted email row. The token is never logged."""
    s: Settings = request.app.state.settings
    user_id = (request.headers.get("x-forwarded-user") or "").strip()
    email = (request.headers.get("x-forwarded-email") or "").strip().lower()
    token = request.headers.get("x-forwarded-access-token")
    if not user_id and s.demo_mode:
        # demo personas carry no stable id; ctx() resolves them by email without binding
        email = email or (request.headers.get("x-demo-user") or s.demo_user).strip().lower()
        return "", email, token
    if not user_id:
        # In Databricks Apps the proxy always sets X-Forwarded-User; absence means an
        # unauthenticated or misrouted request.
        raise HTTPException(401, "No authenticated user (X-Forwarded-User missing)")
    return user_id, email, token


def ctx(request: Request) -> Ctx:
    user_id, email, token = identity(request)
    st: StateStore = request.app.state.store
    # BOOTSTRAP_ADMINS is applied once at startup (backend.bootstrap), never per request: admin
    # rights come only from app_user.
    user = st.resolve_user(user_id, email) if user_id else st.get_user(email)  # "" only in demo mode
    if user is None or not user["active"]:
        return Ctx(email=email or user_id, token=token, is_admin=False, roles={}, known=False, user_id=user_id)
    return Ctx(email=user["email"], token=token, is_admin=user["is_admin"], roles=st.roles_for(user["email"]),
               user_id=user_id)


def analytics(request: Request, c: Ctx = Depends(ctx)):
    return request.app.state.analytics_factory(c.token)


def require(c: Ctx, dam_id: str, min_role: str = "viewer") -> None:
    if not c.can(dam_id, min_role):
        raise HTTPException(403, f"{c.email} lacks '{min_role}' on dam {dam_id}")


def require_admin(c: Ctx) -> None:
    if not c.is_admin:
        raise HTTPException(403, "Global admin required")
