"""Everything that differs between Linux, macOS and Windows."""

import contextlib
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if sys.platform.startswith("linux"):
    OS = "linux"
elif sys.platform == "darwin":
    OS = "macos"
elif os.name == "nt":
    OS = "windows"
else:
    OS = "other"

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "bin" / "primer.py"
HOME = Path.home()
LAUNCHER_MARK = "window-primer launcher"


def data_dir(app):
    if os.environ.get("PRIMER_DATA_DIR"):
        return Path(os.environ["PRIMER_DATA_DIR"])
    if OS == "macos":
        return HOME / "Library/Application Support" / app
    if OS == "windows":
        return Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local") / app
    return Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / app


# ---- running ourselves ----

def python(background=False):
    """The interpreter running now; scheduled jobs pin it so they never pick up an older python3
    (macOS ships 3.9) or the Microsoft Store alias. On Windows, background jobs use pythonw.exe so
    no console window flashes up."""
    exe = Path(sys.executable)
    # Prefer a stable name: rolling distros and Homebrew delete /usr/bin/python3.14 when 3.15
    # arrives, but /usr/bin/python3 keeps pointing at whatever is current.
    stable = shutil.which("python3") if OS != "windows" else None
    if stable and Path(stable) != exe and os.path.realpath(stable) == os.path.realpath(exe):
        exe = Path(stable)
    if background and OS == "windows":
        quiet = exe.with_name("pythonw.exe")
        if quiet.exists():
            return quiet
    return exe


def command(*args, background=False):
    return [str(python(background)), str(ENTRY), *args]


def shell_command(*args):
    """A command line for settings.json (run by bash or cmd.exe): quoted, forward slashes."""
    def q(p):
        return f'"{Path(p).as_posix()}"' if OS == "windows" else f'"{p}"'
    return " ".join([q(python()), q(ENTRY), *args])


def launcher_dir():
    if OS == "windows":
        # On PATH for every Windows 10/11 user by default, and writable without admin rights.
        return Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local") / "Microsoft/WindowsApps"
    return HOME / ".local/bin"


def launcher_files():
    """{path: content} for the `primer` command on PATH."""
    d = launcher_dir()
    exe, entry = python(), ENTRY
    if OS == "windows":
        return {
            d / "primer.cmd": f'@rem {LAUNCHER_MARK}\r\n@"{exe}" "{entry}" %*\r\n',
            # Claude Code runs `!` commands and hooks in Git Bash, which won't run .cmd by bare name.
            d / "primer": f'#!/bin/sh\n# {LAUNCHER_MARK}\nexec "{exe.as_posix()}" "{entry.as_posix()}" "$@"\n',
        }
    return {d / "primer": f'#!/bin/sh\n# {LAUNCHER_MARK}\nexec "{exe}" "{entry}" "$@"\n'}


def install_launchers():
    out = []
    for path, content in launcher_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (path.exists() and LAUNCHER_MARK not in path.read_text(errors="ignore")):
            if not path.is_symlink():
                out.append(f"{path} exists and isn't ours; left alone")
                continue
            path.unlink()  # 1.0 installed a symlink here
        path.write_text(content, newline="")
        path.chmod(0o755)
        out.append(f"wrote {path}")
    on_path = any(Path(p) == launcher_dir() for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    if not on_path:
        out.append(f"note: {launcher_dir()} isn't on your PATH; add it so `primer` works in a terminal")
    return out


def remove_launchers():
    removed = []
    for path in launcher_files():
        if path.is_symlink() or (path.exists() and LAUNCHER_MARK in path.read_text(errors="ignore")):
            path.unlink()
            removed.append(str(path))
    return removed


# ---- plugin folder link ----

def link_dir(link, target):
    """Point `link` at `target`: a symlink on Linux/macOS, a directory junction on Windows
    (symlinks there need admin rights or developer mode; junctions don't)."""
    link = Path(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or is_junction(link):
        if Path(os.path.realpath(link)) == Path(os.path.realpath(target)):
            return f"{link} already linked"
        unlink_dir(link)
    elif link.exists():
        return f"{link} exists and isn't a link; left alone"
    if OS == "windows":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if r.returncode:
            return f"couldn't link {link}: {(r.stderr or r.stdout).strip()}"
    else:
        link.symlink_to(target)
    return f"linked {link} → {target}"


def is_junction(path):
    if OS != "windows":
        return False
    if hasattr(os.path, "isjunction"):  # Python 3.12+
        return os.path.isjunction(path)
    import stat
    try:
        return os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, AttributeError):
        return False


def unlink_dir(link):
    link = Path(link)
    if is_junction(link):
        os.rmdir(link)  # removes the junction, not the target
        return True
    if link.is_symlink():
        link.unlink()
        return True
    return False


# ---- locking ----

@contextlib.contextmanager
def file_lock(path):
    """Exclusive, non-blocking lock; raises BlockingIOError if someone else holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if OS == "windows":
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise BlockingIOError(str(e)) from e
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if OS == "windows":
            import msvcrt
            with contextlib.suppress(OSError):
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        fh.close()


# ---- time zones ----

def local_tz_name():
    """IANA name of the local time zone, or 'UTC' if it can't be determined."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz.lstrip(":")
    if OS == "windows":
        from .winzones import WINDOWS_TO_IANA
        try:
            win = subprocess.run(["tzutil", "/g"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            win = ""
        return WINDOWS_TO_IANA.get(win.removesuffix("_dstoff"), "UTC")
    try:  # Linux and macOS: /etc/localtime → …/zoneinfo/Area/City
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        pass
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        return "UTC"


def zone(name):
    """ZoneInfo for `name`, or None when the tz database is missing (stock Windows Python has none;
    `pip install tzdata` adds it). Callers then fall back to the OS's local time."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def local_utc_offsets(year=None):
    """The local UTC offsets in winter and summer, from the OS clock (no tz database needed)."""
    year = year or datetime.now().year
    return {datetime(year, m, 15, 12).astimezone().utcoffset() for m in (1, 7)}


# ---- finding other programs ----

# npm puts a .cmd wrapper on PATH on Windows; arguments containing quotes or empty strings don't
# survive cmd.exe, so run the real executable the package ships instead.
NPM_BINARIES = {
    "claude": ["node_modules/@anthropic-ai/claude-code/bin/claude.exe"],
    "railway": ["node_modules/@railway/cli/bin/railway.exe"],
}


def find_program(name, extra=()):
    found = shutil.which(name)
    candidates = ([found] if found else []) + [str(p) for p in extra]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if OS == "windows" and p.suffix.lower() in (".cmd", ".bat", ".ps1"):
            for rel in NPM_BINARIES.get(name, []):
                real = p.parent / rel
                if real.exists():
                    return str(real)
            continue
        if p.exists() and (OS == "windows" or os.access(p, os.X_OK)):
            return str(p)
    return None


def program_candidates(name):
    """Usual install locations, for when PATH is minimal (launchd, Task Scheduler, systemd)."""
    if OS == "windows":
        appdata = Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming")
        return [HOME / f".local/bin/{name}.exe", appdata / "npm" / f"{name}.cmd",
                HOME / f".claude/local/{name}.exe"]
    common = [HOME / ".local/bin" / name, HOME / ".npm-global/bin" / name, HOME / ".claude/local" / name]
    if OS == "macos":
        common += [Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name]
    return common


def utf8_stdio():
    """The report draws with block characters; Windows pipes default to a legacy code page."""
    if OS == "windows":
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")
