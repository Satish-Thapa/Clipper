"""Clip a YouTube channel: its N most recent videos and M most recent Shorts, most viewed first, one after another.

Videos LONGER than `min_minutes` are clipped like any video (the run's shorts / longs, its style and uploads).
Shorter ones are posted WHOLE: the full video formatted like the other clips (layout, header, subtitles) as one clip
(worker._whole). The channel's own Shorts are posted AS THEY ARE: same picture, sound and title, no cutting and
nothing burned in (worker._as_is). Anything that already has a job is skipped, so running a channel again only adds
new ones."""
import threading
import time

from . import db, download, worker

ABORT = threading.Event()  # "Stop channel": no more videos from the running channel list (the current job keeps going)
STATE = {"running": False, "channel": "", "plan": [], "at": 0}


def channel_url(c):
    from .autopilot import channel_url as cu
    return cu(c)


def _tab(base, tab, n):
    try:
        with download.ydl({"quiet": True, "extract_flat": True, "playlistend": n, "skip_download": True,
                           "no_warnings": True}) as y:
            es = y.extract_info(f"{base}/{tab}", download=False).get("entries") or []
    except Exception as e:
        if download.bot_checked(e):
            raise
        return []  # a channel without that tab (no streams / no videos / no shorts)
    return [{"id": e["id"], "url": f"https://www.youtube.com/watch?v={e['id']}", "title": e.get("title") or "",
             "duration": float(e.get("duration") or 0), "views": int(e.get("view_count") or 0), "rank": i, "tab": tab}
            for i, e in enumerate(es) if e and e.get("id") and e.get("live_status") not in ("is_live", "is_upcoming")]


def videos(channel, pool=30):
    """The channel's newest `pool` uploads and `pool` past streams (flat: title, duration, views; no download)."""
    base = channel_url(channel)
    return _tab(base, "videos", pool) + _tab(base, "streams", pool)


def shorts(channel, pool=30):
    """The channel's newest `pool` Shorts (flat; the Shorts tab often leaves out the duration)."""
    return _tab(channel_url(channel), "shorts", pool)


def choose(vids, count, label="newest"):
    """The `count` most recent (newest first on each tab, tabs taken in turn), sorted by views, most first: that's the
    order they're done and posted in. -> [video + "why": "#1 by views of the 5 newest"]."""
    recent = sorted(vids, key=lambda v: (v["rank"], v["tab"] != "videos"))[:count]
    best = sorted(recent, key=lambda v: -v["views"])
    kind = lambda v: {"streams": "stream", "shorts": "Short"}.get(v["tab"], "upload")
    return [{**v, "why": f"#{i + 1} by views of the {len(best)} {label} ({kind(v)})"} for i, v in enumerate(best)]


def plan(channel, count, min_minutes, short_count=0):
    """-> the videos, then the Shorts, to do. Each has "whole" (shorter than min_minutes: posted whole in the clip
    format), "as_is" (a Short: re-posted unchanged) and "done" (already has a job)."""
    done = {j["url"] for j in db.all("jobs")}
    out = []
    if count:
        for v in choose([v for v in videos(channel) if v["duration"]], count):
            out.append({**v, "whole": 0 < v["duration"] < min_minutes * 60, "as_is": False, "done": v["url"] in done})
    if short_count:
        for v in choose(shorts(channel), short_count, "newest Shorts"):
            out.append({**v, "whole": False, "as_is": True, "done": v["url"] in done})
    return out


def start(channel, count, min_minutes, job, short_count=0):
    """Plan now (a few seconds), then make one job per new video / Short in the background, one at a time.
    job: the job settings (counts, style, framing, platforms...) as for /api/jobs, without the url."""
    if STATE["running"]:
        raise RuntimeError(f"already working through {STATE['channel']}: stop it first or wait")
    todo = plan(channel, count, min_minutes, short_count)
    ABORT.clear()
    STATE.update(running=True, channel=channel_url(channel), plan=todo, at=time.time())
    db.syslog(f"channel {STATE['channel']}: {len(todo)} picked ("
              + "; ".join(f"{v['why']} {v['title'][:50]!r} {v['duration'] / 60:.0f} min, {v['views']:,} views"
                          f"{' (whole)' if v['whole'] else ' (as is)' if v['as_is'] else ''}"
                          f"{' (already done)' if v['done'] else ''}" for v in todo) + ")")
    threading.Thread(target=_run, args=(todo, job), daemon=True).start()
    return todo


def _run(todo, job):
    try:
        for v in todo:
            if ABORT.is_set():
                db.syslog(f"channel {STATE['channel']}: stopped, the other videos are skipped")
                break
            if v["done"]:
                continue
            topic = (f"Every clip stays on this video's topic, \"{v['title']}\": titles, headlines and hashtags about "
                     "what this video is about (same game / subject / people), never a different one.")
            notes = f"{job.get('notes') or ''}\n{topic}".strip()
            jid = db.insert("jobs", **{**job, "url": v["url"], "title": v["title"], "whole": int(v["whole"]),
                                       "as_is": int(v["as_is"]), "notes": notes, "created": time.time()})
            v["job"] = jid
            db.log(jid, f"from channel {STATE['channel']} ({v['why']}, {v['views']:,} views): "
                        + ("a Short, posted AS IT IS" if v["as_is"] else
                           "shorter than the limit, so posted WHOLE in the clip format" if v["whole"] else "clipped"))
            worker.run(jid)
    except Exception as e:
        db.syslog(f"channel {STATE['channel']}: ERROR {e!r}")
    finally:
        STATE["running"] = False


def stop():
    ABORT.set()
    return STATE["running"]
