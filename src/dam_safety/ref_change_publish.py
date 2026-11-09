"""v1.7: publisher for the app's Lakebase outbox `dam_app.ref_change_publication` (app SCHEMA_CONTRACT §7 items 13 and
15; App Dev 6f7a1d5 / c797151). One outbox row per applied live `cdse_ack_required` change:

    kind = 'ref.threshold.new_version', status 'pending', payload = {kind, threshold_id, dam_id, supersedes_version: N,
    insert: {every canonical ref.threshold column of version N, threshold_version: N+1, cdse_ack_required: <new>,
    effective_from: <apply time, UTC ISO-8601>, effective_to: null, is_current: true, set_by: <CDSE email>}}

Pure planning here (unit-tested, tests/test_ref_change_publish.py); the Spark job is src/jobs/publish_ref_changes.py.
Per row the plan is one of:
  * applied          - valid and the current version of threshold_id is N: version N+1 goes into the single MERGE
  * already_applied  - the current version already IS this insert (a re-run after the MERGE committed but before the
                       outbox status was written): status applied, nothing written to UC
  * rejected_stale   - the current version is not N (UC is the authority; the app checks too)
  * rejected_invalid - the row cannot be published as given; `message` = '; '-joined '<code>: <detail>' for every
                       problem, `codes` the code list: unknown_kind, payload_invalid (keys disagreeing with the row,
                       version not N+1), missing_required_column (the five the app must carry, PUBLISH_REQUIRED),
                       missing_not_null_column, unknown_columns, invalid_value, and the DSSMP codes of
                       dam_safety.dssmp_check (dssmp_not_found, dssmp_other_dam, dssmp_draft, dssmp_superseded,
                       dssmp_not_yet_effective, dssmp_status_unknown; Delta FKs are informational only), and for
                       cause 'dssmp_reversion' (§7 item 21) the codes of dam_safety.rule_reversion_authority
                       (reversion_approval_missing / _wrong_action / _mismatch, reversion_not_authorized,
                       reversion_fields_changed).
  * deferred         - another pending row for the same threshold_id is older; left pending for the next run.
Nothing is defaulted or repaired: a row lacking a column is rejected with the reason, never filled in.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Iterable, List, Mapping

from dam_safety import rule_reversion_authority as rra
from dam_safety.dssmp_check import dssmp_problem
from dam_safety.threshold_publish import BOOKKEEPING, RULE_FIELDS, ThresholdVersionError, ack_flag

KIND = "ref.threshold.new_version"
# ref.threshold columns, sql/ddl/02 order (tests/test_ref_change_publish.py checks it against the DDL)
THRESHOLD_COLS = ("threshold_id", "threshold_version", "dam_id", "instrument_id", "sensor_id", "pfm_id",
                  "dssmp_version_id", "tier", "level", "level_name", "limit_type", "direction", "value", "unit",
                  "rate_window", "deadband", "persistence_on", "persistence_off", "condition_expr", "basis_text",
                  "required_actions", "eap_level_id", "set_by", "effective_from", "effective_to", "is_current",
                  "cdse_ack_required")
# NOT NULL in sql/ddl/02 (cdse_ack_required has a default but the app always sends it; validated as a real bool)
NOT_NULL = ("threshold_id", "threshold_version", "dam_id", "instrument_id", "dssmp_version_id", "tier", "level",
            "limit_type", "direction", "value", "unit", "deadband", "persistence_on", "persistence_off",
            "effective_from", "is_current")
# the five columns the app must carry since c797151 (app onboarding.PUBLISH_REQUIRED)
PUBLISH_REQUIRED = ("dssmp_version_id", "tier", "deadband", "persistence_on", "persistence_off")
TIER_RE = re.compile(r"^(DESIGN_BASIS|THRESHOLD|AL[1-9][0-9]*)$")          # same as the app's onboarding.TIER_RE
LIMIT_TYPES = ("magnitude", "rate", "cyclic")
DIRECTIONS = ("upper", "lower", "abs")


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
        return int(v)
    return None


def _ts(v):
    if isinstance(v, dt.datetime):
        d = v
    else:
        try:
            d = dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)


def payload_of(row: Mapping) -> dict:
    p = row.get("payload")
    if isinstance(p, dict):
        return p
    try:
        v = json.loads(p or "")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def column_problems(ins: Mapping, dssmp: Mapping[str, Mapping]) -> List[str]:
    """Every problem with the insert row's columns (empty = publishable as given), each '<code>: <detail>'.
    dssmp: dssmp_version_id -> ref.dssmp_version row (dam_id, status, effective_from, effective_to); the DSSMP
    version must exist, be the same dam's, approved and in effect at the new version's effective_from
    (dam_safety.dssmp_check codes)."""
    out = []
    for c in PUBLISH_REQUIRED:
        if _blank(ins.get(c)):
            out.append(f"missing_required_column: {c} (ref.threshold NOT NULL; the app must carry it, never defaulted)")
    for c in NOT_NULL:
        if c not in PUBLISH_REQUIRED and _blank(ins.get(c)):
            out.append(f"missing_not_null_column: {c}")
    unknown = sorted(set(ins) - set(THRESHOLD_COLS))
    if unknown:
        out.append(f"unknown_columns: {unknown}")
    t = ins.get("tier")
    if not _blank(t) and not TIER_RE.match(str(t)):
        out.append(f"invalid_value: tier {t!r} is not DESIGN_BASIS | THRESHOLD | AL<n>")
    d = ins.get("deadband")
    if not _blank(d) and (_num(d) is None or _num(d) < 0):
        out.append(f"invalid_value: deadband {d!r} must be a number >= 0")
    for c in ("persistence_on", "persistence_off"):
        v = ins.get(c)
        if not _blank(v) and (_int(v) is None or _int(v) < 1):
            out.append(f"invalid_value: {c} {v!r} must be an integer >= 1")
    for c in ("level", "threshold_version"):
        v = ins.get(c)
        if not _blank(v) and _int(v) is None:
            out.append(f"invalid_value: {c} {v!r} must be an integer")
    if not _blank(ins.get("value")) and _num(ins.get("value")) is None:
        out.append(f"invalid_value: value {ins.get('value')!r} must be a number")
    if not _blank(ins.get("limit_type")) and ins.get("limit_type") not in LIMIT_TYPES:
        out.append(f"invalid_value: limit_type {ins.get('limit_type')!r} not in {LIMIT_TYPES}")
    if not _blank(ins.get("direction")) and ins.get("direction") not in DIRECTIONS:
        out.append(f"invalid_value: direction {ins.get('direction')!r} not in {DIRECTIONS}")
    if not _blank(ins.get("effective_from")) and _ts(ins.get("effective_from")) is None:
        out.append(f"invalid_value: effective_from {ins.get('effective_from')!r} is not an ISO-8601 timestamp")
    if ins.get("is_current") is not True:
        out.append("invalid_value: is_current must be true on the new version")
    if not _blank(ins.get("effective_to")):
        out.append("invalid_value: effective_to must be null on the new version")
    try:
        ack_flag(ins)
        if "cdse_ack_required" not in ins:
            out.append("missing_not_null_column: cdse_ack_required (the app always sends the new flag)")
    except ThresholdVersionError as e:
        out.append(f"invalid_value: {e}")
    dv = ins.get("dssmp_version_id")
    if not _blank(dv):
        at = _ts(ins.get("effective_from")) or dt.datetime.now(dt.timezone.utc)
        p = dssmp_problem(dv, ins.get("dam_id"), dssmp, at, historical=False)
        if p:
            out.append(f"{p[0]}: {p[1]}")
    return out


def normalise(ins: Mapping) -> dict:
    """Insert row typed for ref.threshold (every column present; ints/doubles/bool/timestamps converted)."""
    r = {c: ins.get(c) for c in THRESHOLD_COLS}
    for c in ("threshold_version", "level", "persistence_on", "persistence_off"):
        r[c] = _int(r[c])
    for c in ("value", "deadband"):
        r[c] = _num(r[c])
    r["cdse_ack_required"] = ack_flag(ins)
    r["effective_from"] = _ts(r["effective_from"])
    r["effective_to"] = None
    r["is_current"] = True
    return r


def plan(pending: Iterable[Mapping], current: Iterable[Mapping], dssmp: Mapping[str, str],
         reversion: Mapping | None = None) -> List[dict]:
    """pending: outbox rows (pub_id, kind, dam_id, proposal_id, payload, requested_at, status='pending').
    reversion (§7 item 21): {records_by_hash, authority, now, require_second_approver} for rows whose payload carries
    cause 'dssmp_reversion' (dam_safety.rule_reversion_authority.publication_problems); without it such rows are
    refused (fail closed). A payload with any other cause is payload_invalid; no cause = a flag change (§7.13).
    current: ref.threshold rows with is_current = true for the thresholds concerned (threshold_id, threshold_version,
    effective_from, set_by, cdse_ack_required). dssmp: dssmp_version_id -> ref.dssmp_version row.
    Returns one decision per row, oldest first: {pub_id, decision, status, message, insert, close}. `insert` /
    `close` are set only for decision 'applied'; `status` is the outbox status to write (None = leave pending)."""
    cur = {r["threshold_id"]: r for r in current}
    rows = sorted(pending, key=lambda r: (str(r.get("requested_at")), str(r.get("pub_id"))))
    seen, out = set(), []

    def decide(r, decision, status, message, insert=None, close=None):
        codes = [m.split(":", 1)[0] for m in message.split("; ")] if decision.startswith("rejected") else []
        out.append({"pub_id": r.get("pub_id"), "dam_id": r.get("dam_id"), "decision": decision, "status": status,
                    "message": message, "codes": codes, "insert": insert, "close": close})

    for r in rows:
        p = payload_of(r)
        tid = p.get("threshold_id")
        if tid in seen:
            decide(r, "deferred", None, f"an older pending publication for {tid} is processed first")
            continue
        if tid is not None:
            seen.add(tid)
        probs = []
        if r.get("kind") != KIND or p.get("kind") != KIND:
            probs.append(f"unknown_kind: {r.get('kind')!r}/{p.get('kind')!r} (expected {KIND})")
        ins = p.get("insert") if isinstance(p.get("insert"), dict) else None
        sup = _int(p.get("supersedes_version"))
        if ins is None:
            probs.append("payload_invalid: no insert row")
        if sup is None:
            probs.append(f"payload_invalid: supersedes_version {p.get('supersedes_version')!r} is not an integer")
        if ins is not None:
            if ins.get("threshold_id") != tid or tid is None:
                probs.append(f"payload_invalid: insert.threshold_id {ins.get('threshold_id')!r} != payload threshold_id "
                             f"{tid!r}")
            if not (ins.get("dam_id") == p.get("dam_id") == r.get("dam_id")):
                probs.append(f"payload_invalid: dam_id mismatch: row {r.get('dam_id')!r}, payload {p.get('dam_id')!r}, "
                             f"insert {ins.get('dam_id')!r}")
            probs += column_problems(ins, dssmp)
            if sup is not None and _int(ins.get("threshold_version")) != sup + 1:
                probs.append(f"payload_invalid: insert threshold_version {ins.get('threshold_version')!r} != supersedes_version + 1 "
                             f"({sup + 1})")
        cause = p.get("cause")
        if cause is not None and cause != rra.CAUSE:
            probs.append(f"payload_invalid: unknown cause {cause!r} (expected none or {rra.CAUSE!r})")
        elif cause == rra.CAUSE and ins is not None:
            if reversion is None:
                probs.append("reversion_approval_missing: no approval context supplied (fail closed)")
            else:
                rp = rra.publication_problems(r, p, ins, cur.get(tid), reversion.get("records_by_hash") or {},
                                              reversion.get("authority") or (), reversion.get("now")
                                              or dt.datetime.now(dt.timezone.utc), normalise,
                                              reversion.get("require_second_approver"))
                if rp == "defer":
                    decide(r, "deferred", None, f"approval record {p.get('approval_record_hash')!r} not yet in "
                           "audit.review_record; retried next run")
                    continue
                probs += rp
        if probs:
            decide(r, "rejected_invalid", "rejected_invalid", "; ".join(probs))
            continue
        row = normalise(ins)
        c = cur.get(tid)
        if c is not None and int(c["threshold_version"]) == row["threshold_version"] \
                and _ts(c["effective_from"]) == row["effective_from"] and c.get("set_by") == row["set_by"]:
            decide(r, "already_applied", "applied", f"v{row['threshold_version']} already current (re-run)")
            continue
        if c is None or int(c["threshold_version"]) != sup:
            now = "no current version" if c is None else f"current is v{c['threshold_version']}"
            decide(r, "rejected_stale", "rejected_stale",
                   f"stale: supersedes v{sup} of {tid} but {now}; nothing written")
            continue
        if _ts(c["effective_from"]) >= row["effective_from"]:
            decide(r, "rejected_invalid", "rejected_invalid",
                   f"invalid_value: effective_from {row['effective_from'].isoformat()} is not after v{sup}'s "
                   f"{_ts(c['effective_from']).isoformat()}")
            continue
        decide(r, "applied", "applied", f"published {tid} v{row['threshold_version']} (closed v{sup})", insert=row,
               close={"threshold_id": tid, "threshold_version": sup, "effective_to": row["effective_from"],
                      "is_current": False})
    return out


def merge_source_rows(decisions: Iterable[Mapping]) -> List[dict]:
    """Rows for the single MERGE source: per applied decision one 'close' row keyed on (threshold_id, N) and one
    'insert' row keyed on (threshold_id, N+1). merge_version = the target key; supersedes_version is carried so the
    MERGE re-checks staleness against the target in the same statement."""
    out = []
    for d in decisions:
        if d["decision"] != "applied":
            continue
        ins, cl = d["insert"], d["close"]
        out.append({**ins, "merge_action": "close", "merge_version": cl["threshold_version"],
                    "supersedes_version": cl["threshold_version"], "pub_id": d["pub_id"]})
        out.append({**ins, "merge_action": "insert", "merge_version": ins["threshold_version"],
                    "supersedes_version": cl["threshold_version"], "pub_id": d["pub_id"]})
    return out


def merge_sql(target: str, source: str) -> str:
    """ONE conditional MERGE. Stale rejection inside the statement: a source row is used only while the target's
    CURRENT version of that threshold_id is still supersedes_version (EXISTS against the target, same snapshot); the
    close may set only the SCD2 bookkeeping (effective_to, is_current) of version N; version N+1 is inserted only when
    absent. Concurrent writers to the same files fail the commit (Delta conflict detection) rather than interleave."""
    cols = ", ".join(THRESHOLD_COLS)
    vals = ", ".join(f"s.{c}" for c in THRESHOLD_COLS)
    assert set(BOOKKEEPING) == {"effective_to", "is_current"}
    return (
        f"MERGE INTO {target} AS t\n"
        f"USING (SELECT s.* FROM {source} AS s WHERE EXISTS (SELECT 1 FROM {target} AS c\n"
        f"         WHERE c.threshold_id = s.threshold_id AND c.is_current AND c.threshold_version = s.supersedes_version)"
        f") AS s\n"
        f"ON t.threshold_id = s.threshold_id AND t.threshold_version = s.merge_version\n"
        f"WHEN MATCHED AND s.merge_action = 'close' AND t.is_current AND t.threshold_version = s.supersedes_version\n"
        f"  THEN UPDATE SET effective_to = s.effective_from, is_current = false\n"
        f"WHEN NOT MATCHED AND s.merge_action = 'insert'\n"
        f"  THEN INSERT ({cols}) VALUES ({vals})")


def rule_fields_unchanged(before: Iterable[Mapping], after: Iterable[Mapping]) -> None:
    """Post-check (raises ThresholdVersionError): no existing version had a rule field changed in place."""
    from dam_safety.threshold_publish import assert_versions_immutable
    assert_versions_immutable(before, after)


__all__ = ["KIND", "THRESHOLD_COLS", "NOT_NULL", "PUBLISH_REQUIRED", "RULE_FIELDS", "column_problems", "plan",
           "merge_source_rows", "merge_sql", "normalise", "payload_of", "rule_fields_unchanged"]
