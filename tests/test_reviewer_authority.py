"""v1.6 ROLE-LEVEL (Genie-confirmed): least-privilege ref.v_dam_reviewer_authority (sql/ddl/10b), its per-dam grants,
and the derived review_level_legacy_unverified flag (gold.v_review_level_check, gold.incl_correction_status, extract).

The view SQL is executed in SQLite (temp views; ref.dam_row_filter emulated for a caller's groups) to check the logic.
Databricks evaluation (dynamic view, UC grants, ABAC) is UNVERIFIED (docs/VERIFICATION.md UC-GRANTS). Synthetic data."""
import glob
import hashlib
import importlib.util
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
import lakebase_extract as lx  # noqa: E402

V10B = open(os.path.join(ROOT, "sql", "ddl", "10b_reviewer_authority.sql"), encoding="utf-8").read()
V11 = open(os.path.join(ROOT, "sql", "ddl", "11_gold_views.sql"), encoding="utf-8").read()


def _code(sql):
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def _view_body(src, name):
    m = re.search(rf"CREATE OR REPLACE VIEW {re.escape(name)}\b.*?\bAS\n(.*?);", _code(src), re.S)
    assert m, name
    return m.group(1)


# ---------------------------------------------------------------- static: least privilege
def test_view_exposes_only_cdse_authority_columns_and_rows():
    body = _view_body(V10B, "ref.v_dam_reviewer_authority")
    assert "upper(m.role) = 'CDSE'" in body and "m.dam_id <> '*'" in body
    assert body.count("ref.dam_row_filter(") == 2                     # both branches filtered per dam
    assert "SELECT *" not in body and "m.granted_by" not in body       # no other columns leak


def test_no_grant_on_base_tables_and_grants_only_to_dam_groups():
    for f in glob.glob(os.path.join(ROOT, "sql", "**", "*.sql"), recursive=True):
        code = _code(open(f, encoding="utf-8").read())
        assert not re.search(r"GRANT\s+\w+.*\bON\s+(TABLE\s+)?ref\.(dam|project_membership)\b", code, re.I), f
    grants = re.findall(r"^GRANT (.+?) TO `([^`]+)`;", _code(V10B), re.M)
    assert grants and all(re.fullmatch(r"dam-[A-Za-z0-9_-]+-members", g) for _, g in grants)
    assert {p for p, _ in grants} == {"USE SCHEMA ON SCHEMA ref", "SELECT ON TABLE ref.v_dam_reviewer_authority"}


def test_generated_grants_in_sync_and_wired_into_ci_and_setup():
    spec = importlib.util.spec_from_file_location("rug", os.path.join(ROOT, "tools", "render_uc_reviewer_grants.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.main(["--check"]) == 0
    assert "render_uc_reviewer_grants.py --check" in open(os.path.join(ROOT, ".github", "workflows", "bundle.yml")).read()
    assert "10b_reviewer_authority.sql" in open(os.path.join(ROOT, "resources", "uc_setup.yml")).read()
    with pytest.raises(ValueError):
        m.render(["x`; DROP"])


# ---------------------------------------------------------------- executed logic (SQLite)
def _sqlite(caller_groups):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.create_function("dam_row_filter", 1,
                        lambda d: int("dam-portfolio-all" in caller_groups or f"dam-{d}-members" in caller_groups))
    con.executescript("""
      CREATE TABLE dam (dam_id TEXT, cdse_id TEXT);
      CREATE TABLE project_membership (principal TEXT, dam_id TEXT, role TEXT, granted_by TEXT,
                                       granted_ts TEXT, revoked_ts TEXT);
      CREATE TABLE review_record (review_id TEXT, dam_id TEXT, subject_type TEXT, subject_id TEXT, review_level TEXT,
                                  reviewer_id TEXT, reviewer_email TEXT, signed_ts TEXT);
      INSERT INTO dam VALUES ('D1', 'cdse.one@x.org'), ('D2', 'cdse.two@x.org');
      INSERT INTO project_membership VALUES
        ('deputy@x.org', 'D1', 'CDSE', 'a', '2026-01-01 00:00:00', NULL),
        ('former@x.org', 'D1', 'CDSE', 'a', '2025-01-01 00:00:00', '2026-03-01 00:00:00'),
        ('admin@x.org',  'D1', 'ADMIN', 'a', '2025-01-01 00:00:00', NULL),
        ('dso@x.org',    'D1', 'ENGINEER', 'a', '2025-01-01 00:00:00', NULL),
        ('star@x.org',   '*',  'CDSE', 'a', '2025-01-01 00:00:00', NULL),
        ('d2deputy@x.org', 'D2', 'CDSE', 'a', '2025-01-01 00:00:00', NULL);
    """)
    sub = lambda b: (b.replace("ref.dam_row_filter(", "dam_row_filter(").replace("ref.project_membership", "project_membership")  # noqa: E731
                     .replace("ref.dam ", "dam ").replace("ref.v_dam_reviewer_authority", "v_dam_reviewer_authority")
                     .replace("audit.review_record", "review_record").replace("CAST(NULL AS TIMESTAMP)", "NULL"))
    con.execute("CREATE TEMP VIEW v_dam_reviewer_authority AS " + sub(_view_body(V10B, "ref.v_dam_reviewer_authority")))
    con.execute("CREATE TEMP VIEW v_review_level_check AS " + sub(_view_body(V11, "gold.v_review_level_check")))
    return con


def test_dam_member_sees_only_own_dam_cdse_rows():
    con = _sqlite({"dam-D1-members"})
    rows = {(r["dam_id"], r["principal"], r["authority_source"]) for r in con.execute("SELECT * FROM v_dam_reviewer_authority")}
    assert rows == {("D1", "cdse.one@x.org", "DAM_CDSE_ID"), ("D1", "deputy@x.org", "MEMBERSHIP_CDSE"),
                    ("D1", "former@x.org", "MEMBERSHIP_CDSE")}          # no ADMIN/ENGINEER, no '*', no D2
    assert [c[0] for c in con.execute("SELECT * FROM v_dam_reviewer_authority").description] == \
        ["dam_id", "cdse_id", "principal", "authority_source", "granted_ts", "revoked_ts"]
    assert {r["dam_id"] for r in _sqlite({"dam-portfolio-all"}).execute("SELECT * FROM v_dam_reviewer_authority")} == {"D1", "D2"}
    assert list(_sqlite(set()).execute("SELECT * FROM v_dam_reviewer_authority")) == []


REVIEWS = [  # (review_id, dam, level, reviewer_id, reviewer_email, signed_ts, expected legacy flag)
    ("R1", "D1", "CDSE", "cdse.one@x.org", None, "2026-06-01 00:00:00", False),       # named CDSE
    ("R2", "D1", "CDSE", "uid-77", "CDSE.ONE@x.org", "2026-06-01 00:00:00", False),     # match by e-mail, any case
    ("R3", "D1", "CDSE", "dso@x.org", None, "2026-06-01 00:00:00", True),               # dso under old proposal
    ("R4", "D1", "CDSE", "admin@x.org", None, "2026-06-01 00:00:00", True),             # admin under old proposal
    ("R5", "D1", "CDSE", "deputy@x.org", None, "2026-06-01 00:00:00", False),           # active CDSE membership
    ("R6", "D1", "CDSE", "deputy@x.org", None, "2025-06-01 00:00:00", True),            # before the grant
    ("R7", "D1", "CDSE", "former@x.org", None, "2026-02-01 00:00:00", False),           # before revocation
    ("R8", "D1", "CDSE", "former@x.org", None, "2026-04-01 00:00:00", True),            # after revocation
    ("R9", "D1", "CDSE", "star@x.org", None, "2026-06-01 00:00:00", True),              # '*' grant not resolved
    ("R10", "D1", "CDSE", "d2deputy@x.org", None, "2026-06-01 00:00:00", True),         # CDSE of another dam
    ("R11", "D1", "engineer", "dso@x.org", None, "2026-06-01 00:00:00", False),         # engineer level: no flag
]


def test_gold_review_level_check_flags_only_unverified_cdse_level_records():
    con = _sqlite({"dam-portfolio-all"})
    con.executemany("INSERT INTO review_record VALUES (?,?,?,?,?,?,?,?)",
                    [(r[0], r[1], "incl_correction", "c", r[2], r[3], r[4], r[5]) for r in REVIEWS])
    got = {r["review_id"]: bool(r["review_level_legacy_unverified"]) for r in con.execute("SELECT * FROM v_review_level_check")}
    assert got == {r[0]: r[6] for r in REVIEWS}


def test_extract_flag_matches_gold_rule_and_leaves_the_record_unchanged():
    con = _sqlite({"dam-portfolio-all"})
    auth = [dict(r) for r in con.execute("SELECT * FROM v_dam_reviewer_authority")]
    for rid, dam, lvl, rev, email, ts, want in REVIEWS:
        rec = {"dam_id": dam, "review_level": lvl, "reviewer_id": rev, "reviewer_email": email,
               "signed_ts": ts.replace(" ", "T") + "Z"}
        assert lx.review_level_legacy_unverified(rec, auth) is want, rid


def test_extract_records_carry_the_derived_flag_but_keep_every_stored_value():
    rows = []
    for n, (rev, lvl) in enumerate([("dso@x.org", "CDSE"), ("cdse.one@x.org", "CDSE")], 1):
        rows.append({c: "x" for c in lx.REVIEW_REQUIRED} | {
            "review_id": f"R{n}", "dam_id": "D1", "review_level": lvl, "reviewer_id": rev, "chain_id": "D1",
            "chain_seq": n, "signed_ts": "2026-06-01T00:00:00Z", "record_hash": hashlib.sha256(rev.encode()).hexdigest()})
    auth = [{"dam_id": "D1", "principal": "cdse.one@x.org", "granted_ts": None, "revoked_ts": None}]
    recs = lx.review_rows_to_records(rows, authority=auth)
    assert [r[lx.LEGACY_FLAG] for r in recs] == [True, False]
    for src, rec in zip(rows, recs):
        assert all(rec[c] == src[c] for c in ("review_id", "review_level", "reviewer_id", "record_hash"))
    assert lx.LEGACY_FLAG not in lx.REVIEW_RECORD_COLS                   # never written to audit.review_record
    assert lx.LEGACY_FLAG not in lx.review_rows_to_records(rows)[0]       # no authority -> no derived key


def test_incl_correction_status_exposes_the_flag():
    body = V11[V11.index("CREATE OR REPLACE VIEW gold.incl_correction_status"):]
    assert "coalesce(lc.review_level_legacy_unverified, false) AS review_level_legacy_unverified" in body
    assert "LEFT JOIN gold.v_review_level_check lc ON lc.review_id = r.review_id" in body
