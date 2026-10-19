"""SQLite storage. Editions are immutable (enforced by trigger). Schema version via PRAGMA user_version."""
import json, sqlite3
from datetime import datetime, timezone

def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

MIGRATIONS = [
    """
    CREATE TABLE profile(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE goals(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
    CREATE TABLE articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, url TEXT NOT NULL,
        canon_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        published TEXT, first_retrieved TEXT NOT NULL, content_hash TEXT NOT NULL,
        story_id INTEGER, dedup_note TEXT);
    CREATE INDEX idx_articles_story ON articles(story_id);
    CREATE TABLE editions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
        payload TEXT NOT NULL, profile_snapshot TEXT NOT NULL);
    CREATE TRIGGER editions_immutable BEFORE UPDATE ON editions
        BEGIN SELECT RAISE(ABORT, 'editions are immutable'); END;
    CREATE TABLE checks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
        outcome TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT);
    """,
]

def connect(path):
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(ver, len(MIGRATIONS)):
        conn.execute("BEGIN IMMEDIATE")
        # re-check inside the lock so two processes cannot both migrate
        if conn.execute("PRAGMA user_version").fetchone()[0] > i:
            conn.execute("ROLLBACK")
            continue
        for stmt in [s for s in MIGRATIONS[i].split(";\n") if s.strip()]:
            if "TRIGGER" in stmt and not stmt.rstrip().endswith("END"):
                stmt += ";"
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version={i + 1}")
        conn.execute("COMMIT")
    return conn

def get_state(conn, key, default=None):
    r = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_state(conn, key, value):
    conn.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))

def del_state(conn, key):
    conn.execute("DELETE FROM state WHERE key=?", (key,))

def jdump(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True)
