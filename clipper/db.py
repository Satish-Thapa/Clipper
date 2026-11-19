import logging
import logging.handlers
import re
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS auto_rounds (
  id INTEGER PRIMARY KEY, at REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY, at REAL, text TEXT, sent TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY, link TEXT, title TEXT, source TEXT, kind TEXT, headline TEXT, clip_id INTEGER,
  auto INTEGER DEFAULT 0, at REAL
);
CREATE TABLE IF NOT EXISTS news_runs (
  id INTEGER PRIMARY KEY, at REAL, kind TEXT, topic TEXT, source TEXT DEFAULT 'manual', status TEXT DEFAULT 'searching',
  log TEXT DEFAULT '', data TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS limit_hits (
  platform TEXT PRIMARY KEY, hit_at REAL, message TEXT
);
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY, name TEXT, brief TEXT, rules TEXT DEFAULT '{}', platforms TEXT DEFAULT '[]',
  shorts INTEGER DEFAULT 3, max_seconds INTEGER, aspect REAL, layout TEXT DEFAULT '', subtitles INTEGER DEFAULT 1,
  onscreen INTEGER DEFAULT 1, trial_on_screen INTEGER DEFAULT 0, notes TEXT DEFAULT '',
  status TEXT DEFAULT 'ready', log TEXT DEFAULT '', created REAL
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
                           ("jobs", "hq INTEGER DEFAULT 0"), ("topics", "hq INTEGER DEFAULT 0"),
                           ("topics", "source TEXT DEFAULT 'manual'"), ("topics", "pool INTEGER"),
                           ("jobs", "chunk_minutes INTEGER"), ("jobs", "max_chunks INTEGER DEFAULT 0"), ("jobs", "upload_best INTEGER"), ("jobs", "upload_best_long INTEGER"),
                           ("jobs", "kind TEXT DEFAULT 'video'"), ("clips", "post_at REAL"), ("news", "run_id INTEGER"),
                           ("jobs", "layout TEXT DEFAULT ''"), ("topics", "layout TEXT DEFAULT ''"),
                           ("jobs", "follow INTEGER DEFAULT 1"), ("topics", "follow INTEGER DEFAULT 1"),
                           ("clips", "post_to TEXT"),
                           ("jobs", "notes TEXT DEFAULT ''"), ("topics", "notes TEXT DEFAULT ''"),
                           ("clips", "why TEXT DEFAULT '{}'"),
                           ("jobs", "campaign_id INTEGER"), ("clips", "caption TEXT DEFAULT ''")):  # campaign.py  # why: the Clips page "Why this clip?"  # post_to: platforms for a clip scheduled by hand  # columns added after v0.1
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already there
        # Threads die with the server; don't show them as still running.
        c.execute("UPDATE jobs SET status='interrupted' WHERE status NOT IN ('done','failed','stopped')")
        # Topics still 'queued' are picked up again by topics.loop(); only a half-done one is interrupted.
        c.execute("UPDATE topics SET status='interrupted' WHERE status NOT IN ('queued','done','failed','stopped')")
        c.execute("UPDATE campaigns SET status='ready' WHERE status='reading the brief'")  # its thread died


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


_SECRET = re.compile(r"((?:access_token|client_secret|refresh_token|api_key|key|token)=)[^&\s'\"]+", re.I)


def scrub(text):
    """Hide tokens in URLs (httpx errors include the request URL: '...?access_token=EAAM...') before anything is
    logged, stored or shown."""
    return _SECRET.sub(r"\1***", str(text))


def _print(text, **kw):
    """Console echo that never crashes: a cp1252 Windows console can't show symbols like ▶ ✓ ❌ (they're replaced)."""
    try:
        print(text, **kw)
    except UnicodeEncodeError:
        import sys
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc), **kw)


def log(job_id, msg, table="jobs", exc=False):
    """One line into the job's/topic's own log (shown in the UI), the terminal and data/logs/clipper.log.
    exc=True (inside an `except`) also writes the full traceback to the file. Tokens are scrubbed (see scrub)."""
    msg = scrub(msg)
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    who = f"{table[:-1]} {job_id}"
    _print(f"{who}: {line}", end="")
    _file.log(logging.ERROR if exc else logging.INFO, f"[{who}] {msg}" + (f"\n{scrub(traceback.format_exc())}" if exc else ""))
    with conn() as c:
        c.execute(f"UPDATE {table} SET log = log || ? WHERE id=?", (line, job_id))


def get_setting(key):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None


def set_setting(key, value):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def syslog(msg, exc=False):
    """Events that belong to no job/topic (server start, GPU/encoder choice, view stats, WhatsApp...)."""
    msg = scrub(msg)
    _print(f"system: {msg}")
    _file.log(logging.ERROR if exc else logging.INFO, f"[system] {msg}" + (f"\n{scrub(traceback.format_exc())}" if exc else ""))


_ctx = threading.local()  # where this thread's AI calls log: the running step's job/topic, or a run's own log


def here(msg):
    """Log into whatever this thread is working on (see log_to / step), or the system log when nothing."""
    sink = getattr(_ctx, "sink", None)
    sink(msg) if sink else syslog(msg)


@contextmanager
def log_to(fn):
    """Inside the block, here() lines go to fn (e.g. a News run's log)."""
    prev, _ctx.sink = getattr(_ctx, "sink", None), fn
    try:
        yield
    finally:
        _ctx.sink = prev


@contextmanager
def step(job_id, what, table="jobs"):
    """Log `what`, run the block, log how long it took (or that it failed, with the traceback in the file)."""
    t = time.time()
    log(job_id, f"{what}...", table)
    try:
        with log_to(lambda m: log(job_id, m, table)):
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
