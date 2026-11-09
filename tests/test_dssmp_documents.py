"""v1.7 (b): DSSMP document copy to /Volumes/<catalog>/ref/dssmp_documents/<dam_id>/<dssmp_version_id>/<file> with sha256
checked before and after (dam_safety.dssmp_documents), volume_uri on ref.dssmp_version, the volume declared in
sql/ddl/00 + migration v1_7_003, and the Lakebase SELECT grant for the publisher on the app's dssmp_document. Live
part on PostgreSQL when DAM_TEST_PG is set: the app's own StateStore stores the document, the publisher login reads it
with DOC_SQL, and the copy lands in a temporary volume root. Synthetic bytes only."""
import base64
import copy
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import dssmp_documents as dd  # noqa: E402
from dam_safety import dssmp_registration as dr  # noqa: E402
import load_dssmp_registrations as job  # noqa: E402

BYTES = b"%PDF-1.4 synthetic DSSMP test document\n"
SHA = hashlib.sha256(BYTES).hexdigest()
URI = "lakebase:dam_app.dssmp_document/DOC-1"
ROW = {"outbox_id": "OB-1", "dam_id": "D1", "dssmp_version_id": "DS-2", "document_uri": URI, "document_sha256": SHA}
DOC = {"document_id": "DOC-1", "dam_id": "D1", "filename": "DSSMP 2026 rev2.pdf", "sha256": SHA,
       "content_b64": base64.b64encode(BYTES).decode()}
PATH = "/Volumes/dam_dev/ref/dssmp_documents/D1/DS-2/DSSMP_2026_rev2.pdf"


@pytest.fixture()
def fs(tmp_path):
    return dd.VolumeFS(str(tmp_path))


def test_copy_lands_at_the_contract_path_and_reads_back(fs):
    assert dd.stage(ROW, DOC, "dam_dev", fs) == {"volume_uri": PATH}
    assert fs.read(PATH) == BYTES
    assert dd.stage(ROW, DOC, "dam_dev", fs) == {"volume_uri": PATH}        # re-run: same file accepted


@pytest.mark.parametrize("mut", [{"document_sha256": "0" * 64}])
def test_hash_mismatch_before_copy_refuses_and_writes_nothing(fs, mut):
    r = dd.stage({**ROW, **mut}, DOC, "dam_dev", fs)
    assert r["code"] == "dssmp_document_hash_mismatch" and "before copy" in r["message"]
    assert fs.read(PATH) is None


@pytest.mark.parametrize("doc", [{**DOC, "sha256": "1" * 64}, {**DOC, "content_b64": base64.b64encode(b"x").decode()},
                                 {**DOC, "content_b64": "%%% not base64"}])
def test_document_row_disagreeing_with_the_outbox_is_refused(fs, doc):
    assert dd.stage(ROW, doc, "dam_dev", fs)["code"] == "dssmp_document_hash_mismatch"
    assert fs.read(PATH) is None


def test_hash_mismatch_after_copy_refuses_and_removes_the_copy(fs):
    def corrupt(path):
        with open(fs._p(path), "ab") as f:
            f.write(b"!")
    r = dd.stage(ROW, DOC, "dam_dev", fs, write_hook=corrupt)
    assert r["code"] == "dssmp_document_hash_mismatch" and "after copy" in r["message"]
    assert fs.read(PATH) is None


def test_existing_different_file_is_never_overwritten(fs):
    os.makedirs(os.path.dirname(fs._p(PATH)))
    with open(fs._p(PATH), "wb") as f:
        f.write(b"older")
    assert dd.stage(ROW, DOC, "dam_dev", fs)["code"] == "dssmp_document_hash_mismatch"
    assert fs.read(PATH) == b"older"


def test_missing_other_dam_and_copy_failure(fs):
    assert dd.stage({**ROW, "document_uri": "/Volumes/x/y.pdf"}, DOC, "dam_dev", fs)["code"] == "dssmp_document_missing"
    assert dd.stage(ROW, None, "dam_dev", fs)["code"] == "dssmp_document_missing"
    assert dd.stage(ROW, {**DOC, "dam_id": "D2"}, "dam_dev", fs)["code"] == "dssmp_document_other_dam"
    assert dd.stage({**ROW, "dam_id": "../D1"}, {**DOC, "dam_id": "../D1"}, "dam_dev", fs)["code"] == \
        "dssmp_document_missing"

    class Broken(dd.VolumeFS):
        def write(self, path, data):
            raise PermissionError("no WRITE VOLUME")
    assert dd.stage(ROW, DOC, "dam_dev", Broken(fs.root))["code"] == "dssmp_document_copy_failed"


def test_every_doc_code_is_tested():
    src = open(__file__).read()
    assert all(f'"{c}"' in src for c in dd.DOC_CODES)


@pytest.mark.parametrize("name,want", [("a/b/../c.pdf", "c.pdf"), ("..\\..\\x.pdf", "x.pdf"), ("", "document"),
                                        (".hidden", "hidden"), ("ü ñ.pdf", "___.pdf")])
def test_safe_filename(name, want):
    assert dd.safe_filename(name) == want


def test_volume_path_refuses_garbled_catalog_and_traversal():
    for bad in ("dam_prod2", "", None):
        with pytest.raises(ValueError):
            dd.volume_path(bad, "D1", "DS-2", "f.pdf")
    with pytest.raises(ValueError):
        dd.volume_path("dam_dev", "D1", "..", "f.pdf")
    assert dd.volume_path("DAM_PROD", "D1", "DS-2", "f.pdf") == "/Volumes/dam_prod/ref/dssmp_documents/D1/DS-2/f.pdf"


def _plan_one():
    from test_dssmp_registration import AUTH, H1, NOW, approve_rec, outbox
    r = outbox(document_uri=URI, document_sha256=SHA)
    rec = approve_rec()
    import json
    cv = json.loads(rec["corrected_value"])
    cv["document_sha256"] = SHA
    rec["corrected_value"] = json.dumps(cv)
    return r, dr.plan([r], [], {H1: rec}, AUTH, NOW)


def test_apply_documents_sets_volume_uri_or_turns_the_decision_into_a_refusal(fs):
    r, ds = _plan_one()
    assert ds[0]["decision"] == "applied", ds[0]["message"]
    doc = {**DOC, "dam_id": r["dam_id"]}
    ok = dd.apply_documents(copy.deepcopy(ds), [r], {"DOC-1": doc}, "dam_dev", fs)
    assert ok[0]["insert"]["volume_uri"] == f"/Volumes/dam_dev/ref/dssmp_documents/{r['dam_id']}/DS-2/DSSMP_2026_rev2.pdf" and \
        dr.merge_source_rows(ok)[0]["volume_uri"] == ok[0]["insert"]["volume_uri"]
    bad = dd.apply_documents(copy.deepcopy(ds), [r], {"DOC-1": {**doc, "sha256": "2" * 64}}, "dam_dev",
                             dd.VolumeFS(fs.root + "/b"))
    assert bad[0]["status"] == "rejected" and bad[0]["reason_code"] == "dssmp_document_hash_mismatch"
    assert dr.merge_source_rows(bad) == []
    assert job.outbox_updates(bad, "T")[0][:2] == ("rejected", "dssmp_document_hash_mismatch")


def test_declared_in_ddl_migration_and_contract_columns():
    vol = ("CREATE VOLUME IF NOT EXISTS ref.dssmp_documents COMMENT 'Approved DSSMP documents copied by dam_ref_publish: "
           "/<dam_id>/<dssmp_version_id>/<file> (sha256 checked; v1.7)';")
    assert vol in open(os.path.join(ROOT, "sql", "ddl", "00_catalog_schemas.sql")).read()
    assert vol in open(os.path.join(ROOT, "sql", "migrations", "v1_7_003_dssmp_registration.sql")).read()
    down = open(os.path.join(ROOT, "sql", "migrations", "v1_7_003_dssmp_registration_down.sql")).read()
    assert "DROP VOLUME" not in down and "volume_uri" in down
    assert "volume_uri" in dr.DSSMP_COLS
    d04 = open(os.path.join(ROOT, "sql", "ddl", "04_ref_dssmp_pfm_eap.sql")).read()
    assert re.search(r"^\s+volume_uri\s+STRING", d04, re.M)
    jobsrc = open(os.path.join(ROOT, "src", "jobs", "load_dssmp_registrations.py")).read()
    assert jobsrc.index("apply_documents(") < jobsrc.index("merge_source_rows(decisions)")


def test_lakebase_dssmp_document_mirrors_the_app_ddl_and_publisher_has_select():
    st = open(os.path.join(ROOT, "app", "backend", "repos", "state.py")).read()
    app = re.search(r'"""CREATE TABLE IF NOT EXISTS \{s\}dssmp_document \((.*?)\)"""', st, re.S).group(1)
    lb = open(os.path.join(ROOT, "lakebase", "001_app_schema.sql")).read()
    assert f"CREATE TABLE IF NOT EXISTS dssmp_document ({app});" in lb
    assert "GRANT SELECT ON dssmp_document TO dam_lakebase_publisher;" in lb


# ---------------------------------------------------------------- live
PG = os.environ.get("DAM_TEST_PG")
live = pytest.mark.skipif(not PG or not shutil.which("psql"), reason="set DAM_TEST_PG (libpq conninfo) + psql")
DDL_PATH = os.path.join(ROOT, "lakebase", "001_app_schema.sql")
VALIDATOR = os.path.join(ROOT, "lakebase", "validate_append_only.sql")


def _psql(conninfo, *args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["psql", conninfo, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *args],
                          capture_output=True, text=True, env=env)


@live
def test_live_app_stores_publisher_reads_and_copies(tmp_path):
    tag = uuid.uuid4().hex[:8]
    db, app, pub = f"lbdoc_{tag}", f"lbdoc_app_{tag}", f"lbdoc_pub_{tag}"
    assert _psql(PG, "-c", f"CREATE DATABASE {db}", "-c", f"CREATE ROLE {app} LOGIN",
                 "-c", f"CREATE ROLE {pub} LOGIN").returncode == 0
    conn = f"{PG} dbname={db}"
    opts = {"PGOPTIONS": f"-c dam.app_role={app} -c dam.publisher_login={pub}"}
    try:
        r = _psql(conn, "-f", DDL_PATH, env_extra=opts)
        assert r.returncode == 0, r.stderr
        r = _psql(conn, "-f", VALIDATOR, env_extra=opts)
        assert r.returncode == 0 and r.stdout.strip() == "", r.stdout + r.stderr
        code = f"""
import sys; sys.path.insert(0, {os.path.join(ROOT, 'app')!r})
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from backend.repos.state import StateStore
pool = ConnectionPool({conn + ' user=' + app!r}, kwargs={{"row_factory": dict_row}}, open=True)
st = StateStore("postgres", schema="dam_app", pg_pool=pool)
st.insert_dssmp_document({{**{ {k: v for k, v in DOC.items()}!r}, "content_type": "application/pdf",
                          "size_bytes": {len(BYTES)}, "uploaded_by": "damadmin@x.org", "uploaded_at": "2026-11-09T15:00:00Z"}})
pool.close()
"""
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        if "No module named" in r.stderr:
            pytest.skip(r.stderr.strip().splitlines()[-1])
        assert r.returncode == 0, r.stderr[-2000:]
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(f"{conn} user={pub}", options="-c search_path=dam_app") as c, \
                c.cursor(row_factory=dict_row) as cur:
            cur.execute(dd.DOC_SQL, (dd.document_ids([ROW]),))
            docs = {x["document_id"]: x for x in cur.fetchall()}
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE dssmp_document SET sha256 = 'x'")
        fs = dd.VolumeFS(str(tmp_path))
        assert dd.stage(ROW, docs.get("DOC-1"), "dam_dev", fs) == {"volume_uri": PATH}
        assert fs.read(PATH) == BYTES
        # validator flags a publisher without SELECT on dssmp_document
        assert _psql(conn, "-c", "REVOKE SELECT ON dam_app.dssmp_document FROM dam_lakebase_publisher").returncode == 0
        r = _psql(conn, "-f", VALIDATOR, env_extra=opts)
        assert "lacks SELECT on dam_app.dssmp_document" in r.stdout
    finally:
        _psql(PG, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        _psql(PG, "-c", f"DROP ROLE IF EXISTS {app}", "-c", f"DROP ROLE IF EXISTS {pub}")
