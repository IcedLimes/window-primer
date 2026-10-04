"""Calibrate the window budget, choose ping times per weekday, and backtest them."""

import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import readings
from .sim import BIN, bin_local, crossing_bin, simulate, to_bin

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PING_OFFSET_S = 60  # fire 1 min into the slot so the API's 10-min floor lands on the slot


@dataclass
class History:
    act_bins: list            # activity the simulator replays (observed + imputed lockout demand)
    act_costs: list
    first_day: object
    last_day: object
    now: float
    externals: list = field(default_factory=list)
    hits: list = field(default_factory=list)  # [(hit_ts, reset_ts)] session limit hits
    raw: tuple = None         # (bins, costs) actually observed; calibration must not see imputed demand
    imputed: float = 0.0
    live: list = field(default_factory=list)     # windows seen in live readings
    changes: list = field(default_factory=list)  # plan / limit changes (epoch s); each starts a window
    soft: list = field(default_factory=list)     # windows left early near the limit
    soft_imputed: float = 0.0

    @property
    def forced(self):
        return [to_bin(c) for c in self.changes]

    def cost_between(self, t0, t1):
        bins, costs = self.raw or (self.act_bins, self.act_costs)
        lo, hi = bisect_left(bins, to_bin(t0)), bisect_right(bins, to_bin(t1))
        return sum(costs[lo:hi])

    def without(self, b0, b1):
        """Copy with activity in bins [b0, b1) removed (for cross-validation)."""
        keep = [(b, c) for b, c in zip(self.act_bins, self.act_costs) if not b0 <= b < b1]
        return History([b for b, _ in keep], [c for _, c in keep], self.first_day, self.last_day,
                       self.now, self.externals, self.hits, self.raw, self.imputed, self.live,
                       self.changes, self.soft, self.soft_imputed)


def load_history(db, cfg, now=None):
    now = now or time.time()
    since = now - cfg["lookback_days"] * 86400
    wsec = cfg["window_hours"] * 3600
    demand = {}
    for ts, cost in db.execute("SELECT ts, cost FROM events WHERE ts BETWEEN ? AND ?", (since, now)):
        b = to_bin(ts)
        demand[b] = demand.get(b, 0.0) + cost

    # Claude Code can print the limit message several times for one lockout; keep the first per window.
    hits, seen = [], set()
    for r in db.execute("SELECT hit_ts, reset_ts FROM limit_hits WHERE kind='session' AND hit_ts BETWEEN ? AND ? "
                        "ORDER BY hit_ts", (since, now)):
        key = to_bin(r["reset_ts"]) if r["reset_ts"] else r["hit_ts"]
        if key not in seen:
            seen.add(key)
            hits.append((r["hit_ts"], r["reset_ts"]))
    live = readings.live_windows(db, wsec)
    changes = readings.limit_changes(db, live)
    if cfg.get("budget_since"):
        changes = sorted(set(changes) | {_parse_local(cfg["budget_since"])})
    changes = [c for c in changes if since <= c <= now]
    # Windows we know started (from limit hits / live observations) but with no Claude Code
    # activity at that moment were opened elsewhere (claude.ai, phone). Replay them as zero-cost activity.
    starts = {to_bin(r - wsec) for _, r in hits if r} | {to_bin(w["start"]) for w in live}
    externals = []
    for s in sorted(starts):
        if to_bin(since) <= s <= to_bin(now) and s not in demand:
            demand[s] = 0.0
            externals.append(s)

    raw_bins = sorted(demand)
    raw = (raw_bins, [demand[b] for b in raw_bins])
    imputed = impute_lockouts(demand, hits, cfg, now)
    bins = sorted(demand)
    return History(bins, [demand[b] for b in bins], datetime.fromtimestamp(since).date(),
                   datetime.fromtimestamp(now).date(), now, externals, hits, raw, imputed, live, changes)


def _parse_local(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value)).timestamp()


def apply_soft_lockouts(hist, budget, cfg):
    """Find windows left early near the limit (see readings.soft_lockouts) and fill in the work they
    displaced, so the optimiser sees them as the lockouts they effectively were."""
    wsec = cfg["window_hours"] * 3600
    # Replay without pings, pinning every window start we know for sure.
    known = set(hist.forced) | {to_bin(r - wsec) for _, r in hist.hits if r} | {to_bin(w["start"]) for w in hist.live}
    bins, costs = hist.raw
    windows = simulate(bins, costs, [], int(wsec // BIN), detail=True, forced=sorted(known))
    events = readings.soft_lockouts(hist, hist.live, windows, budget.at, cfg["stop_at"])
    demand = dict(zip(hist.act_bins, hist.act_costs))
    added = readings.impute_soft(demand, events, cfg, to_bin(hist.now))
    hist.act_bins = sorted(demand)
    hist.act_costs = [demand[b] for b in hist.act_bins]
    hist.soft, hist.soft_imputed = events, added
    return events


def impute_lockouts(demand, hits, cfg, now):
    """During a real lockout you couldn't work, so the transcripts show nothing — and the optimiser
    would conclude those hours don't matter. Fill them at a fraction of the pace of the hour before
    the hit. Returns the $ added."""
    factor = cfg.get("impute_lockout_factor", 0)
    if not factor:
        return 0.0
    added = 0.0
    for hit_ts, reset_ts in hits:
        if not reset_ts:
            continue
        hb = to_bin(hit_ts)
        before = sum(demand.get(b, 0.0) for b in range(hb - 6, hb + 1))
        rate = before / 7 * factor
        end = min(to_bin(reset_ts), hb + 1 + int(cfg["impute_lockout_max_hours"] * 3600 // BIN), to_bin(now))
        for b in range(hb + 1, end):
            demand[b] = demand.get(b, 0.0) + rate
            added += rate
    return added


@dataclass
class Budget:
    """Per-window budget in API-equivalent USD. `dist` is what the planner hedges over;
    `point` (weighted median) is what gets displayed and replayed."""
    point: float = None
    dist: list = field(default_factory=list)  # [(value, probability)]; empty = unknown
    source: str = ""
    estimates: list = field(default_factory=list)
    since: float = None                         # estimates before this (a plan change) are ignored
    periods: list = field(default_factory=list)  # [(from_ts, budget)] for each limit period

    def at(self, ts):
        """Budget in force at ts (older periods matter when judging old windows)."""
        value = self.point
        for start, b in self.periods:
            if ts >= start and b:
                value = b
        return value

    def scaled(self, factor):
        """The budget you actually plan against when you stop at `factor` of the real limit."""
        if not self.dist:
            return self
        return Budget(self.point * factor, [(b * factor, p) for b, p in self.dist], self.source,
                      self.estimates, self.since, [(t, b * factor) for t, b in self.periods])


def calibrate(db, hist, cfg):
    """Estimate the budget from limit hits and live utilization readings, using only data since the
    latest plan or limit change (older periods are kept separately for judging old windows)."""
    if cfg.get("budget_override_usd"):
        b = float(cfg["budget_override_usd"])
        return Budget(b, [(b, 1.0)], "config override")
    wsec = cfg["window_hours"] * 3600
    estimates = []
    for hit_ts, reset_ts in hist.hits:
        if reset_ts:
            load = hist.cost_between(reset_ts - wsec, hit_ts)
            if load > 0:
                estimates.append({"source": "limit hit", "ts": hit_ts, "budget": load})
    estimates += readings.budget_estimates(db, hist, hist.live, wsec)
    if not estimates:
        return Budget(source="uncalibrated (no limit hits or utilization readings yet)")
    # Limits and model mix drift, so recent estimates count more; utilization readings know the exact
    # window start, so they count double. Rather than trust one number, the planner hedges across all
    # of them (usage outside Claude Code makes estimates read low, which only errs toward pinging).
    hl = cfg["budget_half_life_days"] * 86400
    for e in estimates:
        e["weight"] = 0.5 ** (max(hist.now - e["ts"], 0) / hl) * (2 if e["source"] == "utilization" else 1)
    bounds = [None] + sorted(hist.changes)
    periods = []
    for i, start in enumerate(bounds):
        end = bounds[i + 1] if i + 1 < len(bounds) else float("inf")
        group = [(e["budget"], e["weight"]) for e in estimates if (start is None or e["ts"] >= start) and e["ts"] < end]
        periods.append((start if start is not None else float("-inf"), weighted_quantile(group, 0.5) if group else None))
    since = hist.changes[-1] if hist.changes else None
    current = [e for e in estimates if since is None or e["ts"] >= since]
    note = ""
    if since is not None:
        note = f", since the limit change on {datetime.fromtimestamp(since):%a %m-%d %H:%M}"
        if not current:
            current, note = estimates, note + " (no readings since it yet; using older ones)"
    pairs = [(e["budget"], e["weight"]) for e in current]
    total = sum(w for _, w in pairs)
    if len(pairs) > 12:
        dist = [(weighted_quantile(pairs, (i + 0.5) / 12), 1 / 12) for i in range(12)]
    else:
        dist = [(v, w / total) for v, w in pairs]
    lo, hi = min(v for v, _ in pairs), max(v for v, _ in pairs)
    spread = f", range ${lo:.0f}–${hi:.0f}" if hi > lo * 1.25 else ""
    point = weighted_quantile(pairs, 0.5)
    for e in estimates:
        e["current"] = since is None or e["ts"] >= since
    return Budget(point, dist, f"recency-weighted median of {len(current)} estimate(s){spread}{note}",
                  estimates, since, [(t, b if b else point) for t, b in periods])


def weighted_quantile(pairs, q):
    pairs = sorted(pairs)
    target = sum(w for _, w in pairs) * q
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= target:
            return value
    return pairs[-1][0]


def slot_label(slot, bin_minutes=10):
    minutes = slot * bin_minutes + PING_OFFSET_S // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def label_slot(label, bin_minutes=10):
    h, m = map(int, label.split(":"))
    return (h * 60 + m - PING_OFFSET_S // 60) // bin_minutes


def ping_bins(schedule, first_day, last_day, now, bin_minutes=10):
    out = []
    day = first_day
    while day <= last_day:
        for slot in schedule.get(day.weekday(), ()):
            ts = datetime.combine(day, datetime.min.time()).timestamp() + slot * bin_minutes * 60 + PING_OFFSET_S
            if ts <= now:
                out.append(to_bin(ts))
        day += timedelta(days=1)
    out.sort()
    return out


class Scorer:
    """Objective = (main, tiebreak). main is the expected overflow past the budget plus a lighter
    penalty past margin·budget, averaged over the budget distribution; with no budget it's Σ load²
    (keep windows balanced)."""

    def __init__(self, hist, budget, cfg, weighted=True):
        self.hist, self.budget, self.cfg = hist, budget, cfg
        self.wbins = int(cfg["window_hours"] * 3600 // BIN)
        self.weighted = weighted
        self._wd = {}
        self._totals = {}
        self._weights = {}
        self.now_bin = to_bin(hist.now)
        self.hl_bins = cfg["half_life_days"] * 86400 / BIN
        # Local midnight of every day in the history, by weekday (computed once; DST-correct).
        self.midnights = [[] for _ in range(7)]
        day = hist.first_day
        while day <= hist.last_day:
            self.midnights[day.weekday()].append(datetime.combine(day, datetime.min.time()).timestamp())
            day += timedelta(days=1)

    def ping_bins(self, schedule):
        step = self.cfg["bin_minutes"] * 60
        out = [to_bin(m + slot * step + PING_OFFSET_S)
               for wd, slots in schedule.items() for slot in slots for m in self.midnights[wd]
               if m + slot * step + PING_OFFSET_S <= self.hist.now]
        out.sort()
        return out

    def weekday(self, b):
        wd = self._wd.get(b)
        if wd is None:
            wd = self._wd[b] = bin_local(b).weekday()
        return wd

    def windows(self, schedule, detail=False, keepalive_from=None):
        pings = self.ping_bins(schedule)
        if keepalive_from is not None:
            pings = sorted(pings + [keepalive_from])
        return simulate(self.hist.act_bins, self.hist.act_costs, pings, self.wbins, detail,
                        forced=self.hist.forced, keepalive=keepalive_from is not None)

    def window_score(self, w):
        return self.score([w])

    def score(self, windows, select=None):
        # Hot path (tens of thousands of calls per plan): most windows sit below every margin, so
        # they only add to the tie-break; the per-budget sum runs only for the rest.
        dist = self.budget.dist
        if dist and not hasattr(self, "_floor"):
            m = self.cfg["margin"]
            self._floor = m * min(B for B, _ in dist)
            self._terms = [(B, m * B, p, p * self.cfg["margin_weight"]) for B, p in dist]
        weights = self._weights if self.weighted else None
        main = tie = 0.0
        for w in windows:
            if select is not None and not select(w):
                continue
            if weights is None:
                wt = 1.0
            else:
                wt = weights.get(w.start)
                if wt is None:
                    wt = weights[w.start] = 0.5 ** ((self.now_bin - w.start) / self.hl_bins)
            L = w.load
            if not dist:
                main += wt * L * L
                continue
            tie += wt * L * L
            if L > self._floor:
                main += wt * sum(p * (L - B if L > B else 0.0) + pm * (L - mB) for B, mB, p, pm in self._terms if L > mB)
        if dist:
            tie /= self.budget.point
        return main, tie

    def total(self, schedule):
        key = tuple(tuple(schedule.get(wd, ())) for wd in range(7))
        hit = self._totals.get(key)
        if hit is None:
            hit = self._totals[key] = self.score(self.windows(schedule))
        return hit

    def weekday_score(self, schedule, wd):
        return self.score(self.windows(schedule), lambda w: self.weekday(w.start) == wd)


def _smooth(values, radius):
    """Triangular-kernel average over neighbouring slots (wrapping midnight). Your start time
    wobbles from week to week, so prefer a slot whose neighbours are also good."""
    if radius <= 0:
        return values
    n = len(values)
    weights = [radius + 1 - abs(k) for k in range(-radius, radius + 1)]
    total = sum(weights)
    out = []
    for i in range(n):
        main = sum(w * values[(i + k) % n][0] for w, k in zip(weights, range(-radius, radius + 1))) / total
        tie = sum(w * values[(i + k) % n][1] for w, k in zip(weights, range(-radius, radius + 1))) / total
        out.append((main, tie))
    return out


def _better(a, b, tol):
    return a[0] < b[0] - tol or (abs(a[0] - b[0]) <= tol and a[1] < b[1] - 1e-12)


def weekday_day_counts(hist):
    counts = [0] * 7
    day = hist.first_day
    while day <= hist.last_day:
        counts[day.weekday()] += 1
        day += timedelta(days=1)
    return counts


def optimize(hist, budget, cfg, weighted=True):
    """Pick ping slots.

    mode 'global'  — same times every day: ~35 days behind each decision, robust.
    mode 'weekday' — each weekday on its own: ~5 days behind each decision, overfits.
    mode 'pooled'  — empirical-Bayes partial pooling: each weekday's gain curve is shrunk toward
                     the all-days curve with weight κ/(n+κ), so a weekday moves away from the
                     shared times only as far as its own n days justify (κ = pool_kappa days).
    """
    sc = Scorer(hist, budget, cfg, weighted)
    per_hour = 60 // cfg["bin_minutes"]
    all_slots = list(range(24 * per_hour))
    lo, hi = cfg["ping_hours"]
    allowed = set(range(int(lo * per_hour), int(hi * per_hour)))
    radius = cfg.get("smooth_slots", 0)
    mode = cfg.get("mode", "global")
    kappa = cfg.get("pool_kappa", 5.0) if mode == "pooled" else 0.0
    counts = weekday_day_counts(hist)
    schedule = {wd: [] for wd in range(7)}
    if not hist.act_bins:
        return schedule
    tol = 1e-9

    def with_slot(base_of, days, s):
        trial = dict(schedule)
        for wd in days:
            trial[wd] = sorted(set(base_of(wd)) | ({s} if s is not None else set()))
        return trial

    def gain_curve(base_of, days):
        """Change in the total objective from adding slot s on `days`, for every slot."""
        base = sc.total(with_slot(base_of, days, None))
        return [(0.0, 0.0) if all(s in base_of(wd) for wd in days) else
                tuple(x - y for x, y in zip(sc.total(with_slot(base_of, days, s)), base)) for s in all_slots]

    def pick(gains, base_of, days, baseline_main):
        gains = _smooth(gains, radius)
        best, best_val = None, (0.0, 0.0)
        for s in all_slots:
            if s in allowed and not any(s in base_of(wd) for wd in days) and _better(gains[s], best_val, tol):
                best, best_val = s, gains[s]
        if best is not None and -best_val[0] > tol and -best_val[0] >= cfg["min_gain_frac"] * baseline_main:
            return best
        return None

    def stage(base_of, eligible):
        """One round of adding at most one ping per group on top of base_of(wd), on `eligible` days."""
        if not eligible:
            return
        if mode == "global":
            s = pick(gain_curve(base_of, eligible), base_of, eligible,
                     sc.total(with_slot(base_of, eligible, None))[0])
            if s is not None:
                for wd in eligible:
                    schedule[wd] = sorted(set(base_of(wd)) | {s})
            return
        prior = gain_curve(base_of, eligible) if kappa else None
        all_main = sc.total(with_slot(base_of, eligible, None))[0] if kappa else 0.0
        for _ in range(2):  # coordinate descent: a ping can shift windows into the next day
            changed = False
            for wd in eligible:
                own = gain_curve(base_of, (wd,))
                baseline = sc.weekday_score(with_slot(base_of, (wd,), None), wd)[0]
                if kappa:
                    w = kappa / (counts[wd] + kappa)
                    n = len(eligible)
                    own = [((1 - w) * g[0] + w * p[0] / n, (1 - w) * g[1] + w * p[1] / n) for g, p in zip(own, prior)]
                    baseline = (1 - w) * baseline + w * all_main / n
                s = pick(own, base_of, (wd,), baseline)
                new = sorted(set(base_of(wd)) | ({s} if s is not None else set()))
                changed |= new != schedule[wd]
                schedule[wd] = new
            if not changed:
                break

    stage(lambda wd: [], tuple(range(7)))
    for _ in range(cfg["max_pings_per_day"] - 1):
        current = {wd: list(v) for wd, v in schedule.items()}
        stage(lambda wd, current=current: current[wd], tuple(wd for wd in range(7) if current[wd]))
    if mode != "pooled":
        # Drop pings that made no measurable difference on a weekday (e.g. a light Friday).
        for wd in range(7):
            if schedule[wd]:
                trial = dict(schedule)
                trial[wd] = []
                if sc.total(trial)[0] <= sc.total(schedule)[0] + tol:
                    schedule[wd] = []
    return schedule


def backtest(hist, budget, cfg, schedule, select=None, keepalive_from=None):
    """Unweighted replay of history under a schedule (or keep-alive pings from a first ping)."""
    sc = Scorer(hist, budget, cfg, weighted=False)
    wins = [w for w in sc.windows(schedule, detail=True, keepalive_from=keepalive_from)
            if select is None or select(w)]
    out = {"windows": len(wins), "peak_load": max((w.load for w in wins), default=0.0),
           "ping_opened": sum(w.by_ping for w in wins)}
    budget = budget.point
    if budget:
        over = [w for w in wins if w.load > budget]
        lockout = 0
        for w in over:
            cross = crossing_bin(w, budget)
            last = w.bins[-1][0] if w.bins else cross
            lockout += max(0, min(w.end, last + 1) - cross) * cfg["bin_minutes"]
        out.update(hits=len(over), overflow=sum(w.load - budget for w in over), lockout_min=lockout,
                   over_hits=[bin_local(w.start).isoformat(" ", "minutes") for w in over])
    else:
        out["sum_sq"] = sum(w.load ** 2 for w in wins)
    return out


def cross_validate(hist, budget, cfg):
    """Leave-one-week-out: plan without week k, evaluate on week k only."""
    now_bin = to_bin(hist.now)
    week = 7 * 86400 // BIN
    nweeks = max(1, int(cfg["lookback_days"] // 7))
    sc = Scorer(hist, budget, cfg, weighted=False)
    agg = {"baseline": {}, "planned": {}}
    for k in range(nweeks):
        b1 = now_bin - k * week
        b0 = b1 - week
        sched = optimize(hist.without(b0, b1), budget, cfg)
        sel = lambda w, b0=b0, b1=b1: b0 <= w.start < b1  # noqa: E731
        for name, s in (("baseline", {wd: [] for wd in range(7)}), ("planned", sched)):
            res = backtest(hist, budget, cfg, s, sel)
            res["main"] = sc.score(sc.windows(s), sel)[0]
            for key in ("main", "hits", "overflow", "lockout_min", "sum_sq"):
                if key in res:
                    agg[name][key] = agg[name].get(key, 0) + res[key]
    agg["weeks"] = nweeks
    return agg


VARIANTS = [("global", 1), ("global", 2), ("pooled", 1), ("pooled", 2), ("weekday", 1), ("weekday", 2)]


def choose_variant(hist, budget, cfg):
    """Cross-validate each schedule shape; keep the simplest one within 1% of the best held-out score,
    and only if it beats not pinging at all by min_gain_frac."""
    rows = []
    for mode, pings in VARIANTS:
        cv = cross_validate(hist, budget, dict(cfg, mode=mode, max_pings_per_day=min(pings, cfg["max_pings_per_day"])))
        rows.append({"mode": mode, "pings": pings, **cv})
    baseline = rows[0]["baseline"]["main"]
    best_main = min(r["planned"]["main"] for r in rows)
    pick = next(r for r in rows if r["planned"]["main"] <= best_main * 1.01 + 1e-9)
    if baseline <= 0 or baseline - pick["planned"]["main"] < cfg["min_gain_frac"] * baseline:
        return None, rows
    return pick, rows


def weekday_profile(hist, cfg, per_hour=2):
    """Recency-weighted mean $ per (weekday, time-of-day cell) and the weight of each weekday observed."""
    hl = cfg["half_life_days"] * 86400
    cells = 24 * per_hour
    grid = [[0.0] * cells for _ in range(7)]
    weight_by_day = {}
    day = hist.first_day
    while day <= hist.last_day:
        age = hist.now - datetime.combine(day, datetime.min.time()).timestamp()
        weight_by_day[day] = 0.5 ** (max(age, 0) / hl)
        day += timedelta(days=1)
    for b, c in zip(hist.act_bins, hist.act_costs):
        dt = bin_local(b)
        grid[dt.weekday()][dt.hour * per_hour + dt.minute * per_hour // 60] += c * weight_by_day.get(dt.date(), 0.0)
    norm = [0.0] * 7
    count = [0] * 7
    for d, w in weight_by_day.items():
        norm[d.weekday()] += w
        count[d.weekday()] += 1
    return [[v / norm[wd] if norm[wd] else 0.0 for v in row] for wd, row in enumerate(grid)], count


def keepalive_compare(hist, budget, cfg):
    """A ping at every window start (windows back to back, 24/7). Its reset times drift an hour a day
    (24 h isn't a multiple of 5 h), so its result depends on where the chain started: try all 30
    ten-minute starting points. Nothing is tuned, so in-sample is also out-of-sample."""
    if not budget.point or not hist.act_bins:
        return None
    first = to_bin(datetime.combine(hist.first_day, datetime.min.time()).timestamp())
    runs = []
    none = {wd: [] for wd in range(7)}
    for phase in range(int(cfg["window_hours"] * 3600 // BIN)):
        r = backtest(hist, budget, cfg, none, keepalive_from=first + phase)
        runs.append(r)
    lock = sorted(r["lockout_min"] for r in runs)
    # A chain sends a ping at every reset, used or not: 24 / 5 a day.
    return {"lockout_mean": sum(lock) / len(lock), "lockout_min": lock[0], "lockout_max": lock[-1],
            "hits_mean": sum(r["hits"] for r in runs) / len(runs),
            "pings_per_day": 24 / cfg["window_hours"],
            "aligned_by_change": bool(hist.changes) and lock[0] == lock[-1]}


def _period_check(hist, budget, cfg):
    """Replay vs reality within the current limit period: predicted vs actual limit hits."""
    since = hist.changes[-1] if hist.changes else None
    none = {wd: [] for wd in range(7)}
    predicted = backtest(hist, budget, cfg, none, lambda w: since is None or w.start * BIN >= since).get("hits", 0)
    actual = sum(1 for h, _ in hist.hits if since is None or h >= since)
    return {"since": since, "predicted": predicted, "actual": actual}


def plan(db, cfg, now=None):
    hist = load_history(db, cfg, now)
    real_budget = calibrate(db, hist, cfg)
    if real_budget.point:
        apply_soft_lockouts(hist, real_budget, cfg)
    # You stop short of the limit, so plan against the budget you actually use.
    budget = real_budget.scaled(cfg["stop_at"])
    none = {wd: [] for wd in range(7)}
    if cfg["mode"] == "auto":
        variant, cv_rows = choose_variant(hist, budget, cfg)
        if variant:
            schedule = optimize(hist, budget, dict(cfg, mode=variant["mode"], max_pings_per_day=variant["pings"]))
            reason = f"{variant['mode']} schedule, up to {variant['pings']} ping(s)/day won cross-validation"
        else:
            schedule = none
            reason = "no schedule beat 'no pings' on held-out weeks, so none are scheduled"
    else:
        cv_rows = [{"mode": cfg["mode"], "pings": cfg["max_pings_per_day"], **cross_validate(hist, budget, cfg)}]
        schedule = optimize(hist, budget, cfg)
        reason = f"{cfg['mode']} schedule (forced by config)"
    result = {
        "generated_at": hist.now,
        "lookback": [hist.first_day.isoformat(), hist.last_day.isoformat()],
        "active_bins": len(hist.act_bins) - len(hist.externals),
        "external_starts": len(hist.externals),
        "limit_hits": len(hist.hits),
        "budget_usd": real_budget.point,
        "budget_source": real_budget.source,
        "budget_estimates": real_budget.estimates,
        "stop_at": cfg["stop_at"],
        "limit_changes": hist.changes,
        "soft_lockouts": hist.soft,
        "soft_imputed_usd": hist.soft_imputed,
        "keepalive": keepalive_compare(hist, budget, cfg),
        "period_check": _period_check(hist, budget, cfg),
        "reason": reason,
        "schedule": {WEEKDAYS[wd]: [slot_label(s, cfg["bin_minutes"]) for s in slots]
                     for wd, slots in schedule.items()},
        "baseline": backtest(hist, budget, cfg, none),
        "planned": backtest(hist, budget, cfg, schedule),
        "cv": cv_rows,
        "real_world": real_world(db, hist),
        "imputed_usd": hist.imputed,
    }
    return result, hist


def upcoming_pings(plan_schedule, now=None, days=7, bin_minutes=10, skip=()):
    now = now or time.time()
    out = []
    today = datetime.fromtimestamp(now).date()
    for i in range(days + 1):
        day = today + timedelta(days=i)
        if day.isoformat() in skip:
            continue
        for label in plan_schedule.get(WEEKDAYS[day.weekday()], []):
            h, m = map(int, label.split(":"))
            dt = datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=m)
            if dt.timestamp() > now:
                out.append(dt)
    return sorted(out)


RESET_COOLDOWN_S = 7 * 86400  # /limit-reset is rationed to roughly once a week


def reset_advice(five_util, five_resets_at, week_util, last_reset_ts, now, min_saving_min=45):
    """Minutes a /limit-reset would save right now, if it's worth spending, else None.
    Worth it when the window is (nearly) exhausted with a long wait left, the weekly limit has room
    (a reset draws on it), and no reset was used in the last week."""
    if five_util is None or five_util < 0.9 or not five_resets_at:
        return None
    remaining = (five_resets_at - now) / 60
    if remaining < min_saving_min:
        return None
    if week_util is not None and week_util >= 0.9:
        return None
    if last_reset_ts and now - last_reset_ts < RESET_COOLDOWN_S:
        return None
    return remaining


def real_world(db, hist):
    """Actual limit hits and lockout time before vs since scheduled pings started opening windows,
    within one plan/limit period (an upgrade in between would swamp any effect of the pings)."""
    row = db.execute("SELECT min(ts) FROM pings WHERE scheduled IN ('timer', 'cloud') AND outcome = 'opened'").fetchone()
    start = row[0] if row else None
    out = {"pings_started": start}
    if not start:
        return out
    period = max([datetime.combine(hist.first_day, datetime.min.time()).timestamp()]
                 + [c for c in hist.changes if c <= start])
    end = min([hist.now] + [c for c in hist.changes if c > start])
    for name, t0, t1 in (("before", period, start), ("since", start, end)):
        hits = [(h, r) for h, r in hist.hits if t0 <= h < t1 and r]
        days = max((t1 - t0) / 86400, 1e-9)
        out[name] = {"days": days, "hits": len(hits), "hits_per_week": len(hits) / days * 7,
                     "lockout_min_per_week": sum((r - h) / 60 for h, r in hits) / days * 7}
    out["opened"] = db.execute("SELECT count(*) FROM pings WHERE scheduled IN ('timer', 'cloud') "
                               "AND outcome = 'opened'").fetchone()[0]
    return out
    since = datetime.combine(hist.first_day, datetime.min.time()).timestamp()
    for name, t0, t1 in (("before", since, start), ("since", start, hist.now)):
        hits = [(h, r) for h, r in hist.hits if t0 <= h < t1 and r]
        days = max((t1 - t0) / 86400, 1e-9)
        out[name] = {"days": days, "hits": len(hits),
                     "hits_per_week": len(hits) / days * 7,
                     "lockout_min_per_week": sum((r - h) / 60 for h, r in hits) / days * 7}
    out["opened"] = db.execute("SELECT count(*) FROM pings WHERE scheduled IN ('timer', 'cloud') "
                               "AND outcome = 'opened'").fetchone()[0]
    return out
