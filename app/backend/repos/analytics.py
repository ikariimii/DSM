"""Analytics reads: Unity Catalog tables through a Databricks SQL warehouse, or the
seeded in-memory dataset in demo mode. Both implement the same interface and
return plain dicts in the app's internal row model; `contract` maps SCHEMA_CONTRACT v1.1 columns to it.
Gold/ref/ops/silver are read ONLY through the SQL warehouse as the signed-in user (OBO), never through the
app service principal and never from a Lakebase synced table (SCHEMA_CONTRACT v1.1 §6, §7.3)."""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from .. import formulas as fx
from ..config import Settings
from . import contract as ct

log = logging.getLogger(__name__)


class Analytics(Protocol):
    def sites(self) -> list[dict]: ...
    def structures(self, dam_id: str) -> list[dict]: ...
    def instruments(self, dam_id: str) -> list[dict]: ...
    def instrument(self, instrument_id: str) -> dict | None: ...
    def calibrations(self, instrument_id: str) -> list[dict]: ...
    def calibrations_for_dam(self, dam_id: str) -> list[dict]: ...
    def alarm_event(self, event_id: str) -> dict | None: ...
    def formulas(self) -> list[dict]: ...
    def thresholds(self, dam_id: str, instrument_id: str | None = None) -> list[dict]: ...
    def latest_readings(self, dam_id: str) -> list[dict]: ...
    def readings(self, instrument_id: str, start: str | None, end: str | None, limit: int = 20000) -> list[dict]: ...
    def incl_surveys(self, instrument_id: str) -> list[dict]: ...
    def incl_profile(self, instrument_id: str, survey_id: str) -> list[dict]: ...
    def incl_corrections(self, record_ids: list[str]) -> list[dict]: ...
    def incl_bias_proposals(self, instrument_id: str, survey_id: str) -> list[dict]: ...
    def incl_installations(self, instrument_id: str) -> list[dict]: ...
    def alarm_events(self, dam_ids: list[str], since: str | None = None) -> list[dict]: ...
    def pfms(self, dam_id: str) -> list[dict]: ...


# ============================================================== demo
class MockAnalytics:
    def __init__(self, data: dict):
        self.d = data
        self._incl_cache: dict[tuple[str, str], list[dict]] = {}

    def sites(self):
        return copy.deepcopy(self.d["sites"])

    def structures(self, dam_id):
        return [dict(s) for s in self.d["structures"] if s["dam_id"] == dam_id]

    def instruments(self, dam_id):
        return [ct.instrument(i) for i in self.d["instruments"] if i["dam_id"] == dam_id]

    def instrument(self, instrument_id):
        for i in self.d["instruments"]:
            if i["instrument_id"] == instrument_id:
                return ct.instrument(i)
        return None

    # Demo rows go through the same contract adapters as warehouse rows, so both paths share one shape.
    def calibrations(self, instrument_id):
        return [ct.calibration(copy.deepcopy(c)) for c in self.d["calibrations"] if c["instrument_id"] == instrument_id]

    def calibrations_for_dam(self, dam_id):
        ids = {i["instrument_id"] for i in self.d["instruments"] if i["dam_id"] == dam_id}
        return [ct.calibration(copy.deepcopy(c)) for c in self.d["calibrations"] if c["instrument_id"] in ids]

    def alarm_event(self, event_id):
        return next((dict(e) for e in self.d["alarm_events"] if e["event_id"] == event_id), None)

    def formulas(self):
        return copy.deepcopy(self.d["formulas"])

    def thresholds(self, dam_id, instrument_id=None):
        return [ct.threshold(t) for t in self.d["thresholds"] if t["dam_id"] == dam_id
                and (instrument_id is None or t["instrument_id"] == instrument_id)]

    def latest_readings(self, dam_id):
        ids = {i["instrument_id"] for i in self.d["instruments"] if i["dam_id"] == dam_id}
        latest: dict[str, dict] = {}
        for r in self.d["readings"]:
            if r["instrument_id"] in ids:
                cur = latest.get(r["instrument_id"])
                if cur is None or r["ts_utc"] > cur["ts_utc"]:
                    latest[r["instrument_id"]] = r
        return [dict(r) for r in latest.values()]

    def readings(self, instrument_id, start, end, limit=20000):
        out = [dict(r) for r in self.d["readings"] if r["instrument_id"] == instrument_id
               and (start is None or r["ts_utc"] >= start) and (end is None or r["ts_utc"] <= end)]
        return out[-limit:]

    def incl_surveys(self, instrument_id):
        return [{k: v for k, v in s.items() if k != "rows"} for s in self.d["incl_surveys"]
                if s["instrument_id"] == instrument_id]

    def incl_profile(self, instrument_id, survey_id):
        """Demo mode reduces on the fly with the app copy of the engine INCL_PROBE_2PASS formula (in
        Databricks this is silver.incl_reduced written by the pipeline)."""
        key = (instrument_id, survey_id)
        if key in self._incl_cache:
            return copy.deepcopy(self._incl_cache[key])
        sv = [s for s in self.d["incl_surveys"] if s["instrument_id"] == instrument_id]
        target = next((s for s in sv if s["survey_id"] == survey_id), None)
        if target is None:
            return []
        base = next(s for s in sv if s["is_baseline"])
        inst = self.instrument(instrument_id)
        cals = [c for c in self.d["calibrations"] if c["instrument_id"] == instrument_id
                and c["effective_from"] <= target["survey_ts"]]
        if not cals or inst is None:
            return []
        cal = ct.calibration(max(cals, key=lambda c: c["effective_from"]))  # calibration in force at survey time
        cp = cal["params"]
        baseline = {q["depth_m"]: (fx.inc_deviation(cp["L_mm"], q["a0"], q["a180"], cp["K"]),
                                   fx.inc_deviation(cp["L_mm"], q["b0"], q["b180"], cp["K"])) for q in base["rows"]}
        # stable_from_m is deliberately NOT passed: the stable-zone-mean bias proposal does not meet procedure §5,
        # so it is suppressed until the QA-020 spec exists and the engine provides it (owner decision 2026-11-02).
        prof = fx.reduce_inclinometer_profile(target["rows"], baseline, cp["L_mm"], cp["K"],
                                              None, cp.get("chk_tol"))
        for r in prof:
            r.pop("proposed_bias", None)
            r.update(instrument_id=instrument_id, survey_id=survey_id, survey_ts=target["survey_ts"],
                     calib_id=cal["calib_id"], cal_version=cal["cal_version"], formula_id=inst["formula_id"],
                     formula_version=str(inst["formula_version"]), eng_unit="mm",
                     qa_flag="OK" if r.get("qc_checksum_ok", True) else "SUSPECT",
                     qa_reason=None if r.get("qc_checksum_ok", True) else "CHECKSUM",
                     correction_record_ids=[], correction_basis="uncorrected")
        self._incl_cache[key] = prof
        return copy.deepcopy(prof)

    def incl_corrections(self, record_ids):
        return [ct.incl_correction(c) for c in self.d.get("incl_corrections", []) if c["record_id"] in set(record_ids)]

    def incl_bias_proposals(self, instrument_id, survey_id):
        # every engine bias QC record for the survey (proposed, approved, superseded); contract sorts it out
        return [ct.incl_correction(c) for c in self.d.get("incl_corrections", [])
                if c.get("instrument_id") == instrument_id and c.get("survey_id") == survey_id
                and c.get("detection_method") == "between_set_bias_qc"]

    def incl_installations(self, instrument_id):
        return [dict(i) for i in self.d.get("incl_installations", []) if i.get("instrument_id") == instrument_id]

    def alarm_events(self, dam_ids, since=None):
        return [dict(e) for e in self.d["alarm_events"] if e["dam_id"] in dam_ids
                and (since is None or e["event_ts"] >= since)]

    def pfms(self, dam_id):
        return [ct.pfm(copy.deepcopy(p)) for p in self.d["pfms"] if p["dam_id"] == dam_id]

    # demo-only: go-live adds the onboarded project to the in-memory dataset
    def apply_publication(self, payload: dict) -> None:
        for k in ("sites", "structures", "instruments", "calibrations", "thresholds", "pfms"):
            self.d[k].extend(copy.deepcopy(payload.get(k, [])))


# ============================================================== Databricks SQL
class SqlAnalytics:
    """Reads UC tables through a SQL warehouse. Every statement is parameterized
    (named :params, databricks-sql-connector >= 3). Every query runs as the end user with the token from
    x-forwarded-access-token (on-behalf-of-user, scope `sql`), so UC row filters (dam-portfolio-all OR
    dam-<id>-members, §6) apply in addition to the app's own ACL. There is no service-principal fallback:
    api_contract.md says the app never reads UC through its service principal."""

    def __init__(self, settings: Settings, user_token: str | None = None):
        self.s = settings
        self.user_token = user_token

    # ---------------------------------------------------------- plumbing
    def _connect(self):
        from databricks import sql
        from fastapi import HTTPException

        if not self.user_token:
            raise HTTPException(401, "No user access token (x-forwarded-access-token); gold is read only as the user")
        from databricks.sdk.core import Config

        host = Config().host.replace("https://", "").rstrip("/")  # DATABRICKS_HOST, injected by Databricks Apps
        return sql.connect(server_hostname=host, http_path=f"/sql/1.0/warehouses/{self.s.warehouse_id}",
                           access_token=self.user_token)

    def q(self, statement: str, params: dict | None = None) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(statement, params or {})
            cols = [c[0] for c in cur.description] if cur.description else []
            return [_norm(dict(zip(cols, row))) for row in cur.fetchall()]

    def t(self, layer: str, name: str) -> str:
        return self.s.uc(layer, name)

    # ---------------------------------------------------------- reads
    # Column names follow SCHEMA_CONTRACT v1.1; `contract` maps them to the app model.
    def _inst_sql(self, where: str) -> str:
        return (f"SELECT i.*, c.formula_id, c.formula_version, f.output_unit FROM {self.t('ref', 'instrument')} i "
                f"LEFT JOIN {self.t('ref', 'calibration')} c ON c.instrument_id = i.instrument_id "
                "AND c.is_current AND NOT c.is_retired "
                f"LEFT JOIN {self.t('ref', 'formula')} f ON f.formula_id = c.formula_id "
                f"AND f.formula_version = c.formula_version WHERE {where} ORDER BY i.instrument_id")

    def sites(self):
        return self.q(f"SELECT * FROM {self.t('ref', 'site')} ORDER BY dam_id")

    def structures(self, dam_id):
        return self.q(f"SELECT * FROM {self.t('ref', 'structure')} WHERE dam_id = :dam_id ORDER BY structure_id",
                      {"dam_id": dam_id})

    def instruments(self, dam_id):
        return [ct.instrument(r) for r in self.q(self._inst_sql("i.dam_id = :dam_id"), {"dam_id": dam_id})]

    def instrument(self, instrument_id):
        rows = self.q(self._inst_sql("i.instrument_id = :iid"), {"iid": instrument_id})
        return ct.instrument(rows[0]) if rows else None

    def calibrations(self, instrument_id):
        return [ct.calibration(r) for r in self.q(
            f"SELECT * FROM {self.t('ref', 'calibration')} WHERE instrument_id = :iid ORDER BY cal_version",
            {"iid": instrument_id})]

    def calibrations_for_dam(self, dam_id):
        return [ct.calibration(r) for r in self.q(
            f"SELECT * FROM {self.t('ref', 'calibration')} WHERE dam_id = :dam_id", {"dam_id": dam_id})]

    def _alarm_sql(self, where: str) -> str:
        # §4/§5 v1.4: ops.alarm_episode carries formula_id, formula_version, qa_flag and the lineage itself, so the
        # app no longer joins ref.calibration (formula_id) or takes the qc flag from ops.reading. ops.reading is
        # joined only for the engineer's erroneous mark (app/ops state, not an episode column).
        return (f"SELECT e.*, t.level, t.limit_type, t.direction, t.value AS value_limit, t.unit, t.pfm_id, "
                "t.required_actions, r.is_erroneous AS reading_is_erroneous "
                f"FROM {self.t('ops', 'alarm_episode')} e LEFT JOIN {self.t('ref', 'threshold')} t "
                "ON t.threshold_id = e.threshold_id AND t.threshold_version = e.threshold_version "
                f"LEFT JOIN {self.t('ops', 'reading')} r ON r.reading_id = e.reading_id WHERE {where}")

    def alarm_event(self, event_id):
        rows = self.q(self._alarm_sql("e.event_id = :eid"), {"eid": event_id})
        return ct.alarm_event(rows[0]) if rows else None

    def formulas(self):
        return [ct.formula(r) for r in self.q(
            f"SELECT * FROM {self.t('ref', 'formula')} ORDER BY formula_id, formula_version")]

    def thresholds(self, dam_id, instrument_id=None):
        sql = f"SELECT t.* FROM {self.t('ref', 'threshold')} t WHERE t.dam_id = :dam_id AND t.is_current"
        p = {"dam_id": dam_id}
        if instrument_id:
            sql += " AND t.instrument_id = :iid"
            p["iid"] = instrument_id
        return [ct.threshold(r) for r in self.q(sql + " ORDER BY instrument_id, level", p)]

    def latest_readings(self, dam_id):
        # gold.instrument_latest through the warehouse as the user (no synced table, §7.3).
        return self.q(f"SELECT * FROM {self.t('gold', 'instrument_latest')} WHERE dam_id = :dam_id", {"dam_id": dam_id})

    def readings(self, instrument_id, start, end, limit=20000):
        # FERC canvas contract view ops.reading (current reduction + engineer erroneous marks)
        return [ct.reading(r) for r in self.q(
            f"SELECT * FROM {self.t('ops', 'reading')} WHERE instrument_id = :iid "
            "AND (:start IS NULL OR reading_ts >= CAST(:start AS TIMESTAMP)) "
            "AND (:end IS NULL OR reading_ts <= CAST(:end AS TIMESTAMP)) ORDER BY reading_ts LIMIT :lim",
            {"iid": instrument_id, "start": start, "end": end, "lim": int(limit)})]

    def incl_surveys(self, instrument_id):
        return self.q(f"SELECT instrument_id, survey_id, survey_ts, is_baseline, probe_serial, operator "
                      f"FROM {self.t('silver', 'incl_survey_header')} WHERE instrument_id = :iid ORDER BY survey_ts",
                      {"iid": instrument_id})

    def incl_profile(self, instrument_id, survey_id):
        return [ct.incl_row(r) for r in self.q(
            f"SELECT r.*, c.cal_version FROM {self.t('silver', 'incl_reduced')} r "
            f"LEFT JOIN {self.t('ref', 'calibration')} c ON c.calib_id = r.calib_id "
            "WHERE r.instrument_id = :iid AND r.survey_id = :sid ORDER BY r.depth_m",
            {"iid": instrument_id, "sid": survey_id})]

    def incl_corrections(self, record_ids):
        if not record_ids:
            return []
        return [ct.incl_correction(r) for r in self.q(
            f"SELECT * FROM {self.t('ref', 'incl_correction')} "
            "WHERE array_contains(from_json(:ids, 'ARRAY<STRING>'), record_id) ORDER BY timestamp_utc, record_id",
            {"ids": json.dumps(sorted(set(record_ids)))})]

    def incl_bias_proposals(self, instrument_id, survey_id):
        # QA-021 engine bias QC records (§5.5 v1.4) of every approval_status (proposed / approved / superseded);
        # display-only, the app never applies one. contract.bias_qc derives approval_status and the summary.
        return [ct.incl_correction(r) for r in self.q(
            f"SELECT * FROM {self.t('ref', 'incl_correction')} WHERE instrument_id = :iid AND survey_id = :sid "
            "AND detection_method = 'between_set_bias_qc' "
            "ORDER BY timestamp_utc, record_id", {"iid": instrument_id, "sid": survey_id})]

    def incl_installations(self, instrument_id):
        # §5.5 v1.4: SCD2 versions; installation_version on silver.incl_reduced must equal one effective_from
        return self.q(f"SELECT instrument_id, effective_from, effective_to FROM {self.t('ref', 'incl_installation')} "
                      "WHERE instrument_id = :iid ORDER BY effective_from", {"iid": instrument_id})

    def alarm_events(self, dam_ids, since=None):
        if not dam_ids:
            return []
        # array_contains keeps the statement parameterized for a variable list of dams
        return [ct.alarm_event(r) for r in self.q(
            self._alarm_sql("array_contains(from_json(:dams, 'ARRAY<STRING>'), e.dam_id) "
                            "AND (:since IS NULL OR e.event_ts >= CAST(:since AS TIMESTAMP))")
            + " ORDER BY e.event_ts DESC LIMIT 5000",
            {"dams": json.dumps(dam_ids), "since": since})]

    def pfms(self, dam_id):
        return [ct.pfm(r) for r in self.q(
            f"SELECT p.*, collect_set(pi.instrument_id) AS instruments FROM {self.t('ref', 'pfm')} p "
            f"LEFT JOIN {self.t('ref', 'pfm_instrument')} pi ON pi.pfm_id = p.pfm_id AND pi.valid_to IS NULL "
            "WHERE p.dam_id = :dam_id AND p.effective_to IS NULL GROUP BY ALL ORDER BY p.pfm_id",
            {"dam_id": dam_id})]


def _norm(row: dict) -> dict:
    """Convert warehouse types (datetime, Decimal, MAP as list of tuples) to JSON-friendly values."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            if v.tzinfo is None:  # warehouse TIMESTAMP_NTZ / session-UTC values are UTC by contract
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif v.__class__.__name__ == "Decimal":
            out[k] = float(v)
        elif k == "params" and isinstance(v, list):
            out[k] = {a: b for a, b in v}
        else:
            out[k] = v
    return out
