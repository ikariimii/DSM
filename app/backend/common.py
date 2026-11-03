"""Helpers shared by routers."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from .deps import Ctx, require
from .verify import Gate

LEVEL_NAMES = {0: "Normal", 1: "Verify", 2: "Evaluate", 3: "Act"}


def gate_for(an, dam_id: str) -> Gate:
    return Gate(an.formulas(), an.calibrations_for_dam(dam_id))


def site_or_404(an, dam_id: str) -> dict:
    site = next((s for s in an.sites() if s["dam_id"] == dam_id), None)
    if site is None:
        raise HTTPException(404, f"Unknown dam {dam_id}")
    return site


def instrument_checked(an, c: Ctx, instrument_id: str, min_role: str = "viewer") -> dict:
    inst = an.instrument(instrument_id)
    if inst is None:
        raise HTTPException(404, f"Unknown instrument {instrument_id}")
    require(c, inst["dam_id"], min_role)
    return inst


def merge_ack(events: list[dict], acks: dict[str, dict]) -> list[dict]:
    for e in events:
        a = acks.get(e["event_id"])
        e["state"] = a["state"] if a else "open"
        e["acknowledged_by"] = a.get("acknowledged_by") if a else None
        e["acknowledged_at"] = a.get("acknowledged_at") if a else None
        e["closed_by"] = a.get("closed_by") if a else None
        e["closed_at"] = a.get("closed_at") if a else None
        e["close_reason"] = a.get("close_reason") if a else None
        e["level_name"] = LEVEL_NAMES.get(e.get("level") or 0, str(e.get("level")))
    return events


def hours_since(iso: str | None, now: datetime | None = None) -> float | None:
    if not iso:
        return None
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    now = now or datetime.now(timezone.utc)
    return round((now - t).total_seconds() / 3600.0, 1)
