"""Display gate. A computed (engineering) value is shown only when the engine has
verified the formula version that produced it AND the calibration used is
approved (not retired, no calibration_issues) AND the reading is not QC-flagged BAD/MISSING
AND, for inclinometer rows with calib_id NULL, the formula is (INCL_PROBE_2PASS, 1.0.0) verified and the
installation_version matches a ref.incl_installation row (§5.5 v1.4) AND, for inclinometer rows, the value is uncorrected (corrections the app cannot verify are withheld) AND, for
alarm events, the v1.1 lineage (calib_id + formula_id/version) is present and consistent. Otherwise the value is withheld
(set to None) and `display_status` explains why. Raw values, units, calibration
version and QC flags are always passed through so the user can see what exists."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

SHOWN = "VERIFIED"
UNVERIFIED_FORMULA = "UNVERIFIED_FORMULA"
UNAPPROVED_CALIBRATION = "UNAPPROVED_CALIBRATION"
QC_BAD = "QC_BAD"
MISSING = "MISSING"
CORRECTION_UNVERIFIED = "CORRECTION_UNVERIFIED"
LINEAGE_INCOMPLETE = "LINEAGE_INCOMPLETE"
LINEAGE_INCONSISTENT = "LINEAGE_INCONSISTENT"
NO_ENGINE_FORMULA = "NO_ENGINE_FORMULA"   # §5.4: instrument type with empty engine_formulas is never computed
UNAPPROVED_INSTALLATION = "UNAPPROVED_INSTALLATION"   # §5.5 v1.4: inclinometer row (calib_id NULL) whose
                                                      # installation_version matches no ref.incl_installation row
INCL_FORMULA = ("INCL_PROBE_2PASS", "1.0.0")   # §5.5 v1.4 "Approval when calib_id is NULL"
COMPUTED_KEYS = ("eng_value", "corrected_value")
INCL_KEYS = ("a_inc_mm", "b_inc_mm", "a_cum_mm", "b_cum_mm", "resultant_mm")


def ts_key(v) -> str | None:
    """Timestamp -> canonical UTC string (microseconds) so installation_version and effective_from compare exactly
    whether they arrive as datetimes or ISO strings ('Z', offset or naive = UTC by contract). None if unparseable."""
    if v is None:
        return None
    try:
        t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Gate:
    def __init__(self, formulas: Iterable[dict], calibrations: Iterable[dict],
                 not_computable: Iterable[str] = (), installations: Iterable[dict] = ()):
        self.not_computable = set(not_computable)
        # §5.5 v1.4: (instrument_id, effective_from) of every ref.incl_installation SCD2 row; an inclinometer row
        # with calib_id NULL counts as "constants approved" only when its installation_version equals one of them
        self.installations = {(i.get("instrument_id"), ts_key(i.get("effective_from"))) for i in installations
                              if ts_key(i.get("effective_from")) is not None}
        self.verified = {(f["formula_id"], str(f["formula_version"])) for f in formulas if f.get("verified")}
        # retired calibrations (superseded corrections) and calibrations with data issues (e.g. sign_mult in params,
        # contract.calibration) never count as approved
        self.approved = {c["calib_id"] for c in calibrations if c.get("approved_by") and not c.get("is_retired")
                         and not c.get("calibration_issues")}

    def status(self, row: dict) -> str:
        if row.get("instrument_id") in self.not_computable:
            return NO_ENGINE_FORMULA
        fid, ver = row.get("formula_id"), row.get("formula_version")
        if fid is None or ver is None or (fid, str(ver)) not in self.verified:
            return UNVERIFIED_FORMULA
        if row.get("calib_id") is None and fid == INCL_FORMULA[0]:
            if not self.installation_approved(row):
                return UNAPPROVED_INSTALLATION
        elif row.get("calib_id") not in self.approved:
            return UNAPPROVED_CALIBRATION
        flag = (row.get("qa_flag") or "").upper()
        if flag == "BAD":
            return QC_BAD
        if flag == "MISSING":
            return MISSING
        if row.get("correction_basis") == "corrected_unverified":
            return CORRECTION_UNVERIFIED
        return SHOWN

    def formula_verified(self, fid, ver) -> bool:
        return fid is not None and ver is not None and (fid, str(ver)) in self.verified

    def installation_approved(self, row: dict) -> bool:
        """§5.5 v1.4 approval when calib_id is NULL: (formula_id, formula_version) == (INCL_PROBE_2PASS, 1.0.0)
        joins a verified ref.formula row AND installation_version == a ref.incl_installation.effective_from for
        that instrument. The calibration-approved clause does not apply."""
        fid, ver = row.get("formula_id"), row.get("formula_version")
        if (fid, str(ver)) != INCL_FORMULA or not self.formula_verified(fid, ver):
            return False
        key = ts_key(row.get("installation_version"))
        return key is not None and (row.get("instrument_id"), key) in self.installations

    def apply(self, row: dict, keys: tuple[str, ...] = COMPUTED_KEYS) -> dict:
        st = self.status(row)
        row["display_status"] = st
        row["verified"] = st == SHOWN
        if st != SHOWN:
            for k in keys:
                if k in row:
                    row[k] = None
        return row

    def apply_all(self, rows: list[dict], keys: tuple[str, ...] = COMPUTED_KEYS) -> list[dict]:
        return [self.apply(r, keys) for r in rows]

    def apply_event(self, ev: dict) -> dict:
        """Alarm events always stay visible (safety first); an unverified triggering value is withheld."""
        fid, ver = ev.get("formula_id"), ev.get("formula_version")
        if ev.get("instrument_id") in self.not_computable:
            st = NO_ENGINE_FORMULA
        elif ev.get("lineage_issue"):
            st = LINEAGE_INCONSISTENT
        elif ev.get("calib_id") is None or fid is None or ver is None:
            st = LINEAGE_INCOMPLETE
        elif (fid, str(ver)) not in self.verified:
            st = UNVERIFIED_FORMULA
        elif ev.get("calib_id") not in self.approved:
            st = UNAPPROVED_CALIBRATION
        elif (ev.get("qa_flag") or "").upper() == "BAD":
            st = QC_BAD
        elif (ev.get("qa_flag") or "").upper() == "MISSING":
            st = MISSING
        elif ev.get("value") is None:
            # §4 v1.4: a reading-driven episode never has a NULL value; kept defensively so a NULL is never
            # presented as a verified value
            st = MISSING
        else:
            st = SHOWN
        ev["display_status"] = st
        ev["verified"] = st == SHOWN
        if st != SHOWN:
            # the free-text message may embed the computed value, so it is replaced as well
            ev["value"] = None
            ev["message"] = (f"{ev.get('instrument_id')}: level {ev.get('level')} {ev.get('rule')} alarm "
                             f"(triggering value withheld: {st})")
        return ev
