"""Plain-text report and status output."""

import time
from datetime import datetime

from .planner import WEEKDAYS, reset_advice, upcoming_pings, weekday_profile

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
        for c in result.get("limit_changes") or []:
            out.append(f"         plan/limit change detected {_when(c)} — older estimates (·) don't count")
        for e in sorted(result["budget_estimates"], key=lambda e: e["ts"]):
            mark = " " if e.get("current", True) else "·"
            out.append(f"       {mark} {e['source']:<12} {_when(e['ts'])}  ${e['budget']:.1f}")
        stop_at = result.get("stop_at", 1.0)
        soft = result.get("soft_lockouts") or []
        out.append(f"        You stop at ~{stop_at:.0%} of the limit (stop_at), so plans use ${budget * stop_at:.0f}. "
                   f"Windows left early near the limit: {len(soft)}"
                   + ("" if soft else " (none detected — set `primer config stop_at` lower if you stop earlier)"))
        for e in soft:
            out.append(f"         early stop {_when(e['start'] * 600)}: at {e['util']:.0%}, stopped "
                       f"{_dur((e['end'] - e['stop'] - 1) * 10)} before the reset ({e['source']})")
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
    pc = result.get("period_check")
    if budget and pc:
        where = f"since {_when(pc['since'])}" if pc["since"] else "over the whole history"
        out.append(f"  Sanity check {where}: the replay predicts {pc['predicted']} limit hit(s); you had {pc['actual']}."
                   + ("  Big gap → the budget estimate is off; fresh statusline readings will settle it."
                      if abs(pc["predicted"] - pc["actual"]) > max(2, pc["actual"] / 2) else ""))
    out.append("")
    out.append("Held-out weeks (plan without that week, test on it) — what to actually expect:")
    for r in result["cv"]:
        b, p = r["baseline"], r["planned"]
        if budget:
            out.append(f"  {r['mode']:<7} ≤{r['pings']} ping/day   lockout {_dur(b['lockout_min'])} → {_dur(p['lockout_min'])}"
                       f"   over-budget windows {b['hits']} → {p['hits']}")
        else:
            out.append(f"  {r['mode']:<7} ≤{r['pings']} ping/day   Σload² {b['sum_sq']:.0f} → {p['sum_sq']:.0f}")
    ka = result.get("keepalive")
    if ka:
        cv_best = min((r["planned"].get("lockout_min", 0) for r in result["cv"]), default=None)
        out.append("")
        out.append(f"Ping at every window start instead (keep-alive, {ka['pings_per_day']:.1f} pings/day): lockout "
                   f"{_dur(ka['lockout_mean'])} (range {_dur(ka['lockout_min'])}–{_dur(ka['lockout_max'])} depending "
                   f"on where the chain starts) vs {_dur(result['baseline'].get('lockout_min', 0))} with no pings"
                   + (f" and {_dur(cv_best)} for the plan on held-out weeks." if cv_best is not None else "."))
        if ka.get("aligned_by_change"):
            out.append("  (Every starting point gives the same result: the plan change re-anchored the chain, and "
                       "all lockouts since came after it.)")
    rw = result.get("real_world") or {}
    out.append("")
    if rw.get("since") and rw["since"]["days"] >= 1:
        b, a = rw["before"], rw["since"]
        out.append(f"Real world — before scheduled pings vs since ({a['days']:.0f} days, {rw['opened']} window(s) opened by pings):")
        out.append(f"  limit hits per week        {b['hits_per_week']:9.1f}   {a['hits_per_week']:9.1f}")
        out.append(f"  lockout per week           {_dur(b['lockout_min_per_week']):>9}   {_dur(a['lockout_min_per_week']):>9}")
    else:
        out.append("Real world: no scheduled ping has opened a window yet — this compares before vs after once one does.")
    blocked = (result.get("imputed_usd") or 0) + (result.get("soft_imputed_usd") or 0)
    if blocked:
        out.append(f"(Replay includes ${blocked:.0f} of estimated work blocked by past lockouts.)")
    out.append("\nCaveats: claude.ai/phone usage is only visible through limit hits and live readings; work blocked "
               "by past lockouts is estimated (impute_lockout_factor), not observed.")
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
        save = reset_advice(obs["five_util"], obs["five_resets_at"], obs["week_util"],
                            db.execute("SELECT max(ts) FROM resets_used").fetchone()[0], now)
        if save:
            out.append(f"Tip:           /limit-reset now would save {_dur(save)} of waiting "
                       "(rationed — roughly once a week)")
    else:
        out.append("No live readings yet (they come from the statusline and from pings).")
    skip = set(cfg.get("skip_dates", []))
    if plan:
        upcoming = upcoming_pings(plan["schedule"], now, days=7, skip=skip)
        out.append(f"Plan from {_when(plan['generated_at'])}: {plan['reason']}")
        out.append("Next pings:    " + (", ".join(dt.strftime("%a %H:%M") for dt in upcoming[:6]) or "none scheduled"))
        if plan.get("budget_usd"):
            out.append(f"Budget:        ≈ ${plan['budget_usd']:.1f}/window ({plan['budget_source']})")
    else:
        out.append("No plan yet — run `primer refresh`.")
    future_skips = sorted(d for d in skip if d >= datetime.fromtimestamp(now).date().isoformat())
    if future_skips:
        out.append("Days off:      " + ", ".join(future_skips))
    if timer_line:
        out.append(f"systemd:       {timer_line}")
    pings = db.execute("SELECT * FROM pings ORDER BY ts DESC LIMIT 6").fetchall()
    if pings:
        out.append("Recent pings:")
        for p in pings:
            out.append(f"  {_when(p['ts'])}  {p['outcome']:<13} {p['detail'] or ''}")
    return "\n".join(out)
