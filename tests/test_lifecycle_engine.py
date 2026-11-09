"""v1.8 dam lifecycle engine (dam_safety.lifecycle): every edge, every refusal code, every suspension reason, the
citation table as DATA, the rules 1-8, the per-dam hash chain and the alarm policy. Synthetic data only."""
import datetime as dt
import itertools

import pytest

from dam_safety import lifecycle as L
from lifecycle_fixtures import (ADMIN, CDSE, CDSE2, NOBODY, PATH_TO, U, citation_for, ctx, history, req,
                                seed_citations, seed_reasons)

SUSP = L.SUSP


def run(r, states=None, **kw):
    c, _ = ctx(states if states is not None else {"D1": PATH_TO[r["from_state"]]}, **kw)
    return L.check_transition(r, **c)


# ---------------------------------------------------------------- the contract (exact names)
def test_states_edges_and_enums_are_the_agreed_contract():
    assert L.STATES == ("draft_commissioning", "live", "suspended_monitoring_modified", "pending_transfer",
                        "transferred", "pending_surrender", "decommissioned_archived", "voided_in_error")
    assert len(L.EDGES) == 12
    assert tuple(seed_reasons()) == L.SUSPENSION_REASONS == (
        "construction_modification", "emergency_modification", "drawdown", "monitoring_change", "other", "re_directed")
    codes = {r["citation_code"]: r for r in seed_citations()}
    assert sorted(codes) == sorted([
        "go_live_dssmp_12_12", "modify_project_works_12_11", "suspend_re_directed_12_4",
        "suspend_ops_drawdown_internal", "resume_dssmp_internal", "transfer_filed_fpa8_9_1",
        "transfer_withdrawn_385_216", "transfer_denied_order", "transfer_final_9_3_12_12c", "surrender_filed_6_1",
        "surrender_withdrawn_385_216", "surrender_denied_order", "surrender_effective_6_2_6_5",
        "void_from_draft_internal", "void_from_live_internal"])
    for banned in ("dssmp_revision_ch14", "re_directed_12_4", "part12d_corrective_plan",
                   "corporate_name_change_internal", "dssmp_version_superseded", "reinstate_from_void_internal"):
        assert banned not in codes
    assert {c for c, r in codes.items() if r["verification_status"] == "VERIFIED"} == {
        "transfer_final_9_3_12_12c", "surrender_effective_6_2_6_5"}
    assert {r["verification_status"] for r in codes.values()} == {"VERIFIED", "UNVERIFIED-pending"}
    assert {c for c, r in codes.items() if r["withdrawal_notice"]} == {"transfer_withdrawn_385_216",
                                                                       "surrender_withdrawn_385_216"}
    for c, r in codes.items():                            # every code's edges are real edges
        for f in r["from_states"]:
            assert (f, r["to_state"]) in L.EDGES, c
    assert codes["modify_project_works_12_11"]["suspension_reasons"] == ["construction_modification",
                                                                         "emergency_modification"]
    assert codes["suspend_re_directed_12_4"]["suspension_reasons"] == ["re_directed"]
    assert codes["suspend_ops_drawdown_internal"]["suspension_reasons"] == ["drawdown", "monitoring_change"]
    assert codes["surrender_filed_6_1"]["from_states"] == ["live", "suspended_monitoring_modified"]
    assert set(L.CODES) >= {"lifecycle_transition_invalid", "lifecycle_stale", "lifecycle_citation_invalid",
                            "lifecycle_incident_report_required", "lifecycle_sworn_verification_required",
                            "lifecycle_cdse_basis_required", "lifecycle_rationale_required",
                            "lifecycle_re_directive_required", "lifecycle_dssmp_required", "lifecycle_unverifiable",
                            "dam_archived"}


def test_no_citation_code_is_hard_coded_in_the_engine():
    src = open(L.__file__).read()
    for r in seed_citations():
        assert r["citation_code"] not in src, r["citation_code"]


# ---------------------------------------------------------------- every edge / every non-edge
@pytest.mark.parametrize("edge", sorted(L.EDGES))
def test_every_edge_is_accepted_with_valid_evidence(edge):
    res = run(req(*edge))
    assert res["codes"] == [], res


@pytest.mark.parametrize("frm,to", sorted(set(itertools.product(L.STATES, L.STATES)) - L.EDGES))
def test_every_other_pair_is_refused(frm, to):
    res = run(req(frm, to, citation_code="go_live_dssmp_12_12"))
    expected = "dam_archived" if frm in L.READ_ONLY else "lifecycle_transition_invalid"
    assert res["codes"][:1] == [expected], res


def test_voided_in_error_is_terminal():
    for to in L.STATES:
        assert run(req(L.VOID, to))["codes"][0] == "lifecycle_transition_invalid"


@pytest.mark.parametrize("state", sorted(L.READ_ONLY))
def test_archived_records_are_read_only(state):
    assert run(req(state, L.LIVE))["codes"] == ["dam_archived"]
    assert L.write_refusal("D1", ctx({"D1": PATH_TO[state]})[0]["current"]).startswith("dam_archived")
    assert L.write_refusal("D1", ctx({"D1": PATH_TO[L.LIVE]})[0]["current"]) is None


def test_stale_from_state_and_backdated_effective_ts():
    assert run(req(L.LIVE, SUSP), {"D1": PATH_TO[L.P_TRANSFER]})["codes"] == ["lifecycle_stale"]
    res = run(req(L.LIVE, SUSP, effective_ts="2026-01-15T00:00:00Z"), {"D1": PATH_TO[L.LIVE]})
    assert res["codes"] == ["lifecycle_stale"]


# ---------------------------------------------------------------- rule 7: unverifiable
def test_unreadable_state_refuses_with_lifecycle_unverifiable_and_alarms_stay_on():
    r = req(L.LIVE, SUSP)
    c, evs = ctx({"D1": PATH_TO[L.LIVE]})
    assert L.check_transition(r, **dict(c, current=None))["codes"] == ["lifecycle_unverifiable"]
    assert L.check_transition(r, **dict(c, citations=None))["codes"] == ["lifecycle_unverifiable"]
    assert L.check_transition(r, **dict(c, reasons=None))["codes"] == ["lifecycle_unverifiable"]
    tampered = [dict(e) for e in evs]
    tampered[0]["citation_code"] = "edited"                       # hash no longer recomputes
    cur = L.current_states(tampered)
    assert cur["D1"] == L.UNREADABLE
    assert L.check_transition(r, **dict(c, current=cur))["codes"] == ["lifecycle_unverifiable"]
    # a ref.dam row without any lifecycle event (not back-filled) is not an implicit draft
    assert L.check_transition(req(L.DRAFT, L.LIVE, dam="D9"), **dict(c, has_ref_dam={"D9"}))["codes"] == [
        "lifecycle_unverifiable"]
    assert L.alarm_mode(L.UNREADABLE) == "production" and L.alarm_mode_at("D1", U("2026-05-01T00:00:00Z"),
                                                                          None) == "production"


# ---------------------------------------------------------------- rule 8: evidence + citation on every transition
def test_evidence_and_citation_are_required():
    assert "lifecycle_evidence_required" in run(req(L.LIVE, L.P_TRANSFER, evidence_uri=[]))["codes"]
    assert "lifecycle_evidence_required" in run(req(L.LIVE, L.P_TRANSFER, evidence_uri="[]"))["codes"]
    assert run(req(L.LIVE, L.P_TRANSFER, evidence_uri='["uri:a", "uri:b"]'))["codes"] == []
    for bad in (None, "", "unknown_code", "surrender_filed_6_1"):          # missing, unknown, wrong edge
        assert run(req(L.LIVE, L.P_TRANSFER, citation_code=bad))["codes"] == ["lifecycle_citation_invalid"], bad


def test_citations_are_data_a_changed_row_changes_the_decision():
    rows = seed_citations()
    for r in rows:
        if r["citation_code"] == "transfer_filed_fpa8_9_1":
            r["from_states"] = ["suspended_monitoring_modified"]
    res = run(req(L.LIVE, L.P_TRANSFER), citations=L.load_citations(rows))
    assert res["codes"] == ["lifecycle_citation_invalid"]
    rows[0]["active"] = False                                                # deactivated row = unknown
    res = run(req(L.DRAFT, L.LIVE), {"D1": []}, citations=L.load_citations(rows))
    assert res["codes"] == ["lifecycle_citation_invalid"]


# ---------------------------------------------------------------- suspension reasons (rule 4)
FULL = {"incident_report_uri": "uri:incident", "sworn_verification_12_13": True, "sworn_verification_uri": "uri:sworn",
        "cdse_basis": "basis", "cdse_basis_by": "u-cdse", "rationale": "why", "re_directive_uri": "uri:re"}


@pytest.mark.parametrize("reason,needs", [
    ("construction_modification", {"sworn_verification_12_13": "lifecycle_sworn_verification_required",
                                   "sworn_verification_uri": "lifecycle_sworn_verification_required"}),
    ("emergency_modification", {"incident_report_uri": "lifecycle_incident_report_required",
                                "sworn_verification_12_13": "lifecycle_sworn_verification_required",
                                "sworn_verification_uri": "lifecycle_sworn_verification_required"}),
    ("drawdown", {"cdse_basis": "lifecycle_cdse_basis_required", "cdse_basis_by": "lifecycle_cdse_basis_required"}),
    ("monitoring_change", {"cdse_basis": "lifecycle_cdse_basis_required",
                           "cdse_basis_by": "lifecycle_cdse_basis_required"}),
    ("re_directed", {"re_directive_uri": "lifecycle_re_directive_required"}),
])
def test_each_suspension_reason_and_its_required_evidence(reason, needs):
    base = req(L.LIVE, SUSP, suspension_reason=reason, citation_code=citation_for(L.LIVE, SUSP, reason))
    keep = {k: v for k, v in FULL.items() if k in needs}
    nulls = {k: None for k in FULL}
    ok = run({**base, **nulls, **keep})
    assert ok["codes"] == [], ok
    for field, code in needs.items():
        res = run({**base, **nulls, **keep, field: None})
        assert res["codes"] == [code], (field, res)
    if "sworn_verification_12_13" in needs:                      # the flag must be TRUE, not just present
        res = run({**base, **nulls, **keep, "sworn_verification_12_13": False})
        assert res["codes"] == ["lifecycle_sworn_verification_required"]
    # a citation for another reason is refused
    other = {"construction_modification": "re_directed", "emergency_modification": "drawdown", "drawdown": "re_directed",
             "monitoring_change": "construction_modification", "re_directed": "drawdown"}[reason]
    res = run({**base, **keep, "citation_code": citation_for(L.LIVE, SUSP, other)})
    assert "lifecycle_citation_invalid" in res["codes"]


def test_other_needs_a_rationale_and_currently_has_no_citation_code():
    """'other': rationale required (409 lifecycle_rationale_required). GAP (reported): no seeded citation_code covers
    'other', so every 'other' suspension is refused lifecycle_citation_invalid until one is added as data."""
    for code in [r["citation_code"] for r in seed_citations() if r["to_state"] == SUSP]:
        res = run(req(L.LIVE, SUSP, suspension_reason="other", citation_code=code, rationale="why"))
        assert res["codes"] == ["lifecycle_citation_invalid"]
        res = run(req(L.LIVE, SUSP, suspension_reason="other", citation_code=code, rationale=" "))
        assert "lifecycle_rationale_required" in res["codes"]
    rows = seed_citations() + [{"citation_code": "suspend_other_test", "from_states": [L.LIVE], "to_state": SUSP,
                                "suspension_reasons": ["other"], "citation": "test", "verification_status":
                                "UNVERIFIED-pending", "withdrawal_notice": False, "active": True}]
    res = run(req(L.LIVE, SUSP, suspension_reason="other", citation_code="suspend_other_test", rationale="why"),
              citations=L.load_citations(rows))
    assert res["codes"] == []


def test_suspension_reason_required_and_only_on_suspensions():
    assert run(req(L.LIVE, SUSP, suspension_reason=None))["codes"][0] == "lifecycle_suspension_reason_required"
    assert run(req(L.LIVE, SUSP, suspension_reason="vacation"))["codes"][0] == "lifecycle_suspension_reason_required"
    assert run(req(L.LIVE, L.P_TRANSFER, suspension_reason="drawdown"))["codes"] == ["lifecycle_citation_invalid"]


def test_sixty_day_warning_only_for_construction_and_never_blocks():
    base = dict(sworn_verification_12_13=True, sworn_verification_uri="uri:s", incident_report_uri="uri:i")
    soon = "2026-11-20T00:00:00Z"                                # 11 days after the approval
    res = run(req(L.LIVE, SUSP, suspension_reason="construction_modification", effective_ts=soon,
                  citation_code="modify_project_works_12_11", **base))
    assert res["codes"] == [] and [w.split(":")[0] for w in res["warnings"]] == ["lifecycle_notice_60_day"]
    res = run(req(L.LIVE, SUSP, suspension_reason="construction_modification", effective_ts="2027-02-01T00:00:00Z",
                  citation_code="modify_project_works_12_11", **base))
    assert res["warnings"] == []
    for reason in ("emergency_modification", "drawdown", "monitoring_change", "re_directed"):
        res = run(req(L.LIVE, SUSP, suspension_reason=reason, effective_ts=soon,
                      citation_code=citation_for(L.LIVE, SUSP, reason), re_directive_uri="uri:r", **base))
        assert res["codes"] == [] and res["warnings"] == [], reason


def test_dssmp_rules_for_suspension_and_resume():
    for v in (None, "DS-X", "DS-D"):                              # none / unknown / draft
        assert "lifecycle_dssmp_required" in run(req(L.LIVE, SUSP, dssmp_version_id=v))["codes"], v
    res = run(req(L.LIVE, SUSP, dssmp_version_id="DS-1"))       # superseded at effective_ts
    assert res["codes"] == ["lifecycle_dssmp_required"]
    states = {"D1": PATH_TO[L.LIVE]}
    c, evs = ctx(states)
    late = [dict(e) for e in history("D1", [L.LIVE], start="2026-10-15T00:00:00Z")]
    res = L.check_transition(req(L.LIVE, SUSP), **dict(c, current=L.current_states(late)))
    assert res["codes"] == ["lifecycle_dssmp_required"] and "NEW version" in res["messages"][0]
    assert run(req(SUSP, L.LIVE, dssmp_version_id=None))["codes"] == ["lifecycle_dssmp_required"]
    assert run(req(SUSP, L.LIVE))["codes"] == []


def test_suspension_needs_the_dams_cdse_regional_engineer_ack_optional():
    assert run(req(L.LIVE, SUSP, approved_by=ADMIN))["codes"] == ["lifecycle_approval_invalid"]
    assert run(req(SUSP, L.LIVE, approved_by=NOBODY))["codes"] == ["lifecycle_approval_invalid"]
    assert run(req(L.LIVE, SUSP, ferc_docket_or_order=None, monitoring_rule=None))["codes"] == []


# ---------------------------------------------------------------- withdrawals
@pytest.mark.parametrize("frm", [L.P_TRANSFER, L.P_SURRENDER])
def test_withdrawal_needs_the_filed_date_and_warns_inside_15_days(frm):
    code = "transfer_withdrawn_385_216" if frm == L.P_TRANSFER else "surrender_withdrawn_385_216"
    assert run(req(frm, L.LIVE, citation_code=code, withdrawal_filed_date=None))["codes"] == [
        "lifecycle_withdrawal_date_required"]
    res = run(req(frm, L.LIVE, citation_code=code, withdrawal_filed_date="2026-11-10"))
    assert res["codes"] == [] and res["warnings"][0].startswith("lifecycle_withdrawal_15_day")
    res = run(req(frm, L.LIVE, citation_code=code, withdrawal_filed_date="2026-11-01"))
    assert res["codes"] == [] and res["warnings"] == []
    denied = "transfer_denied_order" if frm == L.P_TRANSFER else "surrender_denied_order"
    assert run(req(frm, L.LIVE, citation_code=denied, withdrawal_filed_date=None))["codes"] == []


# ---------------------------------------------------------------- go-live and transfer (rules 2, 6)
def test_go_live_needs_readiness_and_cdse_or_admin():
    assert run(req(L.DRAFT, L.LIVE), {"D1": []}, readiness={"D1": False})["codes"] == ["dam_go_live_not_ready"]
    assert run(req(L.DRAFT, L.LIVE, approved_by=ADMIN), {"D1": []})["codes"] == []
    assert run(req(L.DRAFT, L.LIVE, approved_by=NOBODY), {"D1": []})["codes"] == ["lifecycle_approval_invalid"]


def test_transfer_needs_a_live_ready_draft_counterpart_and_writes_both_events_at_one_instant():
    st = {"D1": PATH_TO[L.P_TRANSFER]}
    assert run(req(L.P_TRANSFER, L.TRANSFERRED, counterpart_dam_id=None), st)["codes"] == [
        "lifecycle_transfer_target_invalid"]
    assert run(req(L.P_TRANSFER, L.TRANSFERRED, counterpart_dam_id="D1"), st)["codes"] == [
        "lifecycle_transfer_target_invalid"]
    assert run(req(L.P_TRANSFER, L.TRANSFERRED), st, readiness={"D1": True, "NEW": False})["codes"] == [
        "dam_go_live_not_ready"]
    busy = dict(st, NEW=[L.LIVE])
    assert run(req(L.P_TRANSFER, L.TRANSFERRED), busy)["codes"] == ["lifecycle_transfer_target_invalid"]
    c, _ = ctx(st)
    r = req(L.P_TRANSFER, L.TRANSFERRED)
    assert L.check_transition(r, **c)["codes"] == []
    evs = L.build_events(r, c["current"], "job", U("2026-11-21T00:00:00Z"), "lc:x")
    assert [(e["dam_id"], e["from_state"], e["to_state"]) for e in evs] == [
        ("D1", L.P_TRANSFER, L.TRANSFERRED), ("NEW", L.DRAFT, L.LIVE)]
    assert evs[0]["effective_ts"] == evs[1]["effective_ts"] and evs[1]["counterpart_dam_id"] == "D1"
    assert evs[1]["prev_event_hash"] == L.GENESIS and evs[0]["prev_event_hash"] == c["current"]["D1"]["event_hash"]
    cur = L.current_states(history("D1", PATH_TO[L.P_TRANSFER]) + evs)
    assert cur["D1"]["state"] == L.TRANSFERRED and cur["NEW"]["state"] == L.LIVE
    assert L.alarm_mode(cur["D1"]["state"]) == "off" and L.alarm_mode(cur["NEW"]["state"]) == "production"
    assert L.write_refusal("D1", cur).startswith("dam_archived") and L.write_refusal("NEW", cur) is None


# ---------------------------------------------------------------- void (rule 3)
@pytest.mark.parametrize("frm", [L.DRAFT, L.LIVE])
def test_void_two_distinct_people_cdse_and_platform_admin(frm):
    st = {"D1": PATH_TO[frm]}
    assert run(req(frm, L.VOID, proposed_by=CDSE, approved_by=ADMIN), st)["codes"] == []
    assert run(req(frm, L.VOID, proposed_by=ADMIN, approved_by=CDSE), st)["codes"] == []
    for p, a in ((CDSE, CDSE), (ADMIN, ADMIN), (CDSE, CDSE2), (NOBODY, ADMIN), (CDSE, NOBODY)):
        assert run(req(frm, L.VOID, proposed_by=p, approved_by=a), st)["codes"] == ["lifecycle_approval_invalid"], (p, a)
    # authority at decision time: the CDSE's authority was revoked before the proposal was signed
    revoked = [dict(a, revoked_ts="2026-11-01T00:00:00Z") if a["principal"] == CDSE else a
               for a in ctx()[0]["authority"]]
    assert run(req(frm, L.VOID, proposed_by=CDSE, approved_by=ADMIN), st, authority=revoked)["codes"] == [
        "lifecycle_approval_invalid"]
    res = run(req(frm, L.VOID, proposed_by=CDSE, approved_by=ADMIN, second_approved_by=CDSE), st)
    assert res["codes"] == ["lifecycle_approval_invalid"]


# ---------------------------------------------------------------- rule 1: alarms
def test_alarm_policy_per_state_and_from_the_effective_date():
    assert {s: L.alarm_mode(s) for s in L.STATES} == {
        "draft_commissioning": "test_only", "live": "production", "suspended_monitoring_modified": "production",
        "pending_transfer": "production", "transferred": "off", "pending_surrender": "production",
        "decommissioned_archived": "off", "voided_in_error": "off"}
    hist = [{"dam_id": "D1", "lifecycle_state": L.LIVE, "effective_from": "2026-03-01T00:00:00Z",
             "effective_to": "2026-06-01T00:00:00Z"},
            {"dam_id": "D1", "lifecycle_state": L.DECOM, "effective_from": "2026-06-01T00:00:00Z", "effective_to": None}]
    assert L.alarm_mode_at("D1", U("2026-02-01T00:00:00Z"), hist) == "test_only"     # before go-live
    assert L.alarm_mode_at("D1", U("2026-03-01T00:00:00Z"), hist) == "production"    # from the effective date
    assert L.alarm_mode_at("D1", U("2026-05-31T23:59:59Z"), hist) == "production"
    assert L.alarm_mode_at("D1", U("2026-06-01T00:00:00Z"), hist) == "off"
    assert L.alarm_mode_at("D2", U("2026-06-01T00:00:00Z"), hist) == "production"    # no rows: fail closed
    assert L.alarm_mode_at("D1", None, hist) == "production"


# ---------------------------------------------------------------- hash chain
def test_hash_chain_links_recomputes_and_detects_tampering():
    evs = history("D1", [L.LIVE, SUSP, L.LIVE, L.P_SURRENDER])
    assert L.verify_chain(evs) == {"D1": None}
    assert L.verify_chain(list(reversed(evs))) == {"D1": None}               # order independent
    for field, v in (("effective_ts", U("2026-01-01T00:00:00Z")), ("evidence_uri", ["uri:other"]),
                     ("prev_event_hash", "f" * 64), ("approved_by", "x")):
        bad = [dict(e) for e in evs]
        bad[1][field] = v
        assert L.verify_chain(bad)["D1"] is not None, field
    missing = evs[:1] + evs[2:]                                              # a deleted event breaks the chain
    assert L.verify_chain(missing)["D1"] is not None
    assert L.current_states(missing)["D1"] == L.UNREADABLE
    # length prefixes: moving a character between adjacent fields changes the hash
    a = dict(evs[0], trigger_code="ab", authorizer_role="c")
    b = dict(evs[0], trigger_code="a", authorizer_role="bc")
    assert L.event_hash(a) != L.event_hash(b)
    # NULL and '' differ
    assert L.event_hash(dict(evs[0], rationale=None)) != L.event_hash(dict(evs[0], rationale=""))


def test_hash_sql_lists_every_field_in_order():
    sql = L.hash_sql({f: f"x.{f}" for f in L.HASH_FIELDS})
    pos = [sql.index(f"length(x.{f})") for f in L.HASH_FIELDS]
    assert pos == sorted(pos) and sql.startswith("sha2(concat(") and sql.endswith(", 256)")
