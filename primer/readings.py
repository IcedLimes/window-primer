"""Live readings (statusline and ping responses): de-staling, real windows, plan or limit changes,
and windows you left early.

Every open Claude Code session reports rate limits to the statusline, and an idle session keeps
re-printing its last-known values every 60 s. So only the first appearance of a value is a real
reading; the repeats carry a later timestamp but describe an earlier moment.
"""

from bisect import bisect_left

from .sim import BIN, to_bin

GRID = 600  # the API's window boundaries are multiples of 10 minutes


def live_windows(db, wsec, min_readings=3):
    """Windows seen in readings, oldest first: dicts with start, end (cut short if a later window
    started before it ended — a plan change or reset), resets_at and fresh [(ts, util)] readings."""
    groups = {}
    for ts, util, resets in db.execute(
            "SELECT ts, five_util, five_resets_at FROM observations "
            "WHERE five_resets_at IS NOT NULL AND five_util IS NOT NULL ORDER BY ts"):
        if resets % GRID:  # off-grid values are glitches
            continue
        groups.setdefault(resets, []).append((ts, util))
    starts = sorted(r - wsec for r, rows in groups.items() if len(rows) >= min_readings)
    out = []
    for resets, rows in sorted(groups.items()):
        if len(rows) < min_readings:
            continue
        start = resets - wsec
        later = [s for s in starts if start < s < resets]
        fresh, seen = [], set()
        for ts, util in rows:
            if util not in seen:
                seen.add(util)
                fresh.append((ts, util))
        out.append({"start": start, "end": min(later) if later else resets, "resets_at": resets, "fresh": fresh})
    return out


def limit_changes(db, live, max_snap_s=900):
    """When the plan or limit changed: the weekly counter fell by 20+ points while its scheduled reset
    stayed put — an upgrade, a downgrade or a weekly-limit reset. Each change also starts a window, so
    it's snapped to a window start seen in the readings when one is close."""
    seen, peak, changes = set(), {}, []
    for ts, util, resets in db.execute(
            "SELECT ts, week_util, week_resets_at FROM observations "
            "WHERE week_util IS NOT NULL AND week_resets_at IS NOT NULL ORDER BY ts"):
        if (util, resets) in seen:
            continue
        seen.add((util, resets))
        if resets in peak and peak[resets] - util >= 0.2:
            changes.append(ts)
            peak[resets] = util
        else:
            peak[resets] = max(peak.get(resets, 0.0), util)
    snapped = []
    for ts in changes:
        near = [w["start"] for w in live if abs(w["start"] - ts) <= max_snap_s]
        snapped.append(min(near, key=lambda s: abs(s - ts)) if near else ts // GRID * GRID)
    return sorted(set(snapped))


def budget_estimates(db, hist, live, wsec, min_util=0.15):
    """One estimate per window seen in readings: Claude Code $ up to the moment the window's highest
    utilization was *first* reported, divided by that utilization."""
    out = []
    for w in live:
        util, ts = max(((u, -t) for t, u in w["fresh"]), default=(0, 0))
        ts = -ts
        if util < min_util or ts > hist.now:
            continue
        load = hist.cost_between(w["start"], min(ts, w["end"]))
        if load > 0:
            out.append({"source": "utilization", "ts": ts, "budget": load / util, "util": util})
    return out


def _stop(bins, costs, start_bin, end_bin):
    """Last bin with real activity in [start_bin, end_bin), or None."""
    lo, hi = bisect_left(bins, start_bin), bisect_left(bins, end_bin)
    for i in range(hi - 1, lo - 1, -1):
        if costs[i] > 0:
            return bins[i]
    return None


def _next_activity(bins, costs, from_bin):
    for i in range(bisect_left(bins, from_bin), len(bins)):
        if costs[i] > 0:
            return bins[i]
    return None


def soft_lockouts(hist, live, windows, budget_at, stop_at, min_gap_bins=2, max_resume_bins=3):
    """Windows you left early because the limit was close, without hitting it.

    With live readings: you stopped at least 20 min before the window ended, and the utilization
    when you stopped was at least stop_at.
    From transcripts alone (older windows): the window's load had reached stop_at of that period's
    budget, you stopped 20+ min before its end, AND you came back within 30 min after it reset —
    without that last test a late-night stop at high load is indistinguishable from bedtime.
    `windows` are no-ping replay windows (sim.Window with detail); budget_at(ts) gives the budget then.
    """
    bins, costs = hist.raw
    hard = {to_bin(r) for _, r in hist.hits if r}
    events, covered = [], set()
    now_bin = to_bin(hist.now)
    for w in live:
        sb, eb = to_bin(w["start"]), to_bin(w["end"])
        covered.add(sb)
        if eb > now_bin or eb in hard or to_bin(w["resets_at"]) in hard:
            continue  # still open, or ended in a real limit hit
        stop = _stop(bins, costs, sb, eb)
        if stop is None or eb - stop - 1 < min_gap_bins:
            continue
        util = max((u for t, u in w["fresh"] if t <= (stop + 1) * BIN + 180), default=None)
        if util is not None and util >= stop_at:
            events.append({"source": "readings", "start": sb, "end": eb, "stop": stop, "util": util})
    for w in windows:
        if w.end > now_bin or w.start in covered or any(w.start <= h <= w.end for h in hard):
            continue
        b = budget_at(w.start * BIN)
        if not b or w.load < stop_at * b:
            continue
        stop = _stop(bins, costs, w.start, w.end)
        if stop is None or w.end - stop - 1 < min_gap_bins:
            continue
        back = _next_activity(bins, costs, w.end)
        if back is not None and back - w.end <= max_resume_bins:
            events.append({"source": "transcripts", "start": w.start, "end": w.end, "stop": stop,
                           "util": round(w.load / b, 2)})
    return sorted(events, key=lambda e: e["start"])


def impute_soft(demand, events, cfg, now_bin):
    """Fill the rest of each early-stopped window the way blocked work after a limit hit is filled:
    at impute_lockout_factor of the pace of the 70 min before the stop, for at most 3 h."""
    factor = cfg.get("impute_lockout_factor", 0)
    added = 0.0
    if not factor:
        return added
    cap = int(cfg["impute_lockout_max_hours"] * 3600 // BIN)
    for e in events:
        before = sum(demand.get(b, 0.0) for b in range(e["stop"] - 6, e["stop"] + 1))
        rate = before / 7 * factor
        for b in range(e["stop"] + 1, min(e["end"], e["stop"] + 1 + cap, now_bin)):
            demand[b] = demand.get(b, 0.0) + rate
            added += rate
    return added
