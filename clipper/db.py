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
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS auto_rounds (
  id INTEGER PRIMARY KEY, at REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY, at REAL, text TEXT, sent TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS limit_hits (
  platform TEXT PRIMARY KEY, hit_at REAL, message TEXT
);
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY, topic TEXT, videos INTEGER, shorts INTEGER, longs INTEGER, mode TEXT,
  subtitles INTEGER, voiceover INTEGER, platforms TEXT DEFAULT '[]', queries TEXT DEFAULT '[]',
  status TEXT DEFAULT 'queued', log TEXT DEFAULT '', created REAL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY, kind TEXT, status TEXT DEFAULT 'draft', card TEXT, footer TEXT DEFAULT '',
  caption TEXT DEFAULT '', tags TEXT DEFAULT '[]', link TEXT DEFAULT '', source TEXT DEFAULT '',
  key TEXT DEFAULT '', image TEXT DEFAULT '', uploads TEXT DEFAULT '{}', log TEXT DEFAULT '', created REAL,
  category_id INTEGER
);
CREATE TABLE IF NOT EXISTS post_categories (
  id INTEGER PRIMARY KEY, name TEXT, niche TEXT DEFAULT '', kinds TEXT DEFAULT '[]', accounts TEXT DEFAULT '[]',
  posts_per_day INTEGER DEFAULT 4, post_from INTEGER DEFAULT 9, post_to INTEGER DEFAULT 21,
  approve INTEGER DEFAULT 1, enabled INTEGER DEFAULT 0, handle TEXT DEFAULT '', logo INTEGER DEFAULT 1, created REAL
);
CREATE TABLE IF NOT EXISTS post_templates (
  id INTEGER PRIMARY KEY, name TEXT, label TEXT DEFAULT '', source TEXT DEFAULT 'ai', instructions TEXT DEFAULT '',
  format TEXT DEFAULT 'photo', bg TEXT DEFAULT '#111111', accent TEXT DEFAULT '#ff2e88', font TEXT DEFAULT 'sans',
  created REAL
);
CREATE TABLE IF NOT EXISTS brands (
  id INTEGER PRIMARY KEY, name TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS category_accounts (
  category_id INTEGER, account_id INTEGER, is_default INTEGER DEFAULT 0, PRIMARY KEY (category_id, account_id)
);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, platform TEXT, name TEXT, ext_id TEXT, page_id TEXT, token TEXT,
  limited_until REAL DEFAULT 0, created REAL
);
"""


def conn():
    c = sqlite3.connect(DATA / "clipper.db", timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, far fewer disk syncs (every log line is a write)
    return c


INDEXES = """
CREATE INDEX IF NOT EXISTS clips_job ON clips(job_id);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS jobs_topic ON jobs(topic_id);
CREATE INDEX IF NOT EXISTS topics_status ON topics(status);
CREATE INDEX IF NOT EXISTS posts_cat ON posts(category_id, status);
CREATE INDEX IF NOT EXISTS clips_cat ON clips(category_id);
CREATE INDEX IF NOT EXISTS catacc_acc ON category_accounts(account_id);
"""


def init():
    with conn() as c:
        # WAL: readers (the UI polls every 2s) never block the pipeline threads writing logs, and vice versa.
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for table, col in (("jobs", "mode TEXT DEFAULT 'hype'"), ("jobs", "subtitles INTEGER DEFAULT 1"),
                           ("clips", "headline TEXT DEFAULT ''"), ("jobs", "voiceover INTEGER DEFAULT 0"),
                           ("jobs", "topic_id INTEGER"), ("jobs", """platforms TEXT DEFAULT '["youtube"]'"""),
                           ("clips", "uploads TEXT DEFAULT '{}'"), ("clips", "views TEXT DEFAULT '{}'"), ("topics", "face_check INTEGER DEFAULT 1"), ("topics", "style TEXT DEFAULT ''"),
                           ("jobs", "aspect REAL"), ("topics", "aspect REAL"),
                           ("jobs", "hq INTEGER DEFAULT 0"), ("topics", "hq INTEGER DEFAULT 0"),
                           ("topics", "source TEXT DEFAULT 'manual'"), ("topics", "pool INTEGER"),
                           ("posts", "category_id INTEGER"), ("accounts", "error TEXT DEFAULT ''"),
                           ("posts", "template_id INTEGER"), ("post_categories", "templates TEXT DEFAULT '[]'"),
                           ("post_categories", "logo_file TEXT DEFAULT ''"),
                           # categories for clips too, destinations, per-platform captions, scheduling, renders
                           ("post_categories", "color TEXT DEFAULT '#6b8f86'"), ("post_categories", "description TEXT DEFAULT ''"),
                           ("accounts", "avatar TEXT DEFAULT ''"),
                           ("jobs", "category_id INTEGER"), ("jobs", "targets TEXT DEFAULT '[]'"), ("jobs", "progress REAL"),
                           ("clips", "category_id INTEGER"), ("clips", "targets TEXT DEFAULT '[]'"),
                           ("clips", "captions TEXT DEFAULT '{}'"), ("clips", "publish_at REAL"),
                           ("clips", "renders TEXT DEFAULT '{}'"), ("clips", "progress REAL"),
                           ("posts", "targets TEXT DEFAULT '[]'"), ("posts", "publish_at REAL"),
                           ("posts", "media TEXT DEFAULT '[]'"), ("clips", "words TEXT DEFAULT '[]'"),
                           ("topics", "category_id INTEGER"),
                           # brands / pages: each owns its accounts and categories
                           ("accounts", "brand_id INTEGER"), ("post_categories", "brand_id INTEGER"),
                           ("post_categories", "keywords TEXT DEFAULT ''"),
                           ("clips", "signals TEXT DEFAULT '{}'"),
                           ("posts", "slides TEXT DEFAULT '[]'"),
                           # learning from engagement (engage.py): what each post / clip was, and how it did
                           ("posts", "features TEXT DEFAULT '{}'"), ("posts", "stats TEXT DEFAULT '{}'"),
                           ("posts", "video TEXT DEFAULT ''"), ("clips", "features TEXT DEFAULT '{}'"),
                           ("topics", "autopilot_id INTEGER"), ("posts", "auto_id INTEGER"),
                           ("post_templates", "picture INTEGER DEFAULT 1")):  # added after v0.1
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already there
        c.executescript(INDEXES)  # after the ALTERs: jobs.topic_id / posts.category_id may be new columns
        # Threads die with the server; don't show them as still running.
        c.execute("UPDATE jobs SET status='interrupted' WHERE status NOT IN ('done','failed','stopped')")
        # A clip cut off mid-render / mid-upload by the restart: say so (Retry / re-render), not "rendering" forever.
        c.execute("UPDATE clips SET status='render failed' WHERE status='rendering'")
        c.execute("UPDATE clips SET status='upload failed' WHERE status='uploading'")
        c.execute("UPDATE posts SET status='failed' WHERE status IN ('making','posting')")
        # Topics still 'queued' are picked up again by topics.loop(); only a half-done one is interrupted.
        # An autopilot topic cut off by the restart runs again (it skips videos it already clipped); others wait for you.
        c.execute("UPDATE topics SET status='queued' WHERE source='auto' AND status NOT IN ('queued','done','failed','stopped','interrupted')")
        c.execute("UPDATE topics SET status='interrupted' WHERE status NOT IN ('queued','done','failed','stopped')")
        c.execute("UPDATE posts SET status='failed' WHERE status IN ('making','posting')")


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


def get_setting(key):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None


def set_setting(key, value):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


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
