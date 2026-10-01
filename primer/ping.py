"""Send the tiny message that opens a usage window, and record what the API says about it."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import config, store

NO_HOOKS_SETTINGS = '{"disableAllHooks":true}'


def find_claude(cfg):
    candidates = [cfg.get("claude_bin"), shutil.which("claude"),
                  str(Path.home() / ".npm-global/bin/claude"), str(Path.home() / ".local/bin/claude"),
                  str(Path.home() / ".claude/local/claude")]
    for c in candidates:
        if c and os.access(c, os.X_OK):
            return c
    raise FileNotFoundError("claude CLI not found; set claude_bin with `primer config claude_bin /path/to/claude`")


def ping_command(cfg):
    # --safe-mode + disableAllHooks: no plugins, MCP servers or hooks (a sound-playing Stop hook would fire at 7 am).
    # --tools "": smallest possible request. --no-session-persistence: keep pings out of your transcripts.
    return [find_claude(cfg), "-p", "--safe-mode", "--settings", NO_HOOKS_SETTINGS,
            "--model", cfg["ping_model"], "--tools", "", "--strict-mcp-config",
            "--no-session-persistence", "--output-format", "stream-json", "--verbose", cfg["ping_prompt"]]


def parse_stream(stdout):
    """Pull the rate-limit info and result out of `claude -p --output-format stream-json` output."""
    info, result = None, None
    for line in stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "rate_limit_event":
            info = d.get("rate_limit_info") or {}
        elif d.get("type") == "result":
            result = d
    return info, result


def windows_from_info(info):
    """-> (five_util, five_resets_at, week_util, week_resets_at); utilization as 0..1."""
    if not info:
        return None, None, None, None
    uw = info.get("unifiedWindows") or {}
    five, week = uw.get("five_hour") or {}, uw.get("seven_day") or {}
    if not five and info.get("rateLimitType") == "five_hour":
        five = {"utilization": info.get("utilization"), "resetsAt": info.get("resetsAt")}
    return five.get("utilization"), five.get("resetsAt"), week.get("utilization"), week.get("resetsAt")


def active_window_until(db, now):
    """Reset time of a window we *know* is open (from a live reading), else None.
    Only hard evidence counts: wrongly skipping a ping costs far more than a redundant one."""
    row = db.execute("SELECT max(five_resets_at) FROM observations").fetchone()
    if row and row[0] and row[0] > now + 60:
        return row[0]
    return None


def run(cfg, db, scheduled=None, force=False, runner=subprocess.run, sleep=time.sleep):
    now = time.time()
    wsec = cfg["window_hours"] * 3600
    if not force:
        until = active_window_until(db, now)
        if until:
            detail = f"window already open until {time.strftime('%H:%M', time.localtime(until))}"
            db.execute("INSERT INTO pings VALUES (?,?,?,?,?)", (now, scheduled, "skipped", until, detail))
            db.commit()
            return "skipped", detail

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = ping_command(cfg)
    err = "no attempt"
    for attempt in range(cfg["ping_retries"] + 1):
        if attempt:
            sleep(30 * attempt)  # network is often not up yet right after resume
        try:
            proc = runner(cmd, cwd=str(config.DATA_DIR), capture_output=True, text=True,
                          timeout=cfg["ping_timeout_s"], stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as e:
            err = f"{type(e).__name__}: {e}"
            continue
        info, result = parse_stream(proc.stdout)
        if proc.returncode == 0 and result and not result.get("is_error"):
            break
        err = (result or {}).get("result") or proc.stderr.strip()[-300:] or f"exit {proc.returncode}"
    else:
        db.execute("INSERT INTO pings VALUES (?,?,?,?,?)", (now, scheduled, "error", None, err))
        db.commit()
        return "error", err

    five_util, five_reset, week_util, week_reset = windows_from_info(info)
    sent = time.time()
    if five_reset or week_reset:
        store.add_observation(db, sent, "ping", five_util, five_reset, week_util, week_reset)
    if five_reset:
        opened_at = five_reset - wsec
        # The window we opened starts at our send time floored to 10 min.
        outcome = "opened" if opened_at >= (now // 600) * 600 - 1 else "inside-window"
        detail = (f"window {time.strftime('%H:%M', time.localtime(opened_at))}–"
                  f"{time.strftime('%H:%M', time.localtime(five_reset))}, 5h at {round((five_util or 0) * 100)}%")
    else:
        outcome, detail = "sent", "no rate-limit info in response"
    db.execute("INSERT INTO pings VALUES (?,?,?,?,?)", (now, scheduled, outcome, five_reset, detail))
    db.commit()
    return outcome, detail
