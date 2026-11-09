"""v1.7 (steering 2026-11-09 (b)): the DSSMP load job copies each approved version's uploaded document from the app's
Lakebase table dam_app.dssmp_document (app-owned; document_uri = 'lakebase:dam_app.dssmp_document/<document_id>',
content_b64) into the UC volume

    /Volumes/<catalog>/ref/dssmp_documents/<dam_id>/<dssmp_version_id>/<file>

and records that path in ref.dssmp_version.volume_uri (migration v1_7_003; the original document_uri is kept).

sha256 is checked BEFORE the copy (decoded bytes == outbox document_sha256 == dssmp_document.sha256) and AFTER it (the
bytes read back from the volume). Any difference refuses the row with reason_code dssmp_document_hash_mismatch and
nothing is registered (a copy whose read-back differs is removed). A file already at the path is never overwritten:
the same hash is accepted (re-run), a different hash is refused (dssmp_document_hash_mismatch).
Refusal codes (DOC_CODES):
  dssmp_document_missing         document_uri is not a lakebase:dam_app.dssmp_document/<id> URI, or no such row
  dssmp_document_other_dam       the document row belongs to another dam
  dssmp_document_hash_mismatch   before or after the copy, or an existing different file at the path
  dssmp_document_copy_failed     the volume write / read raised (message carries the error class only)
UNVERIFIED: never run on a workspace (Python file I/O on /Volumes from a serverless job; WRITE VOLUME for the run_as
principal; Lakebase SELECT on dssmp_document for dam_lakebase_publisher, granted in lakebase/001).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from typing import Callable, Iterable, List, Mapping, Optional

from dam_safety.catalog import normalize_catalog

DOC_CODES = ("dssmp_document_missing", "dssmp_document_other_dam", "dssmp_document_hash_mismatch",
             "dssmp_document_copy_failed")
DOC_URI_RE = re.compile(r"^lakebase:dam_app\.dssmp_document/([A-Za-z0-9][A-Za-z0-9._:-]{0,127})$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VOLUME_SCHEMA, VOLUME_NAME = "ref", "dssmp_documents"
DOC_SQL = ("SELECT document_id, dam_id, filename, sha256, content_b64 FROM dssmp_document "
           "WHERE document_id = ANY(%s)")


def document_id(uri) -> Optional[str]:
    m = DOC_URI_RE.match(uri) if isinstance(uri, str) else None
    return m.group(1) if m else None


def safe_filename(name) -> str:
    """Last path segment only; anything outside [A-Za-z0-9._-] becomes '_'; no leading dots; never empty."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")[:128]
    return base or "document"


def volume_path(catalog: str, dam_id: str, dssmp_version_id: str, filename: str) -> str:
    cat = normalize_catalog(catalog)
    for label, v in (("dam_id", dam_id), ("dssmp_version_id", dssmp_version_id)):
        if not isinstance(v, str) or not _COMPONENT_RE.match(v) or ".." in v:
            raise ValueError(f"{label} {v!r} is not a safe volume path component")
    return f"/Volumes/{cat}/{VOLUME_SCHEMA}/{VOLUME_NAME}/{dam_id}/{dssmp_version_id}/{safe_filename(filename)}"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class VolumeFS:
    """Plain file I/O on a /Volumes path (UC volumes are FUSE-mounted on serverless / UC clusters). Tests pass a
    temporary root to map /Volumes/... under it."""

    def __init__(self, root: str = ""):
        self.root = root

    def _p(self, path):
        return self.root + path if self.root else path

    def read(self, path) -> Optional[bytes]:
        try:
            with open(self._p(path), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def write(self, path, data: bytes) -> None:
        os.makedirs(os.path.dirname(self._p(path)), exist_ok=True)
        with open(self._p(path), "xb") as f:               # never overwrite
            f.write(data)

    def remove(self, path) -> None:
        try:
            os.remove(self._p(path))
        except FileNotFoundError:
            pass


def stage(row: Mapping, doc: Optional[Mapping], catalog: str, fs, write_hook: Callable = None) -> dict:
    """row: the outbox row (dam_id, dssmp_version_id, document_uri, document_sha256). doc: its dssmp_document row or
    None. Returns {'volume_uri': path} or {'code': <DOC_CODE>, 'message': '<code>: <detail>'}."""
    def bad(code, detail):
        return {"code": code, "message": f"{code}: {detail}"}

    did = document_id(row.get("document_uri"))
    if did is None:
        return bad("dssmp_document_missing", f"document_uri {row.get('document_uri')!r} is not "
                                             "lakebase:dam_app.dssmp_document/<id>")
    if doc is None:
        return bad("dssmp_document_missing", f"no dam_app.dssmp_document row {did!r}")
    if doc.get("dam_id") != row.get("dam_id"):
        return bad("dssmp_document_other_dam", f"document {did!r} belongs to {doc.get('dam_id')!r}")
    want = str(row.get("document_sha256") or "")
    try:
        data = base64.b64decode(doc.get("content_b64") or "", validate=True)
    except (binascii.Error, ValueError, TypeError):
        return bad("dssmp_document_hash_mismatch", f"document {did!r} content is not valid base64 (before copy)")
    got = _sha(data)
    if got != want or str(doc.get("sha256") or "").lower() != want:
        return bad("dssmp_document_hash_mismatch", f"before copy: content {got}, dssmp_document.sha256 "
                                                   f"{doc.get('sha256')}, outbox document_sha256 {want}")
    try:
        path = volume_path(catalog, row.get("dam_id"), row.get("dssmp_version_id"), doc.get("filename"))
    except ValueError as e:
        return bad("dssmp_document_missing", str(e))
    try:
        existing = fs.read(path)
        if existing is not None:
            if _sha(existing) == want:
                return {"volume_uri": path}
            return bad("dssmp_document_hash_mismatch", f"{path} already holds a different file; not overwritten")
        fs.write(path, data)
        if write_hook:
            write_hook(path)
        back = fs.read(path)
    except OSError as e:
        return bad("dssmp_document_copy_failed", f"{path}: {type(e).__name__}")
    if back is None or _sha(back) != want:
        fs.remove(path)
        return bad("dssmp_document_hash_mismatch", f"after copy: {path} reads back "
                                                   f"{_sha(back) if back is not None else 'nothing'}, expected {want}; "
                                                   "copy removed")
    return {"volume_uri": path}


def apply_documents(decisions: List[dict], pending: Iterable[Mapping], docs: Mapping[str, Mapping], catalog: str,
                    fs, write_hook: Callable = None) -> List[dict]:
    """For each 'applied' decision stage its document; on success set insert.volume_uri, on failure turn the decision
    into a refusal (status rejected, reason_code = the DOC_CODE) so the MERGE never registers it."""
    rows = {r.get("outbox_id"): r for r in pending}
    for d in decisions:
        if d["decision"] != "applied":
            continue
        r = rows[d["outbox_id"]]
        res = stage(r, docs.get(document_id(r.get("document_uri")) or ""), catalog, fs, write_hook)
        if "volume_uri" in res:
            d["insert"]["volume_uri"] = res["volume_uri"]
        else:
            d.update(decision="rejected_invalid", status="rejected", reason_code=res["code"], codes=[res["code"]],
                     message=res["message"] + "; nothing registered", insert=None, close=None)
    return decisions


def document_ids(pending: Iterable[Mapping]) -> List[str]:
    return sorted({i for i in (document_id(r.get("document_uri")) for r in pending) if i})


__all__ = ["DOC_CODES", "DOC_SQL", "document_id", "safe_filename", "volume_path", "VolumeFS", "stage",
           "apply_documents", "document_ids"]
