"""User and per-dam role management.

Global admins manage users and any role; a dam_admin manages roles on their own
dam(s) only and cannot change their own role. All changes are audited."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import Ctx, analytics, ctx, require_admin, store
from ..onboarding import EMAIL_RE
from ..repos.state import ROLES

router = APIRouter(prefix="/api/admin")


class UserBody(BaseModel):
    email: str = Field(max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    is_admin: bool = False


class RoleBody(BaseModel):
    email: str
    dam_id: str
    role: str | None  # None removes the role


def _admin_dams(c: Ctx) -> set[str]:
    return {d for d, r in c.roles.items() if r == "dam_admin"}


def _can_manage(c: Ctx) -> None:
    if not (c.is_admin or _admin_dams(c)):
        raise HTTPException(403, "Admin or dam_admin required")


@router.get("/users")
def users(c: Ctx = Depends(ctx), st=Depends(store), an=Depends(analytics)):
    _can_manage(c)
    all_users = st.list_users()
    scope = None if c.is_admin else _admin_dams(c)
    if scope is not None:
        for u in all_users:
            u["roles"] = {d: r for d, r in u["roles"].items() if d in scope}
    dams = [s["dam_id"] for s in an.sites()]
    return {"users": all_users, "roles": ROLES, "dams": dams if scope is None else sorted(scope),
            "is_admin": c.is_admin}


@router.post("/users")
def create_user(body: UserBody, c: Ctx = Depends(ctx), st=Depends(store)):
    require_admin(c)
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Invalid email")
    u = st.upsert_user(email, body.display_name, body.is_admin, c.email)
    st.audit(c.email, "user_upsert", None, email, {"is_admin": body.is_admin})
    return u


@router.delete("/users/{email}")
def deactivate(email: str, c: Ctx = Depends(ctx), st=Depends(store)):
    require_admin(c)
    email = email.strip().lower()
    if email == c.email:
        raise HTTPException(409, "You cannot deactivate yourself")
    if st.get_user(email) is None:
        raise HTTPException(404, "Unknown user")
    st.deactivate_user(email)
    st.audit(c.email, "user_deactivate", None, email)
    return {"ok": True}


@router.put("/roles")
def set_role(body: RoleBody, c: Ctx = Depends(ctx), st=Depends(store), an=Depends(analytics)):
    email = body.email.strip().lower()
    if not (c.is_admin or body.dam_id in _admin_dams(c)):
        raise HTTPException(403, f"Admin or dam_admin on {body.dam_id} required")
    if email == c.email and not c.is_admin:
        raise HTTPException(409, "You cannot change your own role")
    if body.dam_id not in {s["dam_id"] for s in an.sites()}:
        raise HTTPException(404, f"Unknown dam {body.dam_id}")
    if body.role is not None and body.role not in ROLES:
        raise HTTPException(422, f"role must be one of {ROLES}")
    u = st.get_user(email)
    if u is None or not u["active"]:
        if not c.is_admin:
            raise HTTPException(404, "Unknown user (ask a global admin to add them)")
        st.upsert_user(email, email, False, c.email)
    st.set_role(email, body.dam_id, body.role, c.email)
    st.audit(c.email, "role_set", body.dam_id, email, {"role": body.role})
    return {"email": email, "roles": st.roles_for(email)}


@router.get("/audit")
def audit(dam_id: str | None = None, limit: int = 200, c: Ctx = Depends(ctx), st=Depends(store)):
    _can_manage(c)
    if not c.is_admin:
        if dam_id is None or dam_id not in _admin_dams(c):
            raise HTTPException(403, "dam_admin may only view audit for their dam (pass dam_id)")
    return {"entries": st.audit_entries(dam_id, min(max(limit, 1), 1000))}
