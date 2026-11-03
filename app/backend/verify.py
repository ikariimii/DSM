"""Display gate. A computed (engineering) value is shown only when the engine has
verified the formula version that produced it AND the calibration used is
approved (not retired, no calibration_issues) AND the reading is not QC-flagged BAD/MISSING
AND, for inclinometer rows, the value is uncorrected (corrections the app cannot verify are withheld) AND, for
alarm events, the v1.1 lineage (calib_id + formula_id/version) is present and consistent. Otherwise the value is withheld
(set to None) and `display_status` explains why. Raw values, units, calibration
version and QC flags are always passed through so the user can see what exists."""
from __future__ import annotations

from typing import Iterable

SHOWN = "VERIFIED"
UNVERIFIED_FORMULA = "UNVERIFIED_FORMULA"
UNAPPROVED_CALIBRATION = "UNAPPROVED_CALIBRATION"
QC_BAD = "QC_BAD"
MISSING = "MISSING"
CORRECTION_UNVERIFIED = "CORRECTION_UNVERIFIED"
LINEAGE_INCOMPLETE = "LINEAGE_INCOMPLETE"
LINEAGE_INCONSISTENT = "LINEAGE_INCONSISTENT"
COMPUTED_KEYS = ("eng_value", "corrected_value")
INCL_KEYS = ("a_inc_mm", "b_inc_mm", "a_cum_mm", "b_cum_mm", "resultant_mm")


class Gate:
    def __init__(self, formulas: Iterable[dict], calibrations: Iterable[dict]):
        self.verified = {(f["formula_id"], str(f["formula_version"])) for f in formulas if f.get("verified")}
        # retired calibrations (superseded corrections) and calibrations with data issues (e.g. sign_mult in params,
        # contract.calibration) never count as approved
        self.approved = {c["calib_id"] for c in calibrations if c.get("approved_by") and not c.get("is_retired")
                         and not c.get("calibration_issues")}

    def status(self, row: dict) -> str:
        fid, ver = row.get("formula_id"), row.get("formula_version")
        if fid is None or ver is None or (fid, str(ver)) not in self.verified:
            return UNVERIFIED_FORMULA
        if row.get("calib_id") not in self.approved:
            return UNAPPROVED_CALIBRATION
        flag = (row.get("qa_flag") or "").upper()
        if flag == "BAD":
            return QC_BAD
        if flag == "MISSING":
            return MISSING
        if row.get("correction_basis") == "corrected_unverified":
            return CORRECTION_UNVERIFIED
        return SHOWN

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
        if ev.get("lineage_issue"):
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
