"""Claude Code statusline: records live window state for calibration and shows it.

Claude Code pipes session JSON to the statusline command; recent versions include
rate_limits.five_hour/seven_day {used_percentage, resets_at}.
"""

import json
import os
import sys
import time
from datetime import datetime

from . import config

DIM, RESET = "\033[2m", "\033[0m"


def _color(pct):
    return "\033[32m" if pct < 50 else "\033[33m" if pct < 80 else "\033[31m"


def _epoch(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e11 else 1)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clock(ts):
    dt = datetime.fromtimestamp(ts)
    h = dt.hour % 12 or 12
    return f"{h}{dt.strftime(':%M') if dt.minute else ''}{'a' if dt.hour < 12 else 'p'}"


def next_ping_label(now):
    try:
        plan = json.loads(config.PLAN_PATH.read_text())
    except (OSError, ValueError):
        return None
    from .planner import upcoming_pings
    nxt = upcoming_pings(plan.get("schedule", {}), now, days=7)
    if not nxt:
        return None
    dt = nxt[0]
    day = "" if dt.date() == datetime.fromtimestamp(now).date() else dt.strftime("%a ")
    return f"{day}{dt.strftime('%H:%M')}"


def record(rate_limits, now):
    five = rate_limits.get("five_hour") or {}
    week = rate_limits.get("seven_day") or {}
    pct5, pctw = five.get("used_percentage"), week.get("used_percentage")
    if pct5 is None and pctw is None:
        return
    from . import store
    db = store.connect()
    try:
        store.add_observation(db, now, "statusline",
                              None if pct5 is None else pct5 / 100, _epoch(five.get("resets_at")),
                              None if pctw is None else pctw / 100, _epoch(week.get("resets_at")))
    finally:
        db.close()


def render(data, now):
    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
    if cwd:
        home = os.path.expanduser("~")
        parts.append(cwd.replace(home, "~", 1) if cwd.startswith(home) else cwd)
    rl = data.get("rate_limits") or {}
    five, week = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    if five.get("used_percentage") is not None:
        pct = round(five["used_percentage"])
        reset = _epoch(five.get("resets_at"))
        tail = f" {DIM}↻{_clock(reset)}{RESET}" if reset and reset > now else ""
        parts.append(f"5h {_color(pct)}{pct}%{RESET}{tail}")
    if week.get("used_percentage") is not None:
        pct = round(week["used_percentage"])
        parts.append(f"7d {_color(pct)}{pct}%{RESET}")
    label = next_ping_label(now)
    if label:
        parts.append(f"{DIM}ping {label}{RESET}")
    return f" {DIM}│{RESET} ".join(parts)


def main():
    now = time.time()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    try:
        record(data.get("rate_limits") or {}, now)
    except Exception:  # never break the statusline over bookkeeping
        pass
    try:
        print(render(data, now))
    except Exception:
        print((data.get("model") or {}).get("display_name", ""))
    return 0
