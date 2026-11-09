"""DSSMP version registration (v1.7, SCHEMA_CONTRACT §7 item 19; onboarding wizard step 'dssmp').

POST /api/dams/{dam_id}/dssmp-documents                               proposer: upload the DSSMP document (sha256)
GET  /api/dams/{dam_id}/dssmp-documents/{document_id}                 viewer: download it
GET  /api/dams/{dam_id}/dssmp-registrations                           viewer / proposer / the dam's CDSE
POST /api/dams/{dam_id}/dssmp-registrations                           dam_admin of the dam or platform admin: propose
POST /api/dams/{dam_id}/dssmp-registrations/direct                    the dam's CDSE: register directly (approve record)
POST /api/dams/{dam_id}/dssmp-registrations/{registration_id}/approve the dam's CDSE only (rationale required)
POST /api/dams/{dam_id}/dssmp-registrations/{registration_id}/reject  the dam's CDSE only (rationale required)
GET  /api/dssmp-registrations/pending                                 the caller's CDSE approval queue

Proposers are the dam's STORED dam_admin or a platform admin (a not-yet-live dam has no stored roles, so its
registration is proposed by the platform admin who also holds the go-live right). Approve/reject: only the dam's CDSE
(signoffs.is_cdse via ref.v_dam_reviewer_authority, fail closed) - which for a new dam needs the DA to expose its CDSE
in the view before go-live. Propose, approve and reject each append ONE canon-2 chain record (dssmp_version_propose /
_approve / _reject, subject_type 'dssmp_version') atomically with the workflow change. Approval also inserts ONE
'pending' row into dssmp_registration_outbox in the same transaction, with approval_record_hash = the approve record's
record_hash. The app NEVER writes ref.dssmp_version: the DA load job does, and only the DA publisher role sets the
outbox status / reason_code / message / processed_at (demo: a stand-in loader runs at once)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .. import audit_chain as ac
from ..config import approval_policy
from ..deps import Ctx, analytics, ctx, store
from . import alarms as alarms_router
from . import rule_reversion as rr
from . import signoffs as sg

router = APIRouter()
BASE = "/api/dams/{dam_id}"
DSSMP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_DOC_BYTES = 20 * 1024 * 1024  # base64 text in Lakebase; larger documents go by document_uri
SUBJECT = "dssmp_version"
REG_FIELDS = ("dssmp_version_id", "version_no", "effective_from", "change_reason", "source_ref", "document_uri",
              "document_sha256")


class ProposeBody(BaseModel):
    dssmp_version_id: str | None = None
    version_no: int | None = None
    effective_from: str | None = None
    change_reason: str | None = None
    source_ref: str | None = None
    document_uri: str | None = None
    document_sha256: str | None = None
    document_id: str | None = None
    rationale: str | None = None
    propose_reversion: bool = False   # direct registration only


class DecisionBody(BaseModel):
    rationale: str | None = None
    propose_reversion: bool = False   # approve only: also create the bulk re-version proposal once loaded


class UploadBody(BaseModel):
    filename: str
    content_type: str = "application/pdf"
    content_b64: str


# ------------------------------------------------------------------ access
def may_propose(c: Ctx, dam_id: str) -> bool:
    return c.is_admin or (c.roles or {}).get(dam_id) == "dam_admin"


def _may_view(c: Ctx, dam_id: str, an) -> bool:
    return c.can(dam_id, "viewer") or sg.is_cdse(c, dam_id, an)


def _guard_view(c: Ctx, dam_id: str, an) -> None:
    if not _may_view(c, dam_id, an):
        raise HTTPException(403, f"No access to dam {dam_id}")


def _guard_propose(c: Ctx, dam_id: str) -> None:
    if not may_propose(c, dam_id):
        raise HTTPException(403, f"Only a dam_admin of {dam_id} or a platform admin may register a DSSMP version")


def _guard_cdse(c: Ctx, dam_id: str, an, what: str) -> None:
    if not sg.is_cdse(c, dam_id, an):
        raise HTTPException(403, f"Only the CDSE of dam {dam_id} (ref.v_dam_reviewer_authority) may {what}")


# ------------------------------------------------------------------ helpers
def _utc(v: str | None) -> str:
    """effective_from as UTC 'YYYY-MM-DDTHH:MM:SSZ' (a bare date is midnight UTC; naive times are UTC)."""
    s = (v or "").strip()
    if not s:
        raise HTTPException(422, "effective_from (UTC date or date-time) is required")
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(422, f"effective_from {s!r} is not an ISO date / date-time") from e
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(v: str | None, what: str) -> str:
    s = (v or "").strip()
    if not s:
        raise HTTPException(422, f"{what} is required")
    return s


def _record(c: Ctx, request: Request, an, dam_id: str, version_id: str, action: str, decision: str,
            rationale: str, original: dict, new: dict) -> dict:
    who = sg.reviewer_identity(c, request)
    base = {"review_id": f"RS-{uuid.uuid4().hex}", "dam_id": dam_id, "subject_type": SUBJECT,
            "subject_id": version_id, "review_level": sg.review_level(c, dam_id, an), "decision": decision,
            "comment": rationale, "reviewer_id": who["reviewer_id"], "reviewer_pe_license": None,
            "signed_ts": sg.now_utc(), "action_type": action, "reviewer_email": who["reviewer_email"],
            "reviewer_sub": who["reviewer_sub"], "original_value": ac.canon_json(original),
            "corrected_value": ac.canon_json(new)}
    base["signature_hash"] = alarms_router._content_hash(base, original)
    return base


def _snapshot(r: dict) -> dict:
    """corrected_value of every dssmp_version record, in the engine's shape (src/dam_safety/dssmp_authority, 86993d0):
    {dssmp_version_id, dam_id, version_no, effective_from, document_uri, document_sha256, proposal_id, proposed_by}
    plus change_reason / source_ref; proposal_id = registration_id links approve / reject to the propose record."""
    return {k: r.get(k) for k in REG_FIELDS} | {"dam_id": r["dam_id"], "registration_id": r["registration_id"],
                                                "proposal_id": r["registration_id"], "proposed_by": r["proposed_by"]}


def _pending(st, dam_id: str, registration_id: str) -> dict:
    r = st.dssmp_registration(registration_id)
    if r is None or r["dam_id"] != dam_id:
        raise HTTPException(404, f"Unknown DSSMP registration {registration_id}")
    if r["status"] != "pending":
        raise HTTPException(409, f"DSSMP registration {registration_id} is {r['status']}")
    return r


def _same_person(c: Ctx, who: dict, r: dict) -> bool:
    if (r.get("proposed_by") or "").strip().lower() == (c.email or "").strip().lower():
        return True
    pid, rid = r.get("proposed_by_id"), who.get("reviewer_id")
    return bool(pid) and bool(rid) and str(pid) == str(rid)


def _ref_rows(an, ids: list[str]) -> tuple[dict, str | None]:
    try:
        return {r["dssmp_version_id"]: r for r in an.dssmp_versions(ids)}, None
    except Exception as e:  # noqa: BLE001 - shown as 'unverifiable', never as loaded
        return {}, type(e).__name__


def _view(st, an, rows: list[dict]) -> list[dict]:
    """Registrations with their outbox row (incl. the DA's reason_code / message) and the ref.dssmp_version state."""
    outbox = {o["outbox_id"]: o for d in {r["dam_id"] for r in rows} for o in st.dssmp_outbox(d)}
    ref, err = _ref_rows(an, sorted({r["dssmp_version_id"] for r in rows}))
    out = []
    for r in rows:
        o = outbox.get(r.get("outbox_id"))
        x = ref.get(r["dssmp_version_id"])
        # volume_uri: shown when the DA's load adds it (outbox or ref row; not selected from UC until the DA confirms
        # where the column lives - SCHEMA_CONTRACT §7 item 19)
        if r["status"] == "pending":   # shown on the approval screen: the rules this version would supersede
            try:
                aff = [x["threshold_id"] for x in rr.affected_rules(an, r["dam_id"], r["dssmp_version_id"])]
            except Exception:  # noqa: BLE001
                aff = None
            r = {**r, "affected_rules": aff, "affected_count": None if aff is None else len(aff)}
        out.append({**r, "outbox": o and {k: o.get(k) for k in ("outbox_id", "status", "reason_code", "message",
                                                                 "processed_at", "approval_record_hash", "volume_uri")},
                    "ref": ({"status": "unverifiable", "error": err} if err else
                            x and {k: x.get(k) for k in ("status", "dam_id", "version_no", "effective_from",
                                                         "effective_to", "volume_uri")})})
    return out


def _maybe_reversion(want: bool, c: Ctx, request: Request, an, st, dam_id: str, vid: str, rationale: str) -> dict:
    """Optionally create the bulk re-version proposal right after a CDSE approval (a separate transaction: the version
    must first be LOADED - approved-but-not-loaded fails closed and is reported, to be proposed as a follow-up)."""
    if not want:
        return {}
    try:
        return {"reversion": rr.create(c, request, an, st, dam_id, vid, rationale)}
    except HTTPException as e:
        return {"reversion": None, "reversion_error": e.detail}


# ------------------------------------------------------------------ documents
@router.post(BASE + "/dssmp-documents")
def upload(dam_id: str, body: UploadBody, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    if not (may_propose(c, dam_id) or sg.is_cdse(c, dam_id, an)):   # proposers and the CDSE (direct registration)
        _guard_propose(c, dam_id)
    try:
        raw = base64.b64decode(body.content_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(422, "content_b64 is not valid base64") from e
    if not raw:
        raise HTTPException(422, "the document is empty")
    if len(raw) > MAX_DOC_BYTES:
        raise HTTPException(413, f"document is {len(raw)} bytes; the limit is {MAX_DOC_BYTES} - "
                                 "give a document_uri instead")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", (body.filename or "").strip())[:200] or "document"
    doc = {"document_id": f"DOC-{uuid.uuid4().hex}", "dam_id": dam_id, "filename": name,
           "content_type": (body.content_type or "application/octet-stream").strip()[:100],
           "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),  # server-computed, never client-supplied
           "content_b64": base64.b64encode(raw).decode("ascii"), "uploaded_by": c.email, "uploaded_at": sg.now_utc()}
    st.insert_dssmp_document(doc)
    st.audit(c.email, "dssmp_document_upload", dam_id, doc["document_id"], {"sha256": doc["sha256"],
                                                                          "size_bytes": len(raw)})
    meta = {k: v for k, v in doc.items() if k != "content_b64"}
    return {**meta, "document_uri": document_uri(doc["document_id"])}


def document_uri(document_id: str) -> str:
    return f"lakebase:dam_app.dssmp_document/{document_id}"


@router.get(BASE + "/dssmp-documents/{document_id}")
def download(dam_id: str, document_id: str, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    _guard_view(c, dam_id, an)
    d = st.dssmp_document(document_id, content=True)
    if d is None or d["dam_id"] != dam_id:
        raise HTTPException(404, f"Unknown document {document_id}")
    return Response(base64.b64decode(d["content_b64"]), media_type=d["content_type"],
                    headers={"Content-Disposition": f'attachment; filename="{d["filename"]}"',
                             "X-Content-SHA256": d["sha256"]})


# ------------------------------------------------------------------ registrations
@router.get(BASE + "/dssmp-registrations")
def list_registrations(dam_id: str, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics),
                       st=Depends(store)):
    _guard_view(c, dam_id, an)
    return {"dam_id": dam_id, "registrations": _view(st, an, st.dssmp_registrations(dam_id)),
            "can_propose": may_propose(c, dam_id), "can_decide": sg.is_cdse(c, dam_id, an),
            "can_register_directly": sg.is_cdse(c, dam_id, an)
            and not request.app.state.settings.cdse_flag_require_second_approver,
            "approval_policy": approval_policy(request.app.state.settings)}


@router.get("/api/dssmp-registrations/pending")
def my_queue(request: Request, c: Ctx = Depends(ctx), an=Depends(analytics), st=Depends(store)):
    rows = st.dssmp_registrations(status="pending")
    mine = {d for d in {r["dam_id"] for r in rows} if sg.is_cdse(c, d, an)}
    return {"registrations": _view(st, an, [r for r in rows if r["dam_id"] in mine]),
            "approval_policy": approval_policy(request.app.state.settings)}


def _validated(dam_id: str, body: ProposeBody, st, an) -> dict:
    """The registration fields, validated (422 / 409); shared by propose and CDSE direct registration."""
    vid = _text(body.dssmp_version_id, "dssmp_version_id")
    if not DSSMP_ID_RE.match(vid):
        raise HTTPException(422, f"dssmp_version_id {vid!r}: letters, digits, '.', '_' or '-', at most 64")
    if body.version_no is None or isinstance(body.version_no, bool) or int(body.version_no) < 1:
        raise HTTPException(422, "version_no (integer >= 1) is required")
    reg = {"dssmp_version_id": vid, "version_no": int(body.version_no), "effective_from": _utc(body.effective_from),
           "change_reason": _text(body.change_reason, "change_reason"),
           "source_ref": _text(body.source_ref, "source_ref (DSSMR or IC recommendation id)")}
    # the document: an upload (sha256 computed at upload) XOR a uri (optional sha256, lowercase hex)
    if bool(body.document_id) == bool((body.document_uri or "").strip()):
        raise HTTPException(422, "give exactly one of document_id (an uploaded document) or document_uri")
    if body.document_id:
        d = st.dssmp_document(body.document_id)
        if d is None or d["dam_id"] != dam_id:
            raise HTTPException(422, f"Unknown document {body.document_id} for dam {dam_id}")
        if body.document_sha256 and body.document_sha256.strip().lower() != d["sha256"]:
            raise HTTPException(422, "document_sha256 does not match the uploaded document")
        reg.update(document_uri=document_uri(d["document_id"]), document_sha256=d["sha256"],
                   document_id=d["document_id"])
    else:
        # required for a URI too: the DA's outbox has document_sha256 NOT NULL and the engine requires it on approve
        sha = (body.document_sha256 or "").strip().lower()
        if not SHA256_RE.match(sha):
            raise HTTPException(422, "document_sha256 (64 hex characters) is required with a document_uri - or "
                                     "upload the document and the server computes it")
        reg.update(document_uri=body.document_uri.strip(), document_sha256=sha, document_id=None)
    live = [r for r in st.dssmp_registrations(dam_id)
            if r["dssmp_version_id"] == vid and r["status"] in ("pending", "approved")]
    if live:
        raise HTTPException(409, f"{vid} already has a {live[0]['status']} registration "
                                 f"({live[0]['registration_id']})")
    ref, err = _ref_rows(an, [vid])
    if err:
        raise HTTPException(409, {"code": "dssmp_unverifiable",
                                  "message": f"ref.dssmp_version unreadable ({err}); cannot check {vid} is new"})
    if vid in ref:
        other = ref[vid]["dam_id"] != dam_id
        raise HTTPException(409, {"code": "dssmp_other_dam" if other else "dssmp_exists",
                                  "message": f"{vid} already exists in ref.dssmp_version (dam {ref[vid]['dam_id']})"})
    return reg


def _open_check(vid: str):
    """QA-056: the one-open-registration-per-version rule re-checked inside the write transaction (dam lock held;
    the version id is global, so the unique index dssmp_registration_one_open also covers two dams racing)."""
    def check(q):
        live = q("SELECT registration_id, status FROM {s}dssmp_registration WHERE dssmp_version_id = %s AND status IN "
                 "('pending', 'approved')", (vid,))
        if live:
            raise HTTPException(409, {"code": "dssmp_registration_open",
                                      "message": f"{vid} already has a {live[0]['status']} registration "
                                                 f"({live[0]['registration_id']}); nothing was written"})
    return check


def _no_pending_outbox_check(vid: str):
    def check(q):
        if q("SELECT outbox_id FROM {s}dssmp_registration_outbox WHERE dssmp_version_id = %s AND status = 'pending'",
             (vid,)):
            raise HTTPException(409, {"code": "dssmp_load_pending",
                                      "message": f"{vid} already has a pending outbox row; nothing was written"})
    return check


def _new_row(c: Ctx, request: Request, dam_id: str, reg: dict, rationale: str) -> dict:
    who = sg.reviewer_identity(c, request)
    return {"registration_id": f"DSR-{uuid.uuid4().hex}", "dam_id": dam_id, **reg, "proposed_by": c.email,
            "proposed_by_id": who["reviewer_id"], "proposed_at": sg.now_utc(), "rationale": rationale,
            "status": "pending", "decided_by": None, "decided_by_id": None, "decided_at": None,
            "decision_rationale": None, "outbox_id": None, "approval_record_hash": None}


def _outbox_insert(st, q, r: dict, outbox_id: str, approver: str, rec: dict) -> None:
    """ONE pending outbox row (agreed contract columns); approved_at / approved_by = the approve record's own
    signed_ts / reviewer, approval_record_hash = its record_hash (the DA load job re-checks all three)."""
    st.insert_dssmp_outbox(q, {
        "outbox_id": outbox_id, "dam_id": r["dam_id"], "dssmp_version_id": r["dssmp_version_id"],
        "version_no": r["version_no"], "effective_from": r["effective_from"], "change_reason": r["change_reason"],
        "source_ref": r["source_ref"], "document_uri": r["document_uri"], "document_sha256": r["document_sha256"],
        "proposed_by": r["proposed_by"], "approved_by": approver, "approved_at": rec["signed_ts"],
        "approval_record_hash": rec["record_hash"], "status": "pending", "reason_code": None, "message": None,
        "processed_at": None})


def _demo_load(request: Request, an, st, outbox_id: str) -> None:
    if request.app.state.settings.demo_mode and hasattr(an, "load_dssmp_outbox"):
        # demo stand-in for the DA load job (prod: the publisher role processes the outbox asynchronously)
        status, code, msg = an.load_dssmp_outbox(st.dssmp_outbox(outbox_id=outbox_id)[0])
        if status != "pending":
            st.demo_publish_outbox(outbox_id, status, code, msg)


@router.post(BASE + "/dssmp-registrations")
def propose(dam_id: str, body: ProposeBody, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics),
            st=Depends(store)):
    _guard_propose(c, dam_id)
    rationale = sg.rationale_of(body.rationale)
    reg = _validated(dam_id, body, st, an)
    row = _new_row(c, request, dam_id, reg, rationale)
    base = _record(c, request, an, dam_id, reg["dssmp_version_id"], "dssmp_version_propose", "propose", rationale,
                   {"state": "none"}, _snapshot(row) | {"state": "pending"})
    rec = sg.append(st, request, base, lambda q: st.insert_dssmp_registration(q, row), c=c, an=an,
                    check=_open_check(reg["dssmp_version_id"]))
    st.audit(c.email, "dssmp_version_propose", dam_id, reg["dssmp_version_id"],
             {"registration_id": row["registration_id"]})
    return {**_view(st, an, [st.dssmp_registration(row["registration_id"])])[0], "chain_seq": rec["chain_seq"]}


@router.post(BASE + "/dssmp-registrations/direct")
def register_direct(dam_id: str, body: ProposeBody, request: Request, c: Ctx = Depends(ctx), an=Depends(analytics),
                    st=Depends(store)):
    """The dam's CDSE registers a DSSMP version directly (Genie): ONE dssmp_version_approve record with no prior
    proposal (the engine's 'CDSE may approve directly'; corrected_value.proposed_by = the CDSE) + the registration
    (approved) + ONE pending outbox row, in one transaction. Mirrors the cdse_ack_required direct apply. PROVISIONAL:
    with CDSE_FLAG_REQUIRE_SECOND_APPROVER on, 403 second_approver_required (propose instead)."""
    _guard_cdse(c, dam_id, an, "register a DSSMP version directly")
    if request.app.state.settings.cdse_flag_require_second_approver:
        raise HTTPException(403, {"code": "second_approver_required", "provisional": True,
                                  "setting": "CDSE_FLAG_REQUIRE_SECOND_APPROVER",
                                  "message": f"A second approver is required: the CDSE of {dam_id} cannot register a "
                                             "DSSMP version directly; a dam_admin / admin must propose it"})
    rationale = sg.rationale_of(body.rationale)
    reg = _validated(dam_id, body, st, an)
    row = _new_row(c, request, dam_id, reg, rationale)
    who = sg.reviewer_identity(c, request)
    outbox_id = f"DSO-{uuid.uuid4().hex}"
    base = _record(c, request, an, dam_id, reg["dssmp_version_id"], "dssmp_version_approve", "approve", rationale,
                   {"state": "none"}, _snapshot(row) | {"state": "approved", "outbox_id": outbox_id, "direct": True})

    def also_row(q, rec):
        st.insert_dssmp_registration(q, row | {
            "status": "approved", "decided_by": c.email, "decided_by_id": who["reviewer_id"],
            "decided_at": rec["signed_ts"], "decision_rationale": rationale, "outbox_id": outbox_id,
            "approval_record_hash": rec["record_hash"]})
        _outbox_insert(st, q, row, outbox_id, c.email, rec)

    def check(q):
        _open_check(reg["dssmp_version_id"])(q)
        _no_pending_outbox_check(reg["dssmp_version_id"])(q)

    rec = sg.append(st, request, base, c=c, an=an, also_row=also_row, check=check)
    st.audit(c.email, "dssmp_version_register_direct", dam_id, reg["dssmp_version_id"], {"outbox_id": outbox_id})
    _demo_load(request, an, st, outbox_id)
    return {**_view(st, an, [st.dssmp_registration(row["registration_id"])])[0], "chain_seq": rec["chain_seq"],
            "approval_record_hash": rec["record_hash"],
            **_maybe_reversion(body.propose_reversion, c, request, an, st, dam_id, reg["dssmp_version_id"], rationale)}


@router.post(BASE + "/dssmp-registrations/{registration_id}/approve")
def approve(dam_id: str, registration_id: str, body: DecisionBody, request: Request, c: Ctx = Depends(ctx),
            an=Depends(analytics), st=Depends(store)):
    _guard_cdse(c, dam_id, an, "approve a DSSMP version")
    rationale = sg.rationale_of(body.rationale)
    r = _pending(st, dam_id, registration_id)
    who = sg.reviewer_identity(c, request)
    # PROVISIONAL (same switch as the alarm-rule flag, CDSE_FLAG_REQUIRE_SECOND_APPROVER): when on, the proposer
    # can never approve their own registration; with one CDSE per dam it can then only be rejected (withdrawn).
    if request.app.state.settings.cdse_flag_require_second_approver and _same_person(c, who, r):
        raise HTTPException(403, {"code": "self_approval_forbidden", "provisional": True,
                                  "setting": "CDSE_FLAG_REQUIRE_SECOND_APPROVER",
                                  "message": f"A second approver is required: {c.email} proposed {registration_id} "
                                             "and cannot approve it; it can only be rejected (withdrawn)"})
    outbox_id = f"DSO-{uuid.uuid4().hex}"
    base = _record(c, request, an, dam_id, r["dssmp_version_id"], "dssmp_version_approve", "approve", rationale,
                   _snapshot(r) | {"state": "pending"},
                   _snapshot(r) | {"state": "approved", "outbox_id": outbox_id})

    def also_row(q, rec):  # same transaction as the chain append: decision + ONE pending outbox row
        # approved_at / approved_by are the approve record's own signed_ts / reviewer: the DA load job refuses a row
        # whose values differ from the record (approval_record_mismatch)
        st.decide_dssmp_registration(q, registration_id, "approved", c.email, who["reviewer_id"], rec["signed_ts"],
                                     rationale, outbox_id, rec["record_hash"])
        _outbox_insert(st, q, r, outbox_id, c.email, rec)

    try:
        rec = sg.append(st, request, base, c=c, an=an, also_row=also_row,
                        check=_no_pending_outbox_check(r["dssmp_version_id"]))
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    st.audit(c.email, "dssmp_version_approve", dam_id, r["dssmp_version_id"], {"outbox_id": outbox_id})
    _demo_load(request, an, st, outbox_id)
    return {**_view(st, an, [st.dssmp_registration(registration_id)])[0], "chain_seq": rec["chain_seq"],
            "approval_record_hash": rec["record_hash"],
            **_maybe_reversion(body.propose_reversion, c, request, an, st, dam_id, r["dssmp_version_id"], rationale)}


@router.post(BASE + "/dssmp-registrations/{registration_id}/reject")
def reject(dam_id: str, registration_id: str, body: DecisionBody, request: Request, c: Ctx = Depends(ctx),
           an=Depends(analytics), st=Depends(store)):
    _guard_cdse(c, dam_id, an, "reject a DSSMP version")
    rationale = sg.rationale_of(body.rationale)
    r = _pending(st, dam_id, registration_id)
    who = sg.reviewer_identity(c, request)
    now = sg.now_utc()
    base = _record(c, request, an, dam_id, r["dssmp_version_id"], "dssmp_version_reject", "reject", rationale,
                   _snapshot(r) | {"state": "pending"}, _snapshot(r) | {"state": "rejected"})
    try:
        rec = sg.append(st, request, base, lambda q: st.decide_dssmp_registration(
            q, registration_id, "rejected", c.email, who["reviewer_id"], now, rationale), c=c, an=an)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    st.audit(c.email, "dssmp_version_reject", dam_id, r["dssmp_version_id"], {"registration_id": registration_id})
    return {**_view(st, an, [st.dssmp_registration(registration_id)])[0], "chain_seq": rec["chain_seq"]}
