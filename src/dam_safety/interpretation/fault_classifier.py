"""Sensor-fault vs real-behaviour classification of an anomalous reading (rule-based, explainable).

Evidence rules (each returns a vote and a reason code):
  QC_FAULT        reading already QC-flagged for an instrument problem (SPIKE, STUCK, RANGE_*, CHECKSUM,
                  CALIB_ERROR, NULL_VALUE)                  -> fault   [formula_library.md §8 'Data QA']
  DATUM_OR_CALIB  datum change / calibration change in window -> indeterminate: 'Calibration of the
                  instruments should be checked (this is paramount)' [FERC-CH14 PDF p37]
  ISOLATED        anomalous while every redundant neighbour is within band -> fault lean:
                  'measurement errors in isolated devices are not compatible with serious anomaly
                  scenarios' [SALAZAR-2021 p2]; 'data should be checked against redundant data to
                  determine whether or not trends and magnitudes are the same' [FERC-CH14 PDF p37]
  CORROBORATED    ≥ 1 redundant neighbour anomalous with the same sign -> real lean [same sources]
  TRANSIENT       a single excursion that returned to band next reading -> fault lean;
  PERSISTENT      excursion persisted ≥ persist_n readings -> real lean: 'Readings deviating from
                  established trends should be verified by more frequent readings' [FERC-CH14 PDF p36]
  NO_REDUNDANCY   no neighbours to cross-check -> lowers confidence.
Confidence = |votes_real − votes_fault| / (n_votes + 1), capped at 0.9 and halved without
redundancy. This weighting is an UNVERIFIED engineering heuristic (VERIFICATION.md INT-CONF); the
output is advisory and never alters qa_flag or alarm state."""
from __future__ import annotations

from dataclasses import dataclass, field

FAULT_QC_CODES = ("SPIKE", "STUCK", "RANGE_LOW", "RANGE_HIGH", "CHECKSUM", "CALIB_ERROR", "NULL_VALUE")


@dataclass
class Evidence:
    z: float                                   # this sensor's residual z-score
    qa_reason: str | None = None               # from silver.reading_reduced
    neighbour_z: list = field(default_factory=list)   # z-scores of redundant instruments at the same time
    run_length: int = 1                        # consecutive readings outside band, ending now
    returned_next: bool | None = None          # True if the next reading is back within band
    datum_or_calibration_change: bool = False
    k_warn: float = 2.0
    persist_n: int = 3                         # UNVERIFIED default


@dataclass
class Classification:
    label: str                                  # SENSOR_FAULT | REAL_BEHAVIOUR | INDETERMINATE | NORMAL
    confidence: float
    reasons: list


def classify(e: Evidence) -> Classification:
    if e.z is None or abs(e.z) <= e.k_warn:
        return Classification("NORMAL", 1.0, ["WITHIN_BAND"])
    reasons, fault, real = [], 0, 0
    if e.qa_reason and any(c in e.qa_reason for c in FAULT_QC_CODES):
        reasons.append("QC_FAULT")
        fault += 2
    if e.datum_or_calibration_change:
        reasons.append("DATUM_OR_CALIB")
        return Classification("INDETERMINATE", 0.2, reasons + ["CHECK_CALIBRATION_FIRST"])
    if e.neighbour_z:
        sign = 1 if e.z > 0 else -1
        corroborating = [z for z in e.neighbour_z if z is not None and abs(z) > e.k_warn and (z > 0) == (sign > 0)]
        if corroborating:
            reasons.append(f"CORROBORATED({len(corroborating)}/{len(e.neighbour_z)})")
            real += 1 + (len(corroborating) > 1)
        elif all(z is not None and abs(z) <= e.k_warn for z in e.neighbour_z):
            reasons.append("ISOLATED")
            fault += 1
    else:
        reasons.append("NO_REDUNDANCY")
    if e.run_length >= e.persist_n:
        reasons.append(f"PERSISTENT({e.run_length})")
        real += 1
    elif e.run_length == 1 and e.returned_next:
        reasons.append("TRANSIENT")
        fault += 1
    n = fault + real
    if n == 0 or fault == real:
        label = "INDETERMINATE"
    else:
        label = "SENSOR_FAULT" if fault > real else "REAL_BEHAVIOUR"
    conf = min(0.9, abs(real - fault) / (n + 1)) if n else 0.1
    if not e.neighbour_z:
        conf /= 2
    return Classification(label, round(conf, 3), reasons)
