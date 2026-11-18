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

from . import db, download, insights, news, pick, social, stats, topics, worker
from .config import CROP_ASPECT

DEFAULTS = {"enabled": False, "niche": "", "posts_per_day": 6, "post_from": 10, "post_to": 22,
            "shorts": 3, "longs": 0, "choosy": 3, "hour": 9,
            "mode": "hype", "aspect": CROP_ASPECT, "layout": "", "hq": False, "face_check": True, "subtitles": True, "follow": True,
            "voiceover": False, "platforms": ["youtube"],
            "news_per_day": 0, "news_kind": "news", "news_picture": True, "news_format": "video",  # news/fact posts
            "mix": [],  # framings to rotate, one per topic: [{"aspect", "layout"}]; empty = always aspect/layout above
            # Streamers to watch: when one goes live, the autopilot pauses (rounds, topics, scheduled posts) and the
            # stream is clipped as streamer + gameplay; when it ends, the autopilot carries on.
            "notes": "",  # your own instructions for the AI: trend research, topics, clip picking
            "streamers": [], "watch": False, "live_layout": "cam:top:auto", "live_chunk": 10, "live_shorts": 2}
NEWS_KINDS = ("news", "fact")
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
videos for the channel's niche (title, channel, views, minutes long), each marked "new": true when it was uploaded
TODAY (rising right now) and "live": true when it's streaming now. Find the topics with the most momentum: the same
game/event/creator/news across several big videos from DIFFERENT channels, rising today beats big last week.
Prefer topics with long gameplay, streams or real scenes that can be clipped (not lore explainers, not trailers
alone, not Shorts). Avoid the topics listed as already covered unless they are clearly still exploding.
Each topic is a short search phrase (3-6 words), the way people type it. Score each 0-10 for momentum x how well
it clips. Reply as JSON:
{"topics": [{"topic": "...", "score": 0-10, "why": "one line: which videos/channels show it's trending"}]}"""
MIN_TOPIC_SCORE = 4  # topics the AI scores below this are dropped
PER_CHANNEL = 3  # at most this many videos per channel in what the AI reads (no single channel decides the trend)


class Stopped(Exception):
    pass


def settings():
    raw = db.get_setting("auto")
    stored = json.loads(raw) if raw else {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def save(new):
    """Save settings and apply them to what's already in flight (queued/running topics, unfinished jobs)."""
    was_on = settings()["enabled"]
    s = {**settings(), **{k: v for k, v in new.items() if k in DEFAULTS}}
    if s["enabled"] and not was_on:  # switched ON now: today's slots start from here
        db.set_setting("auto_started_at", str(time.time()))
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
        db.update("topics", t["id"], shorts=s["shorts"], longs=s["longs"], mode=s["mode"], notes=s.get("notes", ""),
                  **({} if s["mix"] else {"aspect": s["aspect"], "layout": s["layout"]}),  # mixed: each topic keeps its own
                  hq=int(s["hq"]), subtitles=int(s["subtitles"]), follow=int(s["follow"]), voiceover=int(s["voiceover"]),
                  face_check=int(s["face_check"]), platforms=plats)
        for j in db.all("jobs", f"topic_id={int(t['id'])}"):
            if j["status"] not in FINISHED:  # its clips aren't posted yet: they go where the settings say now
                db.update("jobs", j["id"], platforms=plats, auto_upload=int(bool(s["platforms"])), follow=int(s["follow"]),
                          notes=s.get("notes", ""),
                          **({} if s["mix"] else {"layout": s["layout"]}))


def _day_start(now):
    return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))


def _post_log(with_clip=False):
    """[(time, kind)] of every autopilot post (older entries are bare times = clips); with_clip: + the clip id."""
    out = []
    for p in json.loads(db.get_setting("auto_posts") or "[]"):
        t, k, cid = (p, "clip", None) if isinstance(p, (int, float)) else (p[0], p[1], p[2] if len(p) > 2 else None)
        out.append((t, k, cid) if with_clip else (t, k))
    return out


def _posts():
    return [t for t, _ in _post_log()]  # when each autopilot post went up


def schedule(s, now=None, posts=None):
    """Today's posting plan: window, spacing, how many are due / done, and when the next post may go up."""
    now = now or time.time()
    posts = _posts() if posts is None else posts
    n, day = max(1, int(s["posts_per_day"]) + int(s.get("news_per_day") or 0)), _day_start(now)
    w0, w1 = day + int(s["post_from"]) * 3600, day + int(s["post_to"]) * 3600
    every = (w1 - w0) / n
    # Switched ON today, after the window opened: today's slots start then (same spacing, so fewer of them).
    started = float(db.get_setting("auto_started_at") or 0)
    start = started if w0 < started < w1 else w0
    slots = [start + i * every for i in range(n) if start + i * every < w1]
    today = [p for p in posts if p >= day]
    last = max(posts) if posts else 0
    due = len([t for t in slots if t <= now])
    if len(today) >= len(slots) or now >= w1:
        nxt = w0 + 86400  # done for today / window over: tomorrow's first slot
    else:
        nxt = max(slots[len(today)], last + every / 2, now)  # its slot, never closer than half a gap
        nxt = nxt if nxt < w1 else w0 + 86400
    return {"posts_per_day": n, "window": [w0, w1], "every_min": round(every / 60), "posted_today": len(today),
            "slots": slots, "started": start, "due": due, "next_at": nxt,
            "can_post_now": len(today) < due and now >= last + every / 2 and now < w1}


def _share(s, kinds):
    """How many of today's slots belong to clips / news (fewer slots when switched on late in the day)."""
    n = max(1, int(s["posts_per_day"]) + int(s.get("news_per_day") or 0))
    k = int(s["posts_per_day"]) if kinds == "clips" else int(s.get("news_per_day") or 0)
    return round(k * len(schedule(s)["slots"]) / n)


def slot_plan(s=None, now=None):
    """Every slot of today (and tomorrow's, for clips left over) with the clip/post that goes up in it:
    posted ones from the post log, upcoming ones = the waiting queue in posting order."""
    s = s or settings()
    now = now or time.time()
    sch = schedule(s, now)
    day = _day_start(now)
    done = [(t, cid) for t, _, cid in _post_log(with_clip=True) if t >= day]
    queue = list(reversed(db.all("clips", "status='scheduled'")))  # oldest first, like upload_tick
    info = lambda c: c and {"id": c["id"], "title": c["title"], "headline": c.get("headline"), "kind": c["kind"],
                            "path": c.get("path"), "uploads": c.get("uploads")}
    out, every = [], sch["every_min"] * 60
    w0, w1 = sch["window"]
    prev = done[-1][0] if done else 0
    for i, t in enumerate(sch["slots"]):
        if i < len(done):
            out.append({"at": done[i][0], "slot": t, "state": "posted", "clip": info(db.get("clips", done[i][1]) if done[i][1] else None)})
            continue
        at = max(t, prev + every / 2, now)  # when it will really go up (catching up never posts closer than half a gap)
        if at >= w1:
            break  # no room left today: it moves to tomorrow
        out.append({"at": at, "slot": t, "state": "next" if i == len(done) else "upcoming",
                    "clip": info(queue.pop(0)) if queue else None})
        prev = at
    for i in range(min(len(queue), sch["posts_per_day"])):  # waiting clips that don't fit today: tomorrow's slots
        t = w0 + 86400 + i * every
        out.append({"at": t, "slot": t, "state": "tomorrow", "clip": info(queue.pop(0))})
    return out


def _today(kinds):
    day = _day_start(time.time())
    return len([t for t, k in _post_log() if t >= day and (k in NEWS_KINDS) == (kinds == "news")])


def news_waiting():
    return len([c for c in db.all("clips", "status='scheduled'") if c["kind"] in NEWS_KINDS])


def news_need(s=None):
    """News/fact posts today still needs that aren't posted or waiting."""
    s = s or settings()
    return max(0, _share(s, "news") - _today("news") - news_waiting())


def pipeline():
    """(clips waiting to be posted, clips the queued/running autopilot topics will still make, roughly)."""
    waiting = len([c for c in db.all("clips", "status='scheduled'") if c["kind"] not in NEWS_KINDS])
    coming = 0
    for t in _active_topics():
        jobs = db.all("jobs", f"topic_id={int(t['id'])}")
        left = t["videos"] - len([j for j in jobs if j["status"] in FINISHED])
        coming += max(0, left) * max(1, (t["shorts"] or 0) + (t["longs"] or 0))
    return waiting, coming


def need(s=None):
    """Clips today still needs that aren't posted, waiting or being made."""
    s = s or settings()
    waiting, coming = pipeline()
    return max(0, _share(s, "clips") - _today("clips") - waiting - coming)


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
            "coming": coming, "need": need(s), "max_rounds": MAX_ROUNDS, "news_waiting": news_waiting(),
            "news_need": news_need(s), "news_today": _today("news"), "clips_today": _today("clips"),
            "rounds_today": len([r for r in rounds(MAX_ROUNDS + 5) if r.get("day") == today and r.get("topics")]),
            "bot_check": bot_paused(), "cookies": download.cookies_state()["source"], "learned": insights.learned(),
            "plan": slot_plan(s), "live": {"paused": live_paused(), "channels": _watch_state()}}


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
    kinds = {c["kind"] in NEWS_KINDS for c in waiting}
    if len(kinds) == 2:  # clips and news both waiting: post whichever is furthest behind its share of the day
        total = max(1, int(s["posts_per_day"]) + int(s.get("news_per_day") or 0))
        want_news = _today("news") < round((_today("news") + _today("clips") + 1) * int(s.get("news_per_day") or 0) / total)
        c = next(x for x in waiting if (x["kind"] in NEWS_KINDS) == want_news)
    log = [[t, k, cid] for t, k, cid in _post_log(with_clip=True) if t > time.time() - 3 * 86400]
    db.set_setting("auto_posts", json.dumps(log + [[time.time(), "news" if c["kind"] in NEWS_KINDS else "clip", c["id"]]]))
    db.log(c["job_id"], f"autopilot: posting #{c['id']} (post {schedule(s)['posted_today']}/{s['posts_per_day']} "
                        f"today, {len(waiting) - 1} more waiting)")
    # today's platforms, even if they changed after it was made. force: Instagram is tried even for clips over
    # IG_MAX_SECONDS (it may fail; the error shows on the clip card, with the red button to retry)
    worker.upload_clip(c["id"], s["platforms"], force=True)
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
    db.set_setting("live_pause", "")  # stopped by hand: don't switch back on after a watched live stream
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


def trending_topics(niche, k, research=None, history="", notes=""):
    """-> [{"topic", "why"}] from this week's most-viewed YouTube videos for the niche (empty niche = anything).
    `research` (a dict) is filled with what was searched and seen, for the activity view."""
    research = research if research is not None else {}
    niche = niche.strip() or ANY_NICHE
    _step(f"asking {pick.ai_name()} for search queries")
    queries = pick.search_queries(niche, notes)
    research["queries"] = []
    seen, fresh = {}, set()
    for q in queries:
        _step(f"searching YouTube: {q}")
        try:
            found = download.search(q, n=20, recent=download.RECENT[0])
            for e in found:
                seen[e["id"]] = e
            today = download.search(q, n=12, recent=download.TODAY)  # what's rising right now
            for e in today:
                seen[e["id"]] = e
                fresh.add(e["id"])
            research["queries"].append({"q": q, "found": len(found), "today": len(today)})
        except Exception as ex:
            research["queries"].append({"q": q, "found": 0, "error": str(ex)[:150]})
            db.syslog(f"autopilot: search '{q}' failed: {ex!r}")
            if download.bot_checked(ex):
                raise RuntimeError("YouTube bot check ('Sign in to confirm you're not a bot'): add YouTube cookies "
                                   "on the Socials page") from None
    # Smarter ranking: Shorts can't be clipped (dropped); videos uploaded today count double (rising now);
    # at most PER_CHANNEL per channel, so one big creator can't fake a trend.
    live = lambda e: e.get("live_status") == "is_live"
    clippable = [e for e in seen.values() if live(e) or (e.get("duration") or 0) >= 90]
    ranked = sorted(clippable, key=lambda e: (e.get("view_count") or 0) * (2 if e["id"] in fresh else 1) + (10**6 if live(e) else 0),
                    reverse=True)
    per, top = {}, []
    for e in ranked:
        ch = e.get("channel_id") or e.get("channel")
        if per.get(ch, 0) < PER_CHANNEL:
            per[ch] = per.get(ch, 0) + 1
            top.append(e)
        if len(top) >= 40:
            break
    recent = recent_topics()
    db.syslog(f"autopilot: {len(seen)} videos for '{niche}' ({len(queries)} searches, {len(fresh)} new today), "
              f"top {len(top)} sent to {pick.ai_name()}")
    rows = [{"title": e.get("title"), "channel": e.get("channel"), "views": e.get("view_count"),
             "minutes": round((e.get("duration") or 0) / 60), **({"new": True} if e["id"] in fresh else {}),
             **({"live": True} if live(e) else {})} for e in top]
    research["seen"] = len(seen)
    research["smart"] = [f"{len(fresh)} of them uploaded today (counted double: rising now)",
                         f"{len(seen) - len(clippable)} Shorts / too-short videos dropped (nothing to clip)",
                         f"at most {PER_CHANNEL} videos per channel, so one creator can't fake a trend",
                         (f"avoiding topics covered in the last 3 days: {', '.join(recent)}" if recent else
                          "no topics covered in the last 3 days"),
                         f"the AI scores each topic 0-10; below {MIN_TOPIC_SCORE} is dropped (a repeat needs 9+)"]
    research["top"] = [{**r, "id": e["id"]} for r, e in zip(rows, top)]
    _step(f"{pick.ai_name()} reading {len(top)} top videos to spot trends")
    out, dropped = [], []
    for t in pick._ask(f"Niche: {niche}\nMost-viewed videos (this week + today):\n{json.dumps(rows, indent=1)}\n"
                       f"Return at most {k + 2} topics, best first."
                       + (f"\nAlready covered in the last 3 days: {json.dumps(recent)}" if recent else "")
                       + (f"\n\nHow this channel's past clips did. Lean toward topics like the ones that got views, away "
                          f"from what flopped; one topic may still be something new that's clearly trending:\n{history}"
                          if history else "") + pick.user_note(notes), TREND_SYSTEM, "topics"):
        if not (isinstance(t, dict) and str(t.get("topic", "")).strip()):
            continue
        try:
            score = float(t.get("score", 5))
        except (TypeError, ValueError):
            score = 5.0
        item = {"topic": str(t["topic"]).strip()[:100], "why": str(t.get("why", ""))[:200], "score": score}
        repeat = _same_topic(item["topic"], recent) and score < 9  # covered lately: only if it's clearly still exploding
        if score < MIN_TOPIC_SCORE or repeat or _same_topic(item["topic"], [o["topic"] for o in out]):
            dropped.append(item)
        else:
            out.append(item)
    out.sort(key=lambda t: -t["score"])
    research["dropped"] = dropped
    fallback = top[0]["title"] if niche == ANY_NICHE and top else niche  # nothing clear: the biggest video's subject
    return out[:k] or [{"topic": fallback[:100], "why": "the AI found no clear trend, using the top result"}]


def recent_topics(days=3):
    """Topics the autopilot queued in the last `days` days (so a round doesn't clip the same thing again)."""
    since = time.time() - days * 86400
    return list(dict.fromkeys(t["topic"] for t in db.all("topics", f"source='auto' AND created >= {since:.0f}")))[:15]


def _same_topic(topic, others):
    """Two topics are the same when most of the shorter one's words are in the other ("gta 6 trailer 3" ~ "GTA 6 trailer")."""
    words = lambda x: {w for w in str(x).lower().replace("-", " ").split() if len(w) > 1}
    a = words(topic)
    for o in others:
        b = words(o)
        if a and b and len(a & b) / min(len(a), len(b)) >= 0.75:
            return True
    return False


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
        missing, news_missing = need(s), news_need(s)
        clips_too = missing > 0 or not news_missing  # a news-only top-up doesn't queue videos
        n, pool = plan(s, missing or max(1, int(s["shorts"]) + int(s["longs"])))  # "Run now" with nothing missing: 1 video
        db.syslog(f"autopilot round ({reason}): niche '{s['niche'] or 'anything (up to the autopilot)'}', "
                  f"{missing} clip(s) missing today -> clip {n} video(s), research {pool}"
                  + (f"; {', '.join(blocked)} at upload limit" if blocked else ""))
        research, learned = {}, None
        niche, history = s["niche"].strip(), ""
        try:  # what worked so far (views from every platform), so the round can lean on it
            if time.time() - float(db.get_setting("views_at") or 0) > 1800:
                _step("checking views on every platform")
                stats.refresh()
            perf = insights.performance()
            history = insights.history_text(perf)
            if not niche and history:
                _step(f"learning from your views ({perf['measured']} clips, {perf['views']:,} views)")
                learned = insights.learn_niche(perf)
                niche = learned["niche"] if learned else ""
        except Stopped:
            raise
        except Exception as e:
            db.syslog(f"autopilot: couldn't use the view history ({e!r}), going on without it", exc=True)
        found = trending_topics(niche, min(3, n), research, history, s.get("notes", "")) if clips_too else []
        _step("queueing topics")
        split = lambda total: [total // len(found) + (1 if i < total % len(found) else 0) for i in range(len(found))]
        queued = []
        for t, k, kp in zip(found, split(n), split(pool)):
            if k <= 0:
                continue
            aspect, layout = next_framing(s)
            tid = topics.enqueue(topic=t["topic"], videos=k, shorts=s["shorts"], longs=s["longs"], mode=s["mode"],
                                 subtitles=int(s["subtitles"]), follow=int(s["follow"]), voiceover=int(s["voiceover"]), face_check=int(s["face_check"]),
                                 aspect=aspect, layout=layout, hq=int(s["hq"]), platforms=json.dumps(s["platforms"]), source="auto",
                                 notes=s.get("notes", ""),
                                 pool=max(kp, k))
            db.log(tid, f"queued by autopilot ({reason}): {t['why']}", "topics")
            queued.append({**t, "videos": k, "research": max(kp, k), "topic_id": tid, "framing": framing_name(aspect, layout)})
            db.syslog(f"autopilot: queued T{tid} '{t['topic']}' x{k} videos: {t['why']}")
        news_rec = None
        if int(s.get("news_per_day") or 0) and (news_missing or reason == "run now"):
            news_rec = _news_round(s, [t["topic"] for t in found] or [niche or ANY_NICHE], news_missing or 1)
        rec = {"at": time.time(), "day": time.strftime("%Y-%m-%d"), "reason": reason, "topics": queued, "news": news_rec,
               "seconds": round(time.time() - t0), "skipped_platforms": blocked, "platforms": platforms,
               "missing": missing, "settings": s, "research": research, "learned": learned, "niche_used": niche,
               "history": history}
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


FRAMING_NAMES = {0.5625: "full screen 9:16", 0.8: "4:5", 1.0: "square 1:1", 1.7778: "whole frame 16:9"}


def framing_name(aspect, layout):
    if str(layout).startswith("split:"):
        return "split + " + layout[6:]
    if str(layout).startswith("cam:"):
        return "streamer " + ("on top" if layout.split(":")[1] == "top" else "below")
    return FRAMING_NAMES.get(round(float(aspect or 0.5625), 4), f"{float(aspect or 0):.2f}:1")


def next_framing(s):
    """(aspect, layout) for the next topic: the settings' framing, or the next one of `mix` in turn."""
    mix = [m for m in s.get("mix") or [] if isinstance(m, dict)]
    if not mix:
        return s["aspect"], s["layout"]
    i = int(db.get_setting("mix_idx") or 0)
    db.set_setting("mix_idx", str(i + 1))
    m = mix[i % len(mix)]
    return float(m.get("aspect") or 0.5625), str(m.get("layout") or "")


# ---------- streamers to watch ----------
LIVE_EVERY = 3  # minutes between live checks


def channel_url(c):
    """'@name', a channel link, or its /live /videos /streams page -> 'https://www.youtube.com/@name' (or /channel/ID)."""
    c = c.strip().rstrip("/")
    for tail in ("/live", "/videos", "/streams", "/featured", "/shorts"):
        if c.endswith(tail):
            c = c[: -len(tail)]
    if c.startswith("@"):
        return "https://www.youtube.com/" + c
    if c.startswith("UC") and len(c) == 24:  # a bare channel id
        return "https://www.youtube.com/channel/" + c
    if c.startswith("http"):
        return c.replace("://youtube.com", "://www.youtube.com").replace("://m.youtube.com", "://www.youtube.com")
    return "https://www.youtube.com/@" + c


def live_now(channel):
    """The channel's live video id if it's streaming right now, else None (cheap: the /live page, no download)."""
    try:
        with download.ydl({"quiet": True, "skip_download": True, "no_warnings": True}) as y:
            i = y.extract_info(channel + "/live", download=False, process=False)
    except Exception as e:
        if "not currently live" in str(e) or "does not exist" in str(e).lower():
            return None
        raise
    vid = i.get("id") if i.get("_type") == "url" or i.get("is_live") else None
    if not vid:
        return None
    with download.ydl({"quiet": True, "skip_download": True, "no_warnings": True}) as y:  # upcoming != live
        full = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
    return vid if full.get("is_live") or full.get("live_status") == "is_live" else None


def _watch_state():
    return json.loads(db.get_setting("live_watch") or "{}")


def live_paused():
    return bool(db.get_setting("live_pause"))


def _pause_for_live(why):
    """A watched streamer is live: stop the round and autopilot topics, hold every scheduled/timed post."""
    s = settings()
    held = {}
    for c in db.all("clips", "status IN ('scheduled', 'timed')"):
        held[str(c["id"])] = c["status"]
        db.update("clips", c["id"], status="paused (streamer live)")
    if LOCK.locked():
        ABORT.set()
    for t in _active_topics():
        topics.stop(t["id"])
    db.set_setting("live_pause", json.dumps({"was_enabled": s["enabled"], "held": held, "at": time.time(), "why": why}))
    if s["enabled"]:
        save({"enabled": False})
    db.syslog(f"🔴 {why}: autopilot paused, {len(held)} scheduled post(s) held")


def _resume_after_live():
    p = json.loads(db.get_setting("live_pause") or "{}")
    db.set_setting("live_pause", "")
    back = 0
    for cid, status in (p.get("held") or {}).items():
        c = db.get("clips", int(cid))
        if c and c["status"] == "paused (streamer live)":
            db.update("clips", c["id"], status=status)
            back += 1
    if p.get("was_enabled"):
        save({"enabled": True})
    db.syslog(f"🟢 watched live stream(s) over: {back} post(s) back on schedule"
              + (", autopilot back ON" if p.get("was_enabled") else ""))


def watch_tick():
    """Check the watched streamers; a new live stream -> pause the autopilot and clip the stream (streamer + gameplay).
    When none of those live jobs is running any more, everything resumes."""
    s, now = settings(), time.time()
    state, chans = _watch_state(), [channel_url(c) for c in s.get("streamers") or [] if c.strip()]
    running = []
    for ch in chans:
        st = state.get(ch, {})
        job = db.get("jobs", st["job_id"]) if st.get("job_id") else None
        if job and job["status"] not in FINISHED:  # already clipping its stream
            running.append(ch)
            st.update(checked=now, live=True)
            state[ch] = st
            continue
        if not s.get("watch"):
            st.update(live=None)
            state[ch] = st
            continue
        try:
            vid = live_now(ch)
            st.update(checked=now, live=bool(vid), error="")
        except Exception as e:
            vid = None
            st.update(checked=now, live=None, error=str(e)[:150])
            if download.bot_checked(e):
                st["error"] = "YouTube bot check: add YouTube cookies on the Socials page"
        if vid and vid != st.get("last_video"):  # a new stream (a stopped/finished one isn't restarted)
            if not live_paused():
                _pause_for_live(f"{ch.rsplit('/', 1)[-1]} went live")
            jid = db.insert("jobs", url=f"https://www.youtube.com/watch?v={vid}", shorts=max(1, int(s["live_shorts"])), longs=0,
                            auto_upload=int(bool(s["platforms"])), platforms=json.dumps(s["platforms"]), aspect=0.5625,
                            hq=int(s["hq"]), mode=s["mode"], subtitles=int(s["subtitles"]), follow=int(s["follow"]),
                            voiceover=int(s["voiceover"]), chunk_minutes=int(s["live_chunk"]), max_chunks=0,
                            layout=s["live_layout"] or "", notes=s.get("notes", ""), created=now)
            db.log(jid, f"started by the streamer watch: {ch} is live")
            worker.start(jid)
            st.update(job_id=jid, last_video=vid, started=now)
            running.append(ch)
        state[ch] = st
    for ch in list(state):
        if ch not in chans:
            del state[ch]
    db.set_setting("live_watch", json.dumps(state))
    if live_paused() and not running:
        _resume_after_live()


def _fmt(s, i):
    """video / photo for the i-th news post of a round ('both' alternates, starting with a video)."""
    f = s.get("news_format") or "video"
    return ("video", "photo")[i % 2] if f == "both" else f


def _news_round(s, subjects, n):
    """Make today's missing news/fact posts about the round's topics; they wait as 'scheduled' for the slots.
    -> what it searched, saw, picked and made (shown in the Activity view)."""
    rid = news.new_run(s.get("news_kind") or "news", " / ".join(subjects)[:200], "auto")
    info = {"subjects": subjects, "want": n, "log": [], "made": [], "candidates": [], "run_id": rid}
    run_say = news.run_logger(rid)

    def say(m):
        info["log"].append(f"{time.strftime('%H:%M:%S')} {m}")
        run_say(m)
    kind = s.get("news_kind") or "news"
    n_fact = n if kind == "facts" else n // 2 if kind == "both" else 0
    n_news = n - n_fact
    posted = [r["headline"] for r in db.all("news")]
    try:
        if n_news:
            _step("searching news portals: " + ", ".join(subjects)[:80])
            queries = [x for x in subjects if x != ANY_NICHE] or news.queries_for(ANY_NICHE)
            stories = news.find_news(queries, say)
            info["found"] = len(stories)
            info["candidates"] = [{k: x.get(k) for k in ("title", "source", "age_h", "link")} for x in stories[:15]]
            _step(f"{pick.ai_name()} picking {n_news} of {len(stories)} stories")
            picked = news.pick_stories(" / ".join(subjects), stories, n_news, posted) if stories else []
            for x in picked:
                say(f"   ★ {x['title'][:100]} ({x['source']}): {x.get('why', '')}")
            for st in picked:
                _step(f"writing + rendering news post: {st['title'][:60]}")
                cid = news.make_post(st, "news", s["platforms"], bool(s["voiceover"]), bool(s.get("news_picture", True)),
                                     say, status="scheduled", auto=True, run_id=rid, fmt=_fmt(s, len(info["made"])),
                                     follow=bool(s["follow"]))
                info["made"].append({"kind": "news", "title": st["title"], "source": st["source"], "link": st["link"],
                                     "why": st.get("why", ""), "clip_id": cid})
        if n_fact:
            _step(f"finding facts about {subjects[0]}")
            fact_stories = news.facts(subjects[0] if subjects[0] != ANY_NICHE else "video games", n_fact, say)
            info["found"] = info.get("found", 0) + len(fact_stories)
            for st in fact_stories:
                _step("writing + rendering a fact post")
                cid = news.make_post(st, "fact", s["platforms"], bool(s["voiceover"]), bool(s.get("news_picture", True)),
                                     say, status="scheduled", auto=True, run_id=rid, fmt=_fmt(s, len(info["made"])),
                                     follow=bool(s["follow"]))
                info["made"].append({"kind": "fact", "title": st["title"], "source": st["source"], "link": st["link"],
                                     "clip_id": cid})
    except Stopped:
        db.update("news_runs", rid, status="stopped")
        raise
    except Exception as e:
        say(f"news part failed: {e!r}")
        info["error"] = str(e)[:200]
    db.update("news_runs", rid, status="failed" if info.get("error") else "made")
    return info


def due_round(s, now=None):
    """Why a round should start now ('daily' / 'top-up'), or None."""
    now = now or time.time()
    lt = time.localtime(now)
    if not s["enabled"] or lt.tm_hour < int(s["hour"]) or lt.tm_hour >= int(s["post_to"]) or bot_paused():
        return None
    today = [r for r in rounds(MAX_ROUNDS + 5) if r.get("day") == time.strftime("%Y-%m-%d", lt)]
    if not today:
        return "daily" if need(s) > 0 or news_need(s) > 0 else None
    if len(today) >= MAX_ROUNDS or now - today[0]["at"] < TOPUP_GAP or _active_topics():
        return None  # enough tries today / too soon / the last round's topics are still working
    return "top-up" if need(s) > 0 or news_need(s) > 0 else None


def _loop():
    tick = 0
    while True:
        try:
            upload_tick()  # every minute: is a post due?
            news.timed_tick()  # ... and any post scheduled by hand for a set time (also while OFF)
            if tick % LIVE_EVERY == 0:  # every few minutes: did a watched streamer go live (or finish)?
                watch_tick()
            if tick % 5 == 0 and not live_paused():  # every 5 minutes: is a round due? (not while a streamer is live)
                why = due_round(settings())
                if why:
                    run_round(why)
        except Exception as e:
            db.syslog(f"autopilot loop error: {e!r}", exc=True)
        tick += 1
        time.sleep(60)


def start_loop():
    threading.Thread(target=_loop, daemon=True).start()
