"""9_DIAGNOSTICS -> diagnostics (REPORT §6.1 row 9; procedure §5). Detection only: results feed a CorrectionRecord's
auto_value / detection_statistic for a human to accept; nothing here applies a correction.

  rotation_fit   factor = sum(errA) / sum(D_B) over the survey, Pearson r of errA vs D_B per interval, where errA is
                 the A-axis departure from an expected D (e.g. time interpolation of neighbours) [MIKKELSEN-2007 PDF
                 p17-18: factor normally < sin(1 deg) = 0.0175]. A factor >> 0.0175 means the survey is invalid
                 (re-survey), not correctable (REPORT S5).
  depth_shift_search  RMS misfit of observed[i+n] vs expected[i] (bottom->top) for n in a window [verify T13l].
  curvature      second difference of the survey's own D profile [MIKKELSEN-2003 PDF p11-12: depth-positioning error
                 is proportional to curvature]; reported, not converted to a correction (factor UNVERIFIED)."""
from __future__ import annotations

import math
import statistics as st


def interpolate(prev, nxt, w):
    return [None if (a is None or b is None) else a + (b - a) * w for a, b in zip(prev, nxt)]


def rotation_fit(errA, DB):
    pairs = [(e, b) for e, b in zip(errA, DB) if e is not None and b is not None]
    sb = sum(b for _, b in pairs)
    factor = sum(e for e, _ in pairs) / sb if sb else math.nan
    r = st.correlation([e for e, _ in pairs], [b for _, b in pairs]) if len(pairs) > 2 else math.nan
    return {"factor": factor, "r": r, "plausible": abs(factor) < math.sin(math.radians(1.0))}


def depth_shift_search(observed_b2t, expected_b2t, window=(-1, 0, 1)):
    n = len(observed_b2t)

    def rms(sh):
        res = [observed_b2t[i + sh] - expected_b2t[i] for i in range(n)
               if 0 <= i + sh < n and observed_b2t[i + sh] is not None and expected_b2t[i] is not None]
        return math.sqrt(sum(x * x for x in res) / len(res)) if res else math.inf

    scores = {sh: rms(sh) for sh in window}
    best = min(scores, key=scores.get)
    return {"best_shift": best, "rms": scores}


def curvature(D_t2b):
    return [None] + [None if None in (a, b, c) else a - 2 * b + c
                     for a, b, c in zip(D_t2b, D_t2b[1:], D_t2b[2:])] + [None]
