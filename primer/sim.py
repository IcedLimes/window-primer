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


def simulate(act_bins, act_costs, ping_bins, wbins, detail=False, forced=(), keepalive=False):
    """act_bins sorted ascending with matching act_costs; ping_bins sorted ascending.

    forced: bins where a new window starts no matter what (a plan change or limit reset replaces
    whatever window is open). keepalive: once the first window opens, every window that ends is
    immediately replaced by the next one, so windows tile time back to back (a ping at each reset).
    """
    windows = []
    end = -1
    cur = None
    forced = sorted(set(forced))
    i = j = k = 0
    n, m, f = len(act_bins), len(ping_bins), len(forced)

    def open_at(b, by_ping):
        nonlocal cur, end
        if keepalive and cur is not None and b >= end:
            b = end + (b - end) // wbins * wbins  # the slot in the back-to-back grid that contains b
            by_ping = True
        cur = Window(b, b + wbins, by_ping=by_ping)
        windows.append(cur)
        end = cur.end

    while i < n or j < m or k < f:
        nxt = min(act_bins[i] if i < n else float("inf"), ping_bins[j] if j < m else float("inf"),
                  forced[k] if k < f else float("inf"))
        if k < f and forced[k] == nxt:
            k += 1
            if cur is None or cur.start != nxt:
                if cur is not None and nxt < end:
                    cur.end = end = nxt  # cut short by the reset
                cur = None  # the grid restarts at a reset
                open_at(nxt, by_ping=False)
            continue
        if j < m and ping_bins[j] == nxt and (i >= n or nxt < act_bins[i]):
            j += 1
            if nxt >= end:
                open_at(nxt, by_ping=True)
            continue
        b, c = act_bins[i], act_costs[i]
        i += 1
        if b >= end:
            open_at(b, by_ping=False)
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
