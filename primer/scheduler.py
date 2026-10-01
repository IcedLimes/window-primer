"""systemd user units: a ping timer driven by the plan, and a daily re-plan timer."""

import os
import subprocess
from pathlib import Path

from . import config

PING = "primer-ping"
REPLAN = "primer-replan"
BIN = Path(__file__).resolve().parent.parent / "bin" / "primer"


def _service(description, args):
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=oneshot
ExecStart={BIN} {args}
Environment=PATH={path}
TimeoutStartSec=600
"""


def ping_timer(schedule):
    calendars = "\n".join(f"OnCalendar={day} *-*-* {t}:00"
                          for day, times in schedule.items() for t in times)
    # No Persistent=: a 07:01 ping missed while asleep must not fire at noon and misplace a window.
    return f"""[Unit]
Description=window-primer: open a Claude usage window at planned times

[Timer]
{calendars}
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


REPLAN_TIMER = """[Unit]
Description=window-primer: re-plan ping times daily

[Timer]
OnCalendar=*-*-* 04:05:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
"""


def systemctl(*args, check=False):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=check)


def _write(name, content):
    path = config.SYSTEMD_DIR / name
    if path.exists() and path.read_text() == content:
        return False
    path.write_text(content)
    return True


def install(schedule):
    config.SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    _write(f"{PING}.service", _service("window-primer: send a tiny message to open a Claude usage window",
                                       "ping --scheduled"))
    _write(f"{REPLAN}.service", _service("window-primer: re-plan ping times", "refresh --quiet"))
    _write(f"{REPLAN}.timer", REPLAN_TIMER)
    systemctl("daemon-reload")
    systemctl("enable", "--now", f"{REPLAN}.timer")
    return sync(schedule)


def sync(schedule):
    """Point the ping timer at the plan's times. Returns a short description of what changed."""
    has_pings = any(schedule.values())
    timer = config.SYSTEMD_DIR / f"{PING}.timer"
    if not has_pings:
        if timer.exists():
            systemctl("disable", "--now", f"{PING}.timer")
            timer.unlink()
            systemctl("daemon-reload")
            return "ping timer removed (no pings planned)"
        return "no pings planned"
    if not (config.SYSTEMD_DIR / f"{PING}.service").exists():
        return "systemd units not installed (run `primer install`)"
    changed = _write(f"{PING}.timer", ping_timer(schedule))
    if changed:
        systemctl("daemon-reload")
        systemctl("enable", f"{PING}.timer")
        systemctl("restart", f"{PING}.timer")
        return "ping timer updated"
    return "ping timer unchanged"


def uninstall():
    for unit in (f"{PING}.timer", f"{REPLAN}.timer"):
        systemctl("disable", "--now", unit)
    for name in (f"{PING}.timer", f"{PING}.service", f"{REPLAN}.timer", f"{REPLAN}.service"):
        (config.SYSTEMD_DIR / name).unlink(missing_ok=True)
    systemctl("daemon-reload")


def next_elapse():
    out = systemctl("list-timers", f"{PING}.timer", "--no-legend", "--no-pager").stdout.strip()
    return out or None
