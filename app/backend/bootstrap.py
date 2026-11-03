"""One-time first-admin bootstrap (BOOTSTRAP_ADMINS).

Intended for the FIRST production deploy only. It runs once at startup, never per request:
- It creates the listed admins only when the admin table is empty, i.e. there is no ACTIVE global
  admin in app_user. Otherwise the variable is ignored, so it cannot be used to grant or re-grant admin later.
- It writes one audit_log entry (action 'bootstrap_admin', actor 'system') for each admin it creates.
- It logs a prominent WARNING at startup whenever the variable is set, even if it does nothing,
  so a forgotten setting stays visible in the app logs until it is removed from prod config.
"""
from __future__ import annotations

import logging

from .repos.state import StateStore

log = logging.getLogger("dam_safety_app.bootstrap")

BANNER = "!" * 78


def bootstrap_admins(st: StateStore, emails: tuple[str, ...]) -> list[str]:
    """Create `emails` as global admins if (and only if) no active admin exists. Returns the emails created."""
    if not emails:
        return []
    log.warning(
        "%s\nSECURITY WARNING: BOOTSTRAP_ADMINS is set (%d address(es)). It is for the FIRST deploy only; "
        "remove it from prod config after the first admin has signed in.\n%s", BANNER, len(emails), BANNER)
    existing = st.active_admin_count()
    if existing:
        log.warning("BOOTSTRAP_ADMINS ignored: %d active admin(s) already exist; no users were created or changed.",
                    existing)
        return []
    created: list[str] = []
    for email in dict.fromkeys(e.strip().lower() for e in emails if e.strip()):
        st.upsert_user(email, email, True, "bootstrap")
        st.audit("system", "bootstrap_admin", target=email, detail={"reason": "admin table empty at startup"})
        created.append(email)
        log.warning("BOOTSTRAP_ADMINS created global admin %s (audited).", email)
    return created
