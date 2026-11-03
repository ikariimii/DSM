"""Nightly interpretation run (job dam_interpretation). ADVISORY ONLY - writes:
  gold.hst_fit            one row per (sensor, variant) fit; champion = lowest hold-out RMSE (ties: ANCOLD_FRANCE)
  gold.anomaly_scores     residual z, band, fault-vs-real classification for the scoring window
  ops.rule_finding        PFM-signature findings (pfm_rules) - requires_engineer_review = true
  rpt.engineering_commentary  grounded commentary per dam (commentary.generate) - always requires review
It never writes qa_flag, alarm state, thresholds or exceedances (core path stays deterministic).

Inputs: silver.v_reading_current (qa_flag = 'OK' only for fitting; DATUM_CHANGE/CALIB_ERROR rows used only as
classifier context), reservoir level from silver.reading_clean.reservoir_el_m (per reading), ref.instrument
(redundancy group = same dam + asset_id + instrument_type), ref.pfm / ref.pfm_instrument, ref.dssmp_version.
Sensors without ≥ 1 year of OK data with reservoir level are skipped and logged (no fit)."""
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta

import _common  # noqa: F401
from _common import args, spark as get_spark

from dam_safety.interpretation import anomaly, commentary, hst, pfm_rules
from dam_safety.interpretation.fault_classifier import Evidence, classify
from dam_safety.spark.engine import fq

a = args(("score_days", "7"), ("train_years", "5"), ("holdout_days", "180"), ("pfm_window_days", "90"),
         ("llm_endpoint", ""))
spark = get_spark()
C = a.catalog
now = datetime.utcnow()
score_from = now - timedelta(days=int(a.score_days))
train_from = now - timedelta(days=365.25 * float(a.train_years))
P = anomaly.AnomalyParams()

rows = spark.sql(f"""
  SELECT r.reading_id, r.dam_id, r.sensor_id, r.instrument_id, r.ts_utc, r.corrected_value, r.qa_flag, r.qa_reason,
         c.reservoir_el_m, i.asset_id, i.instrument_type
  FROM {fq(C,'silver','v_reading_current')} r
  LEFT JOIN {fq(C,'silver','reading_clean')} c USING (reading_id)
  JOIN {fq(C,'ref','instrument')} i ON i.instrument_id = r.instrument_id
  WHERE r.ts_utc >= timestamp'{train_from:%Y-%m-%d %H:%M:%S}'""").collect()
by_sensor = defaultdict(list)
for r in rows:
    by_sensor[r.sensor_id].append(r)
group_of = {s: (v[0].dam_id, v[0].asset_id, v[0].instrument_type) for s, v in by_sensor.items()}

fits, scores, skipped = [], [], []
z_at = defaultdict(dict)          # sensor -> {date: z}
for sid, rs in by_sensor.items():
    rs.sort(key=lambda r: r.ts_utc)
    train = [r for r in rs if r.qa_flag == "OK" and r.reservoir_el_m is not None and r.corrected_value is not None
             and r.ts_utc < score_from]
    if len(train) < 30 or (train[-1].ts_utc - train[0].ts_utc).days < 365:
        skipped.append((sid, "insufficient OK data with reservoir level (<1 y)"))
        continue
    cut = train[-1].ts_utc - timedelta(days=int(a.holdout_days))
    cal = [r for r in train if r.ts_utc < cut]
    hold = [r for r in train if r.ts_utc >= cut]
    best = None
    for variant in hst.VARIANTS:
        try:
            m = hst.fit([r.ts_utc for r in cal], [r.reservoir_el_m for r in cal], [r.corrected_value for r in cal], variant)
        except ValueError as e:
            skipped.append((sid, f"{variant}: {e}"))
            continue
        v = hst.validate(m, [r.ts_utc for r in hold], [r.reservoir_el_m for r in hold], [r.corrected_value for r in hold]) if hold else {}
        mid = hashlib.sha256(f"{sid}|{variant}|{now:%Y%m%d}".encode()).hexdigest()[:24]
        fits.append({"model_id": mid, "dam_id": rs[0].dam_id, "sensor_id": sid, "variant": variant,
                     "coefficients": m.coefficients, "t0_days": m.t0_years * 365.25, "ref_date": m.ref_date,
                     "h_ref": {"h_min": m.h_min, "h_max": m.h_max}, "n_obs": m.n_obs,
                     "train_start": cal[0].ts_utc, "train_end": cal[-1].ts_utc, "r2": m.r2, "rmse": m.rmse,
                     "resid_sigma": m.resid_sigma, "resid_mad_sigma": m.resid_mad_sigma,
                     "validation": json.dumps(dict(v, adequacy=m.adequacy, warnings=m.warnings, holdout_from=str(cut))),
                     "fitted_ts": now, "is_champion": False})
        key = v.get("rmse", m.rmse) + (0 if variant == "ANCOLD_FRANCE" else 1e-12)
        if best is None or key < best[0]:
            best = (key, m, mid, len(fits) - 1)
    if best is None:
        continue
    _, m, mid, idx = best
    fits[idx]["is_champion"] = True
    sigma = m.resid_mad_sigma or m.resid_sigma
    window = [r for r in rs if r.ts_utc >= score_from and r.reservoir_el_m is not None and r.corrected_value is not None]
    if not window or not sigma:
        continue
    pred = hst.predict(m, [r.ts_utc for r in window], [r.reservoir_el_m for r in window])
    resid = [r.corrected_value - p for r, p in zip(window, pred)]
    z, bands = anomaly.score(resid, sigma, P)
    runs = anomaly.run_lengths([b != "WITHIN" for b in bands])
    for i, r in enumerate(window):
        z_at[sid][r.ts_utc.date()] = float(z[i])
        scores.append({"dam_id": r.dam_id, "sensor_id": sid, "reading_id": r.reading_id, "ts_utc": r.ts_utc,
                       "observed": r.corrected_value, "predicted": float(pred[i]), "residual": float(resid[i]),
                       "z_score": float(z[i]), "band": bands[i], "model_id": mid,
                       "_run": int(runs[i]), "_next_within": (bands[i + 1] == "WITHIN") if i + 1 < len(bands) else None,
                       "_qa_reason": r.qa_reason})

# classification needs neighbours' z on the same day (redundancy group)
members = defaultdict(list)
for s, g in group_of.items():
    members[g].append(s)
for sc in scores:
    g = group_of[sc["sensor_id"]]
    nz = [z_at[o].get(sc["ts_utc"].date()) for o in members[g] if o != sc["sensor_id"] and sc["ts_utc"].date() in z_at[o]]
    qa = sc.pop("_qa_reason") or ""
    c = classify(Evidence(z=sc["z_score"], qa_reason=qa, neighbour_z=nz, run_length=sc.pop("_run"),
                          returned_next=sc.pop("_next_within"),
                          datum_or_calibration_change=("DATUM_CHANGE" in qa or "CALIB_ERROR" in qa), k_warn=P.k_warn))
    sc.update({"classification": c.label, "class_reasons": c.reasons, "confidence": c.confidence, "scored_ts": now})

if fits:
    spark.createDataFrame(fits, schema=spark.table(fq(C, "gold", "hst_fit")).schema).write.mode("append").saveAsTable(fq(C, "gold", "hst_fit"))
if scores:
    spark.createDataFrame(scores, schema=spark.table(fq(C, "gold", "anomaly_scores")).schema).write.mode("append").saveAsTable(fq(C, "gold", "anomaly_scores"))

# PFM rules over the last pfm_window_days, residuals preferred (corrected values if no model)
win_from = now - timedelta(days=int(a.pfm_window_days))
links = spark.sql(f"""SELECT pi.pfm_id, pi.dam_id, pi.instrument_id, pi.role, pi.expected_signature
  FROM {fq(C,'ref','pfm_instrument')} pi JOIN {fq(C,'ref','pfm')} p ON p.pfm_id = pi.pfm_id
  WHERE pi.valid_to IS NULL AND p.effective_to IS NULL AND pi.expected_signature IS NOT NULL""").collect()
cls_latest = {}
for sc in scores:
    if sc["band"] != "WITHIN":
        cls_latest[sc["sensor_id"]] = sc["classification"]
per_pfm = defaultdict(list)
for ln in links:
    if ln.expected_signature not in pfm_rules.SIGNATURES:
        continue
    for sid, rs in by_sensor.items():
        if rs[0].instrument_id != ln.instrument_id:
            continue
        w = [r for r in rs if r.ts_utc >= win_from and r.qa_flag == "OK" and r.corrected_value is not None]
        if len(w) < 6:
            continue
        per_pfm[(ln.dam_id, ln.pfm_id)].append(pfm_rules.InstrumentSeries(
            sid, ln.role, ln.expected_signature, [(r.ts_utc - w[0].ts_utc).total_seconds() / 86400 for r in w],
            [r.corrected_value for r in w], [r.reservoir_el_m for r in w] if all(r.reservoir_el_m is not None for r in w) else None,
            cls_latest.get(sid), [r.reading_id for r in w]))
findings = [f for (dam, pfm), ser in per_pfm.items()
            if (f := pfm_rules.evaluate_pfm(dam, pfm, ser, win_from, now, now))]
if findings:
    spark.createDataFrame(findings, schema=spark.table(fq(C, "ops", "rule_finding")).schema).write.mode("append").saveAsTable(fq(C, "ops", "rule_finding"))

# Commentary per dam with anything to say. LLM endpoint optional (model serving); fallback is deterministic.
llm = None
if a.llm_endpoint:
    def llm(system, user):  # noqa: E306
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
        r = WorkspaceClient().serving_endpoints.query(a.llm_endpoint, messages=[
            ChatMessage(role=ChatMessageRole.SYSTEM, content=system), ChatMessage(role=ChatMessageRole.USER, content=user)],
            temperature=0.0, max_tokens=400)
        return r.choices[0].message.content
dssmp = {r.dam_id: r.dssmp_version_id for r in spark.sql(
    f"SELECT dam_id, max_by(dssmp_version_id, effective_from) AS dssmp_version_id FROM {fq(C,'ref','dssmp_version')} WHERE status = 'approved' GROUP BY dam_id").collect()}
pfm_desc = defaultdict(list)
for r in spark.table(fq(C, "ref", "pfm")).where("effective_to IS NULL").collect():
    pfm_desc[r.dam_id].append({"pfm_id": r.pfm_id, "title": r.pfm_code, "description": r.description, "ch17_disposition": r.ch17_disposition})
out = []
for dam in sorted({f["dam_id"] for f in findings} | {s["dam_id"] for s in scores if s["band"] != "WITHIN"}):
    fs = [f for f in findings if f["dam_id"] == dam]
    an = [s for s in scores if s["dam_id"] == dam and s["band"] != "WITHIN"][-20:]
    facts = commentary.build_facts(dam, fs, an, [p for p in pfm_desc[dam] if p["pfm_id"] in {f["pfm_id"] for f in fs}],
                                   dssmp.get(dam), [f for f in fits if f["dam_id"] == dam and f["is_champion"]])
    out.append(commentary.generate(facts, "dam", dam, score_from, now, now, llm=llm, llm_name=a.llm_endpoint or None))
if out:
    spark.createDataFrame(out, schema=spark.table(fq(C, "rpt", "engineering_commentary")).schema).write.mode("append").saveAsTable(fq(C, "rpt", "engineering_commentary"))
print(json.dumps({"fits": len(fits), "scores": len(scores), "findings": len(findings), "commentary": len(out),
                  "skipped": skipped[:50]}, default=str))
