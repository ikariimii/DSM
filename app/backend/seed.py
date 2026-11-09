"""Deterministic demo seed: 5 fictional dams with instruments, calibrations,
readings, inclinometer surveys, thresholds, PFMs and alarm events.

All names, owners and coordinates are FICTIONAL demo data. Engineering values
are produced by backend.formulas from synthetic raw readings, so every value
carries the calibration version and formula version that produced it."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from . import formulas as fx
from .repos import contract as ct

START = datetime(2025, 11, 1, tzinfo=timezone.utc)
DAYS = 366

DAMS = [
    dict(dam_id="CRD", name="Cedar Ridge Dam", dam_type="Zoned earthfill embankment", hazard_class="High",
         owner="Cedar Ridge Water Authority (demo)", nid_id="DEMO00001", state="TN", lat=36.10, lon=-83.90,
         height_m=48.0, crest_elev_m=312.0, normal_pool_m=305.0, datum="NAVD88", status="ACTIVE"),
    dict(dam_id="BFK", name="Black Fork Dam", dam_type="Concrete gravity", hazard_class="High",
         owner="Black Fork Power Co. (demo)", nid_id="DEMO00002", state="NC", lat=35.60, lon=-82.70,
         height_m=62.0, crest_elev_m=520.0, normal_pool_m=514.0, datum="NAVD88", status="ACTIVE"),
    dict(dam_id="HLD", name="Halden Lake Dam", dam_type="Homogeneous earthfill", hazard_class="Significant",
         owner="Halden County (demo)", nid_id="DEMO00003", state="KY", lat=37.00, lon=-84.30,
         height_m=21.0, crest_elev_m=268.0, normal_pool_m=264.5, datum="NAVD88", status="ACTIVE"),
    dict(dam_id="PHR", name="Pine Hollow Dam", dam_type="Concrete-face rockfill", hazard_class="High",
         owner="Pine Hollow Irrigation District (demo)", nid_id="DEMO00004", state="VA", lat=36.80, lon=-81.50,
         height_m=75.0, crest_elev_m=640.0, normal_pool_m=633.0, datum="NAVD88", status="ACTIVE"),
    dict(dam_id="STC", name="Stony Creek Dam", dam_type="Roller-compacted concrete", hazard_class="Low",
         owner="Stony Creek Utility (demo)", nid_id="DEMO00005", state="GA", lat=34.70, lon=-84.10,
         height_m=30.0, crest_elev_m=410.0, normal_pool_m=406.0, datum="NAVD88", status="ACTIVE"),
]

EMBANKMENT = {"CRD", "HLD", "PHR"}


def _ts(day: int) -> datetime:
    return START + timedelta(days=day, hours=6)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat().replace("+00:00", "Z") if d else None


def installation_at(rows: list[dict], instrument_id: str, at: str) -> dict | None:
    """ref.incl_installation row valid at `at` (effective_from <= at < effective_to, NULL = open), as the engine's
    batch.installation_row_at: None when no row covers it; ValueError when rows overlap."""
    from .verify import ts_key
    t = ts_key(at)
    if t is None:
        return None
    hit = [r for r in rows if r.get("instrument_id") == instrument_id and r.get("effective_from") is not None
           and ts_key(r["effective_from"]) is not None and ts_key(r["effective_from"]) <= t
           and (r.get("effective_to") in (None, "")
                or (ts_key(r["effective_to"]) is not None and t < ts_key(r["effective_to"])))]   # bad end: no cover
    if len(hit) > 1:
        raise ValueError(f"overlapping ref.incl_installation rows for {instrument_id} at {at}")
    return hit[0] if hit else None


def build() -> dict:
    rnd = random.Random(20261102)
    sites, structures, instruments, calibrations, readings = [], [], [], [], []
    thresholds, pfms, alarm_events, incl_surveys = [], [], [], []
    incl_installations: list[dict] = []   # §5.5 v1.5 ref.incl_installation (SCD2, half-open)

    for d in DAMS:
        dam = dict(d)
        sites.append(dam)
        did = dam["dam_id"]
        main = f"{did}-MAIN"
        structures.append(dict(structure_id=main, dam_id=did, name="Main dam", structure_type=dam["dam_type"],
                               station_from_m=0.0, station_to_m=420.0))
        structures.append(dict(structure_id=f"{did}-SPW", dam_id=did, name="Spillway", structure_type="Ogee spillway",
                               station_from_m=420.0, station_to_m=480.0))
        crest, pool = dam["crest_elev_m"], dam["normal_pool_m"]
        base = crest - dam["height_m"]

        # reservoir level series (seasonal), shared driver for other instruments
        res = [pool - 2.0 + 1.6 * math.sin(2 * math.pi * (i - 60) / 365.25) + rnd.gauss(0, 0.05) for i in range(DAYS)]

        def add_inst(iid, itype, subtype, formula_id, eng_unit, output, structure=main, station=100.0,
                     offset=0.0, tip=None, top=None, status="ACTIVE", approved=True, params=None,
                     recal_day=None, recal_params=None):
            instruments.append(dict(
                instrument_id=iid, dam_id=did, structure_id=structure, instrument_type=itype, subtype=subtype,
                formula_id=formula_id, formula_version="1.0.0", eng_unit=eng_unit, output_name=output,
                station_m=station, offset_m=offset, lat=dam["lat"] + rnd.uniform(-0.002, 0.002),
                lon=dam["lon"] + rnd.uniform(-0.002, 0.002), tip_el_m=tip, top_el_m=top,
                install_date="2019-06-01", status=status, parent_id=None, replaced_by=None))
            v1_to = _iso(_ts(recal_day)) if recal_day else None

            def split(p):  # SCHEMA_CONTRACT v1.1 §10 Q3: sign_mult / offset are ref.calibration columns
                p = dict(p or {})
                return p, int(p.pop("sign_mult", 1)), float(p.pop("offset", 0.0))

            p1, sm1, off1 = split(params)
            calibrations.append(dict(
                calib_id=f"{iid}-C1", instrument_id=iid, sensor_id=iid, cal_version=1,
                effective_from=_iso(START - timedelta(days=900)), effective_to=v1_to, is_current=v1_to is None,
                is_retired=False, formula_id=formula_id, formula_version="1.0.0", params=p1, sign_mult=sm1,
                offset=off1, method=p1.get("method", "linear"),
                certificate_uri=f"certs/{iid}_v1.pdf", approved_by="cal.reviewer@demo.local" if approved else None,
                approved_at=_iso(START - timedelta(days=899)) if approved else None))
            if recal_day:
                p2, sm2, off2 = split(recal_params)
                calibrations.append(dict(
                    calib_id=f"{iid}-C2", instrument_id=iid, sensor_id=iid, cal_version=2, effective_from=v1_to,
                    effective_to=None, is_current=True, is_retired=False,
                    formula_id=formula_id, formula_version="1.0.0", params=p2, sign_mult=sm2, offset=off2,
                    method=p2.get("method", "linear"),
                    certificate_uri=f"certs/{iid}_v2.pdf", approved_by="cal.reviewer@demo.local",
                    approved_at=v1_to))

        def cal_for(iid, day):
            ts = _iso(_ts(day))
            cs = [c for c in calibrations if c["instrument_id"] == iid
                  and c["effective_from"] <= ts and (c["effective_to"] is None or ts < c["effective_to"])]
            return cs[-1]

        def push(iid, day, raw, raw_unit, temp, eng, eng_unit, qa_flag="OK", qa_reason=None, baro=None,
                 corrected=None, corrected_unit=None):
            # Engine semantics (src/dam_safety/formulas/registry.py): eng_value is the
            # primary reduced value; corrected_value is the plotted/thresholded value
            # (e.g. piezometric elevation). When there is no separate correction they match.
            c = cal_for(iid, day)
            readings.append(dict(
                reading_id=f"{iid}-R{day:04d}", instrument_id=iid, sensor_id=iid, ts_utc=_iso(_ts(day)), raw_value=raw, raw_unit=raw_unit,
                temp_c=temp, baro_kpa=baro, calib_id=c["calib_id"], cal_version=c["cal_version"],
                formula_id=c["formula_id"], formula_version=c["formula_version"], reading_type="auto",
                eng_value=eng, eng_unit=eng_unit, corrected_value=eng if corrected is None else corrected,
                corrected_unit=corrected_unit or eng_unit, qa_flag=qa_flag, qa_reason=qa_reason,
                source_file=f"landing/{did}/CR1000X_{_ts(day):%Y%m%d}.dat", ingest_ts=_iso(_ts(day) + timedelta(minutes=12))))

        # --- reservoir level
        add_inst(f"{did}-RL-1", "reservoir_level", "radar", "LEVEL_DATUM", "m", "elevation_m",
                 structure=f"{did}-SPW", station=440.0, params={"datum_elev_m": base})
        for i in range(DAYS):
            reading = res[i] - base
            push(f"{did}-RL-1", i, round(reading, 3), "m", None,
                 fx.REGISTRY[("LEVEL_DATUM", "1.0.0")].fn({"datum_elev_m": base, "reading_m": round(reading, 3)}), "m")

        # --- VW piezometers
        piezos = [("PZ-1", 120.0, -15.0, base + 4.0, False), ("PZ-2", 200.0, 10.0, base + 2.0, True),
                  ("PZ-3", 280.0, 25.0, base + 6.0, False)]
        for n, (pn, sta, off, tip, poly) in enumerate(piezos):
            iid = f"{did}-{pn}"
            G, R0, K, T0 = 0.1795, 9082.0 - 40 * n, 0.1319, 12.0
            # manufacturer G = -0.1795 stored as |G| with sign_mult -1 (CONVENTIONS.md)
            params = {"method": "linear", "G": G, "sign_mult": -1, "R0": R0, "K": K, "T0": T0, "vented": True,
                      "tip_elev_m": tip}
            if poly:
                params.update({"method": "poly", "A": -7.113e-7, "B": -0.1694, "sign_mult": 1})
            approved = not (did == "STC" and pn == "PZ-3")   # one unapproved calibration -> masked
            recal = (150 if (did == "BFK" and pn == "PZ-1") else None)
            recal_params = dict(params, R0=R0 + 25.0) if recal else None
            add_inst(iid, "piezometer", "vibrating_wire", "VW_PIEZO",
                     "m", "piezometric_elev_m", station=sta, offset=off, tip=tip, approved=approved, params=params,
                     recal_day=recal, recal_params=recal_params)
            ratio = 0.55 - 0.1 * n
            for i in range(DAYS):
                trend = 0.0
                if did == "CRD" and pn == "PZ-2" and i > 250:
                    trend = 0.045 * (i - 250)              # rising pore pressure -> alarms
                target_head = max(0.2, (res[i] - tip) * ratio + trend + rnd.gauss(0, 0.03))
                p = target_head * fx.GAMMA_W_KPA_PER_M
                c = cal_for(iid, i)
                cp = ct.formula_inputs(c)  # params + sign_mult/offset columns
                temp = 12.0 + 1.5 * math.sin(2 * math.pi * (i - 100) / 365.25)
                p_noT = p - cp["K"] * (temp - cp["T0"])
                if cp.get("method") == "poly":
                    a, b = cp["A"], cp["B"]
                    cc = fx.vw_poly_C(a, b, cp["R0"]) - p_noT
                    R1 = (-b - math.sqrt(b * b - 4 * a * cc)) / (2 * a)
                    if not (0 < R1 < 20000):
                        R1 = (-b + math.sqrt(b * b - 4 * a * cc)) / (2 * a)
                else:
                    R1 = cp["R0"] + p_noT / (cp["sign_mult"] * cp["G"])
                R1 = round(R1, 1)
                spike = did == "HLD" and pn == "PZ-1" and i == 200
                if spike:
                    R1 = 2500.0
                out = fx.vw_piezo(cp, R1, temp)
                flag, reason = out["qa_flag"], out["qa_reason"]
                if spike:
                    flag, reason = "BAD", "SPIKE"
                push(iid, i, R1, "digits", round(temp, 2), round(out["pressure_kpa"], 3), "kPa", flag, reason,
                     corrected=out["piezometric_elev_m"], corrected_unit="m")

        # --- standpipe
        sp_top, sp_tip = base + 30.0, base + 3.0
        add_inst(f"{did}-SP-1", "piezometer", "open_standpipe", "STANDPIPE_DTW", "m", "piezometric_elev_m",
                 station=160.0, offset=40.0, tip=sp_tip, top=sp_top, params={"top_elev_m": sp_top, "tip_elev_m": sp_tip})
        for i in range(0, DAYS, 7):  # weekly manual dip
            el = sp_tip + max(0.0, (res[i] - sp_tip) * 0.25 + rnd.gauss(0, 0.05))
            dtw = round(sp_top - el, 2)
            o = fx.standpipe_dtw({"top_elev_m": sp_top, "tip_elev_m": sp_tip}, dtw)
            push(f"{did}-SP-1", i, dtw, "m", None, dtw, "m", o["qa_flag"], o["qa_reason"],
                 corrected=o["piezometric_elev_m"], corrected_unit="m")
            readings[-1]["reading_type"] = "manual"

        # --- seepage weir
        add_inst(f"{did}-WR-1", "seepage_weir", "v_notch_90", "WEIR_VNOTCH", "L/s", "flow_lps",
                 station=210.0, offset=120.0, params={})
        for i in range(DAYS):
            h_ft = max(0.05, 0.25 + 0.06 * (res[i] - (pool - 2.0)) + rnd.gauss(0, 0.004)
                       + (0.0009 * (i - 250) if did == "CRD" and i > 250 else 0.0))
            h_ft = round(h_ft, 3)
            push(f"{did}-WR-1", i, h_ft, "ft", None, fx.weir_vnotch_90_cfs(h_ft) * 28.316846592, "L/s")

        # --- embankments: inclinometer + settlement; concrete: crackmeter
        if did in EMBANKMENT:
            iid = f"{did}-IN-1"
            add_inst(iid, "inclinometer", "probe_metric", "INCL_PROBE_2PASS", "mm", "a_cum_mm", station=150.0, offset=-30.0,
                     params={"L_mm": 500.0, "K": 25000.0, "stable_from_m": 24.0, "chk_tol": 12.0,
                             "a_axis_azimuth_deg": 160.0})
            # §5.5 v1.5: probe constants live in ref.incl_installation (SCD2); a re-survey of the casing on day 180
            # opens a second version with the same constants (the survey on day 180 falls in the second, half-open)
            for f, to, az in ((_ts(0), _ts(180), 160.0), (_ts(180), None, 160.0)):
                incl_installations.append(dict(instrument_id=iid, dam_id=did, system="probe", probe_L_m=0.5,
                                               probe_K=25000.0, raw_unit="digits", casing_depth_m=30.0,
                                               depth_reference="top_of_casing", a0_azimuth_deg=az, sign_mult_A=1,
                                               sign_mult_B=1, baseline_survey_id=f"{iid}-S00", spiral_survey_id=None,
                                               stable_zone_top_m=24.0, effective_from=_iso(f), effective_to=_iso(to)))
            # interval TOPS 0, L, ..., casing - L (procedure §4.0; as the DA demo CSVs). QA-043: 0.5..30.0 put the
            # deepest interval below the 30 m casing and left top 0.0 without a reading.
            depths = [round(0.5 * k, 1) for k in range(0, 60)]   # 0.0..29.5 m
            prof_a = {z: rnd.gauss(0, 300) for z in depths}       # casing as-built inclination, reading units
            prof_b = {z: rnd.gauss(0, 300) for z in depths}
            for s in range(13):                                   # monthly surveys, s=0 is baseline
                day = min(DAYS - 1, s * 30)
                bias_a, bias_b = rnd.gauss(8, 0.5), rnd.gauss(-6, 0.5)
                rows = []
                for z in depths:
                    # slip zone between 11 and 13 m: incremental movement grows with time
                    mv = 0.0
                    if 11.0 <= z <= 13.0:
                        mv = (0.35 if did == "CRD" else 0.12) * s   # mm per interval
                    du = mv / (500.0 / (2 * 25000.0)) / 2.0           # mm -> reading units of (A0-A180)/2
                    a_true = prof_a[z] + du + rnd.gauss(0, 1.0)
                    b_true = prof_b[z] + rnd.gauss(0, 1.0)
                    rows.append(dict(depth_m=z, a0=round(a_true + bias_a), a180=round(-a_true + bias_a),
                                     b0=round(b_true + bias_b), b180=round(-b_true + bias_b)))
                incl_surveys.append(dict(instrument_id=iid, survey_id=f"{iid}-S{s:02d}", survey_ts=_iso(_ts(day)),
                                         is_baseline=s == 0, probe_serial="DEMO-P-1001", operator="field.tech@demo.local",
                                         rows=rows))
            add_inst(f"{did}-SM-1", "settlement", "survey_monument", "SETTLEMENT", "mm", "settlement_mm",
                     station=200.0, offset=0.0, params={"EL0": crest})
            for i in range(0, DAYS, 30):
                el = crest - 0.0004 * i - rnd.gauss(0, 0.0005)
                push(f"{did}-SM-1", i, round(el, 4), "m", None, (crest - round(el, 4)) * 1000.0, "mm")
        else:
            iid = f"{did}-CM-1"
            params = {"G": 0.01211, "R0": 4200.0, "K": 0.0, "T0": 15.0}
            add_inst(iid, "crackmeter", "vibrating_wire", "VW_DISP_LIN", "mm", "displacement_mm",
                     station=300.0, offset=0.0, params=params)
            for i in range(DAYS):
                temp = 15 + 8 * math.sin(2 * math.pi * (i - 100) / 365.25)
                disp = 0.4 * math.sin(2 * math.pi * (i - 280) / 365.25) + rnd.gauss(0, 0.01)
                R1 = round(params["R0"] + disp / params["G"], 1)
                push(iid, i, R1, "digits", round(temp, 2), fx.vw_disp(params, R1, temp), "mm")

        # --- thresholds (levels 1-3) and PFMs
        pfm_seep = f"{did}-PFM-01"
        pfm_slide = f"{did}-PFM-02"
        # ref.pfm columns (SCHEMA_CONTRACT v1.1 §5.4). expected_behavior / monitoring_frequency are demo-only DSSMP
        # narrative fields with no ref.pfm column.
        pfm_from = _iso(START - timedelta(days=730))
        pfms.append(dict(pfm_id=pfm_seep, dam_id=did, pfm_code="PFM-01",
                         description=("Internal erosion through the embankment/foundation" if did in EMBANKMENT
                                      else "Foundation uplift and seepage under the dam")
                         + ": a concentrated seepage path develops; rising piezometric levels and seepage flow with "
                           "turbid water are expected precursors.",
                         loading_condition="NORMAL", ch17_disposition="Credible", legacy_ch14_category="I",
                         mechanism="INTERNAL_EROSION" if did in EMBANKMENT else "UPLIFT", status="ACTIVE",
                         is_general_health=False, effective_from=pfm_from, effective_to=None,
                         expected_behavior="Piezometric levels track reservoir; weir flow < level-1 limit and clear.",
                         instruments=[f"{did}-PZ-1", f"{did}-PZ-2", f"{did}-SP-1", f"{did}-WR-1"],
                         monitoring_frequency="Daily automated; weekly manual standpipe and weir turbidity check"))
        pfms.append(dict(pfm_id=pfm_slide, dam_id=did, pfm_code="PFM-02",
                         description=("Downstream slope instability" if did in EMBANKMENT else "Sliding on a lift joint")
                         + ": shear displacement localizes on a weak zone.",
                         loading_condition="NORMAL", ch17_disposition="Credible", legacy_ch14_category="II",
                         mechanism="SLOPE_INSTABILITY" if did in EMBANKMENT else "SLIDING", status="ACTIVE",
                         is_general_health=False, effective_from=pfm_from, effective_to=None,
                         expected_behavior="No progressive displacement; seasonal cyclic movement only.",
                         instruments=[f"{did}-IN-1", f"{did}-PZ-3"] if did in EMBANKMENT else [f"{did}-CM-1", f"{did}-PZ-3"],
                         monitoring_frequency="Monthly inclinometer survey" if did in EMBANKMENT else "Daily automated"))
        for inst in [x for x in instruments if x["dam_id"] == did]:
            iid, itype = inst["instrument_id"], inst["instrument_type"]
            rs = [r["corrected_value"] for r in readings if r["instrument_id"] == iid and r["qa_flag"] != "BAD"]
            if itype == "piezometer" and rs:
                lvl0 = max(rs[:250]) if len(rs) > 250 else max(rs)
                lims = [lvl0 + 0.5, lvl0 + 2.5, lvl0 + 5.0]
                pfm = [pfm_seep] if inst["instrument_id"] != f"{did}-PZ-3" else [pfm_slide]
            elif itype == "seepage_weir" and rs:
                lvl0 = max(rs[:250])
                lims = [lvl0 * 1.15, lvl0 * 1.5, lvl0 * 2.0]
                pfm = [pfm_seep]
            elif itype in ("inclinometer", "crackmeter"):
                lims = [5.0, 10.0, 20.0] if itype == "inclinometer" else [1.0, 2.0, 3.0]
                pfm = [pfm_slide]
            else:
                continue
            responses = ["Verify: re-read, check QC, notify instrumentation lead",
                         "Evaluate: increase surveillance, engineer evaluation within 24 h",
                         "Act: notify owner/dam safety officer, implement EAP per plan"]
            for lvl, lim in enumerate(lims, start=1):
                # canonical ref.threshold columns (§4); contract.threshold derives the display fields
                thresholds.append(dict(threshold_id=f"{iid}-T{lvl}", threshold_version=1, instrument_id=iid,
                                       dam_id=did, output_name=inst["output_name"], level=lvl,
                                       limit_type="magnitude", direction="upper", value=round(lim, 3),
                                       unit=inst["eng_unit"], effective_from=_iso(START - timedelta(days=365)),
                                       effective_to=None, is_current=True, pfm_id=pfm[0],
                                       required_actions=responses[lvl - 1],
                                       # ref.threshold NOT NULL columns (demo values): DSSMP version, tier,
                                       # hysteresis in the rule unit, consecutive readings to raise / clear
                                       dssmp_version_id=f"DSSMP-{did}-2026", tier=f"AL{lvl}",
                                       deadband=round(lim * 0.02, 3), persistence_on=2, persistence_off=3,
                                       # ref.threshold.cdse_ack_required (DA-owned column, default false): the demo
                                       # flags CRD-IN-1's Verify rule so a level-1 alarm needs CRD's CDSE
                                       cdse_ack_required=f"{iid}-T{lvl}" in CDSE_FLAGGED_RULES))
        if did == "BFK":
            # a cyclic limit: the alarm machine does not evaluate cyclic limits (§10 Q7), shown as "not evaluated"
            thresholds.append(dict(threshold_id=f"{did}-CM-1-TC", threshold_version=1, instrument_id=f"{did}-CM-1",
                                   dam_id=did, output_name="displacement_mm", level=1, limit_type="cyclic",
                                   direction="abs", value=0.8, unit="mm", effective_from=_iso(START - timedelta(days=365)),
                                   effective_to=None, is_current=True, pfm_id=pfm_slide,
                                   required_actions="Review seasonal amplitude at the annual DSSMR",
                                   dssmp_version_id=f"DSSMP-{did}-2026", tier="AL1", deadband=0.05,
                                   persistence_on=1, persistence_off=1,
                                   cdse_ack_required=False))

    # --- alarm events from value thresholds (one event per level episode)
    by_inst: dict[str, list[dict]] = {}
    for r in readings:
        by_inst.setdefault(r["instrument_id"], []).append(r)
    n = 0

    def episode(**kw):
        """Internal alarm row in the shape contract.alarm_event produces from ops.alarm_episode (+ joins)."""
        t = ct.threshold(next(x for x in thresholds if x["threshold_id"] == kw["threshold_id"]))
        return dict(kw, threshold_version=t["threshold_version"], limit_type=t["limit_type"], direction=t["direction"],
                    rule=t["rule"], rule_label=t["rule_label"], evaluated=t["evaluated"], limit=t["value"],
                    unit=t["unit"], pfm_id=t["pfm_id"], pfm_ids=t["pfm_ids"], required_actions=t["required_actions"],
                    machine_state="ACTION_UNACK" if t["level"] >= 2 else "ALERT_UNACK", raise_reading_id=kw["reading_id"],
                    # ops.alarm_episode.cdse_ack_required_at_open: the rule flag snapshotted when the episode opened
                    cdse_ack_required_at_open=bool(t.get("cdse_ack_required")),
                    raise_cal_version=kw["cal_version"], mixed_calibration=False, lineage_issue=None,
                    level=t["level"])

    for t in map(ct.threshold, thresholds):
        if t["rule"] != "value_above":
            continue
        active = None
        for r in by_inst.get(t["instrument_id"], []):
            if r["qa_flag"] in ("BAD", "MISSING") or r["corrected_value"] is None:
                continue
            over = r["corrected_value"] > t["upper_limit"]
            if over and active is None:
                n += 1
                active = episode(event_id=f"EV-{n:05d}", dam_id=t["dam_id"], instrument_id=t["instrument_id"],
                                 sensor_id=r["sensor_id"], threshold_id=t["threshold_id"], reading_id=r["reading_id"],
                                 value=r["corrected_value"], metric_value=r["corrected_value"],
                                 calib_id=r["calib_id"], event_ts=r["ts_utc"],
                                 cleared_ts=None, cal_version=r["cal_version"], formula_id=r["formula_id"],
                                 formula_version=r["formula_version"], qa_flag=r["qa_flag"], qa_reason=r["qa_reason"],
                                 message=f"{t['instrument_id']} {t['output_name']} {r['corrected_value']:.2f} {t['unit']} "
                                      f"> L{t['level']} limit {t['upper_limit']:.2f} {t['unit']}")
                alarm_events.append(active)
            elif not over and active is not None:
                active["cleared_ts"] = r["ts_utc"]
                active["machine_state"] = "CLEARED_UNACK"
                active = None
    # inclinometer: compare max cumulative A-axis displacement per survey to thresholds
    for inst in [x for x in instruments if x["instrument_type"] == "inclinometer"]:
        iid = inst["instrument_id"]
        cp = [c for c in calibrations if c["instrument_id"] == iid][0]["params"]
        sv = [s for s in incl_surveys if s["instrument_id"] == iid]
        base_rows = fx.reduce_inclinometer_profile(sv[0]["rows"], {}, cp["L_mm"], cp["K"])
        baseline = {r["depth_m"]: (fx.inc_deviation(cp["L_mm"], q["a0"], q["a180"], cp["K"]),
                                   fx.inc_deviation(cp["L_mm"], q["b0"], q["b180"], cp["K"]))
                    for r, q in zip(base_rows, sorted(sv[0]["rows"], key=lambda q: q["depth_m"]))}
        fired = set()
        for s in sv[1:]:
            prof = fx.reduce_inclinometer_profile(s["rows"], baseline, cp["L_mm"], cp["K"])
            peak = max(abs(r["a_cum_mm"]) for r in prof)
            inst_row = installation_at(incl_installations, iid, s["survey_ts"])
            for t in [ct.threshold(t) for t in thresholds if t["instrument_id"] == iid]:
                if inst_row is not None and peak > t["upper_limit"] and t["level"] not in fired:
                    fired.add(t["level"])
                    n += 1
                    alarm_events.append(episode(
                        event_id=f"EV-{n:05d}", dam_id=inst["dam_id"], instrument_id=iid, sensor_id=iid,
                        # v1.5 demo CSVs: reading_id is the real survey reading id (the survey), calib_id NULL and
                        # installation_version = effective_from of the ref.incl_installation row at survey time
                        threshold_id=t["threshold_id"], reading_id=s["survey_id"], value=peak, metric_value=peak,
                        event_ts=s["survey_ts"], cleared_ts=None, cal_version=None, calib_id=None,
                        installation_version=inst_row["effective_from"],
                        formula_id="INCL_PROBE_2PASS", formula_version="1.0.0", qa_flag="OK", qa_reason=None,
                        message=f"{iid} cumulative A displacement {peak:.1f} mm > L{t['level']} limit {t['upper_limit']:.1f} mm"))
    # a no-data (watchdog) event
    n += 1
    alarm_events.append(dict(event_id=f"EV-{n:05d}", dam_id="PHR", instrument_id="PHR-PZ-3", sensor_id="PHR-PZ-3",
                             threshold_id=None, threshold_version=None, level=1, limit_type=None, direction=None,
                             cdse_ack_required_at_open=False,
                             # app-display-only watchdog (no reading, so no metric_value/reading_id): an episode,
                             # not a state transition, so QA-035 (contract.check_transition) does not apply to it
                             rule="no_data", rule_label="no data > 48 h", evaluated=True, value=None,
                             metric_value=None, limit=None,
                             unit=None, event_ts=_iso(_ts(DAYS - 3)), cleared_ts=None, machine_state="ALERT_UNACK",
                             pfm_id="PHR-PFM-02", pfm_ids=["PHR-PFM-02"], required_actions=None, reading_id=None,
                             raise_reading_id=None, raise_cal_version=None, mixed_calibration=False, lineage_issue=None,
                             calib_id=None, cal_version=None, formula_id=None, formula_version=None, qa_flag=None,
                             qa_reason=None, message="PHR-PZ-3 no data received for > 48 h"))

    formulas = []
    for (fid, ver), f in fx.REGISTRY.items():
        vr = fx.run_vectors(fid, ver)
        formulas.append(dict(formula_id=fid, formula_version=ver, description=f.description, output_name=f.output,
                             unit=f.unit, inputs=f.inputs, required_params=f.required_params,
                             verified=vr["verified"], verified_by="engine-test-vectors" if vr["verified"] else None,
                             n_test_vectors=len(vr["results"])))
    return dict(sites=sites, structures=structures, instruments=instruments, calibrations=calibrations,
                readings=readings, thresholds=thresholds, pfms=pfms, alarm_events=alarm_events,
                incl_surveys=incl_surveys, incl_installations=incl_installations, formulas=formulas,
                dam_cdse=dict(DAM_CDSE), project_membership=[dict(m) for m in CDSE_MEMBERSHIP])


# ref.threshold rules flagged cdse_ack_required in the demo (CDSE acknowledgement at any level)
CDSE_FLAGGED_RULES = frozenset({"CRD-IN-1-T1"})

# ref.dam.cdse_id per demo dam (the named Chief Dam Safety Engineer; FERC Ch.14 App K 14-K-4). Only CRD's CDSE is a
# demo persona; the others are named principals with no app login, so nobody in the demo can CDSE-ack them.
DAM_CDSE = {"CRD": "cdse.crd@demo.local", "BFK": "cdse.bfk@demo.local", "HLD": "cdse.hld@demo.local",
            "PHR": "cdse.phr@demo.local", "STC": "cdse.stc@demo.local"}
# ref.project_membership role 'CDSE' grants (none in the demo beyond cdse_id; the lookup supports them)
CDSE_MEMBERSHIP: list[dict] = []


SEED_USERS = [
    # email, display name, global admin
    ("admin@demo.local", "Demo Admin", True),
    ("engineer.crd@demo.local", "Casey Engineer (CRD, HLD)", False),
    ("viewer.bfk@demo.local", "Vic Viewer (BFK)", False),
    ("dso.phr@demo.local", "Dana DSO (PHR, STC)", False),
    ("cdse.crd@demo.local", "Chris CDSE (CDSE of CRD; engineer on HLD)", False),
    ("nobody@demo.local", "No Access", False),
]
SEED_ROLES = [
    ("engineer.crd@demo.local", "CRD", "engineer"),
    ("engineer.crd@demo.local", "HLD", "engineer"),
    ("viewer.bfk@demo.local", "BFK", "viewer"),
    ("dso.phr@demo.local", "PHR", "dam_admin"),
    ("dso.phr@demo.local", "STC", "dso"),
    ("cdse.crd@demo.local", "CRD", "engineer"),
    ("cdse.crd@demo.local", "HLD", "engineer"),
]
