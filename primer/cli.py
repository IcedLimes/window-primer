"""primer — command-line entry point."""

import argparse
import fcntl
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import cloud, config, ingest, planner, render, scheduler, store

ROOT = Path(__file__).resolve().parent.parent
LOCAL_BIN = Path.home() / ".local/bin/primer"
PLUGIN_LINK = config.CLAUDE_DIR / "skills/primer"


def _load_plan():
    try:
        return json.loads(config.PLAN_PATH.read_text())
    except (OSError, ValueError):
        return None


def _save_plan(result):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.PLAN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1, default=str))
    tmp.replace(config.PLAN_PATH)


def cmd_refresh(args, cfg):
    if args.if_stale is not None:
        plan = _load_plan()
        if plan and time.time() - plan["generated_at"] < args.if_stale * 3600:
            return 0
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.DATA_DIR / ".refresh.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # another refresh (timer or hook) is already running
        db = store.connect()
        ingest.ingest(db)
        result, hist = planner.plan(db, cfg)
        old = _load_plan()
        _save_plan(result)
        timer = scheduler.sync(result["schedule"])
        remote = cloud.sync(cfg, result["schedule"])
        cloud.pull_results(cfg, db)
    if remote and remote.startswith("cloud sync failed"):
        print(remote, file=sys.stderr)  # lands in the journal even with --quiet; retried next refresh
    if not args.quiet:
        sched = {d: t for d, t in result["schedule"].items() if t}
        print(f"Plan: {result['reason']}")
        print("Pings: " + ("  ".join(f"{d} {', '.join(t)}" for d, t in sched.items()) if sched else "none"))
        if old and old.get("schedule") != result["schedule"]:
            print("(schedule changed since the last plan)")
        print(f"systemd: {timer}")
        if remote:
            print(f"railway: {remote}")
    return 0


def cmd_report(args, cfg):
    db = store.connect()
    ingest.ingest(db)
    result, hist = planner.plan(db, cfg)
    if args.json:
        print(json.dumps(result, indent=1, default=str))
    else:
        print(render.report(result, hist, cfg))
    return 0


def cmd_status(args, cfg):
    db = store.connect()
    cloud.pull_results(cfg, db)
    print(render.status(db, _load_plan(), cfg, scheduler.next_elapse()))
    return 0


def cmd_ping(args, cfg):
    from . import ping
    db = store.connect()
    outcome, detail = ping.run(cfg, db, scheduled="timer" if args.scheduled else "manual", force=args.force)
    print(f"{outcome}: {detail}")
    return 1 if outcome == "error" else 0


def cmd_cloud(args, cfg):
    if args.action == "link":
        cfg["cloud"] = {"project": args.project, "environment": args.environment, "service": args.service}
        config.save(cfg)
        print("linked; pushing current plan…")
        args.action = "sync"
    if not cloud.target(cfg):
        print("not linked: primer cloud link --project ID --environment ID --service ID", file=sys.stderr)
        return 2
    if args.action == "sync":
        (config.DATA_DIR / "cloud_state.json").unlink(missing_ok=True)
        plan = _load_plan() or {"schedule": {}}
        print(cloud.sync(cfg, plan["schedule"]))
    elif args.action == "pull":
        print(f"{cloud.pull_results(cfg, store.connect())} new cloud ping result(s)")
    elif args.action == "cron":
        plan = _load_plan() or {"schedule": {}}
        print(cloud.cron_for(plan["schedule"], cloud.local_tz_name()))
    return 0


def cmd_statusline(args, cfg):
    from . import statusline
    return statusline.main()


def cmd_config(args, cfg):
    if args.key is None:
        print(json.dumps(cfg, indent=2))
        return 0
    if args.key not in config.DEFAULTS:
        print(f"unknown key {args.key}; known: {', '.join(config.DEFAULTS)}", file=sys.stderr)
        return 2
    if args.value is None:
        print(json.dumps(cfg[args.key]))
        return 0
    try:
        cfg[args.key] = json.loads(args.value)
    except ValueError:
        cfg[args.key] = args.value
    config.save(cfg)
    print(f"{args.key} = {json.dumps(cfg[args.key])}  (run `primer refresh` to re-plan)")
    return 0


def _statusline_command():
    return f"{LOCAL_BIN} statusline"


def _install_statusline():
    settings_path = config.CLAUDE_SETTINGS
    try:
        settings = json.loads(settings_path.read_text())
    except FileNotFoundError:
        settings = {}
    current = settings.get("statusLine")
    if current and "primer" not in current.get("command", ""):
        return f"left your existing statusLine alone ({current.get('command')}); live readings will come from pings only"
    if current and current.get("command") == _statusline_command():
        return "statusLine already installed"
    backup = settings_path.with_name(f"settings.json.primer-backup-{int(time.time())}")
    if settings_path.exists():
        shutil.copy2(settings_path, backup)
    settings["statusLine"] = {"type": "command", "command": _statusline_command(), "refreshInterval": 60}
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return f"statusLine installed in {settings_path} (backup: {backup.name})"


def _uninstall_statusline():
    try:
        settings = json.loads(config.CLAUDE_SETTINGS.read_text())
    except (OSError, ValueError):
        return "no settings.json"
    if "primer" not in (settings.get("statusLine") or {}).get("command", ""):
        return "statusLine not ours; left alone"
    del settings["statusLine"]
    config.CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    return "statusLine removed"


def _link(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        if link.is_symlink() and Path(os.readlink(link)) == target:
            return f"{link} already linked"
        if not link.is_symlink():
            return f"{link} exists and isn't a symlink; left alone"
        link.unlink()
    link.symlink_to(target)
    return f"linked {link} → {target}"


def cmd_install(args, cfg):
    steps = [_link(LOCAL_BIN, ROOT / "bin/primer")]
    if not args.no_plugin:
        steps.append(_link(PLUGIN_LINK, ROOT))
    if not args.no_statusline:
        steps.append(_install_statusline())
    db = store.connect()
    ingest.ingest(db)
    result, _ = planner.plan(db, cfg)
    _save_plan(result)
    steps.append(scheduler.install(result["schedule"]))
    for s in steps:
        print("•", s)
    print(f"\nPlan: {result['reason']}")
    print("Run `primer report` for the full picture, `primer status` any time.")
    return 0


def cmd_uninstall(args, cfg):
    scheduler.uninstall()
    print("• systemd units removed")
    print("•", _uninstall_statusline())
    for link in (PLUGIN_LINK, LOCAL_BIN):
        if link.is_symlink():
            link.unlink()
            print(f"• removed {link}")
    print(f"Data kept in {config.DATA_DIR} (delete it to forget everything).")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="primer", description="Time Claude's 5-hour usage windows around how you work.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh", help="ingest history, re-plan, update the ping timer")
    r.add_argument("--if-stale", type=float, metavar="HOURS", help="only if the plan is older than HOURS")
    r.add_argument("--quiet", action="store_true")
    rep = sub.add_parser("report", help="heatmap, budget, schedule, backtest and cross-validation")
    rep.add_argument("--json", action="store_true")
    sub.add_parser("status", help="current window, next pings, recent ping results")
    pg = sub.add_parser("ping", help="send a ping now")
    pg.add_argument("--force", action="store_true", help="ping even if a window is known to be open")
    pg.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("statusline", help="Claude Code statusLine command (reads JSON on stdin)")
    cl = sub.add_parser("cloud", help="Railway runner: link, sync the plan, pull ping results")
    cl.add_argument("action", choices=["link", "sync", "pull", "cron"])
    cl.add_argument("--project")
    cl.add_argument("--environment")
    cl.add_argument("--service")
    c = sub.add_parser("config", help="show or set a setting")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    i = sub.add_parser("install", help="link CLI + plugin, add statusline, install systemd timers")
    i.add_argument("--no-statusline", action="store_true")
    i.add_argument("--no-plugin", action="store_true")
    sub.add_parser("uninstall", help="remove timers, statusline and links (keeps data)")
    args = p.parse_args(argv)
    cfg = config.load()
    return globals()[f"cmd_{args.cmd}"](args, cfg)
