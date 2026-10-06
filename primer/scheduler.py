"""Local scheduling, through whichever service this OS has."""

from . import system


def backend():
    if system.OS == "linux":
        from .backends import systemd as b
    elif system.OS == "macos":
        from .backends import launchd as b
    elif system.OS == "windows":
        from .backends import taskscheduler as b
    else:
        return None
    return b


def install(schedule):
    b = backend()
    return b.install(schedule) if b else f"no local scheduler on {system.OS}; use the cloud runner"


def sync(schedule):
    b = backend()
    return b.sync(schedule) if b else "no local scheduler"


def uninstall():
    b = backend()
    if b:
        b.uninstall()


def next_elapse():
    b = backend()
    return b.describe() if b else None
