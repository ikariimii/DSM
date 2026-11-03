"""Ack -> ACK ownership and DSSMR draft write path (SCHEMA_CONTRACT v1.1 §7.2; app open question 11).
Pure functions of src/jobs/lakebase_extract.py; app row shapes from lakebase/001_app_schema.sql
(alarm_ack, report_draft) and app/backend/repos/state.py now_iso() ('...Z', second precision)."""
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
sys.path.insert(0, os.path.join(ROOT, "src"))
import lakebase_extract as lx  # noqa: E402

NOW = dt.datetime(2026, 11, 2, 23, 0, tzinfo=dt.timezone.utc)


def test_ack_events_deterministic_and_keyed_on_raise_transition():
    rows = [{"event_id": "tr-raise-1", "dam_id": "CRD", "state": "acknowledged", "acknowledged_by": "eng@x",
             "acknowledged_at": "2026-11-02T22:10:05Z", "closed_by": None, "closed_at": None, "close_reason": None},
            {"event_id": "tr-raise-2", "dam_id": "CRD", "state": "open", "acknowledged_by": None,
             "acknowledged_at": None, "closed_by": None, "closed_at": None, "close_reason": None}]
    ev = lx.ack_rows_to_events(rows, NOW)
    assert len(ev) == 1 and ev[0]["event_type"] == "ALARM_ACK" and ev[0]["subject_id"] == "tr-raise-1"
    assert ev[0]["event_ts"] == dt.datetime(2026, 11, 2, 22, 10, 5, tzinfo=dt.timezone.utc)
    assert lx.ack_rows_to_events(rows, NOW)[0]["event_id"] == ev[0]["event_id"]      # re-extract => same id


def test_closed_is_note_not_machine_event():
    rows = [{"event_id": "tr-raise-1", "dam_id": "CRD", "state": "closed", "acknowledged_by": "eng@x",
             "acknowledged_at": "2026-11-02T22:10:05Z", "closed_by": "eng@x", "closed_at": "2026-11-02T22:30:00Z",
             "close_reason": "verified sensor fault"}]
    assert lx.close_rows_to_events(rows, NOW)[0]["event_type"] == "NOTE"
    assert lx.ack_rows_to_events(rows, NOW)[0]["event_type"] == "ALARM_ACK"          # ack still recorded once


def test_draft_snapshot_only_on_content_change():
    r = {"report_id": "rep-1", "dam_id": "CRD", "period_start": "2026-01-01", "period_end": "2026-06-30",
         "created_by": "eng@x", "created_at": "2026-11-02T20:00:00Z", "status": "draft", "content_md": "# DSSMR"}
    s1 = lx.draft_snapshots([r], set(), NOW)
    assert len(s1) == 1
    assert lx.draft_snapshots([r], {("rep-1", s1[0]["content_sha256"])}, NOW) == []
    assert len(lx.draft_snapshots([{**r, "content_md": "# DSSMR v2"}], {("rep-1", s1[0]["content_sha256"])}, NOW)) == 1


def test_evaluator_consumes_alarm_ack_event_type():
    src = open(os.path.join(ROOT, "src", "jobs", "alarm_evaluator.py")).read()
    assert "event_type = 'ALARM_ACK'" in src and "lakebase_ack:" in src
