"""Reset everything: stop all work, then delete every video, clip, post, topic, round, report, view count, limit
timer and log. Logins (YouTube/TikTok tokens, YouTube cookies) and the autopilot settings are kept unless asked.
.env is never touched. The Settings page calls this after you type RESET."""
import logging
import shutil
import time

from . import autopilot, db, stats, topics, worker
from .config import DATA, YT_COOKIES, YT_TOKEN, TIKTOK_TOKEN

TABLES = ("jobs", "clips", "topics", "auto_rounds", "news", "news_runs", "reports", "limit_hits")
LOGINS = (YT_TOKEN, TIKTOK_TOKEN, YT_COOKIES)


def _size(path):
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def storage():
    """What's stored now (Settings page)."""
    with db.conn() as c:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    return {"counts": counts, "videos_bytes": _size(DATA / "jobs"), "logs_bytes": _size(DATA / "logs"),
            "db_bytes": _size(DATA / "clipper.db"), "logins": [p.name for p in LOGINS if p.exists()]}


def _busy():
    return [j["id"] for j in db.all("jobs") if j["status"] not in ("done", "failed", "stopped", "interrupted")]


def reset_all(keep_settings=True, keep_logins=True):
    before = storage()
    db.syslog(f"RESET EVERYTHING requested (keep autopilot settings: {keep_settings}, keep logins: {keep_logins})")

    # 1. stop everything: autopilot OFF + its round/topics/posts, every other topic and job
    autopilot.stop_all()
    for t in db.all("topics"):
        if t["status"] not in ("done", "failed", "stopped", "interrupted"):
            topics.stop(t["id"])
    for jid in _busy():
        worker.stop(jid)
    for _ in range(30):  # give running jobs a moment to notice their stop flag (ffmpeg/yt-dlp steps finish first)
        if not _busy():
            break
        time.sleep(1)
    still = _busy()

    # 2. the database
    auto = autopilot.settings()
    with db.conn() as c:
        for t in TABLES:
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM settings")
    if keep_settings:
        db.set_setting("auto", __import__("json").dumps({**auto, "enabled": False}))
    with db.conn() as c:
        c.execute("VACUUM")
    stats.LAST.update(totals=None, at=0, errors={})
    worker.STOPS.clear()
    topics.STOPS.clear()
    topics.CURRENT.clear()

    # 3. files: every job folder (sources, clips, news pictures), old leftovers, logs
    errors = []
    for p in [DATA / "jobs", DATA / "instagram_export", DATA / "upload_blocks.json"]:
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
        except OSError as e:
            errors.append(f"{p.name}: {e}")
    (DATA / "jobs").mkdir(exist_ok=True)
    for f in (DATA / "logs").glob("clipper.log.*"):  # rotated old logs
        f.unlink(missing_ok=True)
    shutil.rmtree(DATA / "logs" / "prompts", ignore_errors=True)  # saved AI prompts
    for h in logging.getLogger("clipper").handlers:  # the current log is held open: empty it instead
        if getattr(h, "stream", None):
            h.acquire()
            try:
                h.stream.seek(0)
                h.stream.truncate()
            finally:
                h.release()
    if not keep_logins:
        for p in LOGINS:
            p.unlink(missing_ok=True)

    freed = before["videos_bytes"] + before["logs_bytes"] + max(0, before["db_bytes"] - _size(DATA / "clipper.db"))
    db.syslog(f"RESET EVERYTHING done: {sum(before['counts'].values())} records and {freed / 1e9:.2f} GB removed"
              + (f"; jobs still finishing: {still}" if still else "") + (f"; errors: {errors}" if errors else ""))
    return {"removed": before["counts"], "freed_bytes": freed, "kept_settings": keep_settings,
            "kept_logins": keep_logins and before["logins"], "still_running": still, "errors": errors}
