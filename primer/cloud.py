"""Cloud runner (Railway cron) and the local side that keeps it in sync.

Railway cron runs in UTC, at least 5 minutes apart, and may start a few minutes late.
So the cron expression is a superset of every planned ping time under both the
standard and daylight UTC offsets, and the runner itself decides — in your local
time zone — whether a ping is due right now. Planned times sit 1 minute into a
10-minute slot, so up to ~8 minutes of lateness still opens the intended window.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config
from .planner import WEEKDAYS

LATE_OK_MIN = 30  # a late ping still helps; much later and it's a different plan


def local_tz_name():
    tz = os.environ.get("TZ")
    if tz:
        return tz
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def cron_for(schedule, tz_name):
    """Smallest single cron expression (UTC) that fires at every planned time in either DST state."""
    times = {t for ts in schedule.values() for t in ts}
    if not times:
        return None
    tz = ZoneInfo(tz_name)
    year = datetime.now().year
    offsets = {datetime(year, m, 15, 12, tzinfo=tz).utcoffset() for m in (1, 7)}
    minutes, hours = set(), set()
    for t in times:
        h, m = map(int, t.split(":"))
        for off in offsets:
            utc = datetime(2000, 1, 3, h, m) - off
            minutes.add(utc.minute)
            hours.add(utc.hour)
    return f"{','.join(map(str, sorted(minutes)))} {','.join(map(str, sorted(hours)))} * * *"


def due(schedule, tz_name, now=None, late_ok_min=LATE_OK_MIN, skip=()):
    """The planned local time this run is for, or None. Checks yesterday too for runs just past midnight."""
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(tz)
    for back in (0, 1):
        day = (now - timedelta(days=back)).date()
        if day.isoformat() in skip:
            continue
        for t in schedule.get(WEEKDAYS[day.weekday()], []):
            h, m = map(int, t.split(":"))
            planned = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if timedelta(minutes=-1) <= now - planned < timedelta(minutes=late_ok_min):
                return planned
    return None


def run():
    """Entry point inside the container: ping if due, print one JSON line, exit."""
    from . import ping
    payload = json.loads(os.environ.get("PRIMER_SCHEDULE") or "{}")
    schedule, tz_name = payload.get("schedule", {}), payload.get("tz", "UTC")
    planned = due(schedule, tz_name, skip=set(payload.get("skip_dates", [])))
    if os.environ.get("PRIMER_FORCE") == "1":
        planned = planned or datetime.now(ZoneInfo(tz_name))
    if planned is None:
        print(json.dumps({"primer": "not-due", "at": time.time()}), flush=True)
        return 0
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print(json.dumps({"primer": "error", "detail": "CLAUDE_CODE_OAUTH_TOKEN is not set"}), flush=True)
        return 1
    cfg = dict(config.DEFAULTS, ping_model=payload.get("model", "haiku"))
    started = time.time()
    err = None
    for attempt in range(3):
        if attempt:
            time.sleep(20 * attempt)
        try:
            proc = subprocess.run(ping.ping_command(cfg), capture_output=True, text=True,
                                  timeout=cfg["ping_timeout_s"], stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as e:
            err = f"{type(e).__name__}: {e}"
            continue
        info, result = ping.parse_stream(proc.stdout)
        if proc.returncode == 0 and result and not result.get("is_error"):
            five_util, five_reset, week_util, week_reset = ping.windows_from_info(info)
            opened = bool(five_reset) and five_reset - cfg["window_hours"] * 3600 >= (started // 600) * 600 - 1
            print(json.dumps({"primer": "opened" if opened else ("inside-window" if five_reset else "sent"),
                              "at": started, "planned": planned.isoformat(), "five_util": five_util,
                              "five_resets_at": five_reset, "week_util": week_util,
                              "week_resets_at": week_reset}), flush=True)
            return 0
        err = (result or {}).get("result") or proc.stderr.strip()[-300:] or f"exit {proc.returncode}"
    print(json.dumps({"primer": "error", "at": started, "planned": planned.isoformat(), "detail": err}), flush=True)
    return 1


# ---- local side: push the plan to Railway and pull ping results back ----

def _railway(*args, timeout=60):
    exe = shutil.which("railway") or os.path.expanduser("~/.npm-global/bin/railway")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)


def target(cfg):
    c = cfg.get("cloud") or {}
    return c if all(c.get(k) for k in ("project", "environment", "service")) else None


def _scope(t):
    return ["--project", t["project"], "--environment", t["environment"], "--service", t["service"]]


def sync(cfg, schedule, sleep=time.sleep):
    """Push schedule + cron to the Railway service. Returns a short description."""
    t = target(cfg)
    if not t:
        return None
    tz_name = local_tz_name()
    today = datetime.now().date().isoformat()
    payload = json.dumps({"tz": tz_name, "model": cfg["ping_model"], "schedule": schedule,
                          "skip_dates": sorted(d for d in cfg.get("skip_dates", []) if d >= today)},
                         sort_keys=True)
    cron = cron_for(schedule, tz_name)
    state_path = config.DATA_DIR / "cloud_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("payload") == payload and state.get("cron") == cron:
        return "cloud schedule unchanged"
    steps = [("variables", ("variable", "set", f"PRIMER_SCHEDULE={payload}", *_scope(t)))]
    if cron:
        steps.insert(0, ("cron", ("api", "mutation($s:String!,$e:String!,$c:String){serviceInstanceUpdate("
                                  "serviceId:$s,environmentId:$e,input:{cronSchedule:$c})}",
                                  "--variables", json.dumps({"s": t["service"], "e": t["environment"], "c": cron}))))
    for name, args in steps:
        # The daily replan often runs the moment the machine wakes, before the network is back.
        for attempt in range(3):
            if attempt:
                sleep(15 * attempt)
            try:
                r = _railway(*args)
                err = None if r.returncode == 0 else (r.stderr or r.stdout).strip()[-200:]
            except (OSError, subprocess.TimeoutExpired) as e:
                err = str(e)
            if err is None:
                break
        else:
            return f"cloud sync failed ({name}): {err}"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"payload": payload, "cron": cron}))
    return f"cloud schedule updated (cron {cron})" if cron else "cloud schedule cleared (no pings planned)"


def pull_results(cfg, db):
    """Copy ping results from Railway logs into the local store. Returns how many were new."""
    t = target(cfg)
    if not t:
        return 0
    try:
        r = _railway("logs", *_scope(t), "--lines", "500", "--json", timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    from . import store
    new = 0
    for line in r.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        # Railway lifts a JSON log line's keys into the entry itself (leaving "message" empty);
        # fall back to parsing "message" in case a line arrives as plain text.
        rec = entry
        if "primer" not in entry:
            try:
                rec = json.loads(entry.get("message") or "null")
            except ValueError:
                continue
        if not isinstance(rec, dict) or rec.get("primer") in (None, "not-due") or not rec.get("at"):
            continue
        if db.execute("SELECT 1 FROM pings WHERE ts=?", (rec["at"],)).fetchone():
            continue
        detail = rec.get("detail")
        if rec.get("five_resets_at"):
            store.add_observation(db, rec["at"], "cloud-ping", rec.get("five_util"), rec["five_resets_at"],
                                  rec.get("week_util"), rec.get("week_resets_at"))
            detail = (f"cloud · window {time.strftime('%H:%M', time.localtime(rec['five_resets_at'] - 5 * 3600))}–"
                      f"{time.strftime('%H:%M', time.localtime(rec['five_resets_at']))}, "
                      f"5h at {round((rec.get('five_util') or 0) * 100)}%")
        db.execute("INSERT INTO pings VALUES (?,?,?,?,?)",
                   (rec["at"], "cloud", rec["primer"], rec.get("five_resets_at"), detail or "cloud"))
        new += 1
    db.commit()
    return new


if __name__ == "__main__":
    sys.exit(run())
