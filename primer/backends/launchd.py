"""macOS: launchd LaunchAgents — a ping job driven by the plan, and a daily re-plan job.

launchd runs a StartCalendarInterval job that came due while the Mac slept as soon as it
wakes (several missed runs collapse into one). That's what we want for the re-plan; for pings,
`primer ping --scheduled` refuses to send unless a planned time was within the last 15 minutes.
"""

import os
import plistlib
import subprocess
from pathlib import Path

from .. import config, system
from ..planner import WEEKDAYS

NAME = "launchd"
PREFIX = "io.github.icedlimes.window-primer"
PING = f"{PREFIX}.ping"
REPLAN = f"{PREFIX}.replan"
AGENTS = Path.home() / "Library/LaunchAgents"


def _plist_path(label):
    return AGENTS / f"{label}.plist"


def _job(label, args, intervals):
    logs = config.DATA_DIR / "logs"
    return {
        "Label": label,
        "ProgramArguments": system.command(*args),
        "WorkingDirectory": str(system.ROOT),
        "StartCalendarInterval": intervals,
        # launchd starts jobs with a bare PATH; keep the one claude and railway were found on.
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
        "StandardOutPath": str(logs / f"{label.rsplit('.', 1)[1]}.log"),
        "StandardErrorPath": str(logs / f"{label.rsplit('.', 1)[1]}.log"),
        "RunAtLoad": False,
    }


def ping_intervals(schedule):
    """launchd counts weekdays from Sunday = 0."""
    out = []
    for day, times in schedule.items():
        for t in times:
            h, m = map(int, t.split(":"))
            out.append({"Weekday": (WEEKDAYS.index(day) + 1) % 7, "Hour": h, "Minute": m})
    return sorted(out, key=lambda d: (d["Weekday"], d["Hour"], d["Minute"]))


def ping_plist(schedule):
    return plistlib.dumps(_job(PING, ["ping", "--scheduled"], ping_intervals(schedule)))


def replan_plist():
    return plistlib.dumps(_job(REPLAN, ["refresh", "--quiet"], [{"Hour": 4, "Minute": 5}]))


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain():
    return f"gui/{os.getuid()}"


def _load(label, content):
    """(Re)load a job if its plist changed. Returns True if anything changed."""
    path = _plist_path(label)
    if path.exists() and path.read_bytes() == content:
        return False
    AGENTS.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    launchctl("bootout", f"{_domain()}/{label}")
    path.write_bytes(content)
    r = launchctl("bootstrap", _domain(), str(path))
    if r.returncode:  # older macOS
        launchctl("load", "-w", str(path))
    return True


def _unload(label):
    launchctl("bootout", f"{_domain()}/{label}")
    _plist_path(label).unlink(missing_ok=True)


def install(schedule):
    _load(REPLAN, replan_plist())
    return sync(schedule)


def sync(schedule):
    if not any(schedule.values()):
        if _plist_path(PING).exists():
            _unload(PING)
            return "ping job removed (no pings planned)"
        return "no pings planned"
    if not _plist_path(REPLAN).exists():
        return "launchd jobs not installed (run `primer install`)"
    return "ping job updated" if _load(PING, ping_plist(schedule)) else "ping job unchanged"


def uninstall():
    for label in (PING, REPLAN):
        _unload(label)


def describe():
    if not _plist_path(PING).exists():
        return None
    loaded = launchctl("print", f"{_domain()}/{PING}").returncode == 0
    n = len(plistlib.loads(_plist_path(PING).read_bytes()).get("StartCalendarInterval", []))
    return f"launchd job {PING}: {'loaded' if loaded else 'NOT loaded'}, {n} ping(s) a week"
