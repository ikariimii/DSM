"""v1.7 (Genie 2026-11-09): a threshold may only be published, or a live rule change applied, against a DSSMP version
that exists in ref.dssmp_version (sql/ddl/04: dssmp_version_id, dam_id, version_no, effective_from, effective_to,
status 'draft | approved | superseded'), belongs to the SAME dam, is approved and is in effect. Delta FKs are
informational only, so this is checked in code: by the publisher (dam_safety.ref_change_publish) at publication time
and by the §7.13 verifier (dam_safety.rule_flag_authority) on every alarm_rule_flag_apply record at its signed_ts.

One reason code per case (each tested in tests/test_dssmp_check.py):
  dssmp_missing            no dssmp_version_id given
  dssmp_not_found          not in ref.dssmp_version
  dssmp_other_dam          belongs to another dam
  dssmp_draft              status draft (never approved)
  dssmp_superseded         superseded: publisher - status superseded, or effective_to <= the time checked;
                           verifier (historical) - effective_to <= signed_ts, or status superseded with no effective_to
  dssmp_not_yet_effective  effective_from after the time checked
  dssmp_status_unknown     any other status value
Who registers / approves a new dam's DSSMP version: the dam's CDSE, before go-live (dam_admin may only propose);
docs/DEV_WORKSPACE_RUNBOOK.md.
"""
from __future__ import annotations

import datetime as dt
from typing import Mapping, Optional, Tuple

CODES = ("dssmp_missing", "dssmp_not_found", "dssmp_other_dam", "dssmp_draft", "dssmp_superseded",
         "dssmp_not_yet_effective", "dssmp_status_unknown")


def _ts(v) -> Optional[dt.datetime]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    d = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)


def dssmp_problem(dssmp_version_id, dam_id, versions: Mapping[str, Mapping], at, historical: bool = False
                  ) -> Optional[Tuple[str, str]]:
    """(code, detail) or None when the DSSMP version is usable for dam_id at time `at`.
    versions: dssmp_version_id -> ref.dssmp_version row (dam_id, status, effective_from, effective_to)."""
    if dssmp_version_id is None or not str(dssmp_version_id).strip():
        return "dssmp_missing", "no dssmp_version_id"
    v = versions.get(str(dssmp_version_id))
    if v is None:
        return "dssmp_not_found", f"dssmp_version_id {dssmp_version_id!r} does not exist in ref.dssmp_version"
    if v.get("dam_id") != dam_id:
        return "dssmp_other_dam", f"dssmp_version_id {dssmp_version_id!r} belongs to dam {v.get('dam_id')!r}, not {dam_id!r}"
    status = str(v.get("status") or "").strip().lower()
    at, frm, to = _ts(at), _ts(v.get("effective_from")), _ts(v.get("effective_to"))
    if status == "draft":
        return "dssmp_draft", f"DSSMP version {dssmp_version_id!r} is a draft (not approved)"
    if status not in ("approved", "superseded"):
        return "dssmp_status_unknown", f"DSSMP version {dssmp_version_id!r} has status {v.get('status')!r}"
    ended = to is not None and at is not None and to <= at
    if ended or (status == "superseded" and (not historical or to is None)):
        return "dssmp_superseded", (f"DSSMP version {dssmp_version_id!r} is superseded"
                                    + (f" (effective_to {to.isoformat()})" if to else ""))
    if frm is not None and at is not None and frm > at:
        return "dssmp_not_yet_effective", (f"DSSMP version {dssmp_version_id!r} takes effect {frm.isoformat()}, "
                                           f"after {at.isoformat()}")
    return None
