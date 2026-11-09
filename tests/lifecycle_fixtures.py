"""v1.8 test helpers: the citation / suspension-reason enum rows parsed from the UC migration's seed (the single
source; the engine itself hard-codes none of them), plus a context builder for dam_safety.lifecycle."""
import datetime as dt
import hashlib
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIG = os.path.join(ROOT, "sql", "migrations", "v1_8_001_dam_lifecycle.sql")


def _arr(x):
    x = x.strip()
    if x.startswith("CAST(NULL"):
        return None
    return re.match(r"split\('([^']*)', ','\)", x).group(1).split(",")


def seed_citations():
    s = open(MIG).read()
    blk = s[s.index("INSERT INTO ref.lifecycle_citation"):]
    blk = blk[:blk.index("WHERE NOT EXISTS")]
    rows = []
    for m in re.finditer(r"\('(\w+)', (split\('[^']*', ','\)|CAST\(NULL AS ARRAY<STRING>\)), '(\w+)', "
                         r"(split\('[^']*', ','\)|CAST\(NULL AS ARRAY<STRING>\)), '([^']*)', '([\w-]+)', (true|false), "
                         r"(true|false)\)", blk):
        rows.append({"citation_code": m.group(1), "from_states": _arr(m.group(2)), "to_state": m.group(3),
                     "suspension_reasons": _arr(m.group(4)), "citation": m.group(5),
                     "verification_status": m.group(6), "withdrawal_notice": m.group(7) == "true",
                     "active": m.group(8) == "true"})
    return rows


def seed_reasons():
    s = open(MIG).read()
    blk = s[s.index("INSERT INTO ref.lifecycle_suspension_reason"):]
    blk = blk[:blk.index("WHERE NOT EXISTS")]
    return [m.group(1) for m in re.finditer(r"\('(\w+)', '[^']*', true\)", blk)]


def U(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def H(s):
    return hashlib.sha256(s.encode()).hexdigest()


CDSE, ADMIN, CDSE2, NOBODY = "cdse@example.org", "admin@example.org", "cdse2@example.org", "eng@example.org"
AUTH = [{"dam_id": d, "principal": p, "granted_ts": "2026-01-01T00:00:00Z", "revoked_ts": None}
        for d in ("D1", "D2", "NEW") for p in (CDSE, CDSE2)]
ADMINS = [{"principal": ADMIN, "dam_id": "*", "role": "ADMIN", "granted_ts": "2026-01-01T00:00:00Z",
           "revoked_ts": None}]
DSSMP = [{"dam_id": "D1", "dssmp_version_id": "DS-1", "status": "approved", "effective_from": "2026-01-01T00:00:00Z",
          "effective_to": "2026-10-01T00:00:00Z"},
         {"dam_id": "D1", "dssmp_version_id": "DS-2", "status": "approved", "effective_from": "2026-10-01T00:00:00Z",
          "effective_to": None},
         {"dam_id": "D1", "dssmp_version_id": "DS-D", "status": "draft", "effective_from": "2026-10-01T00:00:00Z",
          "effective_to": None}]
EFF = "2026-11-20T12:00:00Z"
APPROVED = "2026-11-09T18:00:00Z"
PROPOSED = "2026-11-09T17:00:00Z"
CIT_FOR = {}            # (from, to, reason) -> a valid citation code (filled lazily)


def citation_for(frm, to, reason=None):
    for r in seed_citations():
        if frm in r["from_states"] and r["to_state"] == to and (
                to != "suspended_monitoring_modified" or reason in (r["suspension_reasons"] or [])):
            return r["citation_code"]
    return None


def history(dam, states, start="2026-02-01T00:00:00Z"):
    """Chained audit.lifecycle_event rows for `dam` walking `states` (first from draft), one per month."""
    from dam_safety import lifecycle as L
    evs, prev, t = [], L.GENESIS, U(start)
    frm = L.DRAFT
    for i, to in enumerate(states):
        e = {"event_id": f"E-{dam}-{i}", "dam_id": dam, "from_state": frm, "to_state": to, "trigger_code": "t",
             "authorizer_role": "cdse", "authorizer_user_id": "u", "evidence_uri": ["uri:x"], "effective_ts": t,
             "created_by": "test", "citation_code": citation_for(frm, to, "drawdown") or "x",
             "suspension_reason": "drawdown" if to == L.SUSP else None, "prev_event_hash": prev}
        e["event_hash"] = L.event_hash(e)
        evs.append(e)
        prev, frm, t = e["event_hash"], to, t + dt.timedelta(days=30)
    return evs


def req(frm, to, dam="D1", **kw):
    r = {"dam_id": dam, "from_state": frm, "to_state": to, "effective_ts": EFF, "evidence_uri": ["uri:evidence/1"],
         "citation_code": citation_for(frm, to, kw.get("suspension_reason")), "trigger_code": "test",
         "authorizer_role": "cdse", "authorizer_user_id": "u1", "proposed_by": CDSE2, "approved_by": CDSE}
    if to == "suspended_monitoring_modified":
        r.update(suspension_reason="drawdown", cdse_basis="piezometer trend", cdse_basis_by="u-cdse",
                 dssmp_version_id="DS-2", citation_code=citation_for(frm, to, "drawdown"))
    if (frm, to) == ("suspended_monitoring_modified", "live"):
        r.update(dssmp_version_id="DS-2")
    if to == "transferred":
        r.update(counterpart_dam_id="NEW")
    if to == "voided_in_error":
        r.update(proposed_by=CDSE, approved_by=ADMIN)
    if r["citation_code"] and "withdrawn" in r["citation_code"]:
        r.update(withdrawal_filed_date="2026-11-01")
    r.update(kw)
    return r


def ctx(states_by_dam=None, **kw):
    """states_by_dam: {dam: [states walked]} -> keyword arguments for check_transition."""
    from dam_safety import lifecycle as L
    evs = []
    for dam, sts in (states_by_dam or {}).items():
        evs += history(dam, sts)
    c = {"current": L.current_states(evs), "has_ref_dam": {d for d, s in (states_by_dam or {}).items() if s},
         "citations": L.load_citations(seed_citations()), "reasons": set(seed_reasons()), "dssmp": DSSMP,
         "readiness": {"D1": True, "D2": True, "NEW": True}, "authority": AUTH, "admins": ADMINS,
         "proposed_at": PROPOSED, "approved_at": APPROVED}
    c.update(kw)
    return c, evs


PATH_TO = {   # how to reach each state from draft for D1
    "draft_commissioning": [], "live": ["live"], "suspended_monitoring_modified": ["live", "suspended_monitoring_modified"],
    "pending_transfer": ["live", "pending_transfer"], "transferred": ["live", "pending_transfer", "transferred"],
    "pending_surrender": ["live", "pending_surrender"],
    "decommissioned_archived": ["live", "pending_surrender", "decommissioned_archived"],
    "voided_in_error": ["voided_in_error"],
}
