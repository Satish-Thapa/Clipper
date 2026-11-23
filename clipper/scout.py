"""CLIP-SCOUT: finds the long YouTube videos most likely to make viral Shorts for a topic, and the topics trending
right now in a category (autopilot.py, Auto mode, uses both). Nothing here cuts a video: picking
the moments inside a video is pick.py's job.

A topic -> videos, step by step (find_videos):
  1. plan_queries   AI: topic -> category (categories.py), 8-10 search queries, this week's angles, launch week?
  2. search         YouTube (US, flat results), this week first, then this month while too few pass
  3. hard_filter    drop: already clipped, live/upcoming, wrong length, too old, dead on arrival (< 5k views after 48h)
  4. enrich         full info for the shortlist: likes, comments, channel followers, heatmap, exact upload time
  5. metrics        velocity (views/hour), outlier (views/followers), engagement ((likes + 3x comments)/views),
                    freshness; each ranked 0-1 WITHIN this batch
  6. judge          AI: clippability 0-10 + the filters only a reader can apply (not English, official league or
                    broadcaster, music video, re-upload) + why / expected moments / copyright risk
  7. score          100 * (.40 velocity + .25 outlier + .15 engagement + .10 freshness + .10 clippability)
  8. look           face check on the picture (vision.py), best score first, until enough videos pass
The stats are computed here, never by the AI ("never invent stats"); the AI only reads and judges."""
import json
import time
from bisect import bisect_right

from . import ai, categories, db, download, settings, vision

MIN_CLIPPABLE = 6  # AI score 0-10; below this a video needs its full context, so it isn't clipped
LAUNCH_DAYS = 7  # a game launch / reveal week: only this week's uploads
DEAD_VIEWS, DEAD_HOURS = 5000, 48  # fewer views than this after this many hours = dead on arrival
# Podcasts (the owner's rule): only big, recent episodes from a real channel; when none is found, the topic is skipped
PODCAST_MIN_VIEWS, PODCAST_MIN_FOLLOWERS = 300_000, 100_000
WEIGHTS = {"velocity": .40, "outlier": .25, "engagement": .15, "freshness": .10, "clippability": .10}
TIER_FROM = (0, 7, 31)  # days: where each download.RECENT search filter (this week / this month / any time) starts

QUERIES_SYSTEM = """You are CLIP-SCOUT, the discovery agent of a short-form clipping channel. You find the
long-form YouTube videos most likely to make viral Shorts / Reels for a US English-speaking audience RIGHT NOW.

Given a TOPIC:
1. Decide its category: "gaming", "podcast" or "sports" (the closest one).
2. Write 8-10 YouTube search queries a US viewer would type THIS WEEK. Mix:
   - core: the topic itself
   - fresh angles that fit: "<topic> first reaction", "<topic> funny moments", "<topic> secrets",
     "<topic> reaction", "<topic> review", "<topic> vs"
   - people: the top creators / streamers / hosts known for this topic, with the topic ("ishowspeed gta 6")
   - news hooks: what is happening this week (launch, trailer, update, trade, drama, leak)
   Short queries, the way people actually type them; no hashtags, no quotes, no years unless the topic is about one.
3. trending_angles: 3-6 short phrases describing what people care about this week.
4. launch: true only when this is a game's launch / reveal / release week.
Reply as JSON: {"category": "...", "launch": false, "queries": ["..."], "trending_angles": ["..."]}"""

JUDGE_SYSTEM = """You are CLIP-SCOUT. You decide which YouTube videos are worth cutting into short vertical clips
(15s-3min) for a viral clip channel with a US English-speaking audience. You get each video's title, channel, length,
views, age and the stats the app measured (views_per_hour, outlier = views / channel followers, likes, comments,
heatmap = YouTube "most replayed" data exists). Never invent stats: judge the ones given, missing ones are null.

Good sources (moments that stand on their own for a stranger scrolling):
- streams and gameplay (a corner facecam is fine) full of reactions, chaos, fails, funny moments, clutch plays, RP drama
- gameplay commentary and reactions where the game/scene fills the screen: hot takes, stories, punchlines
- challenge videos, events with big reveals, big-name guests, several distinct moments likely

Bad sources (people need the whole video for any part to make sense, or there is nothing to cut):
- step-by-step tutorials, guides, walkthroughs where each part depends on the previous one
- story-driven content: lore explanations, documentaries, a plot told from start to finish
- trailers, music videos, official teasers, very short or already-edited highlight videos
- slideshow/news-reading videos with no reactions, static "tier list" or ranking videos that need the full list
- anything that is mostly menus, ads, or one long calm explanation
- videos where the PICTURE is mostly a person: talking-head vlogs, "my thoughts on...", sit-down podcasts and
  interviews, people reacting on camera with little of the thing they react to. The channel wants clips that are
  mostly gameplay or real scenes; a small streamer facecam in the corner over gameplay is fine. (For the podcast
  category, talking IS the content: judge it by the guest, the topic and how quotable it sounds.)

DROP a video (set "drop" to the reason) when: the title/channel is clearly not English; it's an official league /
broadcaster / studio channel (NBA, NFL, ESPN game footage, FIFA, a game publisher's trailer): high Content ID
risk; it's a music video, a movie / TV upload, or a re-upload of someone else's content.

clippability 0-10 (7+ = clearly good, 4 or less = needs the full context): talking / reaction density, a big-name
guest or streamer, a face cam present, many distinct moments likely, heatmap available.
copyright_risk: "low" (the creator's own stream / show), "medium" (lots of someone else's footage), "high"
(official / broadcast footage, re-uploads).

CATEGORY RULES:
{category}

Reply as JSON: {{"videos": [{{"id": "...", "clippability": 0-10, "drop": "" or "reason", "why": "one sentence: why it
will clip well (or not)", "expected_moments": ["2-4 moment types you expect inside"], "copyright_risk":
"low|medium|high", "risk_note": "short reason"}}], "better_queries": ["3 searches that would find better videos"]}}
with every id you were given."""


def plan_queries(topic, notes=""):
    """AI: topic -> {"category", "launch", "queries", "angles"}. Falls back to the topic itself if the AI fails."""
    try:
        a = ai.ask(f"Topic: {topic}" + ai.user_note(notes), QUERIES_SYSTEM, "queries", full=True)
    except Exception as e:
        db.here(f"query planning failed ({e!r}), searching the topic itself")
        a = {}
    qs = [str(q).strip()[:100] for q in a.get("queries") or [] if str(q).strip()]
    return {"category": categories.clean(a.get("category")), "launch": bool(a.get("launch")),
            "queries": list(dict.fromkeys(qs))[:10] or [topic],
            "angles": [str(x)[:80] for x in a.get("trending_angles") or []][:6]}


def search_queries(topic, notes=""):
    """Just the search queries for a topic (news.py and the trend research use these)."""
    return plan_queries(topic, notes)["queries"]


def hours_old(v, now=None):
    """Hours since upload (at least 1), or None when YouTube didn't say. Flat search results carry a rough
    `timestamp` ("2 days ago"); full info has the exact one (or upload_date)."""
    ts = v.get("timestamp")
    if not ts and v.get("upload_date"):
        try:
            ts = time.mktime(time.strptime(str(v["upload_date"]), "%Y%m%d"))
        except ValueError:
            ts = None
    return max(1.0, ((now or time.time()) - ts) / 3600) if ts else None


def max_days(category, launch):
    return LAUNCH_DAYS if launch and category == "gaming" else settings.get().topic_max_days


def not_english(v):
    """Why a video is clearly not in English ('' = it may be): YouTube's own language field, or a title that is
    mostly non-Latin letters. The AI judge drops the rest (e.g. a Spanish title in Latin letters)."""
    lang = str(v.get("language") or "").lower()
    if lang and not lang.startswith("en"):
        return f"not English (YouTube says {lang})"
    letters = [ch for ch in str(v.get("title") or "") if ch.isalpha()]
    if letters and sum(ord(ch) > 0x24F for ch in letters) / len(letters) > 0.3:
        return "not English (title)"
    return ""


def hard_filter(v, now, used=(), category="", days=None):
    """Why the video is out ('' = it stays)."""
    s = settings.get()
    days = s.topic_max_days if days is None else days
    lo = min(s.topic_min_minutes, 4) if category == "sports" else s.topic_min_minutes  # a sports show segment: 4 min
    d, h = v.get("duration") or 0, hours_old(v, now)
    if v["id"] in used:
        return "clipped before"
    if v.get("live_status") in ("is_live", "is_upcoming"):
        return "live / upcoming"
    if not_english(v):
        return not_english(v)
    if not lo * 60 <= d <= s.topic_max_minutes * 60:
        return f"{d / 60:.0f} min long (clipping {lo:g}-{s.topic_max_minutes:g} min videos)"
    if days and h and h > days * 24:
        return f"{h / 24:.0f} days old (max {days:g})"
    if category == "podcast" and (v.get("view_count") or 0) < PODCAST_MIN_VIEWS:
        return f"podcast with {v.get('view_count') or 0:,} views (podcasts need {PODCAST_MIN_VIEWS:,}+)"
    if category == "podcast" and v.get("channel_follower_count") is not None \
            and v["channel_follower_count"] < PODCAST_MIN_FOLLOWERS:  # known after the full stats are read
        return f"podcast channel with {v['channel_follower_count']:,} subscribers (needs {PODCAST_MIN_FOLLOWERS:,}+)"
    if h and h > DEAD_HOURS and (v.get("view_count") or 0) < DEAD_VIEWS:
        return f"dead on arrival: {v.get('view_count') or 0:,} views after {h / 24:.0f} days"
    return ""


def velocity(v, now):
    """Views per hour (an unknown age counts as a week old)."""
    return (v.get("view_count") or 0) / (hours_old(v, now) or 168)


def enrich(v):
    """Full info for one shortlisted video -> (v with likes / comments / followers / heatmap / exact time, info).
    info is passed on to the face check so the video isn't fetched twice. A failure keeps the flat fields."""
    try:
        i = download.info(f"https://www.youtube.com/watch?v={v['id']}")
    except Exception as e:
        if download.bot_checked(e):
            raise
        return {**v, "enrich_error": str(e)[:150]}, None
    keep = ("timestamp", "upload_date", "duration", "view_count", "like_count", "comment_count",
            "channel_follower_count", "language", "description", "was_live", "live_status")
    return {**v, **{k: i[k] for k in keep if i.get(k) is not None}, "heatmap": bool(i.get("heatmap"))}, i


def _pct(values):
    """Percentile rank 0-1 of each value within the batch (None -> 0: a missing stat never helps)."""
    have = sorted(x for x in values if x is not None)
    return [bisect_right(have, x) / len(have) if x is not None else 0.0 for x in values]


def freshness(h):
    return 1.0 if h and h < 24 else .8 if h and h < 48 else .5 if h and h < 168 else .2


def metrics(videos, now):
    """Adds v["stats"] = raw velocity / outlier / engagement / freshness + their 0-1 ranks in this batch."""
    raw = []
    for v in videos:
        views, h, fol = v.get("view_count") or 0, hours_old(v, now), v.get("channel_follower_count")
        likes, comments = v.get("like_count"), v.get("comment_count")
        raw.append({"hours": round(h) if h else None, "velocity": round(views / (h or 168)),
                    "outlier": round(views / max(1000, fol), 2) if fol else None,
                    "engagement": round(((likes or 0) + 3 * (comments or 0)) / max(1, views), 4)
                    if likes is not None or comments is not None else None, "freshness": freshness(h)})
    ranks = {k: _pct([r[k] for r in raw]) for k in ("velocity", "outlier", "engagement")}
    for i, v in enumerate(videos):
        v["stats"] = {**raw[i], **{k + "_pct": round(ranks[k][i], 2) for k in ranks}}
    return videos


def judge(topic, category, videos, notes=""):
    """AI verdict per video -> ({id: verdict}, better_queries). An unscored video passes as MIN_CLIPPABLE."""
    rows = [{"id": v["id"], "title": v.get("title"), "channel": v.get("channel"),
             "minutes": round((v.get("duration") or 0) / 60), "views": v.get("view_count"),
             "hours_since_upload": v["stats"]["hours"], "views_per_hour": v["stats"]["velocity"],
             "outlier": v["stats"]["outlier"], "likes": v.get("like_count"), "comments": v.get("comment_count"),
             "followers": v.get("channel_follower_count"), "was_live": v.get("live_status") == "was_live" or None,
             "heatmap": v.get("heatmap"), "description": (v.get("description") or "")[:200]} for v in videos]
    a = ai.ask(f"Topic: {topic}\nCategory: {category or 'unknown'}\nVideos:\n{json.dumps(rows, indent=1)}"
               + ai.user_note(notes), JUDGE_SYSTEM.format(category=categories.module(category, "scout")), "videos",
               full=True)
    out = {}
    for r in a.get("videos") or []:
        try:
            score = min(max(float(r.get("clippability", r.get("score", 0))), 0), 10)
            out[str(r["id"])] = {"clippability": score, "drop": str(r.get("drop") or "")[:150],
                                 "why": str(r.get("why", ""))[:200],
                                 "moments": [str(m)[:40] for m in r.get("expected_moments") or []][:4],
                                 "risk": str(r.get("copyright_risk") or "low").lower(),
                                 "risk_note": str(r.get("risk_note", ""))[:120]}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out, [str(q).strip()[:100] for q in a.get("better_queries") or [] if str(q).strip()][:3]


def score(v):
    """The final 0-100 rank of a judged video (see the module doc)."""
    s, j = v["stats"], v["verdict"]
    clip = min(1.0, j["clippability"] / 10 + (.2 if v.get("heatmap") else 0))
    return round(100 * (WEIGHTS["velocity"] * s["velocity_pct"] + WEIGHTS["outlier"] * s["outlier_pct"]
                        + WEIGHTS["engagement"] * s["engagement_pct"] + WEIGHTS["freshness"] * s["freshness"]
                        + WEIGHTS["clippability"] * clip))


def rejected(v):
    """Why a judged video isn't clipped ('' = it may be)."""
    j = v["verdict"]
    return (j["drop"] or ("copyright risk high: " + j["risk_note"] if j["risk"] == "high" else "")
            or (f"clippable {j['clippability']:.0f}/10: {j['why']}" if j["clippability"] < MIN_CLIPPABLE else ""))


def look(v, info, say):
    """Face check from YouTube's preview frames: '' when the picture is gameplay / scenes, else why it's out.
    Errors keep the video."""
    try:
        t = time.time()
        a = vision.analyze(f"https://www.youtube.com/watch?v={v['id']}", info)
        say(f"face check {v.get('title')}: face fills screen {a['person']:.0%}, still picture {a['static']:.0%}, "
            f"{a['frames']} frames, {time.time() - t:.1f}s")
    except Exception as e:
        say(f"frame check failed for {v.get('title')} ({e!r}), keeping it", exc=True)
        return ""
    if a["person"] > vision.MAX_PERSON:
        return f"a face fills the screen in {a['person']:.0%} of the video ({a['frames']} frames checked)"
    if a["static"] > vision.MAX_STATIC:
        return f"still picture in {a['static']:.0%} of {a['frames']} frames"
    return ""


def describe(v):
    """One log line about a scored video."""
    s, j = v["stats"], v["verdict"]
    return (f"{v.get('scout_score', 0):>3}/100 {v.get('title')} | {v.get('view_count') or 0:,} views, "
            f"{(v.get('duration') or 0) / 60:.0f} min, " + (f"{s['hours'] / 24:.1f} days old, " if s["hours"] else "")
            + f"{s['velocity']:,}/h" + (f", {s['outlier']:g}x the channel" if s["outlier"] else "")
            + f" | clippable {j['clippability']:.0f}/10, risk {j['risk']}: {j['why']}"
            + (f" (expect: {', '.join(j['moments'])})" if j["moments"] else ""))


def find_videos(t, used, say):
    """A topic row -> (the videos to clip, best first, at most t["videos"]; the query plan). Each video carries
    "stats", "verdict" and "scout_score". say(msg, exc=False) logs into the topic's log."""
    now, want = time.time(), t["videos"]
    pool_size = max(t.get("pool") or want * 3, want)  # how many get the full check: research size, else 3x
    with db.step(t["id"], f"{ai.ai_name()} planning the search", "topics"):
        plan = plan_queries(t["topic"], t.get("notes", ""))
    cat = categories.clean(t.get("category")) or plan["category"]  # an Auto topic's category is yours
    days = max_days(cat, plan["launch"])
    say(f"category {cat or 'unknown'}{', launch week' if plan['launch'] else ''}, uploads from the last "
        + (f"{days:g} days" if days else "any time") + f" | queries: {' | '.join(plan['queries'])}"
        + (f" | this week: {', '.join(plan['angles'])}" if plan["angles"] else ""))
    seen, out, done, extra = {}, {}, set(), []
    tiers = [r for r, lo in zip(download.RECENT, TIER_FROM) if not days or days > lo]
    for k, recent in enumerate(tiers + [tiers[-1]]):  # the last pass searches the AI's better queries, if any
        queries = plan["queries"] if k < len(tiers) else extra
        if not queries:
            break
        for q in queries:
            try:
                found = download.search(q, recent=recent)
                say(f"search '{q}' ({['this week', 'this month', 'any time'][download.RECENT.index(recent)]}): "
                    f"{len(found)} videos")
            except Exception as e:
                say(f"search '{q}' failed: {e!r}", exc=True)
                if download.bot_checked(e):
                    raise RuntimeError("YouTube bot check: add YouTube cookies on the Socials page") from None
                continue
            for v in found:
                seen.setdefault(v["id"], v)
        fresh = [v for i, v in seen.items() if i not in done]
        done.update(seen)
        why = {v["id"]: hard_filter(v, now, used, cat, days) for v in fresh}
        for reason in sorted({w for w in why.values() if w}):
            n = [v for v in fresh if why[v["id"]] == reason]
            if reason == "clipped before" or len(n) > 3:
                say(f"filtered {len(n)}: {reason}")
            else:
                for v in n:
                    say(f"filtered: {v.get('title')} ({reason})")
        short = sorted((v for v in fresh if not why[v["id"]]), key=lambda v: -velocity(v, now))
        short = short[:max(0, pool_size - len(out))]
        if short:
            infos = {}
            with db.step(t["id"], f"reading the full stats of {len(short)} video(s)", "topics"):
                for i, v in enumerate(short):
                    short[i], infos[v["id"]] = enrich(v)
            short = [v for v in short if not hard_filter(v, now, used, cat, days)]  # exact age / views now
            metrics(list(out.values()) + short, now)  # ranks are within the whole batch seen so far
            try:
                with db.step(t["id"], f"{ai.ai_name()} judging {len(short)} videos", "topics"):
                    verdicts, better = judge(t["topic"], cat, short, t.get("notes", ""))
            except Exception as e:
                say(f"clippability check failed ({e!r}), using the stats only", exc=True)
                verdicts, better = {}, []
            extra = [q for q in better if q not in plan["queries"]] if not extra else extra
            for v in short:
                v["verdict"] = verdicts.get(v["id"]) or {"clippability": MIN_CLIPPABLE, "drop": "", "why": "not scored",
                                                         "moments": [], "risk": "low", "risk_note": ""}
                v["info"] = infos.get(v["id"])
                out[v["id"]] = v
        for v in out.values():
            v["scout_score"] = score(v)  # again: the ranks are within everything seen so far
            v.setdefault("out", rejected(v))
        ok = []
        for v in sorted(out.values(), key=lambda v: -v["scout_score"]):  # best first, face check until enough pass
            if len(ok) >= want:
                break
            if not v["out"] and t.get("face_check", 1) and not v.get("looked"):
                v["looked"], v["out"] = True, look(v, v.pop("info", None), say)
            if not v["out"]:
                ok.append(v)
        if len(ok) >= want:  # else: search wider (this month), then the AI's better queries
            break
    picked = {v["id"] for v in ok}
    for v in sorted(out.values(), key=lambda v: -v["scout_score"]):
        v.pop("info", None)  # big (every format): not kept
        say(("OK      " if v["id"] in picked else "skipped " if v["out"] else "spare   ") + describe(v)
            + (f" | out: {v['out']}" if v["out"] else ""))
    if len(ok) < want:
        say(f"only {len(ok)} of {want} video(s) passed"
            + (f" (the AI's better searches were tried too: {' | '.join(extra)})" if extra else ""))
    return ok, plan


# ---------- trending topics (the autopilot's daily research) ----------

# No niche set = it's up to the autopilot: search broadly for what clips well and is big this week.
ANY_NICHE = ("anything trending this week that makes great short clips: gaming (GTA, Fortnite, Minecraft...), "
             "streamer moments, podcasts, sports highlights, viral events")
TREND_SYSTEM = """You spot what is trending right now for a viral clip channel. You get the most-viewed YouTube
videos for the channel's niche (title, channel, views, views_per_hour, minutes long), each marked "new": true when
it was uploaded TODAY (rising right now) and "live": true when it's streaming now. Find the topics with the most
momentum: the same game/event/creator/news across several big videos from DIFFERENT channels, rising today beats big
last week, a high views_per_hour beats a big total. Prefer topics with long gameplay, streams or real scenes that
can be clipped (not lore explainers, not trailers alone, not Shorts). Avoid the topics listed as already covered
unless they are clearly still exploding. Each topic is a short search phrase (3-6 words), the way people type it.
Score each 0-10 for momentum x how well it clips. Reply as JSON:
{"topics": [{"topic": "...", "score": 0-10, "why": "one line: which videos/channels show it's trending"}]}"""
MIN_TOPIC_SCORE = 4  # topics the AI scores below this are dropped
PER_CHANNEL = 3  # at most this many videos per channel in what the AI reads (no single channel decides the trend)


def trending_topics(niche, k, research=None, history="", notes="", step=lambda text: None):
    """-> [{"topic", "why", "score"}] from this week's most-viewed YouTube videos for the niche (empty = anything).
    `research` (a dict) is filled with what was searched and seen, for the activity view. step(text) reports
    progress (the autopilot's STAGE; it raises to abort)."""
    research = research if research is not None else {}
    niche = niche.strip() or ANY_NICHE
    step(f"asking {ai.ai_name()} for search queries")
    queries = search_queries(niche, notes)
    research["queries"] = []
    seen, fresh = {}, set()
    for q in queries:
        step(f"searching YouTube: {q}")
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
    # Ranking: Shorts can't be clipped (dropped); views per hour (velocity) first, videos uploaded today count
    # double (rising now); at most PER_CHANNEL per channel, so one big creator can't fake a trend.
    now = time.time()
    live = lambda e: e.get("live_status") == "is_live"
    clippable = [e for e in seen.values() if live(e) or (e.get("duration") or 0) >= 90]
    ranked = sorted(clippable, key=lambda e: velocity(e, now) * (2 if e["id"] in fresh else 1) + (10**6 if live(e) else 0),
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
              f"top {len(top)} sent to {ai.ai_name()}")
    rows = [{"title": e.get("title"), "channel": e.get("channel"), "views": e.get("view_count"),
             "views_per_hour": round(velocity(e, now)), "minutes": round((e.get("duration") or 0) / 60),
             **({"new": True} if e["id"] in fresh else {}), **({"live": True} if live(e) else {})} for e in top]
    research["seen"] = len(seen)
    research["smart"] = [f"{len(fresh)} of them uploaded today (counted double: rising now)",
                         "ranked by views per hour, not total views (what's rising beats what's old and big)",
                         f"{len(seen) - len(clippable)} Shorts / too-short videos dropped (nothing to clip)",
                         f"at most {PER_CHANNEL} videos per channel, so one creator can't fake a trend",
                         (f"avoiding topics covered in the last 3 days: {', '.join(recent)}" if recent else
                          "no topics covered in the last 3 days"),
                         f"the AI scores each topic 0-10; below {MIN_TOPIC_SCORE} is dropped (a repeat needs 9+)"]
    research["top"] = [{**r, "id": e["id"]} for r, e in zip(rows, top)]
    step(f"{ai.ai_name()} reading {len(top)} top videos to spot trends")
    out, dropped = [], []
    try:
        picked = ai.ask(f"Niche: {niche}\nMost-viewed videos (this week + today):\n{json.dumps(rows, indent=1)}\n"
                        f"Return at most {k + 2} topics, best first."
                        + (f"\nAlready covered in the last 3 days: {json.dumps(recent)}" if recent else "")
                        + (f"\n\nHow this channel's past clips did. Lean toward topics like the ones that got views, away "
                           f"from what flopped; one topic may still be something new that's clearly trending:\n{history}"
                           if history else "") + ai.user_note(notes), TREND_SYSTEM, "topics")
    except Exception as e:  # no AI: the non-AI fallback below (the biggest video's subject, or the niche itself)
        db.here(f"trend reading failed ({e!r}): using the top result instead")
        picked = []
    for t in picked if isinstance(picked, list) else []:
        if not (isinstance(t, dict) and str(t.get("topic", "")).strip()):
            continue
        try:
            sc = float(t.get("score", 5))
        except (TypeError, ValueError):
            sc = 5.0
        item = {"topic": str(t["topic"]).strip()[:100], "why": str(t.get("why", ""))[:200], "score": sc}
        repeat = same_topic(item["topic"], recent) and sc < 9  # covered lately: only if it's clearly still exploding
        if sc < MIN_TOPIC_SCORE or repeat or same_topic(item["topic"], [o["topic"] for o in out]):
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


def same_topic(topic, others):
    """Two topics are the same when most of the shorter one's words are in the other ("gta 6 trailer 3" ~ "GTA 6 trailer")."""
    words = lambda x: {w for w in str(x).lower().replace("-", " ").split() if len(w) > 1}
    a = words(topic)
    for o in others:
        b = words(o)
        if a and b and len(a & b) / min(len(a), len(b)) >= 0.75:
            return True
    return False


if __name__ == "__main__":
    import os
    if "CLIPPER_DATA" not in os.environ:
        raise SystemExit("run as: CLIPPER_DATA=<empty temp dir> python -m clipper.scout")
    db.init()
    NOW, H = 1_700_000_000, 3600
    V = lambda i, mins, views, hours, live=None, **kw: {"id": i, "duration": mins * 60, "view_count": views,
                                                       "timestamp": NOW - hours * H, "live_status": live, **kw}
    assert hard_filter(V("a", 20, 9000, 5), NOW) == ""
    assert hard_filter(V("a", 20, 9000, 5, title="GTA 6 ПРОХОЖДЕНИЕ стрим"), NOW) == "not English (title)"
    assert hard_filter(V("a", 20, 9000, 5, language="es"), NOW).startswith("not English")
    assert hard_filter(V("a", 20, 9000, 5, language="en-US", title="GTA 6 is insane 😭"), NOW) == ""
    assert "podcasts need 300,000+" in hard_filter(V("a", 60, 250_000, 5), NOW, category="podcast")
    assert hard_filter(V("a", 60, 400_000, 5), NOW, category="podcast") == ""
    assert "subscribers" in hard_filter(V("a", 60, 400_000, 5, channel_follower_count=20_000), NOW, category="podcast")
    assert "min long" in hard_filter(V("a", 2, 9000, 5), NOW)                       # under the min length
    assert hard_filter(V("a", 4.5, 9000, 5), NOW, category="sports") == ""          # sports show segment: 4 min ok
    assert "days old" in hard_filter(V("a", 20, 9000, 20 * 24), NOW)                # older than 14 days
    assert "days old" in hard_filter(V("a", 20, 9e5, 9 * 24), NOW, days=max_days("gaming", True))  # launch: 7
    assert "dead" in hard_filter(V("a", 20, 900, 72), NOW)                          # < 5k views after 2 days
    assert hard_filter(V("a", 20, 900, 30), NOW) == ""                              # still young
    assert hard_filter(V("a", 20, 9000, 5, "is_live"), NOW) == "live / upcoming"
    assert hard_filter(V("a", 20, 9000, 5), NOW, used={"a"}) == "clipped before"
    assert hours_old({"timestamp": NOW - 10}, NOW) == 1.0 and hours_old({}, NOW) is None
    assert _pct([3, None, 1, 2]) == [1.0, 0.0, 1 / 3, 2 / 3]
    vs = metrics([V("a", 20, 100_000, 10, like_count=5000, comment_count=500, channel_follower_count=10_000),
                  V("b", 20, 10_000, 100, like_count=100, comment_count=0, channel_follower_count=1_000_000)], NOW)
    a, b = vs[0]["stats"], vs[1]["stats"]
    assert a["velocity"] == 10_000 and a["outlier"] == 10.0 and a["engagement"] == 0.065 and a["freshness"] == 1.0, a
    assert (a["velocity_pct"], b["velocity_pct"], b["freshness"]) == (1.0, 0.5, .5), (a, b)
    for v, c in zip(vs, (8, 3)):
        v["verdict"] = {"clippability": c, "drop": "", "why": "", "moments": [], "risk": "low", "risk_note": ""}
    assert (score(vs[0]), score(vs[1])) == (98, 48), (score(vs[0]), score(vs[1]))  # 100*(.4+.25+.15+.1+.08), 100*(.2+.125+.075+.05+.03)
    assert rejected(vs[0]) == "" and rejected(vs[1]).startswith("clippable 3/10")
    vs[0]["verdict"]["risk"] = "high"
    assert rejected(vs[0]).startswith("copyright risk high")
    assert same_topic("gta 6 trailer 3", ["GTA 6 trailer"]) and not same_topic("fortnite", ["gta 6"])
    print("scout ok")
