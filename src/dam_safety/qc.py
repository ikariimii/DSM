"""Data-quality (QC) flags for reduced readings — pure Python.

Flag vocabulary per /workspace/dam-refs/CONVENTIONS.md canonical field ``qa_flag``:
OK | SUSPECT | BAD | MISSING, with a ``qa_reason`` code list.

Rules and their basis:
  RANGE    value outside the instrument's valid/rated range                  -> BAD
           (FERC Eng. Guidelines Ch.9, PDF p.47: limits set per instrument; rated range
           from the manufacturer calibration sheet, e.g. GEOKON-4500 calibration record)
  SPIKE    Hampel-type outlier vs trailing window median (robust z on MAD)  -> SUSPECT
  STUCK    >= N consecutive identical readings (within eps)                  -> SUSPECT
  MISSING  gap > gap_factor x scheduled interval (ref.reading_schedule)      -> MISSING
           (FERC canvas REQ-18..21 reading-frequency compliance)
  CHECKSUM inclinometer checksum departs from survey median by > tol         -> SUSPECT
           (SI-DR-2001 'Checksums', PDF p.2)
  BAD readings are excluded from alarm evaluation (FERC canvas REQ-15).

Numeric parameters (window, n_sigma, stuck N, checksum tol) are SITE SETTINGS stored in
ref.qc_rule; defaults below are engineering defaults and are flagged UNVERIFIED in
docs/VERIFICATION.md (no manufacturer value exists for them).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import List, Optional, Sequence, Tuple

OK, SUSPECT, BAD, MISSING = "OK", "SUSPECT", "BAD", "MISSING"
_SEVERITY = {OK: 0, SUSPECT: 1, MISSING: 2, BAD: 3}


@dataclass
class QcParams:
    min_valid: Optional[float] = None
    max_valid: Optional[float] = None
    spike_window: int = 12          # trailing readings
    spike_n_sigma: float = 6.0      # robust z threshold (UNVERIFIED default)
    spike_min_abs: float = 0.0      # ignore tiny deviations (engineering units)
    stuck_count: int = 24           # consecutive identical values (UNVERIFIED default)
    stuck_eps: float = 0.0
    expected_interval_s: Optional[float] = None
    gap_factor: float = 2.0
    checksum_tol: Optional[float] = None


@dataclass
class QcResult:
    flag: str = OK
    reasons: List[str] = field(default_factory=list)

    def add(self, flag: str, reason: str) -> None:
        if _SEVERITY[flag] > _SEVERITY[self.flag]:
            self.flag = flag
        self.reasons.append(reason)

    @property
    def reason(self) -> Optional[str]:
        return ",".join(self.reasons) if self.reasons else None


def range_check(v: Optional[float], p: QcParams) -> Optional[str]:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "NULL_VALUE"
    if p.min_valid is not None and v < p.min_valid:
        return "RANGE_LOW"
    if p.max_valid is not None and v > p.max_valid:
        return "RANGE_HIGH"
    return None


def robust_z(x: float, window: Sequence[float]) -> float:
    """|x - median| / (1.4826 * MAD); 1.4826 makes MAD consistent with sigma for normal
    data (standard Hampel identifier constant)."""
    m = median(window)
    mad = median([abs(w - m) for w in window])
    if mad == 0:
        return math.inf if x != m else 0.0
    return abs(x - m) / (1.4826 * mad)


def qc_series(ts: Sequence[datetime], values: Sequence[Optional[float]], p: QcParams) -> List[QcResult]:
    """QC a time-ordered series for ONE sensor. Spike uses only trailing (past) good
    values so results are identical in streaming and batch."""
    out: List[QcResult] = []
    good_hist: List[float] = []
    run_val, run_len = None, 0
    prev_t = None
    for t, v in zip(ts, values):
        r = QcResult()
        rc = range_check(v, p)
        if rc:
            r.add(BAD, rc)
        else:
            # stuck
            if run_val is not None and abs(v - run_val) <= p.stuck_eps:
                run_len += 1
            else:
                run_val, run_len = v, 1
            if run_len >= p.stuck_count:
                r.add(SUSPECT, "STUCK")
            # spike (needs a full trailing window)
            if len(good_hist) >= p.spike_window:
                win = good_hist[-p.spike_window:]
                if abs(v - median(win)) > p.spike_min_abs and robust_z(v, win) > p.spike_n_sigma:
                    r.add(SUSPECT, "SPIKE")
        if p.expected_interval_s and prev_t is not None:
            if (t - prev_t).total_seconds() > p.gap_factor * p.expected_interval_s:
                r.reasons.append("AFTER_GAP")  # informational; gap rows come from find_gaps
        if r.flag == OK and v is not None:
            good_hist.append(v)
        prev_t = t
        out.append(r)
    return out


def find_gaps(ts: Sequence[datetime], expected_interval_s: float, gap_factor: float = 2.0,
              until: Optional[datetime] = None) -> List[Tuple[datetime, datetime]]:
    """Missing-data windows: (last_ts, next_ts) where spacing > gap_factor*interval; an
    open trailing gap up to ``until`` is reported too (stale logger)."""
    lim = timedelta(seconds=gap_factor * expected_interval_s)
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > lim]
    if until is not None and ts and until - ts[-1] > lim:
        gaps.append((ts[-1], until))
    return gaps


def checksum_qc(checksums: Sequence[float], tol: float) -> List[QcResult]:
    from .formulas.core import incl_checksum_flags

    res = []
    for bad in incl_checksum_flags(checksums, tol):
        r = QcResult()
        if bad:
            r.add(SUSPECT, "CHECKSUM")
        res.append(r)
    return res
