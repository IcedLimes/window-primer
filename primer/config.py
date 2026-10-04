"""Paths and user-tunable settings."""

import json
import os
from pathlib import Path

APP = "window-primer"

DATA_DIR = Path(os.environ.get("PRIMER_DATA_DIR")
                or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / APP)
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
PROJECTS_DIR = CLAUDE_DIR / "projects"
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
SYSTEMD_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"

DB_PATH = DATA_DIR / "primer.db"
CONFIG_PATH = DATA_DIR / "config.json"
PLAN_PATH = DATA_DIR / "plan.json"

DEFAULTS = {
    # History used for planning ("a month" plus a bit, so each weekday has ~5 samples).
    "lookback_days": 35,
    # Recent weeks matter more: a day's weight halves every N days.
    "half_life_days": 21,
    # Budget estimates fade faster: Anthropic changes limits every few months (promotions, the
    # May 2026 doubling, the September 2026 cut), so an old limit hit says little about today.
    "budget_half_life_days": 7,
    # Work you were locked out of never reached the transcripts. Assume you'd have kept going at
    # this fraction of your pre-lockout pace (for at most N hours) so the optimiser can see it.
    "impute_lockout_factor": 0.5,
    "impute_lockout_max_hours": 3,
    "window_hours": 5,
    # The API floors window starts to 10 minutes (observed in limit-hit messages and resetsAt).
    "bin_minutes": 10,
    "max_pings_per_day": 2,
    # A ping (or a second ping) must cut that weekday's objective by at least this fraction.
    "min_gain_frac": 0.03,
    # Local hours [start, end) in which pings may be scheduled, e.g. [6, 23] if the machine sleeps at night.
    "ping_hours": [0, 24],
    # Plan for windows to stay under this fraction of the budget; the excess is penalised lightly.
    "margin": 0.8,
    "margin_weight": 0.2,
    # Per-window budget in API-equivalent USD. null = calibrate from limit hits / observations.
    "budget_override_usd": None,
    # Only calibrate from data after this local time ("2026-10-01T21:30"), e.g. after a plan change.
    # Plan changes that reset the weekly counter are detected automatically; this covers the rest.
    "budget_since": None,
    # You stop before the limit to avoid being cut off mid-response; plan as if the budget were this
    # fraction of the real one. Windows you left at this level count as (soft) lockouts.
    "stop_at": 0.95,
    # 'auto' cross-validates global, pooled and per-weekday schedules (1 or 2 pings) and keeps the
    # simplest one that generalises; 'global', 'pooled' or 'weekday' force a mode.
    "mode": "auto",
    # Partial-pooling strength for mode 'pooled', in days: a weekday with n days of history is
    # shrunk toward the all-days curve with weight κ/(n+κ).
    "pool_kappa": 5.0,
    # Smooth the objective over ±N slots (10 min each) so a small shift in your start time doesn't
    # turn a good ping into a bad one.
    "smooth_slots": 3,
    "ping_model": "haiku",
    "ping_prompt": "Reply with just: k",
    "ping_timeout_s": 90,
    "ping_retries": 2,
    "claude_bin": None,
    # Local dates (YYYY-MM-DD) with no pings, e.g. holidays. Managed with `primer skip`.
    "skip_dates": [],
    # Railway cron runner: {"project": id, "environment": id, "service": id}; set by `primer cloud link`.
    "cloud": None,
}


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    overrides = {k: v for k, v in cfg.items() if DEFAULTS.get(k, object()) != v}
    CONFIG_PATH.write_text(json.dumps(overrides, indent=2) + "\n")
