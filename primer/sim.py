"""5-hour window simulator.

Time is measured in absolute bins of BIN seconds (10 min). A window opens at the
first bin with activity or a ping while no window is active, and covers
`wbins` bins. A ping inside an active window does nothing, exactly as in reality.
Floor-to-10-minute window starts fall out of the binning for free.
"""

from dataclasses import dataclass, field
from datetime import datetime

BIN = 600


def to_bin(ts):
    return int(ts // BIN)


def bin_local(b):
    """Naive local datetime of a bin (the OS applies the right DST offset)."""
    return datetime.fromtimestamp(b * BIN)


@dataclass
class Window:
    start: int
    end: int
    load: float = 0.0
    by_ping: bool = False
    bins: list = field(default_factory=list)  # [(bin, cost)] when detail=True


def simulate(act_bins, act_costs, ping_bins, wbins, detail=False):
    """act_bins sorted ascending with matching act_costs; ping_bins sorted ascending."""
    windows = []
    end = -1
    cur = None
    i = j = 0
    n, m = len(act_bins), len(ping_bins)
    while i < n or j < m:
        if j < m and (i >= n or ping_bins[j] < act_bins[i]):
            b = ping_bins[j]
            j += 1
            if b >= end:
                cur = Window(b, b + wbins, by_ping=True)
                windows.append(cur)
                end = cur.end
            continue
        b, c = act_bins[i], act_costs[i]
        i += 1
        if b >= end:
            cur = Window(b, b + wbins)
            windows.append(cur)
            end = cur.end
        cur.load += c
        if detail:
            cur.bins.append((b, c))
    return windows


def crossing_bin(window, budget):
    """First bin at which the window's cumulative load exceeds budget (needs detail=True)."""
    total = 0.0
    for b, c in window.bins:
        total += c
        if total > budget:
            return b
    return None
