"""Frame validation; the gate that keeps corrupt serial data out of the dataset."""

from .schema import STREAM_COLUMNS, RANGE_LIMITS

N_FIELDS = len(STREAM_COLUMNS)


class FrameStats:
    """Running tally of frame health for the live status display."""
    def __init__(self):
        self.total = 0
        self.good = 0
        self.bad_shape = 0      # not exactly the current STREAM_COLUMNS shape
        self.bad_range = 0      # value outside physical limits
        self.time_gaps = 0      # non-monotonic / large jump in t_ms
        self._prev_t = None

    @property
    def good_pct(self):
        return 100.0 * self.good / self.total if self.total else 0.0

    def as_text(self):
        return (f"frames {self.total} | good {self.good} ({self.good_pct:0.1f}%) | "
                f"shape-bad {self.bad_shape} | range-bad {self.bad_range} | "
                f"time-gaps {self.time_gaps}")


def parse_and_validate(line, stats):
    """    Parse one raw serial line into a dict of stream values, validating as we go."""
    stats.total += 1
    raw = line.strip()

    # 1) SHAPE: must be exactly N integer fields separated by commas.
    parts = raw.split(",")
    if len(parts) != N_FIELDS:
        stats.bad_shape += 1
        return None, f"shape: {len(parts)} fields != {N_FIELDS}"
    try:
        vals = [int(p) for p in parts]
    except ValueError:
        stats.bad_shape += 1
        return None, "shape: non-integer field"

    frame = dict(zip(STREAM_COLUMNS, vals))

    # 2) RANGE: physical sanity (datasheet-grounded limits).
    for col, (lo, hi) in RANGE_LIMITS.items():
        if not (lo <= frame[col] <= hi):
            stats.bad_range += 1
            return None, f"range: {col}={frame[col]} outside [{lo},{hi}]"

    # 3) TIME: t_ms should increase in ~100 ms steps; flag dropped frames.
    t = frame["t_ms"]
    if stats._prev_t is not None:
        dt = t - stats._prev_t
        if dt <= 0 or dt > 500:
            stats.time_gaps += 1
    stats._prev_t = t

    stats.good += 1
    return frame, None
