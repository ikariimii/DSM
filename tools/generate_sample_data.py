"""Seeded SYNTHETIC sample data for 5 fictitious dams (SYN-DAM-01..05).

Nothing here is derived from any dataset under /workspace/dam-refs (no restricted or vendor data;
see docs/PROVENANCE.md). Values are generated from simple closed-form signals + seeded noise so
the pipeline, QC, alarm and inclinometer paths have something realistic-looking to chew on.

Output layout (mirrors the landing volume /Volumes/<cat>/raw/landing/<dam>/..., bronze.py):
  sample_data/ref/{portfolio,dam,structure,asset,instrument,sensor}.csv   (ref hierarchy subset)
  sample_data/landing/<dam_id>/readings/<dam_id>_readings.csv   columns == bronze READING_SCHEMA
  sample_data/landing/<dam_id>/inclinometer/<instrument_id>.csv columns == bronze INCL_SCHEMA

Deliberate defects (so QC/quarantine paths are exercised):
  * one unparsable timestamp per dam             -> bronze reading_quarantine (parsable_ts)
  * one row with blank sensor_id per dam         -> bronze reading_quarantine (has_sensor)
  * a few NaN raw values / gaps                  -> silver QC MISSING
  * one spike per dam                            -> silver QC SPIKE/RANGE (if calibrated limits set)
  * SYN-DAM-03 piezometer PZ-02 rising trend     -> threshold exceedance once thresholds are loaded
  * SYN-DAM-02 inclinometer IN-01 shear zone at ~8 m developing over 5 surveys

Usage:  python tools/generate_sample_data.py [--out sample_data] [--seed 20261102]
Deterministic: same seed -> byte-identical files (tested in tests/test_sample_data.py).
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import random
from datetime import datetime, timedelta, timezone

READING_COLS = ["dam_id", "logger_id", "sensor_id", "ts_utc", "raw_value", "raw_unit", "temp_raw", "temp_c",
                "baro_kpa", "reading_type", "source", "reservoir_el_m", "datum_id"]
INCL_COLS = ["instrument_id", "survey_ts", "depth_m", "a0", "a180", "b0", "b180", "probe_serial", "operator"]

N_DAMS = 5
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAYS = 60                # 6-hourly ADAS readings for 60 days
STEP_H = 6
PROBE_L_M = 0.5          # synthetic metric probe (0.5 m wheelbase, K = 25000; installation.METRIC_DIGITILT)
PROBE_K = 25000.0
CASING_M = 20.0


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        return f"{v:.4f}"
    return str(v)


def _write(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in cols])


def dams():
    out = []
    for i in range(1, N_DAMS + 1):
        out.append({
            "dam_id": f"SYN-DAM-{i:02d}", "portfolio_id": "SYN-PORT", "name": f"Synthetic Dam {i}",
            "hazard_potential": ["High", "Significant", "High", "Low", "Significant"][i - 1],
            "dam_type": ["EMBANKMENT", "EMBANKMENT", "CONCRETE_GRAVITY", "EMBANKMENT", "ARCH"][i - 1],
            "timezone": "America/New_York", "datum": "SYN-LOCAL",
            "normal_pool_el_m": 100.0 + 10 * i, "crest_el_m": 106.0 + 10 * i,
        })
    return out


def build(seed: int = 20261102):
    rng = random.Random(seed)
    ref = {k: [] for k in ("portfolio", "dam", "structure", "asset", "instrument", "sensor")}
    ref["portfolio"].append({"portfolio_id": "SYN-PORT", "name": "Synthetic portfolio (sample data)", "owner_org": "SAMPLE"})
    readings, incl = {}, {}
    for d in dams():
        dam = d["dam_id"]
        ref["dam"].append(d)
        st = f"{dam}-ST1"
        ref["structure"].append({"structure_id": st, "dam_id": dam, "name": "Main section", "structure_type": d["dam_type"]})
        assets = {"SEC": f"{dam}-A-SEC1", "WEIR": f"{dam}-A-WEIR1", "BH": f"{dam}-A-BH1"}
        for t, a in assets.items():
            ref["asset"].append({"asset_id": a, "dam_id": dam, "structure_id": st, "name": t,
                                 "asset_type": {"SEC": "SECTION", "WEIR": "WEIR", "BH": "BOREHOLE"}[t]})
        inst = [  # (instrument_id, type, subtype, asset, sensor channel, unit)
            (f"{dam}-HW", "reservoir_level", "radar", assets["SEC"], "LEVEL", "m"),
            (f"{dam}-PZ-01", "piezometer", "vw", assets["SEC"], "VW", "digits"),
            (f"{dam}-PZ-02", "piezometer", "vw", assets["SEC"], "VW", "digits"),
            (f"{dam}-SP-01", "piezometer", "standpipe", assets["SEC"], "DTW", "m"),
            (f"{dam}-WR-01", "seepage_weir", "v_notch_90", assets["WEIR"], "HEAD", "mm"),
        ]
        if dam in ("SYN-DAM-01", "SYN-DAM-02"):
            inst.append((f"{dam}-IN-01", "inclinometer", "probe_metric", assets["BH"], "A", "digits"))
        for iid, typ, sub, a, ch, unit in inst:
            ref["instrument"].append({"instrument_id": iid, "dam_id": dam, "asset_id": a, "structure_id": st,
                                      "instrument_type": typ, "subtype": sub, "top_el_m": d["crest_el_m"],
                                      "tip_el_m": d["crest_el_m"] - (CASING_M if typ == "inclinometer" else 15.0)})
            chans = ["A", "B"] if typ == "inclinometer" else [ch]
            for c in chans:
                ref["sensor"].append({"sensor_id": f"{iid}-{c}", "instrument_id": iid, "dam_id": dam, "channel": c,
                                      "reading_type": "manual" if typ == "inclinometer" else "auto",
                                      "native_unit": unit, "logger_id": None if typ == "inclinometer" else f"{dam}-LG1",
                                      "active": "true"})

        rows = []
        n = DAYS * 24 // STEP_H
        pool0 = d["normal_pool_el_m"]
        for k in range(n):
            ts = START + timedelta(hours=STEP_H * k)
            day = k * STEP_H / 24.0
            pool = pool0 - 1.5 + 1.2 * math.sin(2 * math.pi * day / 45.0) + rng.gauss(0, 0.02)
            temp = 12 + 3 * math.sin(2 * math.pi * day / 30.0) + rng.gauss(0, 0.1)
            baro = 101.3 + rng.gauss(0, 0.3)
            base = {"dam_id": dam, "logger_id": f"{dam}-LG1", "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "reading_type": "auto", "source": "SYNTHETIC", "reservoir_el_m": round(pool, 4), "datum_id": None}
            rows.append({**base, "sensor_id": f"{dam}-HW-LEVEL", "raw_value": round(pool, 4), "raw_unit": "m"})
            for j, frac in ((1, 0.55), (2, 0.35)):
                head = frac * (pool - (pool0 - 20))
                if dam == "SYN-DAM-03" and j == 2:
                    head += 0.05 * day            # rising piezometer (exceedance scenario)
                kpa = head * 9.80665
                digits = 9000.0 - kpa / 0.18 + rng.gauss(0, 1.5)   # linear VW, G ~ 0.18 kPa/digit (synthetic)
                rows.append({**base, "sensor_id": f"{dam}-PZ-{j:02d}-VW", "raw_value": round(digits, 1),
                             "raw_unit": "digits", "temp_c": round(temp, 2), "baro_kpa": round(baro, 2)})
            dtw = 6.0 - 0.4 * (pool - pool0) + rng.gauss(0, 0.01)
            rows.append({**base, "sensor_id": f"{dam}-SP-01-DTW", "raw_value": round(dtw, 3), "raw_unit": "m"})
            h_mm = max(0.0, 40 + 8 * (pool - pool0 + 1.5) + rng.gauss(0, 0.5))
            rows.append({**base, "sensor_id": f"{dam}-WR-01-HEAD", "raw_value": round(h_mm, 1), "raw_unit": "mm"})
        # deliberate defects
        rows[17]["raw_value"] = float("nan")
        rows[40]["raw_value"] = rows[40]["raw_value"] * 3 if rows[40]["raw_value"] else 999.0
        bad_ts = dict(rows[55]); bad_ts["ts_utc"] = "2026-13-45 99:99"; rows.append(bad_ts)
        no_sensor = dict(rows[60]); no_sensor["sensor_id"] = None; rows.append(no_sensor)
        readings[dam] = rows

        if dam in ("SYN-DAM-01", "SYN-DAM-02"):
            iid = f"{dam}-IN-01"
            depths = [round(PROBE_L_M * i, 3) for i in range(int(CASING_M / PROBE_L_M))]   # interval tops from top
            # fixed "as installed" casing profile (digits), seeded per instrument
            base_a = [rng.gauss(0, 300) for _ in depths]
            base_b = [rng.gauss(0, 300) for _ in depths]
            bias_a, bias_b = rng.gauss(0, 8), rng.gauss(0, 8)
            irows = []
            for s in range(6):                  # baseline + 5 monthly surveys
                ts = (START + timedelta(days=30 * s)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for i, z in enumerate(depths):
                    move = 0.0
                    if dam == "SYN-DAM-02" and 7.5 <= z <= 8.5:
                        # 1.5 mm per survey over the shear interval -> digits: dev_mm = D*L*1000/(2K)
                        move = 1.5 * s * 2 * PROBE_K / (PROBE_L_M * 1000) / 3
                    a_true = base_a[i] + move
                    b_true = base_b[i]
                    a0 = a_true / 2 + bias_a / 2 + rng.gauss(0, 2)
                    a180 = -a_true / 2 + bias_a / 2 + rng.gauss(0, 2)
                    b0 = b_true / 2 + bias_b / 2 + rng.gauss(0, 3)
                    b180 = -b_true / 2 + bias_b / 2 + rng.gauss(0, 3)
                    irows.append({"instrument_id": iid, "survey_ts": ts, "depth_m": z, "a0": round(a0),
                                  "a180": round(a180), "b0": round(b0), "b180": round(b180),
                                  "probe_serial": "SYN-PROBE-1", "operator": "sample"})
            incl[iid] = (dam, irows)
    return ref, readings, incl


def write_all(out: str, seed: int = 20261102):
    ref, readings, incl = build(seed)
    for name, rows in ref.items():
        cols = list(dict.fromkeys(k for r in rows for k in r))
        _write(os.path.join(out, "ref", f"{name}.csv"), cols, rows)
    for dam, rows in readings.items():
        _write(os.path.join(out, "landing", dam, "readings", f"{dam}_readings.csv"), READING_COLS, rows)
    for iid, (dam, rows) in incl.items():
        _write(os.path.join(out, "landing", dam, "inclinometer", f"{iid}.csv"), INCL_COLS, rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "sample_data"))
    ap.add_argument("--seed", type=int, default=20261102)
    a = ap.parse_args()
    write_all(os.path.normpath(a.out), a.seed)
    print("wrote", os.path.normpath(a.out))
