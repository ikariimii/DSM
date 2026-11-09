"""v1.7 decision: changing ref.threshold.cdse_ack_required creates a NEW threshold version (never an in-place
UPDATE); the publish plan carries the flag; the audit view lists the changes."""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dam_safety import threshold_publish as tp  # noqa: E402
from dam_safety.alarms import cdse_ack_snapshot  # noqa: E402

V11 = open(os.path.join(ROOT, "sql", "ddl", "11_gold_views.sql")).read()


def rule(**kw):
    base = dict(threshold_id="SYN-DAM-01-PZ1-T1", dam_id="SYN-DAM-01", instrument_id="PZ1", sensor_id="PZ1",
                pfm_id=None, dssmp_version_id="DSSMP-1", tier="AL1", level=1, level_name="ALERT",
                limit_type="magnitude", direction="upper", value=12.5, unit="m", rate_window=None, deadband=0.1,
                persistence_on=2, persistence_off=2, condition_expr=None, basis_text=None, required_actions=None,
                eap_level_id=None)
    base.update(kw)
    return base


V1 = dict(rule(cdse_ack_required=False), threshold_version=1, effective_from="2026-01-01T00:00:00Z",
          effective_to=None, is_current=True, set_by="eng@example.org")


def test_first_publication_carries_the_flag_as_a_bool():
    ins, closes = tp.plan_threshold_publication([], [rule(cdse_ack_required=True)], "2026-11-09T15:00:00Z", "a@x")
    assert closes == [] and len(ins) == 1
    assert ins[0]["cdse_ack_required"] is True and ins[0]["threshold_version"] == 1 and ins[0]["is_current"]
    ins, _ = tp.plan_threshold_publication([], [rule()], "2026-11-09T15:00:00Z", "a@x")   # absent -> default false
    assert ins[0]["cdse_ack_required"] is False


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, "yes"])
def test_non_bool_flag_is_refused(bad):
    with pytest.raises(tp.ThresholdVersionError):
        tp.plan_threshold_publication([], [rule(cdse_ack_required=bad)], "2026-11-09T15:00:00Z", "a@x")


def test_flag_change_is_a_new_version_and_the_old_version_is_untouched():
    before = [dict(V1)]
    ins, closes = tp.plan_threshold_publication(before, [rule(cdse_ack_required=True)], "2026-11-09T15:00:00Z",
                                                "admin@example.org")
    assert [(r["threshold_version"], r["cdse_ack_required"], r["effective_from"]) for r in ins] == \
        [(2, True, "2026-11-09T15:00:00Z")]
    assert closes == [{"threshold_id": V1["threshold_id"], "threshold_version": 1,
                       "effective_to": "2026-11-09T15:00:00Z", "is_current": False}]
    assert set(closes[0]) - {"threshold_id", "threshold_version"} == set(tp.BOOKKEEPING)
    # apply the plan the only allowed way: close (bookkeeping only) + insert
    after = [dict(V1, **{k: closes[0][k] for k in tp.BOOKKEEPING})] + ins
    tp.assert_versions_immutable(before, after)                    # no in-place rule change
    assert after[0]["cdse_ack_required"] is False                  # v1 keeps its flag: audit trail
    # an episode opened under v1 keeps v1's snapshot, one opened under v2 gets v2's
    assert cdse_ack_snapshot("RAISE", V1["threshold_id"], 1, after) is False
    assert cdse_ack_snapshot("RAISE", V1["threshold_id"], 2, after) is True


def test_unchanged_rule_is_a_no_op_and_flag_back_to_false_is_another_version():
    assert tp.plan_threshold_publication([V1], [rule(cdse_ack_required=False)], "2026-11-09T15:00:00Z", "a") == ([], [])
    v2 = dict(V1, threshold_version=2, cdse_ack_required=True, effective_from="2026-11-09T15:00:00Z")
    v1c = dict(V1, effective_to="2026-11-09T15:00:00Z", is_current=False)
    ins, closes = tp.plan_threshold_publication([v1c, v2], [rule(cdse_ack_required=False)], "2026-11-10T09:00:00Z", "a")
    assert [(r["threshold_version"], r["cdse_ack_required"]) for r in ins] == [(3, False)]
    assert closes[0]["threshold_version"] == 2


def test_in_place_update_of_the_flag_is_detected():
    after = [dict(V1, cdse_ack_required=True)]
    with pytest.raises(tp.ThresholdVersionError, match="cdse_ack_required changed in place"):
        tp.assert_versions_immutable([V1], after)
    with pytest.raises(tp.ThresholdVersionError, match="deleted"):
        tp.assert_versions_immutable([V1], [])


def test_flag_change_is_never_back_dated_before_publication():
    ins, _ = tp.plan_threshold_publication([V1], [rule(cdse_ack_required=True, effective_from="2026-02-01T00:00:00Z")],
                                           "2026-11-09T15:00:00Z", "a")
    assert ins[0]["effective_from"] == "2026-11-09T15:00:00Z"


def test_app_golive_payload_shape_maps_through():
    """The app's go-live payload row (App Dev c4a384c onboarding.golive_payload) names the column
    cdse_ack_required and sends a bool; extra keys (output_name, effective_to, is_current) are ignored."""
    app_row = dict(rule(cdse_ack_required=True), threshold_version=1, output_name="head_m", effective_to=None,
                   is_current=True, effective_from="2026-11-09T00:00:00Z")
    ins, _ = tp.plan_threshold_publication([], [app_row], "2026-11-09T15:00:00Z", "a")
    assert ins[0]["cdse_ack_required"] is True and "output_name" not in ins[0]


def test_audit_view_lists_flag_changes_by_version():
    body = V11[V11.index("CREATE OR REPLACE VIEW gold.v_threshold_cdse_ack_changes"):]
    assert "lag(cdse_ack_required) OVER (PARTITION BY threshold_id ORDER BY threshold_version) AS prev_flag" in body
    assert re.search(r"WHERE \(prev_flag IS NULL AND new_flag\) OR prev_flag <> new_flag;", body)
