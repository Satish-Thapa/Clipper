import logging
import logging.handlers
import sqlite3
import time
import traceback
from contextlib import contextmanager

from .config import DATA

# Everything (jobs, topics, system events, errors with tracebacks) also goes to one file that survives restarts:
# data/logs/clipper.log, rotated at 5 MB, 5 old files kept. The UI's Logs page shows its tail.
LOG_FILE = DATA / "logs" / "clipper.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_file = logging.getLogger("clipper")
if not _file.handlers:
    _h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S"))
    _file.addHandler(_h)
    _file.setLevel(logging.INFO)
    _file.propagate = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, url TEXT, title TEXT, is_live INTEGER DEFAULT 0,
  shorts INTEGER, longs INTEGER, auto_upload INTEGER,
  status TEXT DEFAULT 'queued', log TEXT DEFAULT '', created REAL, mode TEXT DEFAULT 'hype',
  subtitles INTEGER DEFAULT 1, voiceover INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY, job_id INTEGER, kind TEXT, start REAL, "end" REAL,
  score REAL, title TEXT, description TEXT, tags TEXT, reason TEXT,
  path TEXT, youtube_id TEXT, status TEXT DEFAULT 'rendered', headline TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY, topic TEXT, videos INTEGER, shorts INTEGER, longs INTEGER, mode TEXT,
  subtitles INTEGER, voiceover INTEGER, platforms TEXT DEFAULT '[]', queries TEXT DEFAULT '[]',
  status TEXT DEFAULT 'queued', log TEXT DEFAULT '', created REAL
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
                           ("clips", "headline TEXT DEFAULT ''"), ("jobs", "voiceover INTEGER DEFAULT 0"),
                           ("jobs", "topic_id INTEGER"), ("jobs", """platforms TEXT DEFAULT '["youtube"]'"""),
                           ("clips", "uploads TEXT DEFAULT '{}'"), ("clips", "views TEXT DEFAULT '{}'"), ("topics", "face_check INTEGER DEFAULT 1"),
                           ("jobs", "aspect REAL"), ("topics", "aspect REAL"),
                           ("jobs", "hq INTEGER DEFAULT 0"), ("topics", "hq INTEGER DEFAULT 0")):  # columns added after v0.1
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already there
        # Threads die with the server; don't show them as still running.
        c.execute("UPDATE jobs SET status='interrupted' WHERE status NOT IN ('done','failed','stopped')")
        # Topics still 'queued' are picked up again by topics.loop(); only a half-done one is interrupted.
        c.execute("UPDATE topics SET status='interrupted' WHERE status NOT IN ('queued','done','failed','stopped')")


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


def log(job_id, msg, table="jobs", exc=False):
    """One line into the job's/topic's own log (shown in the UI), the terminal and data/logs/clipper.log.
    exc=True (inside an `except`) also writes the full traceback to the file."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    who = f"{table[:-1]} {job_id}"
    print(f"{who}: {line}", end="")
    _file.log(logging.ERROR if exc else logging.INFO, f"[{who}] {msg}" + (f"\n{traceback.format_exc()}" if exc else ""))
    with conn() as c:
        c.execute(f"UPDATE {table} SET log = log || ? WHERE id=?", (line, job_id))


def syslog(msg, exc=False):
    """Events that belong to no job/topic (server start, GPU/encoder choice, view stats, WhatsApp...)."""
    print(f"system: {msg}")
    _file.log(logging.ERROR if exc else logging.INFO, f"[system] {msg}" + (f"\n{traceback.format_exc()}" if exc else ""))


@contextmanager
def step(job_id, what, table="jobs"):
    """Log `what`, run the block, log how long it took (or that it failed, with the traceback in the file)."""
    t = time.time()
    log(job_id, f"{what}...", table)
    try:
        yield
    except Exception:
        log(job_id, f"{what} FAILED after {time.time() - t:.1f}s", table, exc=True)
        raise
    log(job_id, f"{what} done in {time.time() - t:.1f}s", table)


def tail(lines=400):
    """Last `lines` lines of the log file (for the UI)."""
    if not LOG_FILE.exists():
        return ""
    with open(LOG_FILE, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - lines * 300))
        return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-lines:])
