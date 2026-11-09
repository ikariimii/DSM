"""v1.8 backfill sql/migrations/v1_8_002_lifecycle_backfill.sql (rule 6), executed on PostgreSQL 17 through the same
Spark-SQL -> PG translation as tests/test_migration_downs.py: one draft_commissioning -> live audit.lifecycle_event per
dam from the earliest onboarding_golive audit_log record in audit.app_extract (at-least-once copies deduplicated on
pk), strict 'YYYY-MM-DDTHH:MM:SSZ' ts, fail closed (nothing written) on any malformed / unreadable / mismatched
record, chained (event_hash recomputed here in Python with dam_safety.lifecycle), idempotent, then the SCD-2 row and
the ref.dam mirror. Synthetic data only. UNVERIFIED on Databricks (LIFECYCLE-BACKFILL)."""
import json
import os
import uuid

import pytest

from dam_safety import lifecycle as L
from test_migration_downs import PG, TABLES, TXT, UPS, _base_table, _pg, _psql, _run, live

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BF = "v1_8_002_lifecycle_backfill.sql"
GO = "2026-03-01T14:05:09Z"


def test_the_file_carries_exactly_the_engine_hash_expression():
    s = TXT[BF]
    expr = {"prev_event_hash": f"'{L.GENESIS}'", "event_id": "b.event_id", "dam_id": "b.dam_id",
            "from_state": "'draft_commissioning'", "to_state": "'live'", "trigger_code": "'backfill_onboarding_golive'",
            "authorizer_role": "'legacy_onboarding_admin'", "authorizer_user_id": "b.actor",
            "evidence_uri": "concat('uc:audit.app_extract/audit_log/', b.pk)", "effective_ts": "b.ts",
            "created_by": "'migration:v1_8_002'",
            "rationale": "'backfilled from the onboarding_golive audit_log record'",
            "citation_code": "'go_live_dssmp_12_12'", "approved_by": "b.actor",
            "reason_code": "'backfill_onboarding_golive'"}
    assert L.hash_sql(expr) in s
    assert BF in UPS and "v1_8_002_lifecycle_backfill_down.sql" in TXT
    down = "\n".join(l for l in TXT["v1_8_002_lifecycle_backfill_down.sql"].splitlines() if not l.startswith("--"))
    assert "DELETE" not in down and "lifecycle_event" not in down          # permanent records: the down is a no-op


def _payload(dam, ts=GO, action="onboarding_golive", pk="7", actor="admin@example.org"):
    return json.dumps({"id": int(pk), "ts": ts, "actor": actor, "action": action, "dam_id": dam,
                       "target": "DRAFT-1"}, sort_keys=True)


def _row(pk, dam, payload=None, env_dam="same", **kw):
    p = payload if payload is not None else _payload(dam, pk=pk, **kw)
    e = dam if env_dam == "same" else env_dam
    lit = lambda v: "NULL" if v is None else "'" + v.replace("'", "''") + "'"
    return f"INSERT INTO audit.app_extract (table_name, pk, dam_id, payload) VALUES ('audit_log', {lit(pk)}, " \
           f"{lit(e)}, {lit(p)});"


@pytest.fixture()
def db(tmp_path):
    name = f"lcbf_{uuid.uuid4().hex[:8]}"
    assert _psql(PG, f"CREATE DATABASE {name}").returncode == 0
    conn = f"{PG} dbname={name}"
    base = "CREATE SCHEMA silver; CREATE SCHEMA ops; CREATE SCHEMA ref; CREATE SCHEMA audit;\n" + \
        "\n".join(_base_table(t) for t in TABLES)
    r = _psql(conn, base)
    assert r.returncode == 0, r.stderr
    for f in UPS:
        if f != BF:
            _run(conn, tmp_path, TXT[f], f)
    yield conn, tmp_path
    _psql(PG, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _backfill(conn, tmp, label="bf"):
    p = tmp / f"{label}.sql"
    p.write_text(_pg(TXT[BF]))
    return _psql(conn, f=str(p))


def _events(conn):
    import psycopg
    cols = [c for c in L.EVENT_COLS if c != "created_ts"]
    with psycopg.connect(conn) as c:
        cur = c.execute(f"SELECT {', '.join(cols)} FROM audit.lifecycle_event ORDER BY dam_id, event_id")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@live
def test_backfill_dedupes_takes_the_earliest_chains_and_is_idempotent(db):
    conn, tmp = db
    setup = [_row("7", "D1"), _row("7", "D1"),                                      # at-least-once copy
             _row("9", "D1", ts="2026-04-01T00:00:00Z"),                             # a later second go-live
             _row("8", "D2", ts="2026-05-02T08:00:00Z", actor=None),
             _row("10", "D3", action="onboarding_create"),                           # another action: ignored
             "INSERT INTO ref.dam (dam_id) VALUES ('D1'), ('D2');"]
    assert _psql(conn, "\n".join(setup)).returncode == 0
    for k in range(2):
        r = _backfill(conn, tmp, f"bf{k}")
        assert r.returncode == 0, r.stderr
        evs = _events(conn)
        assert [(e["event_id"], e["dam_id"]) for e in evs] == [("backfill:onboarding_golive:7", "D1"),
                                                               ("backfill:onboarding_golive:8", "D2")]
        for e in evs:
            assert (e["from_state"], e["to_state"], e["prev_event_hash"]) == (L.DRAFT, L.LIVE, L.GENESIS)
            assert e["event_hash"] == L.event_hash(e), e["event_id"]               # Python recomputes the SQL hash
            assert e["citation_code"] == "go_live_dssmp_12_12" and e["evidence_uri"] == [
                "uc:audit.app_extract/audit_log/" + e["event_id"].rsplit(":", 1)[1]]
        assert L.ts_text(evs[0]["effective_ts"]) == GO and evs[1]["authorizer_user_id"] is None
        assert L.verify_chain(evs) == {"D1": None, "D2": None}
        st = _psql(conn, "SELECT dam_id || '|' || lifecycle_state || '|' || (effective_to IS NULL) "
                         "FROM ref.dam_lifecycle_state ORDER BY 1").stdout.split()
        assert st == ["D1|live|true", "D2|live|true"]
        mirror = _psql(conn, "SELECT dam_id || '|' || lifecycle_state FROM ref.dam ORDER BY 1").stdout.split()
        assert mirror == ["D1|live", "D2|live"]
    cur = L.current_states(_events(conn))
    assert cur["D1"]["state"] == L.LIVE and L.alarm_mode(cur["D1"]["state"]) == "production"


@live
def test_a_dam_that_already_has_lifecycle_events_is_skipped(db):
    conn, tmp = db
    assert _psql(conn, _row("7", "D1") + _row("8", "D2")).returncode == 0
    r = _backfill(conn, tmp, "first")
    assert r.returncode == 0, r.stderr
    assert _psql(conn, _row("11", "D1", ts="2026-01-01T00:00:00Z")).returncode == 0    # an earlier copy arrives late
    assert _backfill(conn, tmp, "second").returncode == 0
    ids = [e["event_id"] for e in _events(conn)]
    assert ids == ["backfill:onboarding_golive:7", "backfill:onboarding_golive:8"]


@pytest.mark.parametrize("bad", [
    _row("5", "D9", ts="2026-03-01 14:05:09"),                       # not strict ISO-8601 Z
    _row("5", "D9", ts="2026-03-01T14:05:09+00:00"),
    _row("5", "D9", ts="2026-03-01T14:05:09.123Z"),
    _row("5", "D9", ts="2026-02-30T00:00:00Z"),                      # matches the pattern, not a date
    _row("5", "D9", ts=None),
    _row("5", "D9", payload='{"action": "onboarding_golive", "dam_id": "D9", "ts": '),   # unreadable, mentions it
    _row("5", None, payload=_payload(None)),                        # no dam_id
    _row("5", "D9", env_dam="D8"),                                   # envelope dam differs from the payload's
])
@live
def test_any_malformed_go_live_record_fails_the_whole_backfill(db, bad):
    conn, tmp = db
    assert _psql(conn, _row("7", "D1") + bad).returncode == 0
    r = _backfill(conn, tmp)
    assert r.returncode != 0 and any(m in r.stderr for m in ("v1_8_002", "timestamp", "out of range")), r.stderr
    assert _psql(conn, "SELECT count(*) FROM audit.lifecycle_event").stdout.strip() == "0"
    assert _psql(conn, "SELECT count(*) FROM ref.dam_lifecycle_state").stdout.strip() == "0"
