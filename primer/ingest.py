"""Incrementally load Claude Code transcripts into the store."""

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, pricing

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# "resets 6:40pm (America/Los_Angeles)", "resets 1am", "resets Oct 3, 5pm (UTC)", "reset at 3pm"
RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)\b(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE)
LEGACY_RE = re.compile(r"usage limit reached\|(\d{9,11})", re.IGNORECASE)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_limit_message(text, hit_ts):
    """Return (kind, reset_ts | None) for a limit message, or None if it isn't one."""
    low = text.lower()
    if "limit" not in low or not any(w in low for w in ("hit your", "limit reached", "reset")):
        return None
    kind = "weekly" if ("weekly" in low or "7-day" in low) else "session"
    legacy = LEGACY_RE.search(text)
    if legacy:
        return kind, float(legacy.group(1))
    m = RESET_RE.search(text)
    if not m:
        return kind, None
    try:
        tz = ZoneInfo(m.group("tz")) if m.group("tz") else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    hit = datetime.fromtimestamp(hit_ts, tz) if tz else datetime.fromtimestamp(hit_ts).astimezone()
    hour = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    minute = int(m.group("m") or 0)
    if m.group("mon") and m.group("mon").lower()[:3] in MONTHS:
        month, day = MONTHS[m.group("mon").lower()[:3]], int(m.group("day"))
        year = hit.year + (1 if month < hit.month else 0)
        reset = hit.replace(year=year, month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
    else:
        reset = hit.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= hit:
            reset += timedelta(days=1)
    return kind, reset.timestamp()


def _text_of(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def ingest_file(db, path):
    events, hits = 0, 0
    with open(path, errors="ignore") as fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant" or not d.get("timestamp"):
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            ts = parse_ts(d["timestamp"])
            model = msg.get("model")
            if model == "<synthetic>":
                parsed = parse_limit_message(_text_of(msg), ts)
                if parsed:
                    db.execute("INSERT OR IGNORE INTO limit_hits VALUES (?,?,?,?)",
                               (ts, parsed[1], parsed[0], _text_of(msg)[:300]))
                    hits += 1
                continue
            usage = msg.get("usage")
            mid = msg.get("id") or d.get("uuid")
            if not usage or not mid:
                continue
            # A response is logged once per content block; keep the earliest ts and the largest usage.
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "ts=min(ts, excluded.ts), cost=max(cost, excluded.cost), "
                "output_tokens=max(output_tokens, excluded.output_tokens)",
                (mid, ts, model, pricing.cost_usd(model, usage), usage.get("output_tokens") or 0))
            events += 1
    return events, hits


def ingest(db, projects_dir=None, force=False):
    projects_dir = projects_dir or config.PROJECTS_DIR
    scanned = 0
    for path in sorted(projects_dir.rglob("*.jsonl")):
        try:
            st = path.stat()
        except OSError:
            continue
        row = db.execute("SELECT mtime, size FROM files WHERE path=?", (str(path),)).fetchone()
        if not force and row and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
            continue
        ingest_file(db, path)
        db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (str(path), st.st_mtime, st.st_size))
        scanned += 1
    db.commit()
    return scanned
