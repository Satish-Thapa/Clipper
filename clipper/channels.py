"""Clip a YouTube channel: its best videos by views and how recently they went up, one after another.

Videos LONGER than `min_minutes` are clipped like any video (the job's shorts / longs, its style and uploads).
Shorter ones are posted WHOLE: the full video formatted like the other clips (layout, header, subtitles, hook) as one
clip (worker._whole). Videos that already have a job are skipped, so running a channel again only adds new ones."""
import json
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
        return []  # a channel without that tab (no streams / no videos)
    return [{"id": e["id"], "url": f"https://www.youtube.com/watch?v={e['id']}", "title": e.get("title") or "",
             "duration": float(e.get("duration") or 0), "views": int(e.get("view_count") or 0), "rank": i, "tab": tab}
            for i, e in enumerate(es) if e and e.get("id") and e.get("live_status") not in ("is_live", "is_upcoming")]


def videos(channel, pool=30):
    """The channel's newest `pool` uploads and `pool` past streams (flat: title, duration, views; no download)."""
    base = channel_url(channel)
    return _tab(base, "videos", pool) + _tab(base, "streams", pool)


def choose(vids, count):
    """The best `count` by views AND how recently they went up: each video's place by views + its place by upload
    order (newest = 0, uploads and streams each from their tab), lowest total first, more views on a tie.
    -> [video + "why": "#2 by views, #1 newest"]."""
    vids = [v for v in vids if v["duration"]]
    by_views = {v["id"]: i for i, v in enumerate(sorted(vids, key=lambda v: -v["views"]))}
    best = sorted(vids, key=lambda v: (by_views[v["id"]] + v["rank"], -v["views"]))[:count]
    return [{**v, "why": f"#{by_views[v['id']] + 1} by views, #{v['rank'] + 1} newest {'stream' if v['tab'] == 'streams' else 'upload'}"}
            for v in best]


def plan(channel, count, min_minutes):
    """-> the videos to do, each with "whole": True when it's shorter than min_minutes (posted whole) and "done"
    when it already has a job."""
    done = {j["url"] for j in db.all("jobs")}
    out = []
    for v in choose(videos(channel), count):
        out.append({**v, "whole": 0 < v["duration"] < min_minutes * 60, "done": v["url"] in done})
    return out


def start(channel, count, min_minutes, job):
    """Plan now (a few seconds), then make one job per new video in the background, one at a time.
    job: the job settings (counts, style, framing, platforms...) as for /api/jobs, without the url."""
    if STATE["running"]:
        raise RuntimeError(f"already working through {STATE['channel']}: stop it first or wait")
    todo = plan(channel, count, min_minutes)
    ABORT.clear()
    STATE.update(running=True, channel=channel_url(channel), plan=todo, at=time.time())
    db.syslog(f"channel {STATE['channel']}: {len(todo)} video(s) picked ("
              + "; ".join(f"{v['why']} {v['title'][:50]!r} {v['duration'] / 60:.0f} min, {v['views']:,} views"
                          f"{' (whole)' if v['whole'] else ''}{' (already done)' if v['done'] else ''}" for v in todo) + ")")
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
                                       "notes": notes, "created": time.time()})
            v["job"] = jid
            db.log(jid, f"from channel {STATE['channel']} ({v['why']}, {v['views']:,} views): " + ("shorter than the limit, so posted WHOLE in the clip format"
                                                     if v["whole"] else "clipped"))
            worker.run(jid)
    except Exception as e:
        db.syslog(f"channel {STATE['channel']}: ERROR {e!r}")
    finally:
        STATE["running"] = False


def stop():
    ABORT.set()
    return STATE["running"]
