"""Alarm state machine with hysteresis, persistence and acknowledgement — pure Python.

States (ISA-18.2-style alarm lifecycle, adapted to multi-level dam thresholds):
    NORMAL
    <LEVEL>_UNACK   active, not yet acknowledged       e.g. ALERT_UNACK, ACTION_UNACK
    <LEVEL>_ACK     active, acknowledged by a user     e.g. ALERT_ACK,   ACTION_ACK
    CLEARED_UNACK   returned to normal before anyone acknowledged it (needs ack to close)

Level order comes from ref.threshold (tier/level index); defaults ALERT=1, ACTION=2,
matching the user's 'normal > alert > action' hierarchy. FERC basis:
  * both magnitude and rate-of-change limits  — FERC Eng. Guidelines Ch.9, PDF p.47
    ("Both magnitude and rate of change limits should be established")
  * QC BAD / MISSING readings never drive alarms — FERC canvas REQ-15
  * every transition is logged (ops.alarm_transition + audit.event_log) — REQ-22..25
Hysteresis: a level is raised when the value crosses ``limit`` and only cleared once it
retreats past ``limit - deadband`` (direction-aware). Persistence: N consecutive raising
readings to escalate (persistence_on), M consecutive clearing readings to de-escalate
(persistence_off). Escalation always resets acknowledgement.
Deadband/persistence values are site settings per threshold version (UNVERIFIED defaults).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Dict, List, Optional, Sequence

NORMAL = "NORMAL"
CLEARED_UNACK = "CLEARED_UNACK"
DEFAULT_LEVEL_NAMES = {1: "ALERT", 2: "ACTION"}


@dataclass(frozen=True)
class ThresholdSpec:
    threshold_id: str
    version: int
    level: int                    # 1 = ALERT, 2 = ACTION, ...
    limit_type: str               # 'magnitude' | 'rate' (per day)
    direction: str                # 'high' | 'low' | 'abs'
    value: float
    deadband: float = 0.0
    persistence_on: int = 1
    persistence_off: int = 1
    level_name: Optional[str] = None


@dataclass
class AlarmState:
    key: str                      # sensor_id (or sensor_id|metric)
    level: int = 0
    acked: bool = True
    cleared_unack: bool = False
    pending_up: int = 0
    pending_up_level: int = 0
    pending_down: int = 0
    last_value: Optional[float] = None
    last_ts: Optional[datetime] = None
    active_threshold_id: Optional[str] = None
    active_threshold_version: Optional[int] = None
    level_names: Dict[int, str] = field(default_factory=lambda: dict(DEFAULT_LEVEL_NAMES))

    @property
    def state(self) -> str:
        if self.level == 0:
            return CLEARED_UNACK if self.cleared_unack else NORMAL
        name = self.level_names.get(self.level, f"L{self.level}")
        return f"{name}_{'ACK' if self.acked else 'UNACK'}"


@dataclass(frozen=True)
class Transition:
    key: str
    ts: datetime
    event: str                    # RAISE | ESCALATE | DEESCALATE | CLEAR | RTN_UNACK | ACK
    from_state: str
    to_state: str
    value: Optional[float]
    metric_value: Optional[float]
    threshold_id: Optional[str]
    threshold_version: Optional[int]
    actor: str = "system"
    comment: Optional[str] = None


def _exceeds(metric: float, spec: ThresholdSpec, margin: float = 0.0) -> bool:
    lim = spec.value
    if spec.direction == "high":
        return metric > lim - margin
    if spec.direction == "low":
        return metric < lim + margin
    if spec.direction == "abs":
        return abs(metric) > lim - margin
    raise ValueError(spec.direction)


def _metric(spec: ThresholdSpec, value: float, st: AlarmState, ts: datetime) -> Optional[float]:
    if spec.limit_type == "magnitude":
        return value
    if spec.limit_type == "rate":
        if st.last_value is None or st.last_ts is None:
            return None
        days = (ts - st.last_ts).total_seconds() / 86400.0
        if days <= 0:
            return None
        return (value - st.last_value) / days
    raise ValueError(spec.limit_type)


def evaluate(st: AlarmState, ts: datetime, value: Optional[float], qa_flag: str,
             specs: Sequence[ThresholdSpec]) -> List[Transition]:
    """Advance the state machine by one reading. Mutates ``st``; returns transitions."""
    if qa_flag in ("BAD", "MISSING") or value is None:
        return []   # REQ-15: invalid data neither raises nor clears alarms
    for s in specs:
        if s.level_name:
            st.level_names[s.level] = s.level_name

    raise_level, hold_level, raise_spec, raise_metric = 0, 0, None, None
    metrics = {}                                  # level -> evaluated metric (None = not evaluable, e.g. 1st rate)
    for s in specs:
        m = _metric(s, value, st, ts)
        if m is None:
            continue
        metrics[s.level] = m if s.level not in metrics else metrics[s.level]
        if _exceeds(m, s) and s.level > raise_level:
            raise_level, raise_spec, raise_metric = s.level, s, m
        if _exceeds(m, s, margin=s.deadband) and s.level > hold_level:
            hold_level = s.level
    out: List[Transition] = []
    if not metrics:
        # QA-035: nothing evaluable on this reading (e.g. first rate reading after a datum re-baseline). It must not
        # clear or de-escalate (a transition without its metric would be untraceable): only remember the reading.
        st.last_value, st.last_ts = value, ts
        return out
    before, before_level = st.state, st.level

    def tr(event, spec=None, metric=None):
        # QA-035 / SCHEMA_CONTRACT §4: every reading-driven transition carries value AND the evaluated metric. For
        # CLEAR / RTN_UNACK / DEESCALATE that is the metric of the level being left (the one that stopped holding),
        # else the highest-level metric evaluated on this reading.
        if metric is None:
            metric = metrics.get(before_level, metrics[max(metrics)])
        out.append(Transition(st.key, ts, event, before, st.state, value, metric,
                              spec.threshold_id if spec else st.active_threshold_id,
                              spec.version if spec else st.active_threshold_version))

    if raise_level > st.level:
        st.pending_down = 0
        st.pending_up = st.pending_up + 1 if st.pending_up_level and st.pending_up > 0 else 1
        st.pending_up_level = raise_level if st.pending_up == 1 else min(st.pending_up_level, raise_level)
        need = min(s.persistence_on for s in specs if s.level == st.pending_up_level)
        if st.pending_up >= need:
            target = st.pending_up_level
            spec = raise_spec if raise_spec and raise_spec.level == target else next(s for s in specs if s.level == target)
            event = "RAISE" if st.level == 0 else "ESCALATE"
            st.level, st.acked, st.cleared_unack = target, False, False
            st.active_threshold_id, st.active_threshold_version = spec.threshold_id, spec.version
            st.pending_up, st.pending_up_level = 0, 0
            tr(event, spec, raise_metric)
    elif hold_level < st.level:
        st.pending_up, st.pending_up_level = 0, 0
        st.pending_down += 1
        need = max([s.persistence_off for s in specs if s.level == st.level] or [1])
        if st.pending_down >= need:
            st.pending_down = 0
            if hold_level == 0:
                if st.acked:
                    st.level = 0
                    tr("CLEAR")
                else:
                    st.level, st.cleared_unack = 0, True
                    tr("RTN_UNACK")
                st.active_threshold_id = st.active_threshold_version = None
            else:
                st.level = hold_level
                spec = next(s for s in specs if s.level == hold_level)
                st.active_threshold_id, st.active_threshold_version = spec.threshold_id, spec.version
                tr("DEESCALATE", spec)
    else:
        st.pending_up, st.pending_up_level, st.pending_down = 0, 0, 0

    st.last_value, st.last_ts = value, ts
    return out


def acknowledge(st: AlarmState, ts: datetime, actor: str, comment: Optional[str] = None) -> List[Transition]:
    """User acknowledgement (written by the app to Lakebase, applied by the evaluator)."""
    before = st.state
    if st.level > 0 and not st.acked:
        st.acked = True
    elif st.level == 0 and st.cleared_unack:
        st.cleared_unack = False
    else:
        return []
    return [Transition(st.key, ts, "ACK", before, st.state, None, None,
                       st.active_threshold_id, st.active_threshold_version, actor, comment)]


def run_series(key: str, rows: Sequence[dict], specs: Sequence[ThresholdSpec],
               state: Optional[AlarmState] = None, acks: Sequence[dict] = ()) -> tuple:
    """Replay a time-ordered series (+ optional acks {ts, actor, comment}) through the
    machine. Returns (final_state, transitions). Used by the Spark evaluator per sensor
    (applyInPandas) and by tests."""
    st = state or AlarmState(key)
    events = [(r["ts_utc"], 1, r) for r in rows] + [(a["ts"], 0, a) for a in acks]
    events.sort(key=lambda e: (e[0], e[1]))
    trs: List[Transition] = []
    for ts, kind, e in events:
        if kind == 1:
            if str(e.get("qa_reason") or "").startswith("DATUM_CHANGE"):
                # datum break is a re-baseline, not movement: restart the rate-of-change
                # baseline (CONVENTIONS.md 'Schema additions'; settlement procedure §7).
                st.last_value, st.last_ts = None, None
            trs += evaluate(st, ts, e.get("value"), e.get("qa_flag", "OK"), specs)
        else:
            trs += acknowledge(st, ts, e.get("actor", "unknown"), e.get("comment"))
    return st, trs


# ---------------------------------------------------------------------------------------------
# ref.threshold row -> ThresholdSpec (used by src/jobs/alarm_evaluator.py; unit-tested)
# ---------------------------------------------------------------------------------------------
DIRECTION_MAP = {"upper": "high", "high": "high", "above": "high",
                 "lower": "low", "low": "low", "below": "low", "abs": "abs"}


def spec_from_threshold_row(row: dict) -> ThresholdSpec:
    """Map a ref.threshold row (FERC canvas vocabulary: direction upper|lower) onto the
    machine's ThresholdSpec (high|low|abs). 'cyclic' limits are not evaluated by this
    machine (NOT IMPLEMENTED, docs/VERIFICATION.md) and raise ValueError."""
    d = str(row["direction"]).strip().lower()
    if d not in DIRECTION_MAP:
        raise ValueError(f"unknown threshold direction {row['direction']!r}")
    lt = str(row["limit_type"]).strip().lower()
    if lt not in ("magnitude", "rate"):
        raise ValueError(f"limit_type {lt!r} not supported by the alarm machine")
    return ThresholdSpec(
        threshold_id=str(row["threshold_id"]), version=int(row["threshold_version"]),
        level=int(row["level"]), limit_type=lt, direction=DIRECTION_MAP[d], value=float(row["value"]),
        deadband=float(row.get("deadband") or 0.0), persistence_on=int(row.get("persistence_on") or 1),
        persistence_off=int(row.get("persistence_off") or 1), level_name=row.get("level_name"))


STATE_FIELDS = ("level", "acked", "cleared_unack", "pending_up", "pending_up_level", "pending_down",
                "last_value", "last_ts", "active_threshold_id", "active_threshold_version")


def state_to_row(st: AlarmState) -> dict:
    d = {f: getattr(st, f) for f in STATE_FIELDS}
    d.update(alarm_key=st.key, state=st.state)
    return d


def state_from_row(row: dict) -> AlarmState:
    st = AlarmState(row["alarm_key"])
    for f in STATE_FIELDS:
        if f in row and row[f] is not None:
            setattr(st, f, row[f])
    return st


def evaluate_sensor_batch(sensor_id: str, readings: Sequence[dict], threshold_rows: Sequence[dict],
                          state_row: Optional[dict], acks: Sequence[dict]) -> tuple:
    """One evaluator step for one sensor: resume persisted state, skip readings at or before
    the watermark (late data -> not alarmed here; see VERIFICATION 'ALARM-LATE'), apply
    acks and new readings in time order. Returns (state_row, transitions, n_late)."""
    st = state_from_row(state_row) if state_row else AlarmState(sensor_id)
    wm = (state_row or {}).get("evaluated_through_ts")
    new = [r for r in readings if wm is None or r["ts_utc"] > wm]
    n_late = len(readings) - len(new)
    specs = []
    for t in threshold_rows:
        try:
            specs.append(spec_from_threshold_row(t))
        except ValueError:
            continue                           # cyclic etc.: NOT IMPLEMENTED, skipped (flagged in docs)
    st, trs = run_series(sensor_id, sorted(new, key=lambda r: r["ts_utc"]), specs, st, acks)
    row = state_to_row(st)
    row["evaluated_through_ts"] = max([r["ts_utc"] for r in new], default=wm)
    return row, trs, n_late


# ---------------------------------------------------------------------------------------------
# v1.7 cdse_ack_required (DSSMP; App Dev agreed 2026-11-09): snapshot at episode open
# ---------------------------------------------------------------------------------------------
def cdse_ack_snapshot(event: str, threshold_id: Optional[str], threshold_version: Optional[int],
                      threshold_rows: Sequence[dict]) -> Optional[bool]:
    """Value for ops.alarm_transition.cdse_ack_required_at_open. Only the RAISE transition (it opens the episode)
    carries it: ref.threshold.cdse_ack_required of the exact rule version that raised (threshold_id,
    threshold_version), i.e. the version in effect at evaluation. Every other event -> None. The row is appended to
    an append-only table, so the snapshot is immutable for the life of the episode; un-flagging the rule later only
    changes the CURRENT flag that ops.alarm_episode shows next to it. Fails closed: a RAISE whose rule version or
    flag cannot be found raises (pre-v1.7 schema, or the migration did not run) instead of guessing false."""
    if event != "RAISE":
        return None
    for r in threshold_rows:
        if str(r.get("threshold_id")) == str(threshold_id) and int(r.get("threshold_version")) == int(threshold_version):
            v = r.get("cdse_ack_required")
            if v is None:
                raise ValueError(f"ref.threshold {threshold_id} v{threshold_version}: cdse_ack_required is NULL/absent "
                                 "(run sql/migrations/v1_7_001_cdse_ack.sql)")
            return bool(v)
    raise ValueError(f"RAISE on {threshold_id} v{threshold_version}: rule version not among the evaluated thresholds")
