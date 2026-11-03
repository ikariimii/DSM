"""Datum-aware baselining for change-type series (settlement first).

Sources (see formulas.core.SOURCES):
  * SETTLEMENT_SIGN  - CONVENTIONS.md 'Sign conventions': settlement (down) positive, heave
    negative; change = current - initial. Settlement procedure §0: stored value =
    sign_mult x (value - value0); raw up-positive value kept unchanged in raw_value.
    verify.py Ex14.
  * DATUM_REBASELINE - CONVENTIONS.md 'Schema additions (lead, 2026-11-02)': a change of
    datum_id sets QA code DATUM_CHANGE and triggers a re-baseline, not movement.
    Settlement procedure §7: flag SUSPECT, re-baseline to the new datum_id and carry
    cumulative settlement across the break. verify.py Ex15.

Assumption (stated, from Ex15): no real movement occurs between the last reading under
the old datum and the first reading under the new datum. Where a surveyed datum tie
(offset between datums) exists it should be used instead - NOT IMPLEMENTED, open question
to the Geotech lead (docs/VERIFICATION.md 'DATUM-TIE').
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .formulas.core import check_sign_mult

DATUM_CHANGE = "DATUM_CHANGE"
UNIT_TO_MM = {"mm": 1.0, "m": 1000.0, "cm": 10.0, "ft": 304.8, "in": 25.4}


@dataclass
class Baseline:
    datum_id: Optional[str]
    base_raw: float          # raw value (native unit) that maps to `carry`
    carry: float             # cumulative eng value (mm) at the baseline reading
    established_ts: object = None


@dataclass
class SeriesState:
    baselines: Dict[Optional[str], Baseline] = field(default_factory=dict)
    last_datum: Optional[str] = None
    last_value: Optional[float] = None     # last cumulative eng value (mm)
    seen: bool = False


def rebaseline_series(rows: Sequence[dict], sign_mult: int = 1, raw_unit: str = "mm", offset: float = 0.0,
                      value0: Optional[float] = None, state: Optional[SeriesState] = None):
    """rows: time-ordered dicts {ts_utc, raw_value, datum_id}. Returns (outputs, state) where
    each output = {eng_value (mm, sign convention applied), datum_change (bool)}.

    eng = sign_mult * (raw - base_raw[datum]) * unit_to_mm + carry[datum]
    first datum: base_raw = value0 (initial reading) or first raw; carry = offset
    new datum:   base_raw = first raw under it; carry = last cumulative eng value
    """
    sm = check_sign_mult(sign_mult)
    k = UNIT_TO_MM[str(raw_unit).lower()]
    st = state or SeriesState()
    out: List[dict] = []
    for r in rows:
        raw = r.get("raw_value")
        did = r.get("datum_id")
        if raw is None:
            out.append({"eng_value": None, "datum_change": False})
            continue
        raw = float(raw)
        change = False
        if did not in st.baselines:
            if not st.seen:
                base = float(value0) if value0 is not None else raw
                st.baselines[did] = Baseline(did, base, float(offset or 0.0), r.get("ts_utc"))
            else:
                change = True
                st.baselines[did] = Baseline(did, raw, st.last_value if st.last_value is not None else float(offset or 0.0),
                                             r.get("ts_utc"))
        elif st.seen and did != st.last_datum:
            change = True            # switching back to a previously-seen datum: reuse its baseline, still flagged
        b = st.baselines[did]
        eng = sm * (raw - b.base_raw) * k + b.carry
        st.last_datum, st.last_value, st.seen = did, eng, True
        out.append({"eng_value": eng, "datum_change": change})
    return out, st


def datum_changes(datum_ids: Sequence[Optional[str]], prev: Optional[str] = None, have_prev: bool = False) -> List[bool]:
    """Generic DATUM_CHANGE detector for any series (flag only, no re-baseline)."""
    flags, last, seen = [], prev, have_prev
    for d in datum_ids:
        flags.append(seen and d != last)
        last, seen = d, True
    return flags


def state_from_history(history: Sequence[dict]) -> SeriesState:
    """Rebuild SeriesState from already-reduced rows (silver.v_reading_current: ts_utc,
    datum_id, raw_value, eng_value) so incremental batches continue the same baselines
    without a separate state table: the first row under each datum_id defines
    (base_raw, carry = its eng_value); the last row gives last_datum/last_value.
    Late rows inserted before an existing datum break are NOT re-chained (flagged
    UNVERIFIED in docs/VERIFICATION.md 'DATUM-LATE'; run a recompute for the window)."""
    st = SeriesState()
    for r in sorted((h for h in history if h.get("raw_value") is not None and h.get("eng_value") is not None),
                    key=lambda h: h["ts_utc"]):
        d = r.get("datum_id")
        if d not in st.baselines:
            st.baselines[d] = Baseline(d, float(r["raw_value"]), float(r["eng_value"]), r["ts_utc"])
        st.last_datum, st.last_value, st.seen = d, float(r["eng_value"]), True
    return st
