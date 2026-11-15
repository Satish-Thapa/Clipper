"""Autopilot: you say how many clips to post a day and between which hours; it does the rest.

  posts_per_day + posting window (post_from..post_to)  ->  one post every (window / posts_per_day) minutes
  each round: trending topics -> clippability + face check -> clip -> clips wait as "scheduled" -> posted on the slots

How a round finds trends (YouTube removed its Trending page in 2025, so this uses real search data):
  1. DeepSeek turns the niche ("GTA 5, GTA 6 and gaming", or anything when empty) into search queries.
  2. Those are searched on YouTube (US, uploaded THIS WEEK); the most-viewed titles are collected.
  3. DeepSeek reads those titles + view counts and names 1-3 topics that are trending right now.
  4. It works out how many clips today still needs (posts_per_day - posted - waiting - being made), turns that
     into videos to clip (clips per video) and videos to research (x `choosy`), and queues the topics.
The first round runs at `hour`; if the day runs short (videos failed, too few passed the checks), up to MAX_ROUNDS
top-up rounds follow. Clips that miss today's window are posted first tomorrow.

Settings apply live: saving updates queued/running autopilot topics, unfinished jobs and the posting schedule.
OFF = no new rounds and no posts (work in progress finishes). stop_all() = OFF + stop every autopilot topic/job +
unschedule waiting clips.

Upload limits: a platform whose API said "limit reached" in the last 24h (social.limit_timers) is skipped by
autopilot uploads (worker.upload_clip); if ALL its platforms are limited, rounds pause until the timers end.
YouTube's bot check ("Sign in to confirm you're not a bot") pauses rounds too, for BOT_PAUSE or until new cookies."""
import json
import math
import threading
import time

from . import db, download, pick, social, topics, worker
from .config import CROP_ASPECT

DEFAULTS = {"enabled": False, "niche": "", "posts_per_day": 6, "post_from": 10, "post_to": 22,
            "shorts": 3, "longs": 0, "choosy": 3, "hour": 9, "keep_sources": False,
            "mode": "hype", "aspect": CROP_ASPECT, "hq": False, "face_check": True, "subtitles": True,
            "voiceover": False, "platforms": ["youtube"]}
# No niche set = it's up to the autopilot: search broadly for what clips well and is big this week.
ANY_NICHE = ("anything trending this week that makes great short clips: gaming (GTA, Fortnite, Minecraft...), "
             "streamer moments, podcasts, sports highlights, viral events")
MAX_ROUNDS = 3  # rounds per day: the daily one + top-ups when the day runs short
TOPUP_GAP = 45 * 60  # seconds after a round before a top-up may start
BOT_PAUSE = 2 * 3600  # after YouTube's bot check, rounds wait this long (or until cookies are added)
LOCK = threading.Lock()  # one round at a time (loop + "Run now")
STAGE = [""]  # what the running round is doing right now, for the UI
ABORT = threading.Event()  # set by stop_all(): the running round gives up at its next step
FINISHED = ("done", "failed", "stopped", "interrupted")

TREND_SYSTEM = """You spot what is trending right now for a viral clip channel. You get the most-viewed YouTube
videos uploaded THIS WEEK for the channel's niche (title, channel, views). Find the 1-3 topics that clearly have the
most momentum: the same game/event/creator/news across several big videos. Prefer topics with gameplay or real
scenes that can be clipped (not lore explainers, not trailers alone). Each topic is a short search phrase
(3-6 words), the way people type it. Reply as JSON:
{"topics": [{"topic": "...", "why": "one line: which videos show it's trending"}]}"""


class Stopped(Exception):
    pass


def settings():
    raw = db.get_setting("auto")
    stored = json.loads(raw) if raw else {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def save(new):
    """Save settings and apply them to what's already in flight (queued/running topics, unfinished jobs)."""
    s = {**settings(), **{k: v for k, v in new.items() if k in DEFAULTS}}
    s["post_from"] = min(max(int(s["post_from"]), 0), 23)
    s["post_to"] = min(max(int(s["post_to"]), s["post_from"] + 1), 24)
    db.set_setting("auto", json.dumps(s))
    db.syslog(f"autopilot settings saved: {s}")
    _apply_live(s)
    return s


def _active_topics():
    return [t for t in db.all("topics", "source='auto'") if t["status"] not in FINISHED]


def _apply_live(s):
    plats = json.dumps(s["platforms"])
    for t in _active_topics():  # a running topic reads its row again before each video (topics._run)
        db.update("topics", t["id"], shorts=s["shorts"], longs=s["longs"], mode=s["mode"], aspect=s["aspect"],
                  hq=int(s["hq"]), subtitles=int(s["subtitles"]), voiceover=int(s["voiceover"]),
                  face_check=int(s["face_check"]), platforms=plats)
        for j in db.all("jobs", f"topic_id={int(t['id'])}"):
            if j["status"] not in FINISHED:  # its clips aren't posted yet: they go where the settings say now
                db.update("jobs", j["id"], platforms=plats, auto_upload=int(bool(s["platforms"])))


def _day_start(now):
    return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))


def _posts():
    return json.loads(db.get_setting("auto_posts") or "[]")  # when each autopilot post went up


def schedule(s, now=None, posts=None):
    """Today's posting plan: window, spacing, how many are due / done, and when the next post may go up."""
    now = now or time.time()
    posts = _posts() if posts is None else posts
    n, day = max(1, int(s["posts_per_day"])), _day_start(now)
    w0, w1 = day + int(s["post_from"]) * 3600, day + int(s["post_to"]) * 3600
    every = (w1 - w0) / n
    today = [p for p in posts if p >= day]
    last = max(posts) if posts else 0
    due = 0 if now < w0 else min(n, int((min(now, w1) - w0) // every) + 1)
    if len(today) >= n or now >= w1:
        nxt = w0 + 86400  # done for today / window over: tomorrow's first slot
    else:
        nxt = max(w0 + len(today) * every, last + every / 2, now)  # its slot, never closer than half a gap
        nxt = nxt if nxt < w1 else w0 + 86400
    return {"posts_per_day": n, "window": [w0, w1], "every_min": round(every / 60), "posted_today": len(today),
            "due": due, "next_at": nxt, "can_post_now": len(today) < due and now >= last + every / 2 and now < w1}


def pipeline():
    """(clips waiting to be posted, clips the queued/running autopilot topics will still make, roughly)."""
    waiting = len(db.all("clips", "status='scheduled'"))
    coming = 0
    for t in _active_topics():
        jobs = db.all("jobs", f"topic_id={int(t['id'])}")
        left = t["videos"] - len([j for j in jobs if j["status"] in FINISHED])
        coming += max(0, left) * max(1, (t["shorts"] or 0) + (t["longs"] or 0))
    return waiting, coming


def need(s=None):
    """Clips today still needs that aren't posted, waiting or being made."""
    s = s or settings()
    sch, (waiting, coming) = schedule(s), pipeline()
    return max(0, sch["posts_per_day"] - sch["posted_today"] - waiting - coming)


def bot_paused():
    hit = download.cookies_state()["bot_check_at"]
    return hit if hit and time.time() - hit < BOT_PAUSE else None


def status():
    """For the UI: settings, today's plan, what's in the pipeline, the last round, what's running right now."""
    s = settings()
    raw = db.get_setting("auto_last")
    waiting, coming = pipeline()
    today = time.strftime("%Y-%m-%d")
    return {"settings": s, "last": json.loads(raw) if raw else None, "running": LOCK.locked(),
            "stage": STAGE[0] if LOCK.locked() else "", "schedule": schedule(s), "scheduled": waiting,
            "coming": coming, "need": need(s), "max_rounds": MAX_ROUNDS,
            "rounds_today": len([r for r in rounds(MAX_ROUNDS + 5) if r.get("day") == today and r.get("topics")]),
            "bot_check": bot_paused(), "cookies": download.cookies_state()["source"]}


def upload_tick(now=None):
    """Post the oldest 'scheduled' clip when its slot has come (see schedule()). Clips whose platforms are all at
    their upload limit are left waiting (the next one gets the slot). Nothing is posted while OFF."""
    s = settings()
    if not s["enabled"] or not s["platforms"]:
        return None
    waiting = list(reversed(db.all("clips", "status='scheduled'")))  # oldest first
    if not waiting or not schedule(s, now)["can_post_now"]:
        return None
    limited = social.limit_timers()
    if all(p in limited for p in s["platforms"]):
        return None
    c = waiting[0]
    posts = [p for p in _posts() if p > time.time() - 3 * 86400] + [time.time()]
    db.set_setting("auto_posts", json.dumps(posts))
    db.log(c["job_id"], f"autopilot: posting #{c['id']} (post {schedule(s)['posted_today']}/{s['posts_per_day']} "
                        f"today, {len(waiting) - 1} more waiting)")
    worker.upload_clip(c["id"], s["platforms"])  # today's platforms, even if they changed after it was made
    return c["id"]


def stop_all():
    """OFF + stop the running round, every queued/running autopilot topic and its job, and unschedule waiting clips."""
    s = save({"enabled": False})
    if LOCK.locked():
        ABORT.set()
    stopped = 0
    for t in _active_topics():
        topics.stop(t["id"])
        stopped += 1
    clips = db.all("clips", "status='scheduled'")
    for c in clips:
        db.update("clips", c["id"], status="not uploaded (autopilot stopped)")
        db.log(c["job_id"], f"#{c['id']} unscheduled: autopilot stopped")
    db.syslog(f"autopilot STOPPED by user: {stopped} topic(s) stopped, {len(clips)} scheduled clip(s) cancelled")
    return {"topics": stopped, "clips": len(clips), "round": LOCK.locked(), "settings": s}


def rounds(limit=20):
    """Past rounds, newest first, each with its full research record (Auto page activity view)."""
    with db.conn() as c:
        return [{"id": r["id"], **json.loads(r["data"])}
                for r in c.execute("SELECT * FROM auto_rounds ORDER BY id DESC LIMIT ?", (limit,))]


def _save(rec):
    db.set_setting("auto_last", json.dumps(rec))
    db.insert("auto_rounds", at=rec["at"], data=json.dumps(rec))
    return rec


def _step(text):
    if ABORT.is_set():
        raise Stopped()
    STAGE[0] = text


def trending_topics(niche, k, research=None):
    """-> [{"topic", "why"}] from this week's most-viewed YouTube videos for the niche (empty niche = anything).
    `research` (a dict) is filled with what was searched and seen, for the activity view."""
    research = research if research is not None else {}
    niche = niche.strip() or ANY_NICHE
    _step("asking DeepSeek for search queries")
    queries = pick.search_queries(niche)
    research["queries"] = []
    seen = {}
    for q in queries:
        _step(f"searching YouTube: {q}")
        try:
            found = download.search(q, n=20, recent=download.RECENT[0])
            for e in found:
                seen[e["id"]] = e
            research["queries"].append({"q": q, "found": len(found)})
        except Exception as ex:
            research["queries"].append({"q": q, "found": 0, "error": str(ex)[:150]})
            db.syslog(f"autopilot: search '{q}' failed: {ex!r}")
            if download.bot_checked(ex):
                raise RuntimeError("YouTube bot check ('Sign in to confirm you're not a bot'): add YouTube cookies "
                                   "on the Socials page") from None
    top = sorted(seen.values(), key=lambda e: e.get("view_count") or 0, reverse=True)[:40]
    db.syslog(f"autopilot: {len(seen)} videos this week for '{niche}' ({len(queries)} searches), top {len(top)} sent to DeepSeek")
    rows = [{"title": e.get("title"), "channel": e.get("channel"), "views": e.get("view_count")} for e in top]
    research["seen"] = len(seen)
    research["top"] = [{**r, "id": e["id"], "minutes": round((e.get("duration") or 0) / 60)} for r, e in zip(rows, top)]
    _step(f"DeepSeek reading {len(top)} top videos to spot trends")
    out = []
    for t in pick._ask(f"Niche: {niche}\nMost-viewed videos this week:\n{json.dumps(rows, indent=1)}\n"
                       f"Return at most {k} topics.", TREND_SYSTEM, "topics"):
        if isinstance(t, dict) and str(t.get("topic", "")).strip():
            out.append({"topic": str(t["topic"]).strip()[:100], "why": str(t.get("why", ""))[:200]})
    fallback = top[0]["title"] if niche == ANY_NICHE and top else niche  # nothing clear: the biggest video's subject
    return out[:k] or [{"topic": fallback[:100], "why": "DeepSeek found no clear trend, using the top result"}]


def plan(s, missing):
    """Clips missing -> (videos to clip, videos to research). Always at least 1 video."""
    per = max(1, int(s["shorts"]) + int(s["longs"]))
    videos = max(1, math.ceil(missing / per))
    return videos, videos * max(1, int(s["choosy"]))


def run_round(reason="scheduled"):
    """Find trending topics and queue enough videos for today's missing clips. Returns the round record."""
    if not LOCK.acquire(blocking=False):
        return {"error": "a round is already running"}
    ABORT.clear()
    try:
        s, t0 = settings(), time.time()
        limited = social.limit_timers()
        blocked = [p for p in s["platforms"] if p in limited]
        platforms = [p for p in s["platforms"] if p not in limited]
        if s["platforms"] and not platforms:  # every platform it uploads to is at its limit: don't clip for nothing
            until = max(limited[p]["until"] for p in blocked)
            rec = {"at": time.time(), "day": None, "reason": reason, "topics": [], "paused_until": until,
                   "settings": s, "skipped_platforms": blocked,
                   "error": f"paused: upload limit reached on {', '.join(blocked)}; resumes after "
                            f"{time.strftime('%d %b %H:%M', time.localtime(until))}"}
            db.syslog(f"autopilot round ({reason}) skipped: {rec['error']}")
            return _save(rec)
        missing = need(s)
        n, pool = plan(s, missing or max(1, int(s["shorts"]) + int(s["longs"])))  # "Run now" with nothing missing: 1 video
        db.syslog(f"autopilot round ({reason}): niche '{s['niche'] or 'anything (up to the autopilot)'}', "
                  f"{missing} clip(s) missing today -> clip {n} video(s), research {pool}"
                  + (f"; {', '.join(blocked)} at upload limit" if blocked else ""))
        research = {}
        found = trending_topics(s["niche"], min(3, n), research)
        _step("queueing topics")
        split = lambda total: [total // len(found) + (1 if i < total % len(found) else 0) for i in range(len(found))]
        queued = []
        for t, k, kp in zip(found, split(n), split(pool)):
            if k <= 0:
                continue
            tid = topics.enqueue(topic=t["topic"], videos=k, shorts=s["shorts"], longs=s["longs"], mode=s["mode"],
                                 subtitles=int(s["subtitles"]), voiceover=int(s["voiceover"]), face_check=int(s["face_check"]),
                                 aspect=s["aspect"], hq=int(s["hq"]), platforms=json.dumps(s["platforms"]), source="auto",
                                 pool=max(kp, k))
            db.log(tid, f"queued by autopilot ({reason}): {t['why']}", "topics")
            queued.append({**t, "videos": k, "research": max(kp, k), "topic_id": tid})
            db.syslog(f"autopilot: queued T{tid} '{t['topic']}' x{k} videos: {t['why']}")
        rec = {"at": time.time(), "day": time.strftime("%Y-%m-%d"), "reason": reason, "topics": queued,
               "seconds": round(time.time() - t0), "skipped_platforms": blocked, "platforms": platforms,
               "missing": missing, "settings": s, "research": research}
        return _save(rec)
    except Stopped:
        db.syslog("autopilot round stopped by user")
        return _save({"at": time.time(), "day": None, "reason": reason, "error": "stopped by you", "topics": []})
    except Exception as e:
        db.syslog(f"autopilot round failed: {e!r}", exc=True)
        bot = download.BOT_CHECK in str(e) or "bot check" in str(e)
        return _save({"at": time.time(), "day": None if bot else time.strftime("%Y-%m-%d"), "reason": reason,
                      "error": str(e)[:300], "topics": []})
    finally:
        STAGE[0] = ""
        ABORT.clear()
        LOCK.release()


def due_round(s, now=None):
    """Why a round should start now ('daily' / 'top-up'), or None."""
    now = now or time.time()
    lt = time.localtime(now)
    if not s["enabled"] or lt.tm_hour < int(s["hour"]) or lt.tm_hour >= int(s["post_to"]) or bot_paused():
        return None
    today = [r for r in rounds(MAX_ROUNDS + 5) if r.get("day") == time.strftime("%Y-%m-%d", lt)]
    if not today:
        return "daily" if need(s) > 0 else None
    if len(today) >= MAX_ROUNDS or now - today[0]["at"] < TOPUP_GAP or _active_topics():
        return None  # enough tries today / too soon / the last round's topics are still working
    return "top-up" if need(s) > 0 else None


def _loop():
    tick = 0
    while True:
        try:
            upload_tick()  # every minute: is a post due?
            if tick % 5 == 0:  # every 5 minutes: is a round due?
                why = due_round(settings())
                if why:
                    run_round(why)
        except Exception as e:
            db.syslog(f"autopilot loop error: {e!r}", exc=True)
        tick += 1
        time.sleep(60)


def start_loop():
    threading.Thread(target=_loop, daemon=True).start()
