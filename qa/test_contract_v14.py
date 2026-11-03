"""QA-003 v1.4 sign-off checks (QA, 2026-11-02 ~20:40 EST). All data SYNTHETIC; no restricted values.

Covers SCHEMA_CONTRACT v1.4 / api_contract v1.4 claims that QA must hold independently of the authors' tests:
  * an applied bias record without reviewer approval raises (engine) and a stored one is skipped + flagged (batch);
  * no QA test builds an applied, unreviewed bias record as a valid fixture;
  * open v1.4 defects as strict xfails (flip to XPASS -> fail when fixed, then drop the marker):
      QA-035 reading-driven CLEAR / RTN_UNACK / DEESCALATE transitions have metric_value NULL -> violate
             ck_alarm_transition_value and trip the evaluator's pre-write guard;
      QA-036 contract.approval_status returns 'unreviewed_applied', which api_contract §4.1 does not list;
      QA-037 dam-portfolio-all gets CAN_USE on the app: app-level in every target, plus prod target CAN_VIEW -> CAN_USE.
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app" / "backend"))

from dam_safety import alarms                                   # noqa: E402
from dam_safety.inclinometer import batch, corrections as C     # noqa: E402

TS = "2026-11-02T00:00:00"


def _bias(**kw):
    base = dict(record_id="QA-B1", dam_id="D1", instrument_id="I1", survey_id="S5", axis="A", correction_type="bias",
                parameter_name="c", auto_value=6.0, detection_method="between_set_bias_qc", detection_statistic=None,
                override_value=None, applied=True, sign_convention=None, depth_range=None, reason="qa",
                source_citation="procedure §5", user="eng", timestamp_utc=TS, superseded_by=None,
                reviewed_by=None, reviewed_utc=None)
    base.update(kw)
    return base


@pytest.mark.parametrize("review", [
    dict(),                                              # no reviewer, no time
    dict(reviewed_by="eng", reviewed_utc=TS),            # self-review
    dict(reviewed_by="rev"),                             # reviewer but no reviewed_utc
    dict(reviewed_utc=TS),                               # time but no reviewer
    dict(reviewed_by="", reviewed_utc=TS),               # empty reviewer
])
def test_applied_bias_without_reviewer_approval_raises(review):
    with pytest.raises(ValueError, match="never auto-applied"):
        C.CorrectionRecord(**_bias(**review))


def test_reviewed_applied_bias_and_unapplied_proposal_are_accepted():
    assert C.CorrectionRecord(**_bias(reviewed_by="rev", reviewed_utc=TS)).reviewed
    assert not C.CorrectionRecord(**_bias(applied=False)).applied          # proposal: no review needed
    rec = C.CorrectionRecord(**_bias(applied=False))
    with pytest.raises(Exception):                                         # frozen: cannot be flipped to applied
        rec.applied = True


def _inst():
    return dict(instrument_id="I1", dam_id="D1", probe_L_m=0.5, probe_K=25000.0, raw_unit="sin*25000",
                casing_depth_m=10.0, a0_azimuth_deg=0.0, baseline_survey_id="S0", system="probe",
                spiral_survey_id=None, stable_zone_top_m=7.0, depth_reference="top_of_casing",
                effective_from="2026-01-01T00:00:00")


def _survey(sid, ts, a0_shift=0.0):
    rows = []
    for i in range(20):
        d = round(i * 0.5, 9)
        a, b = 100.0 + i, -(100.0 + i) + 12.0
        rows.append(dict(survey_id=sid, survey_ts=ts, depth_m=d, a0=a + (a0_shift if d >= 7.0 else 0.0), a180=b,
                         b0=50.0, b180=-38.0))
    return rows


def test_stored_unreviewed_applied_bias_row_is_skipped_flagged_and_not_applied():
    surveys = _survey("S0", "2026-01-01T00:00:00") + _survey("S5", "2026-02-01T00:00:00", a0_shift=6.0)
    clean_rows, clean_props = batch.reduce_instrument(_inst(), surveys, [], "B1", TS)
    rows, props = batch.reduce_instrument(_inst(), surveys, [_bias()], "B1", TS)
    s5 = [r for r in rows if r["survey_id"] == "S5"]
    c5 = [r for r in clean_rows if r["survey_id"] == "S5"]
    assert s5 and all("CORRECTION_RECORD_INVALID" in (r["qa_reason"] or "") for r in s5)
    assert all("QA-B1" not in str(r["correction_record_ids"] or "") for r in s5)
    assert [r["cum_a"] for r in s5] == [r["cum_a"] for r in c5]           # value NOT applied
    assert [(p.survey_id, p.axis, p.status) for p in props] == [(p.survey_id, p.axis, p.status) for p in clean_props]
    assert all(r["formula_version"] == "1.0.0" and r["qc_rule_version"] == "QA021-v2" and r["calib_id"] is None
               and r["installation_version"] == "2026-01-01T00:00:00" and r["probe_constants"].startswith("L_m=0.5;")
               for r in rows)


def test_no_qa_test_builds_applied_unreviewed_bias_record():
    """Every applied=True literal in qa/ test files lives in this file's _bias() helper (negative fixtures)."""
    hits = []
    for f in sorted((ROOT / "qa").glob("*.py")):
        if f.name == Path(__file__).name:
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"""applied["']?\s*[:=]\s*True""", line):
                hits.append(f"{f.name}:{n}")
    assert hits == []


# ---- QA-024 (c): the best-ranked (AGREE) proposal under a configured tolerance still needs independent review -----
@pytest.mark.parametrize("review", [dict(), dict(reviewed_by="eng", reviewed_utc=TS),   # none / self-review
                                    dict(reviewed_by="rev2", reviewed_utc=None)])        # no review time
def test_qa024_agree_ranked_bias_cannot_be_applied_without_independent_review(review):
    import dataclasses
    from dam_safety.inclinometer import qc
    sys.path.insert(0, str(ROOT / "tests"))
    import test_inclinometer_bias_qc as F
    q = F.run(F.passes(d0=-6.5, d180=-1.5), params=qc.BiasQCParams(crosscheck_tol_abs=3.0))
    assert q.crosscheck_verdict == "AGREE" and q.status == "PROPOSED"
    rec = qc.bias_proposal_record(q, "R1", "D1", "I1", "eng", TS)
    assert rec.applied is False
    with pytest.raises(ValueError, match="needs reviewer approval"):
        dataclasses.replace(rec, applied=True, **review)
    ok = dataclasses.replace(rec, applied=True, reviewed_by="rev2", reviewed_utc=TS)
    assert ok.applied and ok.reviewed_by != ok.user


# ---- QA-035 ---------------------------------------------------------------------------------------------------
def _alarm_series(two_levels, ack):
    specs = [dict(threshold_id="T1", threshold_version=1, level=1, limit_type="magnitude", direction="upper", value=10,
                  deadband=0.0, persistence_on=1, persistence_off=1, level_name="ALERT")]
    vals = [12.0, 5.0]
    if two_levels:
        specs.append(dict(specs[0], threshold_id="T2", level=2, value=20, level_name="ALARM"))
        vals = [12.0, 25.0, 15.0, 5.0]
    t0 = datetime(2026, 1, 1)
    rows = [dict(reading_id=f"r{i}", ts_utc=t0 + timedelta(hours=i), value=v, unit="m", qa_flag="OK")
            for i, v in enumerate(vals)]
    acks = [dict(ts=t0 + timedelta(minutes=30), actor="u", comment="c")] if ack else []
    return alarms.evaluate_sensor_batch("S1", rows, specs, None, acks)[1]


@pytest.mark.xfail(strict=True, reason="QA-035: CLEAR/RTN_UNACK/DEESCALATE carry metric_value=None -> "
                                       "ck_alarm_transition_value violation + evaluator RuntimeError")
@pytest.mark.parametrize("two_levels,ack", [(False, False), (False, True), (True, False), (True, True)])
def test_reading_driven_transitions_satisfy_ck_alarm_transition_value(two_levels, ack):
    trs = _alarm_series(two_levels, ack)
    events = {t.event for t in trs}
    assert events & {"CLEAR", "RTN_UNACK", "DEESCALATE"}                  # the path is exercised
    bad = [(t.event, t.value, t.metric_value) for t in trs
           if t.event != "ACK" and (t.value is None or t.metric_value is None)]
    assert bad == []


# ---- QA-036 ---------------------------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="QA-036: api_contract §4.1 lists proposed/approved/superseded only")
def test_api_contract_lists_every_approval_status_the_app_returns():
    from repos import contract as ct
    produced = {ct.approval_status(r) for r in (
        dict(applied=False), dict(applied=True, reviewed_by="r", user="u", reviewed_utc=TS),
        dict(applied=True), dict(superseded_by="X"))}
    doc = (ROOT / "docs" / "api_contract.md").read_text()
    row = next(l for l in doc.splitlines() if "`approval_status`" in l)
    assert produced <= set(re.findall(r"`([a-z_]+)`", row))


# ---- QA-037 ---------------------------------------------------------------------------------------------------
def _effective_app_permissions(target):
    """Mirror databricks/cli bundle/config/mutator/resourcemutator/apply_bundle_permissions.go (main, read
    2026-11-02): target/top-level permissions are appended to every app unless the principal already has an
    app-level entry; for apps CAN_MANAGE -> CAN_MANAGE, CAN_VIEW -> CAN_USE (CAN_RUN not applicable)."""
    dbx = yaml.safe_load((ROOT / "databricks.yml").read_text())
    app_yml = yaml.safe_load((ROOT / "resources" / "app.yml").read_text())
    app = next(iter(app_yml["resources"]["apps"].values()))
    perms = list(app.get("permissions") or [])
    top = (dbx.get("targets", {}).get(target, {}) or {}).get("permissions") or dbx.get("permissions") or []
    have = {p.get("group_name") or p.get("user_name") or p.get("service_principal_name") for p in perms}
    lm = {"CAN_MANAGE": "CAN_MANAGE", "CAN_VIEW": "CAN_USE"}
    for p in top:
        who = p.get("group_name") or p.get("user_name") or p.get("service_principal_name")
        if p["level"] in lm and who not in have:
            perms.append(dict(p, level=lm[p["level"]]))
    return perms


def _per_dam_only(target):
    use = [p.get("group_name") for p in _effective_app_permissions(target) if p["level"] == "CAN_USE"]
    return bool(use) and all(re.fullmatch(r"dam-.+-members", g or "") for g in use), use


# QA-037 at f33a5a1: resources/app.yml grants dam-portfolio-all CAN_USE on the app in EVERY target, and the prod
# target-level CAN_VIEW maps to CAN_USE on top. Strict xfails: drop each marker only where it really XPASSes.
@pytest.mark.parametrize("target", [
    pytest.param(t, marks=pytest.mark.xfail(strict=True, reason="QA-037: dam-portfolio-all CAN_USE on the app (%s)" % t))
    for t in ("dev", "test", "prod")])
def test_effective_app_can_use_is_per_dam_only(target):
    ok, use = _per_dam_only(target)
    assert ok, use
