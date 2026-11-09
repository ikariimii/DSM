"""v1.7: publish alarm rules (onboarding go-live payload `thresholds`) into ref.threshold, SCD2, never in place.

Decision (Genie, 2026-11-09): ref.threshold is versioned on (threshold_id, threshold_version). Changing ANY rule
field, `cdse_ack_required` included, creates a NEW version row (threshold_version = max + 1, its own
effective_from); the earlier version keeps every rule field unchanged and only has its SCD2 bookkeeping closed
(effective_to = the new effective_from, is_current = false). The version chain is the audit trail of the flag
(who: set_by; when: effective_from). Nothing here issues an UPDATE of a rule field.

Pure functions (unit-tested in tests/test_threshold_publish.py). A Spark job that reads `onboarding_publication`
from Lakebase and MERGEs these rows does NOT exist in this repo yet (the app's outbox is written, the publisher is
pending), so `plan_threshold_publication` is the contract that job must call. UNVERIFIED: never run on Spark.
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Tuple

# SCD2 bookkeeping: the only columns a publish may change on an EXISTING version row (closing it).
BOOKKEEPING = ("effective_to", "is_current")
# rule fields: a change in any of these needs a new version. cdse_ack_required is a rule field.
RULE_FIELDS = ("dam_id", "instrument_id", "sensor_id", "pfm_id", "dssmp_version_id", "tier", "level", "level_name",
               "limit_type", "direction", "value", "unit", "rate_window", "deadband", "persistence_on",
               "persistence_off", "condition_expr", "basis_text", "required_actions", "eap_level_id",
               "cdse_ack_required")


class ThresholdVersionError(ValueError):
    pass


def ack_flag(row: Mapping) -> bool:
    """cdse_ack_required of a published rule: absent -> false (the column default); anything but a real bool is
    refused (no truthy strings: 'false' must never become True or vice versa)."""
    v = row.get("cdse_ack_required", False)
    if v is None:
        return False
    if not isinstance(v, bool):
        raise ThresholdVersionError(f"threshold {row.get('threshold_id')}: cdse_ack_required must be true/false, got {v!r}")
    return v


def _rule(row: Mapping) -> dict:
    r = {k: row.get(k) for k in RULE_FIELDS}
    r["cdse_ack_required"] = ack_flag(row)
    return r


def plan_threshold_publication(current: Iterable[Mapping], published: Iterable[Mapping],
                               published_ts: str, set_by: str) -> Tuple[List[dict], List[dict]]:
    """Return (inserts, closes).

    current   = existing ref.threshold rows (all versions) for the dam.
    published = payload thresholds (one per threshold_id).
    inserts   = full new version rows (is_current true, effective_to None, cdse_ack_required always a bool).
    closes    = [{threshold_id, threshold_version, effective_to, is_current: False}] for the superseded version;
                the publisher may only SET those two columns on that key.
    An unchanged rule is a no-op. effective_from of a new version = max(payload effective_from, published_ts):
    a flag change is never back-dated before the publication, so already-opened episodes keep their snapshot."""
    by_id: dict = {}
    for r in current:
        by_id.setdefault(r["threshold_id"], []).append(r)
    inserts, closes, seen = [], [], set()
    for p in published:
        tid = p["threshold_id"]
        if tid in seen:
            raise ThresholdVersionError(f"threshold {tid} published twice in one payload")
        seen.add(tid)
        versions = sorted(by_id.get(tid, []), key=lambda r: r["threshold_version"])
        live = [r for r in versions if r.get("is_current")]
        if len(live) > 1:
            raise ThresholdVersionError(f"threshold {tid}: {len(live)} current versions")
        new_rule = _rule(p)
        if live and _rule(live[0]) == new_rule:
            continue                                            # identical: no new version, no write
        eff = max(p.get("effective_from") or published_ts, published_ts)
        if live and str(live[0]["effective_from"]) >= eff:
            raise ThresholdVersionError(f"threshold {tid}: new version must start after v{live[0]['threshold_version']}")
        ver = (versions[-1]["threshold_version"] + 1) if versions else 1
        inserts.append({"threshold_id": tid, "threshold_version": ver, **new_rule, "set_by": set_by,
                        "effective_from": eff, "effective_to": None, "is_current": True})
        if live:
            closes.append({"threshold_id": tid, "threshold_version": live[0]["threshold_version"],
                           "effective_to": eff, "is_current": False})
    return inserts, closes


def assert_versions_immutable(before: Iterable[Mapping], after: Iterable[Mapping]) -> None:
    """Audit check for any writer of ref.threshold (publisher, migration, hand fix): every (threshold_id,
    threshold_version) present before must still exist after with identical rule fields (cdse_ack_required
    included); only BOOKKEEPING may differ. Raises ThresholdVersionError naming the first in-place change."""
    a = {(r["threshold_id"], r["threshold_version"]): r for r in after}
    for r in before:
        k = (r["threshold_id"], r["threshold_version"])
        if k not in a:
            raise ThresholdVersionError(f"threshold {k[0]} v{k[1]} was deleted")
        for f in RULE_FIELDS:
            if r.get(f) != a[k].get(f):
                raise ThresholdVersionError(f"threshold {k[0]} v{k[1]}: {f} changed in place "
                                            f"({r.get(f)!r} -> {a[k].get(f)!r}); publish a new version instead")
