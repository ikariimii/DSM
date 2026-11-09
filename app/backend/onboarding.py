"""New-project onboarding: step validation, instrument CSV import, and the go-live payload.

Pure functions (no I/O) so every rule is unit-testable. A draft is a dict keyed by
step name. Severity: 'error' blocks go-live; 'warning' is shown and recorded."""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from . import formulas as fx
from .repos import contract as ct
from .repos.state import ROLE_RANK

STEPS = ("details", "structures", "instruments", "calibrations", "formulas", "thresholds",
         "data_sources", "users", "validate", "golive")
EDITABLE = STEPS[:8]
HAZARD = ("High", "Significant", "Low")
ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
DAM_RE = re.compile(r"^[A-Z0-9]{2,8}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# allowed display units per instrument type: the §5.4 ref.instrument_type vocabulary (SCHEMA_CONTRACT v1.1).
# Kept as a literal: the root test tests/test_instrument_vocab.py parses it with ast.literal_eval, and
# app/tests/test_onboarding.py::test_type_units_match_contract_vocab keeps it equal to ct.INSTRUMENT_TYPES.
TYPE_UNITS = {
    "piezometer": {"m", "kPa"}, "reservoir_level": {"m"}, "seepage_weir": {"L/s", "ft3/s"},
    "inclinometer": {"mm"}, "crackmeter": {"mm"}, "extensometer": {"mm"}, "settlement": {"mm"},
    "strain_gauge": {"microstrain"}, "pressure_cell": {"kPa"}, "load_cell": {"kN"},
    "tiltmeter": {"arcsec", "mm/m"}, "plumbline": {"mm"}, "accelerometer": {"cm/s2", "g"}, "thermometer": {"degC"},
}
# ref.instrument spelling (§5) is tip_el_m / top_el_m; the older app spelling is accepted on import and renamed
CSV_COLUMNS = ("instrument_id", "structure_id", "instrument_type", "subtype", "formula_id", "formula_version",
               "eng_unit", "output_name", "station_m", "offset_m", "tip_el_m", "top_el_m", "install_date")
CSV_ALIASES = {"tip_elev_m": "tip_el_m", "top_elev_m": "top_el_m"}
CSV_REQUIRED = ("instrument_id", "structure_id", "instrument_type", "formula_id", "formula_version", "eng_unit")
CSV_FLOATS = ("station_m", "offset_m", "tip_el_m", "top_el_m")
LIMIT_TYPES = ("magnitude", "rate", "cyclic")      # §4 ref.threshold.limit_type
DIRECTIONS = ("upper", "lower", "abs")              # §4 ref.threshold.direction
METHOD_FORMULAS = ("VW_PIEZO",)  # linear (G) or polynomial (A, B) per calibration 'method'
SOURCE_TYPES = ("logger_file", "manual", "api", "survey_file")


def issue(step: str, severity: str, msg: str, ref: str | None = None) -> dict:
    return {"step": step, "severity": severity, "message": msg, "ref": ref}


def _num(v):
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    return float(v)


def parse_instrument_csv(text: str, max_rows: int = 5000) -> dict:
    """Parse an instrument registry CSV. Returns rows (typed) and issues; never guesses."""
    issues: list[dict] = []
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    header = [CSV_ALIASES.get(h.strip(), h.strip()) for h in (reader.fieldnames or [])]
    missing = [c for c in CSV_REQUIRED if c not in header]
    if missing:
        return {"rows": [], "issues": [issue("instruments", "error", f"missing columns: {', '.join(missing)}")]}
    unknown = [h for h in header if h not in CSV_COLUMNS]
    if unknown:
        issues.append(issue("instruments", "warning", f"ignored columns: {', '.join(unknown)}"))
    seen = set()
    for n, raw in enumerate(reader, start=2):
        if n - 1 > max_rows:
            issues.append(issue("instruments", "error", f"more than {max_rows} rows"))
            break
        r = {CSV_ALIASES.get(k.strip(), k.strip()): (v or "").strip() for k, v in raw.items()
             if k and CSV_ALIASES.get(k.strip(), k.strip()) in CSV_COLUMNS}
        ref = f"line {n}"
        bad = False
        for c in CSV_REQUIRED:
            if not r.get(c):
                issues.append(issue("instruments", "error", f"{c} is required", ref))
                bad = True
        for c in CSV_FLOATS:
            try:
                r[c] = _num(r.get(c))
            except ValueError:
                issues.append(issue("instruments", "error", f"{c} must be a number, got {r.get(c)!r}", ref))
                bad = True
        if r.get("install_date"):
            try:
                datetime.strptime(r["install_date"], "%Y-%m-%d")
            except ValueError:
                issues.append(issue("instruments", "error", "install_date must be YYYY-MM-DD", ref))
                bad = True
        else:
            r["install_date"] = None
        iid = r.get("instrument_id", "")
        if iid and not ID_RE.match(iid):
            issues.append(issue("instruments", "error", f"invalid instrument_id {iid!r}", ref))
            bad = True
        if iid in seen:
            issues.append(issue("instruments", "error", f"duplicate instrument_id {iid}", ref))
            bad = True
        seen.add(iid)
        if not bad:
            r.setdefault("subtype", None)
            r.setdefault("output_name", None)
            rows.append(r)
    return {"rows": rows, "issues": issues}


def validate(draft: dict, existing_dam_ids: set[str], formula_catalog: list[dict] | None = None) -> list[dict]:
    """Full cross-step validation. formula_catalog: ref.formula rows (engine verification)."""
    out: list[dict] = []
    d = draft.get("details") or {}
    dam_id = d.get("dam_id") or ""
    if not DAM_RE.match(dam_id):
        out.append(issue("details", "error", "dam_id must be 2-8 upper-case letters/digits"))
    elif dam_id in existing_dam_ids:
        out.append(issue("details", "error", f"dam_id {dam_id} already exists"))
    for k in ("name", "dam_type", "owner", "datum"):
        if not d.get(k):
            out.append(issue("details", "error", f"{k} is required"))
    if d.get("hazard_class") not in HAZARD:
        out.append(issue("details", "error", f"hazard_class must be one of {HAZARD}"))
    for k, lo, hi in (("lat", -90, 90), ("lon", -180, 180)):
        try:
            v = float(d.get(k))
            if not lo <= v <= hi:
                raise ValueError
        except (TypeError, ValueError):
            out.append(issue("details", "error", f"{k} must be a number in [{lo}, {hi}]"))
    for k in ("crest_elev_m", "normal_pool_m", "height_m"):
        if d.get(k) in (None, ""):
            out.append(issue("details", "warning", f"{k} not set"))

    structs = draft.get("structures") or []
    sids = [s.get("structure_id") for s in structs]
    if not structs:
        out.append(issue("structures", "error", "at least one structure is required"))
    if len(set(sids)) != len(sids):
        out.append(issue("structures", "error", "duplicate structure_id"))
    for s in structs:
        if not s.get("structure_id") or not ID_RE.match(s["structure_id"]):
            out.append(issue("structures", "error", "invalid structure_id", s.get("structure_id")))
        if not s.get("name"):
            out.append(issue("structures", "error", "name is required", s.get("structure_id")))

    insts = draft.get("instruments") or []
    iids = {i["instrument_id"] for i in insts}
    if not insts:
        out.append(issue("instruments", "error", "at least one instrument is required"))
    catalog = {(f["formula_id"], str(f["formula_version"])): f for f in (formula_catalog or [])}
    for i in insts:
        ref = i["instrument_id"]
        if i.get("structure_id") not in sids:
            out.append(issue("instruments", "error", f"unknown structure_id {i.get('structure_id')}", ref))
        allowed = TYPE_UNITS.get(i.get("instrument_type"))
        if allowed is None:
            out.append(issue("instruments", "error", f"unsupported instrument_type {i.get('instrument_type')} "
                             "(not in ref.instrument_type, SCHEMA_CONTRACT §5.4)", ref))
        elif not ct.type_is_computable(i["instrument_type"]):
            out.append(issue("instruments", "error", f"{i['instrument_type']} has no engine formula "
                             "(ref.instrument_type.engine_formulas is empty), so the app cannot compute it", ref))
        elif i.get("eng_unit") not in allowed:
            out.append(issue("instruments", "error",
                             f"unit {i.get('eng_unit')!r} not valid for {i['instrument_type']} ({sorted(allowed)})", ref))
        key = (i.get("formula_id"), str(i.get("formula_version")))
        if fx.get_formula(*key) is None and key not in catalog:
            out.append(issue("instruments", "error", f"unknown formula {key[0]} v{key[1]}", ref))

    cals = draft.get("calibrations") or []
    for i in insts:
        mine = [c for c in cals if c.get("instrument_id") == i["instrument_id"]]
        ref = i["instrument_id"]
        if not mine:
            out.append(issue("calibrations", "error", "no calibration", ref))
            continue
        f = fx.get_formula(i.get("formula_id"), str(i.get("formula_version")))
        for c in mine:
            params = ct.normalize_params(c.get("params"))
            for k in ct.CAL_COLUMNS_NOT_PARAMS:   # §10 Q3: calibration columns, never params
                if k in params:
                    out.append(issue("calibrations", "error",
                                     f"{k} belongs in the calibration's {k} field, not in params (SCHEMA_CONTRACT §10 Q3)",
                                     ref))
            method = c.get("method") or params.get("method")
            if method is not None and method not in ct.CAL_METHODS:
                out.append(issue("calibrations", "error", f"method must be one of {'|'.join(ct.CAL_METHODS)}", ref))
            try:
                fx.check_sign_mult(c.get("sign_mult", 1) if c.get("sign_mult") is not None else 1)
            except ValueError as e:
                out.append(issue("calibrations", "error", str(e), ref))
            if c.get("offset") not in (None, "") and not _isnum(c.get("offset")):
                out.append(issue("calibrations", "error", "offset must be numeric", ref))
            req = list(f.required_params if f else [])
            if f is not None and f.formula_id in METHOD_FORMULAS:
                req += ["A", "B"] if method == "poly" else ["G"]
                g = params.get("G")
                if method != "poly" and _isnum(g) and float(g) < 0:
                    out.append(issue("calibrations", "warning",
                                     "G is negative: store |G| with sign_mult -1 (CONVENTIONS.md)", ref))
            for p in req:
                if p not in params:
                    out.append(issue("calibrations", "error", f"missing parameter {p}", ref))
                else:
                    try:
                        float(params[p])
                    except (TypeError, ValueError):
                        out.append(issue("calibrations", "error", f"parameter {p} is not numeric", ref))
            if not c.get("effective_from"):
                out.append(issue("calibrations", "error", "effective_from is required", ref))
            if not c.get("approved_by"):
                out.append(issue("calibrations", "warning",
                                 "calibration not approved: computed values will be withheld until approved", ref))
            if not c.get("certificate_uri"):
                out.append(issue("calibrations", "warning", "no calibration certificate reference", ref))
        if len({c.get("cal_version") for c in mine}) != len(mine):
            out.append(issue("calibrations", "error", "duplicate cal_version", ref))

    used = {(i.get("formula_id"), str(i.get("formula_version"))) for i in insts}
    fcheck = (draft.get("formulas") or {}).get("checks") or {}
    for fid, ver in sorted(used, key=str):
        eng = catalog.get((fid, ver))
        app = fx.run_vectors(fid, ver) if fx.get_formula(fid, ver) else None
        verified = bool(eng.get("verified")) if eng else bool(app and app["verified"])
        if not verified:
            out.append(issue("formulas", "warning",
                             f"{fid} v{ver} is not engine-verified: its values will be hidden as UNVERIFIED", fid))
        if app is not None and not app["verified"] and f"{fid}:{ver}" in fcheck and fcheck[f"{fid}:{ver}"].get("verified"):
            out.append(issue("formulas", "error", f"{fid} v{ver}: stored check disagrees with re-run", fid))

    ths = [_canonical_threshold(t) for t in draft.get("thresholds") or []]
    by_inst: dict[str, list[dict]] = {}
    for t in ths:
        ref = t.get("instrument_id")
        if ref not in iids:
            out.append(issue("thresholds", "error", f"threshold for unknown instrument {ref}", ref))
            continue
        inst = next(i for i in insts if i["instrument_id"] == ref)
        if t.get("unit") != inst.get("eng_unit"):
            out.append(issue("thresholds", "error",
                             f"threshold unit {t.get('unit')!r} differs from instrument unit {inst.get('eng_unit')!r}", ref))
        if t.get("level") not in (1, 2, 3):
            out.append(issue("thresholds", "error", "level must be 1, 2 or 3", ref))
        if t.get("limit_type") not in LIMIT_TYPES:
            out.append(issue("thresholds", "error", f"limit_type must be one of {'|'.join(LIMIT_TYPES)}", ref))
        if t.get("direction") not in DIRECTIONS:
            out.append(issue("thresholds", "error", f"direction must be one of {'|'.join(DIRECTIONS)}", ref))
        if not _isnum(t.get("value")):
            out.append(issue("thresholds", "error", "limit value must be numeric", ref))
            continue
        if t.get("limit_type") == "cyclic":
            out.append(issue("thresholds", "warning",
                             f"L{t.get('level')} cyclic limit is not evaluated by the alarm machine (§10 Q7)", ref))
        if t.get("direction") == "abs" and float(t["value"]) < 0:
            out.append(issue("thresholds", "error", "abs limit must be >= 0", ref))
        if not t.get("required_actions"):
            out.append(issue("thresholds", "warning", f"L{t.get('level')} has no required actions", ref))
        if t.get("cdse_ack_required") not in (None, True, False):
            out.append(issue("thresholds", "error", "cdse_ack_required must be true or false", ref))
        by_inst.setdefault(ref, []).append(t)
    for ref, lst in by_inst.items():
        for lt in ("magnitude", "rate"):
            for dr in DIRECTIONS:
                lv = sorted((t["level"], float(t["value"])) for t in lst
                            if t.get("limit_type") == lt and t.get("direction") == dr)
                vals = [v for _, v in lv]
                ordered = vals == sorted(vals, reverse=True) if dr == "lower" else vals == sorted(vals)
                if not ordered:
                    out.append(issue("thresholds", "error", f"{lt} {dr} limits are not monotonic by level", ref))
                if len({lv_[0] for lv_ in lv}) != len(lv):
                    out.append(issue("thresholds", "error", f"duplicate {lt} {dr} level", ref))
    for i in insts:
        if i["instrument_id"] not in by_inst:
            out.append(issue("thresholds", "warning", "no thresholds defined", i["instrument_id"]))

    ds = {m.get("instrument_id"): m for m in (draft.get("data_sources") or [])}
    for i in insts:
        m = ds.get(i["instrument_id"])
        if m is None:
            out.append(issue("data_sources", "error", "no data source mapping", i["instrument_id"]))
            continue
        if m.get("source_type") not in SOURCE_TYPES:
            out.append(issue("data_sources", "error", f"source_type must be one of {SOURCE_TYPES}", i["instrument_id"]))
        if m.get("source_type") in ("logger_file", "api", "survey_file") and not (m.get("path") or m.get("table")):
            out.append(issue("data_sources", "error", "path or table is required", i["instrument_id"]))
        if m.get("source_type") == "logger_file" and not m.get("column"):
            out.append(issue("data_sources", "error", "logger column is required", i["instrument_id"]))
        if not m.get("raw_unit"):
            out.append(issue("data_sources", "error", "raw_unit is required", i["instrument_id"]))
    for k in ds:
        if k not in iids:
            out.append(issue("data_sources", "warning", f"mapping for unknown instrument {k}", k))

    users = draft.get("users") or []
    if not any(u.get("role") == "dam_admin" for u in users):
        out.append(issue("users", "error", "at least one dam_admin is required"))
    emails = [str(u.get("email", "")).lower() for u in users]
    if len(set(emails)) != len(emails):
        out.append(issue("users", "error", "duplicate user email"))
    for u in users:
        if not EMAIL_RE.match(str(u.get("email", ""))):
            out.append(issue("users", "error", f"invalid email {u.get('email')!r}"))
        if u.get("role") not in ROLE_RANK:
            out.append(issue("users", "error", f"invalid role {u.get('role')!r}", u.get("email")))
    return out


def golive_payload(draft: dict) -> dict:
    """Rows to publish into ref.* (shape per SCHEMA_CONTRACT.md)."""
    d = draft["details"]
    did = d["dam_id"]
    site = {k: d.get(k) for k in ("dam_id", "name", "dam_type", "hazard_class", "owner", "nid_id", "state", "lat",
                                  "lon", "height_m", "crest_elev_m", "normal_pool_m", "datum")}
    site["lat"], site["lon"] = float(site["lat"]), float(site["lon"])
    site["status"] = "ACTIVE"
    structures = [{**s, "dam_id": did} for s in draft.get("structures") or []]
    instruments = [{**{CSV_ALIASES.get(k, k): v for k, v in i.items()}, "dam_id": did,
                    "formula_version": str(i["formula_version"]), "status": "ACTIVE",
                    "parent_id": None, "replaced_by": None} for i in draft.get("instruments") or []]
    calibrations = []
    for c in draft.get("calibrations") or []:
        inst = next(i for i in instruments if i["instrument_id"] == c["instrument_id"])
        v = int(c.get("cal_version") or 1)
        calibrations.append({"calib_id": c.get("calib_id") or f"{c['instrument_id']}-C{v}",
                             "instrument_id": c["instrument_id"], "sensor_id": c["instrument_id"], "cal_version": v,
                             "effective_from": c["effective_from"], "effective_to": None, "is_current": True,
                             "is_retired": False,
                             "formula_id": inst["formula_id"], "formula_version": inst["formula_version"],
                             "params": {k: float(x) if isinstance(x, (int, float, str)) and _isnum(x) else x
                                        for k, x in (c.get("params") or {}).items()},
                             "sign_mult": int(float(c["sign_mult"])) if c.get("sign_mult") not in (None, "") else 1,
                             "offset": _f(c.get("offset")) or 0.0,
                             "method": c.get("method") or (c.get("params") or {}).get("method"),
                             "certificate_uri": c.get("certificate_uri"),
                             "approved_by": c.get("approved_by"), "approved_at": c.get("approved_at")})
    thresholds = []
    for n, t in enumerate(map(_canonical_threshold, draft.get("thresholds") or []), start=1):
        inst = next(i for i in instruments if i["instrument_id"] == t["instrument_id"])
        # canonical ref.threshold columns (SCHEMA_CONTRACT v1.1 §4)
        thresholds.append({"threshold_id": t.get("threshold_id") or f"{t['instrument_id']}-T{t['level']}-{n}",
                           "threshold_version": 1, "instrument_id": t["instrument_id"], "dam_id": did,
                           "output_name": inst.get("output_name"), "level": int(t["level"]),
                           "limit_type": t["limit_type"], "direction": t["direction"], "value": _f(t["value"]),
                           "unit": t["unit"],
                           "effective_from": t.get("effective_from") or datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z"),
                           "effective_to": None, "is_current": True, "pfm_id": t.get("pfm_id"),
                           "required_actions": t.get("required_actions"),
                           # ref.threshold.cdse_ack_required (DA-owned, NOT NULL DEFAULT false)
                           "cdse_ack_required": t.get("cdse_ack_required") is True})
    return {"dam_id": did, "sites": [site], "structures": structures, "instruments": instruments,
            "calibrations": calibrations, "thresholds": thresholds, "pfms": [],
            "data_source_map": [{**m, "dam_id": did} for m in draft.get("data_sources") or []],
            "users": draft.get("users") or []}


def rule_key(t: dict) -> str:
    """Stable key of a draft alarm rule: its threshold_id, else instrument/level/limit_type/direction."""
    t = _canonical_threshold(t)
    return t.get("threshold_id") or f"{t.get('instrument_id')}/L{t.get('level')}/{t.get('limit_type')}/{t.get('direction')}"


def cdse_flag_changes(old_rules: list[dict] | None, new_rules: list[dict] | None) -> list[dict]:
    """cdse_ack_required changes between two saves of the thresholds step (audited as an admin action).
    A new rule counts as a change only when it is flagged (default off)."""
    old = {rule_key(t): t.get("cdse_ack_required") is True for t in old_rules or [] if isinstance(t, dict)}
    out = []
    for t in new_rules or []:
        if not isinstance(t, dict):
            continue
        k, now = rule_key(t), t.get("cdse_ack_required") is True
        if old.get(k, False) != now:
            out.append({"rule": k, "instrument_id": t.get("instrument_id"), "level": t.get("level"),
                        "from": old.get(k, False), "to": now})
    for k, was in old.items():
        if was and k not in {rule_key(t) for t in new_rules or [] if isinstance(t, dict)}:
            out.append({"rule": k, "from": True, "to": None})   # flagged rule removed from the draft
    return out


def _canonical_threshold(t: dict) -> dict:
    """Draft threshold -> canonical §4 fields. Legacy drafts (rule/upper_limit/...) are converted by the contract
    adapter; canonical drafts pass through unchanged (limit_type/direction lower-cased)."""
    t = dict(t)
    if not t.get("limit_type") and t.get("rule"):
        c = ct.threshold(t)
        t.update(limit_type=c["limit_type"], direction=c["direction"], value=c["value"], pfm_id=c.get("pfm_id"),
                 required_actions=c.get("required_actions"))
    for k in ("limit_type", "direction"):
        if isinstance(t.get(k), str):
            t[k] = t[k].strip().lower()
    return t


def _isnum(x) -> bool:
    try:
        float(x)
        return not isinstance(x, bool)
    except (TypeError, ValueError):
        return False


def _f(x):
    return None if x in (None, "") else float(x)
