import sqlite3
import time

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, url TEXT, title TEXT, is_live INTEGER DEFAULT 0,
  shorts INTEGER, longs INTEGER, auto_upload INTEGER,
  status TEXT DEFAULT 'queued', log TEXT DEFAULT '', created REAL, mode TEXT DEFAULT 'hype',
  subtitles INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY, job_id INTEGER, kind TEXT, start REAL, "end" REAL,
  score REAL, title TEXT, description TEXT, tags TEXT, reason TEXT,
  path TEXT, youtube_id TEXT, status TEXT DEFAULT 'rendered', headline TEXT DEFAULT ''
);
"""


def conn():
    c = sqlite3.connect(DATA / "clipper.db", timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for table, col in (("jobs", "mode TEXT DEFAULT 'hype'"), ("jobs", "subtitles INTEGER DEFAULT 1"),
                           ("clips", "headline TEXT DEFAULT ''")):  # columns added after v0.1
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already there
        # Threads die with the server; don't show them as still running.
        c.execute("UPDATE jobs SET status='interrupted' WHERE status NOT IN ('done','failed','stopped')")


def insert(table, **row):
    with conn() as c:
        cols = ",".join(f'"{k}"' for k in row)
        cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        return cur.lastrowid


def update(table, id, **row):
    with conn() as c:
        sets = ",".join(f'"{k}"=?' for k in row)
        c.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*row.values(), id])


def delete(table, id):
    with conn() as c:
        c.execute(f"DELETE FROM {table} WHERE id=?", (id,))


def get(table, id):
    with conn() as c:
        r = c.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone()
        return dict(r) if r else None


def all(table, where="1", args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC", args)]


def log(job_id, msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    print(f"job {job_id}: {line}", end="")
    with conn() as c:
        c.execute("UPDATE jobs SET log = log || ? WHERE id=?", (line, job_id))
