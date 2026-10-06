"""Windows: Task Scheduler — a ping task driven by the plan, and a daily re-plan task.

Tasks run as you, only while you're logged on (no stored password), via pythonw.exe so no
console window appears. The ping task doesn't catch up after sleep (StartWhenAvailable=false);
the re-plan task does. Set `wake_to_run` to let the ping wake the PC.
"""

import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .. import config, system
from ..planner import WEEKDAYS

NAME = "Task Scheduler"
FOLDER = "\\window-primer\\"
PING = FOLDER + "ping"
REPLAN = FOLDER + "replan"
DAY_NAMES = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
             "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}


def _triggers_weekly(schedule):
    """One weekly trigger per time of day, covering every weekday that pings then."""
    by_time = {}
    for day, times in schedule.items():
        for t in times:
            by_time.setdefault(t, []).append(day)
    out = []
    for t in sorted(by_time):
        days = "".join(f"<{DAY_NAMES[d]} />" for d in sorted(by_time[t], key=WEEKDAYS.index))
        out.append(f"""    <CalendarTrigger>
      <StartBoundary>2026-01-01T{t}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>{days}</DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>""")
    return "\n".join(out)


def _trigger_daily(t):
    return f"""    <CalendarTrigger>
      <StartBoundary>2026-01-01T{t}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>"""


def _task(description, args, triggers, catch_up, wake):
    exe, *rest = system.command(*args, background=True)
    arguments = " ".join(f'"{a}"' if " " in a or a.endswith(".py") else a for a in rest)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>{str(catch_up).lower()}</StartWhenAvailable>
    <WakeToRun>{str(wake).lower()}</WakeToRun>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(system.ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def ping_xml(schedule, wake=False):
    return _task("window-primer: send a tiny message to open a Claude usage window",
                 ["ping", "--scheduled"], _triggers_weekly(schedule), catch_up=False, wake=wake)


def replan_xml():
    return _task("window-primer: re-plan ping times", ["refresh", "--quiet"],
                 _trigger_daily("04:05"), catch_up=True, wake=False)


def schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True)


def _state_path(name):
    return config.DATA_DIR / f"task-{name.rsplit(chr(92), 1)[1]}.xml"


def _register(name, xml):
    """Create or replace a task if its definition changed. Returns True if anything changed."""
    state = _state_path(name)
    if state.exists() and state.read_text(encoding="utf-16") == xml:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "task.xml"
        path.write_text(xml, encoding="utf-16")
        r = schtasks("/Create", "/TN", name, "/XML", str(path), "/F")
    if r.returncode:
        raise RuntimeError(f"schtasks /Create {name} failed: {(r.stderr or r.stdout).strip()}")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    state.write_text(xml, encoding="utf-16")
    return True


def _delete(name):
    schtasks("/Delete", "/TN", name, "/F")
    _state_path(name).unlink(missing_ok=True)


def install(schedule):
    _register(REPLAN, replan_xml())
    return sync(schedule)


def sync(schedule):
    if not any(schedule.values()):
        if _state_path(PING).exists():
            _delete(PING)
            return "ping task removed (no pings planned)"
        return "no pings planned"
    if not _state_path(REPLAN).exists():
        return "scheduled tasks not installed (run `primer install`)"
    changed = _register(PING, ping_xml(schedule, wake=config.load().get("wake_to_run", False)))
    return "ping task updated" if changed else "ping task unchanged"


def uninstall():
    for name in (PING, REPLAN):
        _delete(name)


def describe():
    if not _state_path(PING).exists():
        return None
    out = schtasks("/Query", "/TN", PING, "/FO", "LIST").stdout
    nxt = next((line.split(":", 1)[1].strip() for line in out.splitlines()
                if line.lower().startswith("next run time")), None)
    return f"Task Scheduler {PING}: next run {nxt}" if nxt else f"Task Scheduler {PING}: registered"
