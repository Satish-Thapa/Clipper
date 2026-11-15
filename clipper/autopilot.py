"""Auto research: once a day, find what's trending in your niche, queue it as topics, and let the normal flow
(topics -> clippability + face check -> clip -> upload) do the rest.

How a "round" finds trends (YouTube removed its Trending page in 2025, so this uses real search data):
  1. DeepSeek turns the niche ("GTA 5, GTA 6 and gaming") into search queries.
  2. Those are searched on YouTube (US, uploaded THIS WEEK); the most-viewed titles are collected.
  3. DeepSeek reads those titles + view counts and names 1-3 topics that are trending right now.
  4. `videos_per_day` is split over those topics, and each is queued in the normal topic queue (source='auto').
The loop checks every 10 minutes: when enabled and today's round hasn't run yet (after `hour`), it runs one.

Upload limits: a platform whose API said "limit reached" in the last 24h (social.limit_timers) is left out of the
round's uploads, and autopilot clips skip it (worker.upload_clip). If ALL its platforms are limited, the round is
skipped (no "day" recorded), so the loop runs it once the timers end. Manual uploads are never held back."""
import json
import threading
import time

from . import db, download, pick, social, topics, worker
from .config import CROP_ASPECT

DEFAULTS = {"enabled": False, "niche": "GTA 5, GTA 6 and gaming", "research_per_day": 10, "videos_per_day": 3,
            "shorts": 2, "longs": 0,
            "mode": "hype", "aspect": CROP_ASPECT, "hq": False, "face_check": True, "subtitles": True,
            "voiceover": False, "platforms": ["youtube"], "hour": 9,
            "upload_every": 0}  # minutes between autopilot uploads; 0 = upload each clip as soon as it's ready
# No niche set = it's up to the autopilot: search broadly for what clips well and is big this week.
ANY_NICHE = ("anything trending this week that makes great short clips: gaming (GTA, Fortnite, Minecraft...), "
             "streamer moments, podcasts, sports highlights, viral events")
LOCK = threading.Lock()  # one round at a time (loop + "Run now")
STAGE = [""]  # what the running round is doing right now, for the UI

TREND_SYSTEM = """You spot what is trending right now for a viral clip channel. You get the most-viewed YouTube
videos uploaded THIS WEEK for the channel's niche (title, channel, views). Find the 1-3 topics that clearly have the
most momentum: the same game/event/creator/news across several big videos. Prefer topics with gameplay or real
scenes that can be clipped (not lore explainers, not trailers alone). Each topic is a short search phrase
(3-6 words), the way people type it. Reply as JSON:
{"topics": [{"topic": "...", "why": "one line: which videos show it's trending"}]}"""


def settings():
    raw = db.get_setting("auto")
    return {**DEFAULTS, **(json.loads(raw) if raw else {})}


def save(new):
    s = {**settings(), **{k: v for k, v in new.items() if k in DEFAULTS}}
    db.set_setting("auto", json.dumps(s))
    db.syslog(f"auto research settings saved: {s}")
    return s


def status():
    """For the UI: settings, the last round, whether one is running and what it's doing, the upload schedule."""
    raw = db.get_setting("auto_last")
    every, last_up = int(settings()["upload_every"] or 0), float(db.get_setting("auto_last_upload") or 0)
    return {"settings": settings(), "last": json.loads(raw) if raw else None, "running": LOCK.locked(),
            "stage": STAGE[0] if LOCK.locked() else "",
            "scheduled": len(db.all("clips", "status='scheduled'")),
            "next_upload": max(last_up + every * 60, time.time()) if every else None}


def upload_tick():
    """Upload the oldest 'scheduled' clip if `upload_every` minutes have passed since the last autopilot upload.
    Clips whose platforms are all at their upload limit are left waiting (the next one gets the slot)."""
    every = int(settings()["upload_every"] or 0)
    waiting = list(reversed(db.all("clips", "status='scheduled'")))  # oldest first
    if not waiting:
        return None
    last = float(db.get_setting("auto_last_upload") or 0)
    if every and time.time() - last < every * 60:
        return None
    limited = social.limit_timers()
    for c in waiting:
        job = db.get("jobs", c["job_id"]) or {}
        if any(p not in limited for p in json.loads(job.get("platforms") or "[]")):
            db.set_setting("auto_last_upload", str(time.time()))
            db.log(c["job_id"], f"autopilot schedule: uploading #{c['id']} now ({len(waiting) - 1} more waiting, "
                                f"one every {every} min)")
            worker.upload_clip(c["id"])
            return c["id"]
    return None


def rounds(limit=20):
    """Past rounds, newest first, each with its full research record (Auto page activity view)."""
    with db.conn() as c:
        return [{"id": r["id"], **json.loads(r["data"])}
                for r in c.execute("SELECT * FROM auto_rounds ORDER BY id DESC LIMIT ?", (limit,))]


def _save(rec):
    db.set_setting("auto_last", json.dumps(rec))
    db.insert("auto_rounds", at=rec["at"], data=json.dumps(rec))
    return rec


def trending_topics(niche, k, research=None):
    """-> [{"topic", "why"}] from this week's most-viewed YouTube videos for the niche (empty niche = anything).
    `research` (a dict) is filled with what was searched and seen, for the activity view."""
    research = research if research is not None else {}
    niche = niche.strip() or ANY_NICHE
    STAGE[0] = "asking DeepSeek for search queries"
    queries = pick.search_queries(niche)
    research["queries"] = []
    seen = {}
    for q in queries:
        STAGE[0] = f"searching YouTube: {q}"
        try:
            found = download.search(q, n=20, recent=download.RECENT[0])
            for e in found:
                seen[e["id"]] = e
            research["queries"].append({"q": q, "found": len(found)})
        except Exception as ex:
            research["queries"].append({"q": q, "found": 0, "error": str(ex)[:150]})
            db.syslog(f"auto research: search '{q}' failed: {ex!r}")
    top = sorted(seen.values(), key=lambda e: e.get("view_count") or 0, reverse=True)[:40]
    db.syslog(f"auto research: {len(seen)} videos this week for '{niche}' ({len(queries)} searches), top {len(top)} sent to DeepSeek")
    rows = [{"title": e.get("title"), "channel": e.get("channel"), "views": e.get("view_count")} for e in top]
    research["seen"] = len(seen)
    research["top"] = [{**r, "id": e["id"], "minutes": round((e.get("duration") or 0) / 60)} for r, e in zip(rows, top)]
    STAGE[0] = f"DeepSeek reading {len(top)} top videos to spot trends"
    out = []
    for t in pick._ask(f"Niche: {niche}\nMost-viewed videos this week:\n{json.dumps(rows, indent=1)}\n"
                       f"Return at most {k} topics.", TREND_SYSTEM, "topics"):
        if isinstance(t, dict) and str(t.get("topic", "")).strip():
            out.append({"topic": str(t["topic"]).strip()[:100], "why": str(t.get("why", ""))[:200]})
    fallback = top[0]["title"] if niche == ANY_NICHE and top else niche  # nothing clear: the biggest video's subject
    return out[:k] or [{"topic": fallback[:100], "why": "DeepSeek found no clear trend, using the top result"}]


def run_round(reason="scheduled"):
    """Find trending topics and queue them. Returns the round record (also saved as setting 'auto_last')."""
    if not LOCK.acquire(blocking=False):
        return {"error": "a round is already running"}
    try:
        s, t0 = settings(), time.time()
        n = max(1, int(s["videos_per_day"]))
        limited = social.limit_timers()
        blocked = [p for p in s["platforms"] if p in limited]
        platforms = [p for p in s["platforms"] if p not in limited]
        if s["platforms"] and not platforms:  # every platform it uploads to is at its limit: don't clip for nothing
            until = max(limited[p]["until"] for p in blocked)
            rec = {"at": time.time(), "day": None, "reason": reason, "topics": [], "paused_until": until,
                   "settings": s, "skipped_platforms": blocked,
                   "error": f"paused: upload limit reached on {', '.join(blocked)}; resumes after "
                            f"{time.strftime('%d %b %H:%M', time.localtime(until))}"}
            db.syslog(f"auto research round ({reason}) skipped: {rec['error']}")
            return _save(rec)
        if blocked:
            db.syslog(f"auto research: {', '.join(blocked)} at upload limit, this round uploads to {', '.join(platforms)}")
        db.syslog(f"auto research round ({reason}): niche '{s['niche'] or 'anything (up to the autopilot)'}', {n} videos")
        research = {}
        found = trending_topics(s["niche"], min(3, n), research)
        STAGE[0] = "queueing topics"
        split = lambda total: [total // len(found) + (1 if i < total % len(found) else 0) for i in range(len(found))]
        per = split(n)  # videos to clip, split over the topics
        pools = split(max(int(s.get("research_per_day") or n * 3), n))  # videos to research (judge), split the same way
        queued = []
        for t, k, pool in zip(found, per, pools):
            if k <= 0:
                continue
            tid = topics.enqueue(topic=t["topic"], videos=k, shorts=s["shorts"], longs=s["longs"], mode=s["mode"],
                                 subtitles=int(s["subtitles"]), voiceover=int(s["voiceover"]), face_check=int(s["face_check"]),
                                 aspect=s["aspect"], hq=int(s["hq"]), platforms=json.dumps(platforms), source="auto",
                                 pool=max(pool, k))
            db.log(tid, f"queued by auto research ({reason}): {t['why']}", "topics")
            queued.append({**t, "videos": k, "research": max(pool, k), "topic_id": tid})
            db.syslog(f"auto research: queued T{tid} '{t['topic']}' x{k} videos: {t['why']}")
        rec = {"at": time.time(), "day": time.strftime("%Y-%m-%d"), "reason": reason, "topics": queued,
               "seconds": round(time.time() - t0), "skipped_platforms": blocked, "platforms": platforms,
               "settings": s, "research": research}
        return _save(rec)
    except Exception as e:
        db.syslog(f"auto research round failed: {e!r}", exc=True)
        return _save({"at": time.time(), "day": time.strftime("%Y-%m-%d"), "reason": reason, "error": str(e)[:300],
                      "topics": []})
    finally:
        STAGE[0] = ""
        LOCK.release()


def _loop():
    tick = 0
    while True:
        try:
            upload_tick()  # every minute: is it time for the next scheduled upload?
            if tick % 10 == 0:  # every 10 minutes: is today's round due?
                s, last = settings(), status()["last"]
                today = time.strftime("%Y-%m-%d")
                if s["enabled"] and (not last or last.get("day") != today) and time.localtime().tm_hour >= int(s["hour"]):
                    run_round("daily")
        except Exception as e:
            db.syslog(f"auto research loop error: {e!r}", exc=True)
        tick += 1
        time.sleep(60)


def start_loop():
    threading.Thread(target=_loop, daemon=True).start()
