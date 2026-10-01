"""SQLite store. Keeps its own copy of usage history so it survives Claude Code's transcript cleanup."""

import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,          -- API message id (responses repeat once per content block)
    ts REAL NOT NULL,             -- epoch seconds
    model TEXT,
    cost REAL NOT NULL,           -- API-equivalent USD
    output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS limit_hits (
    hit_ts REAL PRIMARY KEY,
    reset_ts REAL,                -- NULL when the message had no parseable reset time
    kind TEXT NOT NULL,           -- 'session' | 'weekly'
    text TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    ts REAL NOT NULL,
    source TEXT NOT NULL,         -- 'statusline' | 'ping'
    five_util REAL,               -- 0..1
    five_resets_at REAL,
    week_util REAL,
    week_resets_at REAL
);
CREATE INDEX IF NOT EXISTS observations_ts ON observations(ts);

CREATE TABLE IF NOT EXISTS pings (
    ts REAL NOT NULL,
    scheduled TEXT,               -- e.g. 'Mon 07:01', NULL for manual
    outcome TEXT NOT NULL,        -- 'opened' | 'inside-window' | 'skipped' | 'error'
    resets_at REAL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER
);
"""


def connect(path=None):
    path = path or config.DB_PATH
    if path != ":memory:":
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def add_observation(db, ts, source, five_util=None, five_resets_at=None, week_util=None, week_resets_at=None):
    """Insert unless identical to the latest observation (the statusline calls this constantly)."""
    last = db.execute("SELECT five_util, five_resets_at, week_util, week_resets_at FROM observations "
                      "ORDER BY ts DESC LIMIT 1").fetchone()
    row = (five_util, five_resets_at, week_util, week_resets_at)
    if last is not None and tuple(last) == row:
        return False
    db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", (ts, source, *row))
    db.commit()
    return True


def latest_observation(db):
    return db.execute("SELECT * FROM observations ORDER BY ts DESC LIMIT 1").fetchone()
