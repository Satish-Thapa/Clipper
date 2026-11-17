"""Autopilot: you say how many clips to post a day and between which hours; it does the rest.

  posts_per_day + posting window (post_from..post_to)  ->  one post every (window / posts_per_day) minutes
  each round: trending topics -> clippability + face check -> clip -> clips wait as "scheduled" -> posted on the slots

How a round finds trends (YouTube removed its Trending page in 2025, so this uses real search data):
  1. DeepSeek turns the niche (or, when it's empty, Settings → "Autopilot with no niche looks for") into queries.
  2. Those are searched on YouTube (US, uploaded THIS WEEK); the most-viewed titles are collected.
  3. DeepSeek reads those titles + view counts and names 1-3 topics that are trending right now.
  4. It works out how many clips today still needs (posts_per_day - posted - waiting - being made), turns that
     into videos to clip (clips per video) and videos to research (x `choosy`), and queues the topics.
The first round runs at `hour`; if the day runs short (videos failed, too few passed the checks), up to MAX_ROUNDS
top-up rounds follow. Clips that miss today's window are posted first tomorrow.

Several autopilots can run side by side (Automate → Clips), each with its own niche, category, accounts and
posting hours; each has its own schedule, posting history, rounds and Stop. They're stored as a list ("autopilots").

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
from . import config

DEFAULTS = {"enabled": False, "name": "", "niche": "", "posts_per_day": 6, "post_from": 10, "post_to": 22,
            "shorts": 3, "longs": 0, "choosy": 3, "hour": 9, "keep_sources": False,
            "mode": "hype", "aspect": None, "hq": False, "face_check": True, "subtitles": True,
            "voiceover": False, "platforms": ["youtube"], "category_id": None}
# No niche set = it's up to the autopilot: search broadly for what clips well and is big this week.
MAX_ROUNDS = 3  # rounds per day per autopilot: the daily one + top-ups when the day runs short
TOPUP_GAP = 45 * 60  # seconds after a round before a top-up may start
BOT_PAUSE = 2 * 3600  # after YouTube's bot check, rounds wait this long (or until cookies are added)
LOCKS, STAGE, ABORT = {}, {}, {}  # per autopilot id: one round at a time / what it's doing / stop its round
_guard = threading.Lock()
FINISHED = ("done", "failed", "stopped", "interrupted")

TREND_SYSTEM = """You spot what is trending right now for a viral clip channel. You get the most-viewed YouTube
videos uploaded THIS WEEK for the channel's niche (title, channel, views). Find the 1-3 topics that clearly have the
most momentum: the same game/event/creator/news across several big videos. Prefer topics with gameplay or real
scenes that can be clipped (not lore explainers, not trailers alone). Each topic is a short search phrase
(3-6 words), the way people type it. Reply as JSON:
{"topics": [{"topic": "...", "why": "one line: which videos show it's trending"}]}"""


class Stopped(Exception):
    pass


def _lock(aid):
    with _guard:
        return LOCKS.setdefault(aid, threading.Lock())


def _abort(aid):
    with _guard:
        return ABORT.setdefault(aid, threading.Event())


def _clean(s):
    s = {**DEFAULTS, **{k: v for k, v in s.items() if k in DEFAULTS}, "id": int(s["id"])}
    s["aspect"] = s["aspect"] or config.CROP_ASPECT  # not chosen yet: the Settings default
    return s


def profiles():
    """Every autopilot. The first start turns the old single autopilot ("auto") into the first one."""
    raw = db.get_setting("autopilots")
    if raw is None:
        old = json.loads(db.get_setting("auto") or "{}")
        lst = [{**DEFAULTS, **{k: v for k, v in old.items() if k in DEFAULTS}, "id": 1}]
        db.set_setting("autopilots", json.dumps(lst))
        if db.get_setting("auto_posts"):
            db.set_setting("auto_posts_1", db.get_setting("auto_posts"))
        if db.get_setting("auto_last"):
            db.set_setting("auto_last_1", db.get_setting("auto_last"))
        raw = json.dumps(lst)
    return [_clean(s) for s in json.loads(raw)]


def settings(aid=None):
    """One autopilot's settings (default: the first)."""
    lst = profiles()
    return next((s for s in lst if s["id"] == int(aid)), None) if aid else lst[0] if lst else _clean({"id": 1})


def _store(lst):
    db.set_setting("autopilots", json.dumps([{k: v for k, v in s.items() if k in DEFAULTS or k == "id"} for s in lst]))


def save(new, aid=None):
    """Save one autopilot's settings and apply them to what's already in flight (its topics, unfinished jobs)."""
    lst = profiles()
    cur = next((s for s in lst if s["id"] == int(aid)), None) if aid else lst[0]
    if not cur:
        raise ValueError("no such autopilot")
    s = {**cur, **{k: v for k, v in new.items() if k in DEFAULTS}}
    s["post_from"] = min(max(int(s["post_from"]), 0), 23)
    s["post_to"] = min(max(int(s["post_to"]), s["post_from"] + 1), 24)
    _store([s if x["id"] == s["id"] else x for x in lst])
    db.syslog(f"autopilot {s['id']} settings saved: {s}")
    _apply_live(s)
    return s


def create(new=None):
    """A new autopilot (off until you switch it on)."""
    lst = profiles()
    aid = max([s["id"] for s in lst] + [0]) + 1
    s = _clean({**DEFAULTS, **(new or {}), "id": aid, "enabled": False})
    _store(lst + [s])
    return s


def remove(aid):
    """Delete an autopilot: it's stopped first (its topics, jobs and waiting clips)."""
    lst = profiles()
    if len(lst) <= 1:
        raise ValueError("keep at least one autopilot (switch it off instead)")
    stop_all(aid)
    _store([s for s in lst if s["id"] != int(aid)])


def _first_id():
    lst = profiles()
    return lst[0]["id"] if lst else 1


def _topic_aid(t):
    return t.get("autopilot_id") or _first_id()  # topics from before there were several: the first autopilot's


def _active_topics(aid=None):
    return [t for t in db.all("topics", "source='auto'") if t["status"] not in FINISHED
            and (aid is None or _topic_aid(t) == int(aid))]


def _apply_live(s):
    plats = json.dumps(s["platforms"])
    for t in _active_topics(s["id"]):  # a running topic reads its row again before each video (topics._run)
        db.update("topics", t["id"], category_id=s["category_id"], shorts=s["shorts"], longs=s["longs"], mode=s["mode"], aspect=s["aspect"],
                  hq=int(s["hq"]), subtitles=int(s["subtitles"]), voiceover=int(s["voiceover"]),
                  face_check=int(s["face_check"]), platforms=plats)
        for j in db.all("jobs", f"topic_id={int(t['id'])}"):
            if j["status"] not in FINISHED:  # its clips aren't posted yet: they go where the settings say now
                db.update("jobs", j["id"], platforms=plats, auto_upload=int(bool(s["platforms"])))


def _day_start(now):
    return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))


def _posts(aid=None):
    return json.loads(db.get_setting(f"auto_posts_{aid or _first_id()}") or "[]")  # when each of its posts went up


def clip_aid(c):
    """Which autopilot made a clip (None: not an autopilot clip)."""
    job = db.get("jobs", c["job_id"]) or {}
    t = db.get("topics", job["topic_id"]) if job.get("topic_id") else None
    return _topic_aid(t) if t and t.get("source") == "auto" else None


def waiting_clips(aid):
    """Its clips waiting for a posting slot, oldest first (clips scheduled for a time: worker.publish_due)."""
    return [c for c in reversed(db.all("clips", "status='scheduled' AND publish_at IS NULL")) if clip_aid(c) == int(aid)]


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


def pipeline(aid=None):
    """(its clips waiting to be posted, clips its queued/running topics will still make, roughly)."""
    aid = aid or _first_id()
    waiting = len(waiting_clips(aid))
    coming = 0
    for t in _active_topics(aid):
        jobs = db.all("jobs", f"topic_id={int(t['id'])}")
        left = t["videos"] - len([j for j in jobs if j["status"] in FINISHED])
        coming += max(0, left) * max(1, (t["shorts"] or 0) + (t["longs"] or 0))
    return waiting, coming


def need(s=None):
    """Clips today still needs that aren't posted, waiting or being made."""
    s = s or settings()
    sch, (waiting, coming) = schedule(s, posts=_posts(s["id"])), pipeline(s["id"])
    return max(0, sch["posts_per_day"] - sch["posted_today"] - waiting - coming)


def bot_paused():
    hit = download.cookies_state()["bot_check_at"]
    return hit if hit and time.time() - hit < BOT_PAUSE else None


def status(aid=None):
    """For the UI: one autopilot's settings, today's plan, its pipeline, last round, what it's doing right now."""
    s = settings(aid)
    raw = db.get_setting(f"auto_last_{s['id']}")
    waiting, coming = pipeline(s["id"])
    today = time.strftime("%Y-%m-%d")
    running = _lock(s["id"]).locked()
    return {"settings": s, "last": json.loads(raw) if raw else None, "running": running,
            "stage": STAGE.get(s["id"], "") if running else "", "schedule": schedule(s, posts=_posts(s["id"])),
            "scheduled": waiting, "coming": coming, "need": need(s), "max_rounds": MAX_ROUNDS,
            "rounds_today": len([r for r in rounds(MAX_ROUNDS + 5, s["id"]) if r.get("day") == today and r.get("topics")]),
            "bot_check": bot_paused(), "cookies": download.cookies_state()["source"]}


def status_all():
    return {"autopilots": [status(s["id"]) for s in profiles()], "bot_check": bot_paused(),
            "cookies": download.cookies_state()["source"]}


def upload_tick(now=None):
    """Each autopilot: post its oldest waiting clip when its slot has come (see schedule()). Clips whose platforms
    are all at their upload limit are left waiting. Nothing is posted while it's OFF."""
    posted = []
    for s in profiles():
        if not s["enabled"] or not s["platforms"]:
            continue
        waiting = waiting_clips(s["id"])
        if not waiting or not schedule(s, now, _posts(s["id"]))["can_post_now"]:
            continue
        limited = social.limit_timers()
        if all(p in limited for p in s["platforms"]):
            continue
        c = waiting[0]
        posts = [p for p in _posts(s["id"]) if p > time.time() - 3 * 86400] + [time.time()]
        db.set_setting(f"auto_posts_{s['id']}", json.dumps(posts))
        db.log(c["job_id"], f"autopilot {s['name'] or s['id']}: posting #{c['id']} (post "
                            f"{schedule(s, posts=posts)['posted_today']}/{s['posts_per_day']} today, {len(waiting) - 1} more waiting)")
        # today's platforms, even if they changed after it was made (force: Instagram over its limit gets a trimmed copy)
        worker.upload_clip(c["id"], s["platforms"], force=True)
        posted.append(c["id"])
    return posted


def stop_all(aid=None):
    """One autopilot (or all): OFF + stop its running round, its queued/running topics and their jobs, and unschedule
    its waiting clips."""
    targets = [s for s in profiles() if aid is None or s["id"] == int(aid)]
    stopped = cancelled = 0
    for s in targets:
        save({"enabled": False}, s["id"])
        if _lock(s["id"]).locked():
            _abort(s["id"]).set()
        for t in _active_topics(s["id"]):
            topics.stop(t["id"])
            stopped += 1
        for c in waiting_clips(s["id"]):
            db.update("clips", c["id"], status="not uploaded (autopilot stopped)")
            db.log(c["job_id"], f"#{c['id']} unscheduled: autopilot stopped")
            cancelled += 1
    db.syslog(f"autopilot STOPPED by user ({'all' if aid is None else aid}): {stopped} topic(s), {cancelled} clip(s) cancelled")
    return {"topics": stopped, "clips": cancelled}


def rounds(limit=20, aid=None):
    """Past rounds, newest first (one autopilot's, or all), each with its full research record."""
    with db.conn() as c:
        rows = [{"id": r["id"], **json.loads(r["data"])}
                for r in c.execute("SELECT * FROM auto_rounds ORDER BY id DESC LIMIT ?", (limit * (1 if aid is None else 6),))]
    if aid is not None:
        rows = [r for r in rows if (r.get("autopilot") or _first_id()) == int(aid)]
    return rows[:limit]


def _save(rec, aid):
    rec["autopilot"] = aid
    db.set_setting(f"auto_last_{aid}", json.dumps(rec))
    db.insert("auto_rounds", at=rec["at"], data=json.dumps(rec))
    return rec


def _step(text, aid=None):
    if aid is not None and _abort(aid).is_set():
        raise Stopped()
    STAGE[aid] = text


def trending_topics(niche, k, research=None, aid=None, category_id=None):
    """-> [{"topic", "why"}] from this week's most-viewed YouTube videos for the niche (empty niche = the category's
    topic, else anything). Every step stays inside the category (categories.scope).
    `research` (a dict) is filled with what was searched and seen, for the activity view."""
    from . import categories
    research = research if research is not None else {}
    scope = categories.scope(category_id)
    niche = niche.strip() or categories.search_topic(category_id) or config.ANY_NICHE
    _step("asking DeepSeek for search queries", aid)
    queries = pick.search_queries(niche, scope)
    research["queries"] = []
    seen = {}
    _step(f"searching YouTube: {len(queries)} queries", aid)
    for q, found, ex in download.search_many(queries, n=20, recent=download.RECENT[0]):  # all at once
        if ex is None:
            for e in found:
                seen[e["id"]] = e
            research["queries"].append({"q": q, "found": len(found)})
            continue
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
    _step(f"DeepSeek reading {len(top)} top videos to spot trends", aid)
    out = []
    for t in pick._ask(f"Niche: {niche}\n" + (f"{scope}\nOnly topics inside this category.\n" if scope else "")
                       + f"Most-viewed videos this week:\n{json.dumps(rows, indent=1)}\nReturn at most {k} topics.",
                       TREND_SYSTEM, "topics"):
        if isinstance(t, dict) and str(t.get("topic", "")).strip():
            out.append({"topic": str(t["topic"]).strip()[:100], "why": str(t.get("why", ""))[:200]})
    fallback = top[0]["title"] if niche == config.ANY_NICHE and top else niche  # nothing clear: the biggest video's subject
    return out[:k] or [{"topic": fallback[:100], "why": "DeepSeek found no clear trend, using the top result"}]


def plan(s, missing):
    """Clips missing -> (videos to clip, videos to research). Always at least 1 video."""
    per = max(1, int(s["shorts"]) + int(s["longs"]))
    videos = max(1, math.ceil(missing / per))
    return videos, videos * max(1, int(s["choosy"]))


def run_round(reason="scheduled", aid=None, one=False):
    """One autopilot: find trending topics and queue enough videos for today's missing clips. one=True ("Make one
    clip now"): a single video for a single clip, whatever today's plan says. Returns the round record."""
    s = settings(aid)
    aid = s["id"]
    lock = _lock(aid)
    if not lock.acquire(blocking=False):
        return {"error": "a round is already running"}
    _abort(aid).clear()
    try:
        t0 = time.time()
        limited = social.limit_timers()
        blocked = [p for p in s["platforms"] if p in limited]
        platforms = [p for p in s["platforms"] if p not in limited]
        if s["platforms"] and not platforms:  # every platform it uploads to is at its limit: don't clip for nothing
            until = max(limited[p]["until"] for p in blocked)
            rec = {"at": time.time(), "day": None, "reason": reason, "topics": [], "paused_until": until,
                   "settings": s, "skipped_platforms": blocked,
                   "error": f"paused: upload limit reached on {', '.join(blocked)}; resumes after "
                            f"{time.strftime('%d %b %H:%M', time.localtime(until))}"}
            db.syslog(f"autopilot {aid} round ({reason}) skipped: {rec['error']}")
            return _save(rec, aid)
        missing = 1 if one else need(s)
        shorts, longs = (1, 0) if one else (s["shorts"], s["longs"])
        n, pool = (1, max(2, int(s["choosy"]))) if one else plan(s, missing or max(1, int(s["shorts"]) + int(s["longs"])))
        db.syslog(f"autopilot {aid} round ({reason}): niche '{s['niche'] or 'anything (up to the autopilot)'}', "
                  f"{missing} clip(s) missing -> clip {n} video(s), research {pool}"
                  + (f"; {', '.join(blocked)} at upload limit" if blocked else ""))
        research = {}
        found = trending_topics(s["niche"], min(3, n), research, aid, s["category_id"])
        _step("queueing topics", aid)
        split = lambda total: [total // len(found) + (1 if i < total % len(found) else 0) for i in range(len(found))]
        queued = []
        for tp, k, kp in zip(found, split(n), split(pool)):
            if k <= 0:
                continue
            tid = topics.enqueue(topic=tp["topic"], videos=k, shorts=shorts, longs=longs, mode=s["mode"],
                                 subtitles=int(s["subtitles"]), voiceover=int(s["voiceover"]), face_check=int(s["face_check"]),
                                 aspect=s["aspect"], hq=int(s["hq"]), platforms=json.dumps(s["platforms"]), source="auto",
                                 pool=max(kp, k), category_id=s["category_id"], autopilot_id=aid)
            db.log(tid, f"queued by autopilot {s['name'] or aid} ({reason}): {tp['why']}", "topics")
            queued.append({**tp, "videos": k, "research": max(kp, k), "topic_id": tid})
            db.syslog(f"autopilot {aid}: queued T{tid} '{tp['topic']}' x{k} videos: {tp['why']}")
        rec = {"at": time.time(), "day": None if one else time.strftime("%Y-%m-%d"), "reason": reason, "topics": queued,
               "seconds": round(time.time() - t0), "skipped_platforms": blocked, "platforms": platforms,
               "missing": missing, "settings": s, "research": research}
        return _save(rec, aid)
    except Stopped:
        db.syslog(f"autopilot {aid} round stopped by user")
        return _save({"at": time.time(), "day": None, "reason": reason, "error": "stopped by you", "topics": []}, aid)
    except Exception as e:
        db.syslog(f"autopilot {aid} round failed: {e!r}", exc=True)
        bot = download.BOT_CHECK in str(e) or "bot check" in str(e)
        return _save({"at": time.time(), "day": None if bot else time.strftime("%Y-%m-%d"), "reason": reason,
                      "error": str(e)[:300], "topics": []}, aid)
    finally:
        STAGE[aid] = ""
        _abort(aid).clear()
        lock.release()


def due_round(s, now=None):
    """Why this autopilot's round should start now ('daily' / 'top-up'), or None."""
    now = now or time.time()
    lt = time.localtime(now)
    if not s["enabled"] or lt.tm_hour < int(s["hour"]) or lt.tm_hour >= int(s["post_to"]) or bot_paused():
        return None
    today = [r for r in rounds(MAX_ROUNDS + 5, s["id"]) if r.get("day") == time.strftime("%Y-%m-%d", lt)]
    if not today:
        return "daily" if need(s) > 0 else None
    if len(today) >= MAX_ROUNDS or now - today[0]["at"] < TOPUP_GAP or _active_topics(s["id"]):
        return None  # enough tries today / too soon / the last round's topics are still working
    return "top-up" if need(s) > 0 else None


def _loop():
    tick = 0
    while True:
        try:
            worker.publish_due()  # every minute: clips scheduled for a time
            upload_tick()  # every minute: is a post due on any autopilot?
            if tick % 10 == 0:  # every 10 minutes: delete published clips' files and old source videos
                worker.sweep()
            if tick % 5 == 0:  # every 5 minutes: is a round due? (each autopilot's round runs on its own thread)
                for s in profiles():
                    why = due_round(s)
                    if why and not _lock(s["id"]).locked():
                        threading.Thread(target=run_round, args=(why, s["id"]), daemon=True).start()
        except Exception as e:
            db.syslog(f"autopilot loop error: {e!r}", exc=True)
        tick += 1
        time.sleep(60)


def start_loop():
    threading.Thread(target=_loop, daemon=True).start()
