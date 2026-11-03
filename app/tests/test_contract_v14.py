"""SCHEMA_CONTRACT v1.4 / api_contract §4.1 alignment (inclinometer approval when calib_id is NULL, bias proposals
with approval_status, alarm episode columns, ENDPOINT_NAME via `.id`)."""
import os
from datetime import datetime, timezone

import pytest

from backend.repos import contract as ct
from backend.verify import Gate, ts_key

INCL_F = [{"formula_id": "INCL_PROBE_2PASS", "formula_version": "1.0.0", "verified": True}]
INST = [{"instrument_id": "IN1", "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-06-01T00:00:00Z"},
        {"instrument_id": "IN1", "effective_from": "2026-06-01T00:00:00Z", "effective_to": None}]
ROW = {"instrument_id": "IN1", "calib_id": None, "formula_id": "INCL_PROBE_2PASS", "formula_version": "1.0.0",
       "installation_version": "2026-06-01T00:00:00Z", "survey_ts": "2026-07-01T00:00:00Z", "qa_flag": "OK",
       "correction_basis": "uncorrected"}


def test_ts_key_normalizes_formats():
    a = ts_key("2026-06-01T00:00:00Z")
    assert a == ts_key("2026-06-01T00:00:00+00:00") == ts_key("2026-06-01 00:00:00") \
        == ts_key(datetime(2026, 6, 1, tzinfo=timezone.utc)) == ts_key(datetime(2026, 6, 1))
    assert ts_key("2026-05-31T20:00:00-04:00") == a
    assert ts_key(None) is None and ts_key("not a time") is None and ts_key("2026-06-01T00:00:01Z") != a


def test_incl_approved_when_formula_verified_and_installation_matches():
    g = Gate(INCL_F, [], (), INST)
    assert g.status(dict(ROW)) == "VERIFIED"
    assert g.status({**ROW, "installation_version": "2026-01-01T00:00:00Z",
                     "survey_ts": "2026-03-01T00:00:00Z"}) == "VERIFIED"                  # older SCD2 row
    assert g.installation_approved(dict(ROW))


@pytest.mark.parametrize("change,status", [
    ({"installation_version": "2026-03-01T00:00:00Z"}, "UNAPPROVED_INSTALLATION"),   # no such SCD2 row
    ({"installation_version": None}, "UNAPPROVED_INSTALLATION"),                     # legacy row, no lineage
    ({"installation_version": "garbage"}, "UNAPPROVED_INSTALLATION"),
    ({"instrument_id": "IN2"}, "UNAPPROVED_INSTALLATION"),                           # another instrument's row
    # v1.5 half-open: the version must be the row valid at survey time, not just any row of the instrument
    ({"installation_version": "2026-01-01T00:00:00Z"}, "UNAPPROVED_INSTALLATION"),   # survey 07-01: row 2 valid
    ({"installation_version": "2026-01-01T00:00:00Z", "survey_ts": "2026-06-01T00:00:00Z"},
     "UNAPPROVED_INSTALLATION"),                                                       # effective_to exclusive
    ({"installation_version": "2026-01-01T00:00:00Z", "survey_ts": "2025-12-31T23:59:59Z"},
     "UNAPPROVED_INSTALLATION"),                                                       # before any row: no reduce
    ({"survey_ts": None}, "UNAPPROVED_INSTALLATION"),
    ({"formula_version": "1.0.0+QA021-v1"}, "UNVERIFIED_FORMULA"),                   # v1.3 writer value
    ({"formula_id": None}, "UNVERIFIED_FORMULA"),
    ({"qa_flag": "BAD"}, "QC_BAD"),
    ({"correction_basis": "corrected_unverified"}, "CORRECTION_UNVERIFIED"),
])
def test_incl_not_approved(change, status):
    g = Gate(INCL_F, [], (), INST)
    r = g.apply({**ROW, "a_cum_mm": 5.2, **change}, ("a_cum_mm",))
    assert r["display_status"] == status and r["a_cum_mm"] is None and r["verified"] is False


def test_incl_formula_unverified_in_ref_formula():
    g = Gate([{**INCL_F[0], "verified": False}], [], (), INST)
    assert g.status(dict(ROW)) == "UNVERIFIED_FORMULA" and not g.installation_approved(dict(ROW))


def test_non_incl_row_without_calibration_still_unapproved():
    g = Gate([{"formula_id": "F", "formula_version": "1", "verified": True}], [], (), INST)
    assert g.status({"formula_id": "F", "formula_version": "1", "calib_id": None,
                     "installation_version": "2026-06-01T00:00:00Z", "instrument_id": "IN1"}) == "UNAPPROVED_CALIBRATION"


def _rec(rid, verdict, status, ts, **kw):
    stat = (f"rule=QA021-v2;n=6;mean_dD=6.0000;bs=6.0000;sd=0.1000;se=0.0400;spread=0.2000;ZA=-6.0000;abs_dD=6.0000;"
            f"abs_bs=6.0000;crosscheck_verdict={verdict};status={status};requires_review=True;crosscheck_diff=0.0000;"
            "crosscheck_tol=2.0000;reference_survey_id=REF")
    return {"record_id": rid, "instrument_id": "IN1", "survey_id": "S", "axis": "A", "auto_value": -6.0,
            "detection_method": "between_set_bias_qc", "detection_statistic": stat, "applied": False,
            "superseded_by": None, "timestamp_utc": ts, "user": "qa", **kw}


OK_REVIEW = dict(applied=True, reviewed_by="rev", reviewed_utc="2026-11-02T00:00:00Z", review_id="RV1")


def test_installation_boundaries_and_overlap():
    g = Gate(INCL_F, [], (), INST)
    assert g.status({**ROW, "survey_ts": "2026-06-01T00:00:00Z"}) == "VERIFIED"         # effective_from inclusive
    assert g.status({**ROW, "installation_version": "2026-01-01T00:00:00Z",
                     "survey_ts": "2026-05-31T23:59:59Z"}) == "VERIFIED"
    over = Gate(INCL_F, [], (), INST + [{"instrument_id": "IN1", "effective_from": "2026-05-01T00:00:00Z",
                                          "effective_to": None}])
    assert over.status(dict(ROW)) == "UNAPPROVED_INSTALLATION"                            # overlap: never approve
    bad_to = Gate(INCL_F, [], (), [{"instrument_id": "IN1", "effective_from": "2026-06-01T00:00:00Z",
                                    "effective_to": "garbage"}])
    assert bad_to.status(dict(ROW)) == "UNAPPROVED_INSTALLATION"


def test_incl_alarm_episode_gated_by_installation_at_event_ts():
    g = Gate(INCL_F, [], (), INST)
    ev = {"event_id": "E", "instrument_id": "IN1", "calib_id": None, "formula_id": "INCL_PROBE_2PASS",
          "formula_version": "1.0.0", "installation_version": "2026-06-01T00:00:00Z",
          "event_ts": "2026-07-01T00:00:00Z", "value": 25.0, "qa_flag": "OK", "level": 3, "rule": "value_above"}
    assert g.apply_event(dict(ev))["display_status"] == "VERIFIED"
    for ch, st in (({"installation_version": None}, "LINEAGE_INCOMPLETE"),        # SQL episode today: withheld
                   ({"installation_version": "2026-01-01T00:00:00Z"}, "UNAPPROVED_INSTALLATION"),
                   ({"qa_flag": "BAD"}, "QC_BAD"), ({"qa_flag": "MISSING"}, "MISSING"), ({"value": None}, "MISSING")):
        out = g.apply_event({**ev, **ch})
        assert out["display_status"] == st and out["value"] is None and out["verified"] is False


def test_approval_status():
    assert ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t")) == "proposed"
    assert ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t", **OK_REVIEW)) == "approved"
    # QA-036 / v1.5: an applied record without an independent review (incl. review_id -> audit.review_record) is
    # not given an invented status; it is a data-integrity error (ck_incl_correction_applied_reviewed)
    bad = [{**OK_REVIEW, "reviewed_by": "qa"}, {**OK_REVIEW, "reviewed_utc": None}, {**OK_REVIEW, "review_id": None},
           dict(applied=True), dict(applied=True, superseded_by="b")]
    for kw in bad:
        with pytest.raises(ct.DataIntegrityError) as e:
            ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t", **kw))
        assert e.value.code == "APPLIED_WITHOUT_VALID_REVIEW" and e.value.record_id == "a"
    assert ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t", superseded_by="b", **OK_REVIEW)) == "superseded"


def test_approval_status_from_gold_view():
    """v1.5 §5.5.1: gold.incl_correction_status.approval_status is used as is (after the row checks)."""
    gold = dict(OK_REVIEW, approval_status="approved", approved_by="rev", approved_utc="2026-11-02T00:00:00Z")
    assert ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t", **gold)) == "approved"
    assert ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t", approval_status="proposed")) == "proposed"
    assert ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t", approval_status="superseded")) == "superseded"
    for kw, code in ((dict(approval_status="unreviewed_applied"), "APPLIED_WITHOUT_VALID_REVIEW"),   # never served
                     ({**OK_REVIEW, "approval_status": "unreviewed_applied"}, "APPLIED_WITHOUT_VALID_REVIEW"),
                     (dict(approval_status="approved"), "APPROVAL_STATUS_INCONSISTENT"),           # not applied
                     ({**OK_REVIEW, "approval_status": "approved"}, "APPROVAL_STATUS_INCONSISTENT"),  # no approver
                     ({**OK_REVIEW, "approval_status": "proposed"}, "APPROVAL_STATUS_INCONSISTENT"),
                     (dict(approval_status="pending"), "UNKNOWN_APPROVAL_STATUS")):
        with pytest.raises(ct.DataIntegrityError) as e:
            ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t", **kw))
        assert e.value.code == code
    p = ct.bias_proposal(_rec("U", "AGREE", "PROPOSED", "t", approval_status="unreviewed_applied"))
    assert p["approval_status"] is None and p["display_status"] == "DATA_INTEGRITY_ERROR" and p["za"] is None


def test_derive_superseded_from_append_only_rows():
    rows = [{"record_id": "A", "timestamp_utc": "2026-11-01"}, {"record_id": "B", "supersedes": "A",
                                                                "timestamp_utc": "2026-11-02"},
            {"record_id": "C", "supersedes": "A", "timestamp_utc": "2026-11-03"}]
    out = {r["record_id"]: r["superseded_by"] for r in ct.derive_superseded(rows)}
    assert out == {"A": "C", "B": None, "C": None}
    assert ct.derive_superseded([{"record_id": "A", "superseded_by": "L"}])[0]["superseded_by"] == "L"


def test_approval_statuses_are_exactly_the_api_contract_values():
    import re
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[2] / "docs" / "api_contract.md").read_text()
    line = next(x for x in doc.splitlines() if "`approval_status`" in x)
    assert set(ct.APPROVAL_STATUSES) <= set(re.findall(r"`([a-z_]+)`", line))
    got = {ct.approval_status(_rec("a", "AGREE", "PROPOSED", "t", **kw))
           for kw in ({}, OK_REVIEW, {"superseded_by": "b"}, {**OK_REVIEW, "superseded_by": "b"})}
    assert got == set(ct.APPROVAL_STATUSES)


def test_bias_proposal_integrity_error_is_shown_but_values_hidden(caplog):
    p = ct.bias_proposal(_rec("BAD", "AGREE", "PROPOSED", "t", applied=True, reviewed_by="qa",
                              reviewed_utc="2026-11-02T00:00:00Z"))
    assert p["record_id"] == "BAD" and p["approval_status"] is None
    assert p["integrity_error"] == "APPLIED_WITHOUT_VALID_REVIEW" and p["display_status"] == "DATA_INTEGRITY_ERROR"
    assert all(p[k] is None for k in ("za", "abs_dD", "abs_bs", "crosscheck_diff", "crosscheck_tol"))
    assert p["evidence"] == {} and p["applied_by_app"] is False
    assert "APPLIED_WITHOUT_VALID_REVIEW" in caplog.text
    ok = ct.bias_proposal(_rec("OK", "AGREE", "PROPOSED", "t"))
    assert ok["integrity_error"] is None and ok["za"] == -6.0 and ok["user"] == "qa"
    # still listed (anomaly visible), never as a valid proposal
    assert [q["record_id"] for q in ct.bias_proposals([_rec("BAD", "AGREE", "PROPOSED", "t", applied=True)])] == ["BAD"]


def test_bias_proposals_fields_and_agree_first():
    recs = [_rec("S1", "SUSPECT", "SUSPECT_CROSSCHECK", "2026-11-03T00:00:00Z"),
            _rec("A1", "AGREE", "PROPOSED", "2026-11-01T00:00:00Z", superseded_by="A2"),
            _rec("A2", "AGREE", "PROPOSED", "2026-11-02T00:00:00Z"),
            _rec("X", "AGREE", "BAD_RESURVEY", "2026-11-04T00:00:00Z"),               # not a proposal status
            {**_rec("Y", "AGREE", "PROPOSED", "t"), "detection_method": "manual"}]  # not engine QC
    out = ct.bias_proposals(recs)
    assert [p["record_id"] for p in out] == ["A2", "A1", "S1"]
    p = out[0]
    assert (p["za"], p["abs_dD"], p["abs_bs"], p["crosscheck_verdict"], p["status"], p["approval_status"]) == \
        (-6.0, 6.0, 6.0, "AGREE", "PROPOSED", "proposed")
    assert p["rule"] == "QA021-v2" and p["requires_review"] and p["crosscheck_tol"] == 2.0
    assert p["display_status"] == "UNVERIFIED" and p["applied_by_app"] is False
    assert out[1]["approval_status"] == "superseded" and out[2]["crosscheck_verdict"] == "SUSPECT"
    # ZA missing from the statistic -> auto_value; unparseable numbers -> None (never invented)
    q = ct.bias_proposal({**recs[2], "detection_statistic": "status=PROPOSED;abs_dD=x", "auto_value": "1.5"})
    assert q["za"] == 1.5 and q["abs_dD"] is None and q["crosscheck_verdict"] is None


def test_bias_qc_summary_keeps_only_open_proposal_and_lists_all():
    recs = [_rec("A2", "AGREE", "PROPOSED", "2026-11-02T00:00:00Z"),
            _rec("AP", "AGREE", "PROPOSED", "2026-11-05T00:00:00Z", **OK_REVIEW)]
    a = ct.bias_qc([{"qa_reason": "BIAS_PROPOSED_A"}], recs)["axes"]["A"]
    assert a["proposal_record_id"] == "A2" and a["proposed_c"] == -6.0 and a["applied"] is False
    assert [(p["record_id"], p["approval_status"]) for p in a["proposals"]] == [("AP", "approved"), ("A2", "proposed")]
    assert ct.bias_qc([], recs)["axes"]["B"]["proposals"] == []


def test_profile_exposes_v14_lineage_and_gates_on_installation(rc, monkeypatch):
    from backend.repos import analytics as an_mod
    orig = an_mod.MockAnalytics.incl_profile
    sid = rc.get("/api/instruments/CRD-IN-1/incl/surveys").json()["surveys"][-1]["survey_id"]

    def v14(self, iid, s):
        rows = orig(self, iid, s)
        for r in rows:
            r.update(calib_id=None, cal_version=None, formula_id="INCL_PROBE_2PASS", formula_version="1.0.0",
                     qc_rule_version="QA021-v2", installation_version="2026-06-01T00:00:00Z",
                     probe_constants="L_m=0.5;K=25000.0;raw_unit=digits;system=metric;sign_mult_A=1;sign_mult_B=1")
        return rows
    monkeypatch.setattr(an_mod.MockAnalytics, "incl_profile", v14)
    monkeypatch.setattr(an_mod.MockAnalytics, "incl_installations",
                        lambda self, iid: [{"instrument_id": iid, "effective_from": "2026-06-01T00:00:00Z"}])
    p = rc.get(f"/api/instruments/CRD-IN-1/incl/profile?survey_ids={sid}").json()["profiles"][0]
    assert p["qc_rule_version"] == "QA021-v2" and p["installation_version"] == "2026-06-01T00:00:00Z"
    assert p["probe_constants"].startswith("L_m=0.5") and p["formula_verified"] and p["installation_approved"]
    assert set(p["display_status"]) <= {"VERIFIED", "QC_BAD", "MISSING"} and p["unit"] == "mm"
    assert any(r["cum_a"] is not None for r in p["rows"])
    # installation_version that matches no ref.incl_installation row -> every computed value withheld
    monkeypatch.setattr(an_mod.MockAnalytics, "incl_installations",
                        lambda self, iid: [{"instrument_id": iid, "effective_from": "2026-01-01T00:00:00Z"}])
    p = rc.get(f"/api/instruments/CRD-IN-1/incl/profile?survey_ids={sid}").json()["profiles"][0]
    assert p["installation_approved"] is False and set(p["display_status"]) == {"UNAPPROVED_INSTALLATION"}
    assert all(r["cum_a"] is None and r["resultant"] is None for r in p["rows"])


def test_profile_distinct_lineage_is_never_collapsed():
    from backend.routers.instruments import _distinct
    assert _distinct([{"k": "b"}, {"k": "a"}, {"k": "b"}], "k") == ["a", "b"]
    assert _distinct([{"k": None}], "k") is None and _distinct([{"k": 1}, {}], "k") == 1


def test_mock_and_sql_installation_and_proposal_reads():
    from backend.repos.analytics import MockAnalytics, SqlAnalytics
    m = MockAnalytics({"incl_installations": [{"instrument_id": "IN1", "effective_from": "x"}, {"instrument_id": "Z"}],
                       "incl_corrections": [_rec("A", "AGREE", "PROPOSED", "t", applied=True),
                                            {**_rec("B", "AGREE", "PROPOSED", "t"), "detection_method": "manual"}]})
    assert m.incl_installations("IN1") == [{"instrument_id": "IN1", "effective_from": "x"}]
    assert [r["record_id"] for r in m.incl_bias_proposals("IN1", "S")] == ["A"]   # applied rows included now
    seen = []
    s = SqlAnalytics.__new__(SqlAnalytics)
    s.t = lambda layer, name: f"{layer}.{name}"
    s.q = lambda sql, p=None: seen.append((sql, p)) or []
    assert s.incl_installations("IN1") == [] and s.incl_bias_proposals("IN1", "S") == []
    assert "ref.incl_installation" in seen[0][0] and seen[0][1] == {"iid": "IN1"}
    assert "applied = false" not in seen[1][0] and "between_set_bias_qc" in seen[1][0]


def test_endpoint_name_from_bundle_id_reference():
    """(d) resources/app.yml sets ENDPOINT_NAME from `${resources.postgres_endpoints.<k>.id}`, documented to be the
    full resource name projects/<p>/branches/<b>/endpoints/<e>; the app accepts exactly that shape and fails
    loudly on a bare short id."""
    from backend.repos import state as st_mod
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    bundle = open(os.path.join(root, "resources", "app.yml"), encoding="utf-8").read()
    assert "value: ${resources.postgres_endpoints.dam_lakebase_primary.id}" in bundle
    assert st_mod.LakebaseCredentials("projects/p/branches/production/endpoints/primary").endpoint_name.endswith(
        "/endpoints/primary")
    for bad in ("primary", "dam_lakebase_primary", "${resources.postgres_endpoints.dam_lakebase_primary.id}"):
        with pytest.raises(ValueError, match="ENDPOINT_NAME"):
            st_mod.LakebaseCredentials(bad)


@pytest.mark.parametrize("change,status", [
    ({"instrument_id": "SG"}, "NO_ENGINE_FORMULA"),
    ({"lineage_issue": "cal formula differs"}, "LINEAGE_INCONSISTENT"),   # kept in the gate; v1.4 never produces it
    ({"calib_id": None}, "LINEAGE_INCOMPLETE"),
    ({"formula_version": "2"}, "UNVERIFIED_FORMULA"),
    ({"calib_id": "C-unapproved"}, "UNAPPROVED_CALIBRATION"),
    ({}, "VERIFIED"),
])
def test_alarm_gate_branches(change, status):
    g = Gate([{"formula_id": "F", "formula_version": "1", "verified": True}],
             [{"calib_id": "C1", "approved_by": "x"}, {"calib_id": "C-unapproved"}], {"SG"})
    ev = g.apply_event({"instrument_id": "P1", "calib_id": "C1", "formula_id": "F", "formula_version": "1",
                        "qa_flag": "OK", "value": 3.2, "level": 1, "rule": "upper", **change})
    assert ev["display_status"] == status and (ev["value"] is None) == (status != "VERIFIED")
    assert g.status({"instrument_id": "SG", "formula_id": "F", "formula_version": "1"}) == "NO_ENGINE_FORMULA"


def test_correction_reads_mock_and_sql():
    from backend.repos.analytics import MockAnalytics, SqlAnalytics
    m = MockAnalytics({"incl_corrections": [{"record_id": "R1"}, {"record_id": "R2"}]})
    assert [c["record_id"] for c in m.incl_corrections(["R2"])] == ["R2"]
    s = SqlAnalytics.__new__(SqlAnalytics)
    s.t = lambda layer, name: f"{layer}.{name}"
    seen = []
    s.q = lambda sql, p=None: seen.append((sql, p)) or [{"record_id": "R1"}]
    assert s.incl_corrections([]) == [] and not seen
    assert [c["record_id"] for c in s.incl_corrections(["R1"])] == ["R1"] and "gold.incl_correction_status" in seen[0][0]
