"""Plain-text report and status output."""

import time
from datetime import datetime

from .planner import WEEKDAYS, upcoming_pings, weekday_profile

LEVELS = " ·▁▂▃▄▅▆▇█"


def _dur(minutes):
    minutes = int(round(minutes))
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%a %m-%d %H:%M")


def heatmap(hist, cfg, schedule, per_hour=2):
    grid, counts = weekday_profile(hist, cfg, per_hour)
    peak = max((v for row in grid for v in row), default=0) or 1
    cells = 24 * per_hour
    lines = ["            " + "".join(f"{h:02d}".ljust(3 * per_hour) for h in range(0, 24, 3))]
    for wd, row in enumerate(grid):
        bar = []
        for i, v in enumerate(row):
            bar.append(LEVELS[0] if v <= 0 else LEVELS[min(len(LEVELS) - 1, 1 + int(v / peak * (len(LEVELS) - 2) + 0.5))])
        marks = [" "] * cells
        for label in schedule.get(WEEKDAYS[wd], []):
            h, m = map(int, label.split(":"))
            marks[(h * 60 + m) * per_hour // 60] = "▲"
        lines.append(f"{WEEKDAYS[wd]} ({counts[wd]}d)  {''.join(bar)}")
        if any(m != " " for m in marks):
            lines.append(f"{'':11} {''.join(marks)}  ping {', '.join(schedule[WEEKDAYS[wd]])}")
    lines.append(f"{'':11} █ ≈ ${peak:.2f} per {60 // per_hour} min (recency-weighted average); ▲ = planned ping")
    return "\n".join(lines)


def _bt_rows(base, planned, budget):
    rows = []
    if budget:
        rows += [("windows over budget", base["hits"], planned["hits"]),
                 ("est. time locked out", _dur(base["lockout_min"]), _dur(planned["lockout_min"])),
                 ("overflow ($)", f"{base['overflow']:.0f}", f"{planned['overflow']:.0f}")]
    rows += [("peak window load ($)", f"{base['peak_load']:.1f}", f"{planned['peak_load']:.1f}"),
             ("windows", base["windows"], planned["windows"]),
             ("…opened by a ping", "-", planned["ping_opened"])]
    return rows


def report(result, hist, cfg):
    out = []
    b0, b1 = result["lookback"]
    out.append(f"window-primer report — history {b0} → {b1} "
               f"({result['active_bins'] * cfg['bin_minutes'] / 60:.0f}h of activity, "
               f"{result['limit_hits']} session-limit hit(s))\n")
    out.append(heatmap(hist, cfg, result["schedule"]))
    out.append("")
    budget = result["budget_usd"]
    if budget:
        out.append(f"Budget  ≈ ${budget:.1f} API-equivalent per {cfg['window_hours']}h window "
                   f"[{result['budget_source']}]")
        for e in sorted(result["budget_estimates"], key=lambda e: e["ts"]):
            out.append(f"         {e['source']:<12} {_when(e['ts'])}  ${e['budget']:.1f}")
    else:
        out.append(f"Budget  unknown — {result['budget_source']}; planning to balance load across windows")
    out.append("")
    out.append(f"Plan    {result['reason']}")
    sched = {d: t for d, t in result["schedule"].items() if t}
    out.append("        " + ("  ".join(f"{d} {', '.join(t)}" for d, t in sched.items()) if sched else "no pings"))
    out.append("")
    out.append(f"Replay of the last {cfg['lookback_days']} days     no pings   with plan")
    for name, a, b in _bt_rows(result["baseline"], result["planned"], budget):
        out.append(f"  {name:<28} {str(a):>9}   {str(b):>9}")
    if budget:
        predicted = result["baseline"]["hits"]
        out.append(f"  Sanity check: at ${budget:.0f} the replay predicts {predicted} limit hit(s); "
                   f"you actually had {result['limit_hits']}."
                   + ("  Big gap → the limit has probably changed; fresh statusline readings will settle it."
                      if abs(predicted - result["limit_hits"]) > max(2, result["limit_hits"] / 2) else ""))
    out.append("")
    out.append("Held-out weeks (plan without that week, test on it) — what to actually expect:")
    for r in result["cv"]:
        b, p = r["baseline"], r["planned"]
        if budget:
            out.append(f"  {r['mode']:<7} ≤{r['pings']} ping/day   lockout {_dur(b['lockout_min'])} → {_dur(p['lockout_min'])}"
                       f"   over-budget windows {b['hits']} → {p['hits']}")
        else:
            out.append(f"  {r['mode']:<7} ≤{r['pings']} ping/day   Σload² {b['sum_sq']:.0f} → {p['sum_sq']:.0f}")
    out.append("\nCaveats: claude.ai/phone usage is only visible through limit hits and live readings; the replay "
               "can't see work you would have done during real lockouts, so gains are understated.")
    return "\n".join(out)


def status(db, plan, cfg, timer_line=None):
    now = time.time()
    out = []
    obs = db.execute("SELECT * FROM observations ORDER BY ts DESC LIMIT 1").fetchone()
    if obs:
        age = (now - obs["ts"]) / 60
        if obs["five_resets_at"] and obs["five_resets_at"] > now:
            out.append(f"5-hour window: open, {round((obs['five_util'] or 0) * 100)}% used, "
                       f"resets {datetime.fromtimestamp(obs['five_resets_at']):%a %H:%M}  "
                       f"(reading {_dur(age)} old)")
        else:
            out.append(f"5-hour window: none open as of last reading ({_dur(age)} ago)")
        if obs["week_util"] is not None:
            out.append(f"7-day limit:   {round(obs['week_util'] * 100)}% used, resets "
                       f"{datetime.fromtimestamp(obs['week_resets_at']):%a %m-%d %H:%M}"
                       + ("  ← pings can't help with this one" if obs["week_util"] >= 0.8 else ""))
    else:
        out.append("No live readings yet (they come from the statusline and from pings).")
    if plan:
        upcoming = upcoming_pings(plan["schedule"], now, days=7)
        out.append(f"Plan from {_when(plan['generated_at'])}: {plan['reason']}")
        out.append("Next pings:    " + (", ".join(dt.strftime("%a %H:%M") for dt in upcoming[:6]) or "none scheduled"))
        if plan.get("budget_usd"):
            out.append(f"Budget:        ≈ ${plan['budget_usd']:.1f}/window ({plan['budget_source']})")
    else:
        out.append("No plan yet — run `primer refresh`.")
    if timer_line:
        out.append(f"systemd:       {timer_line}")
    pings = db.execute("SELECT * FROM pings ORDER BY ts DESC LIMIT 6").fetchall()
    if pings:
        out.append("Recent pings:")
        for p in pings:
            out.append(f"  {_when(p['ts'])}  {p['outcome']:<13} {p['detail'] or ''}")
    return "\n".join(out)
