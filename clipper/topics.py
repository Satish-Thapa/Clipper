"""Topic queue: topic -> DeepSeek search queries -> trending YouTube videos -> one normal clipping job per video.

Up to Settings → "Automations at once" topics run side by side (several autopilots / topics at the same time);
inside a topic the videos are clipped one after another (worker.run). Queued topics survive a server restart;
loop() picks them up again."""
import json
import re
import threading
import time

from . import categories, db, download, pick, vision, worker
from . import config

WAKE = threading.Event()  # set when a topic is added, so the loop doesn't wait for its next poll
STOPS = {}  # topic_id -> threading.Event
CURRENT = {}  # topic_id -> job_id being clipped right now
MIN_CLIPPABLE = 6  # DeepSeek score 0-10; below this a video needs its full context, so it isn't clipped


def start_loop():
    threading.Thread(target=_loop, daemon=True).start()


def enqueue(**topic):
    tid = db.insert("topics", **topic, created=time.time())
    WAKE.set()
    return tid


def stop(tid):
    """Queued: just cancelled. Running: stops the current video's job and skips the rest."""
    t = db.get("topics", tid)
    if t and t["status"] == "queued":
        db.update("topics", tid, status="stopped")
    elif tid in STOPS:
        STOPS[tid].set()
        if tid in CURRENT:
            worker.stop(CURRENT[tid])
    db.log(tid, "stop requested", "topics")


RUNNING = set()  # topic ids being worked on right now
_claim = threading.Lock()


def _loop():
    while True:
        at_once = max(1, min(int(config.AUTOMATIONS_AT_ONCE or 1), 6))
        with _claim:
            queued = [t for t in reversed(db.all("topics", "status='queued'")) if t["id"] not in RUNNING]  # oldest first
            if queued and len(RUNNING) < at_once:
                t = queued[0]
                RUNNING.add(t["id"])
                threading.Thread(target=_run_one, args=(t,), daemon=True).start()
                continue
        WAKE.wait(10)
        WAKE.clear()


def _run_one(t):
    try:
        _run(t)
    finally:
        with _claim:
            RUNNING.discard(t["id"])
        WAKE.set()  # a slot is free: start the next queued topic


FAKE_TITLE = re.compile(r"\bconcepts?\b|fan[ -]?made|\bai[ -]generated\b|\bai\b (video|gameplay|trailer)|unreal engine|"
                        r"\bue ?5\b|\bwhat if\b|\bimagined\b|\breimagined\b|\bfake\b|\bre-?upload", re.I)


def choose_videos(entries, n, used):
    """Most-viewed eligible videos: right length, not live/upcoming, not clipped before. entries: flat yt-dlp results."""
    lo, hi = config.TOPIC_MIN_MINUTES * 60, config.TOPIC_MAX_MINUTES * 60
    ok = {e["id"]: e for e in entries
          if lo <= (e.get("duration") or 0) <= hi and e.get("live_status") not in ("is_live", "is_upcoming")
          and e["id"] not in used}
    return sorted(ok.values(), key=lambda e: e.get("view_count") or 0, reverse=True)[:n]


def _judge(tid, topic, videos, face_check=True):
    """DeepSeek's clippability verdict per video -> {id: (score, reason)}. If DeepSeek fails or skips a video,
    it passes unjudged (the topic still runs, ranked by views alone)."""
    if not videos:
        return {}
    try:
        with db.step(tid, f"DeepSeek judging {len(videos)} videos", "topics"):
            t = db.get("topics", tid) or {}
            scope = categories.scope(t.get("category_id"))
            if t.get("mode") == "nostalgia":
                from . import nostalgia
                scope += "\n" + nostalgia.VIDEO_RULE
            verdicts = pick.clippable(topic, videos, scope)
    except Exception as e:
        db.log(tid, f"clippability check failed ({e!r}), using views only", "topics", exc=True)
        verdicts = {}
    out = {}
    for v in videos:
        out[v["id"]] = verdicts.get(v["id"], (MIN_CLIPPABLE, "not scored"))
        if face_check and out[v["id"]][0] >= MIN_CLIPPABLE:  # text looks good: now check the picture is gameplay/scenes
            out[v["id"]] = _look(tid, v, out[v["id"]])
        score, why = out[v["id"]]
        db.log(tid, f"{'OK     ' if score >= MIN_CLIPPABLE else 'skipped'} {score:.0f}/10 {v.get('title')} "
                    f"({v.get('view_count') or 0:,} views, {(v.get('duration') or 0) / 60:.0f} min): {why}", "topics")
    return out


def _look(tid, v, verdict):
    """Visual check from YouTube's preview frames: reject videos that are mostly a person on camera (talking head,
    podcast studio) or a still picture. A small facecam over gameplay passes. Errors pass the video through."""
    try:
        t = time.time()
        a = vision.analyze(f"https://www.youtube.com/watch?v={v['id']}")
        db.log(tid, f"face check {v.get('title')}: face fills screen {a['person']:.0%}, still picture {a['static']:.0%}, "
                    f"{a['frames']} frames, {time.time() - t:.1f}s", "topics")
    except Exception as e:
        db.log(tid, f"frame check failed for {v.get('title')} ({e!r}), keeping it", "topics", exc=True)
        return verdict
    if a["person"] > vision.MAX_PERSON:
        return 0, f"a face fills the screen in {a['person']:.0%} of the video ({a['frames']} frames checked)"
    if a["static"] > vision.MAX_STATIC:
        return 0, f"still picture in {a['static']:.0%} of {a['frames']} frames"
    return verdict[0], f"{verdict[1]} (person on screen {a['person']:.0%})"


@config.in_brand(lambda t: config.brand_of_category(t.get("category_id")))
def _run(t):
    tid = t["id"]
    db.log(tid, f"topic started: '{t['topic']}' | {t['videos']} videos x ({t['shorts']} shorts + {t['longs']} long), "
                f"mode {t['mode']}" + (f", sources: {t['style']}" if t.get("style") else "")
                + f", face check {'on' if t.get('face_check', 1) else 'off'}, "
                f"{'4K' if t.get('hq') else '1080p'}, upload to {t['platforms']}", "topics")
    STOPS[tid] = threading.Event()
    try:
        db.update("topics", tid, status="searching")
        try:
            queries = pick.search_queries(t["topic"], categories.scope(t.get("category_id")))
            if t.get("mode") == "nostalgia":  # calm footage without a voice over it
                queries = [q if "commentary" in q.lower() else f"{q} no commentary" for q in queries]
            elif t.get("style") == "streamer":  # a streamer playing live: their facecam goes above the game
                queries = [f"{q} streamer live facecam" for q in queries]
            elif t.get("style") == "clean":  # the game itself, no streamer cam
                queries = [q if "gameplay" in q.lower() else f"{q} gameplay" for q in queries]
        except Exception as e:
            db.log(tid, f"DeepSeek failed ({e!r}), searching the topic itself", "topics")
            queries = [t["topic"]]
        db.update("topics", tid, queries=json.dumps(queries))
        db.log(tid, f"queries: {' | '.join(queries)}", "topics")

        # Newest first: this week's uploads; widen to this month / any time only if there aren't enough.
        # The most-viewed candidates (3x what's needed) are judged by DeepSeek: does it clip well, or do viewers
        # need the whole video? Rejected ones are skipped; the rest stay in view order.
        used = {j["url"].rsplit("v=", 1)[-1] for j in db.all("jobs")}
        entries, videos, judged = [], [], {}
        for recent in download.RECENT:
            when = ["this week", "this month", "any time"][download.RECENT.index(recent)]
            for q, found, e in download.search_many(queries, recent=recent):  # all queries at once
                if e is None:
                    entries += found
                    db.log(tid, f"search '{q}' ({when}): {len(found)} videos", "topics")
                    continue
                db.log(tid, f"search '{q}' failed: {e!r}", "topics")
                if download.bot_checked(e):
                    raise RuntimeError("YouTube bot check: add YouTube cookies in Settings → Downloads") from None
            rejected = {i for i, (score, _) in judged.items() if score < MIN_CLIPPABLE}
            # how many candidates get judged: the topic's "research" size (autopilot setting), else 3x the clips wanted
            if t.get("mode") == "nostalgia":  # obvious commentary / stream / subtitled / wrong-game titles: never judged
                from . import nostalgia
                entries = [e for e in entries if not nostalgia.bad_title(e.get("title"))]
            entries = [e for e in entries if not FAKE_TITLE.search(str(e.get("title") or ""))]  # concept / AI / reuploads
            pool = choose_videos(entries, max(t.get("pool") or t["videos"] * 3, t["videos"]), used | rejected)
            if t.get("mode") == "nostalgia":  # variety: one video per channel (their videos look alike)
                seen_ch, kept = set(), []
                for v in pool:
                    ch = v.get("channel") or v.get("uploader") or v["id"]
                    if ch not in seen_ch:
                        seen_ch.add(ch)
                        kept.append(v)
                pool = kept
            judged.update(_judge(tid, t["topic"], [v for v in pool if v["id"] not in judged], t.get("face_check", 1)))
            videos = [v for v in pool if judged[v["id"]][0] >= MIN_CLIPPABLE][:t["videos"]]
            if len(videos) >= t["videos"]:
                break
        if not videos:
            db.log(tid, "no suitable videos found", "topics")
            db.update("topics", tid, status="failed")
            return
        for v in videos:
            db.log(tid, f"picked: {v.get('title')} ({v.get('view_count') or 0:,} views, {v['duration'] / 60:.0f} min, "
                        f"clippable {judged[v['id']][0]:.0f}/10: {judged[v['id']][1]})", "topics")

        for k, v in enumerate(videos, 1):
            if STOPS[tid].is_set():
                break
            t = db.get("topics", tid)  # read again: autopilot settings saved meanwhile apply to the next video
            if db.all("jobs", "url=?", (f"https://www.youtube.com/watch?v={v['id']}",)):  # another topic took it meanwhile
                db.log(tid, f"video {k}/{len(videos)} skipped: already clipped ({v.get('title')})", "topics")
                continue
            platforms = json.loads(t["platforms"] or "[]")
            db.update("topics", tid, status=f"clipping {k}/{len(videos)}")
            jid = db.insert("jobs", url=f"https://www.youtube.com/watch?v={v['id']}", title=v.get("title"),
                            shorts=t["shorts"], longs=t["longs"], auto_upload=int(bool(platforms)),
                            platforms=json.dumps(platforms), mode=t["mode"], subtitles=t["subtitles"],
                            voiceover=t["voiceover"], aspect=t.get("aspect"), hq=t.get("hq", 0), topic_id=tid,
                            category_id=t.get("category_id"), created=time.time())
            db.log(tid, f"video {k}/{len(videos)} -> job #{jid}", "topics")
            CURRENT[tid] = jid
            t0 = time.time()
            worker.run(jid)
            if t.get("source") == "auto":
                _tidy(tid, jid)
            if float(db.get_setting("yt_bot_check") or 0) >= t0:  # every next video would hit the same wall
                db.log(tid, "YouTube bot check: stopping this topic. Add YouTube cookies in Settings → Downloads.", "topics")
                STOPS[tid].set()
        db.update("topics", tid, status="stopped" if STOPS[tid].is_set() else "done")
    except Exception as e:
        db.log(tid, f"ERROR: {e!r}", "topics", exc=True)
        db.update("topics", tid, status="failed")
    finally:
        CURRENT.pop(tid, None)


def _tidy(tid, jid):
    """Autopilot jobs: delete the downloaded source video once clipped (sources are ~0.5-2 GB each), unless the
    autopilot's 'keep source videos' is on. Clips are kept; only re-cutting that video by hand needs the source."""
    from . import autopilot
    from .config import DATA
    t = db.get("topics", tid) or {}
    s = autopilot.settings(t.get("autopilot_id")) or autopilot.settings()
    if s["keep_sources"]:
        return
    freed = 0
    for f in (DATA / "jobs" / str(jid)).glob("source.*"):
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError as e:
            db.log(tid, f"could not delete {f.name} of job #{jid}: {e!r}", "topics")
    if freed:
        db.log(tid, f"deleted job #{jid}'s source video ({freed / 1e6:.0f} MB freed)", "topics")


if __name__ == "__main__":
    E = lambda i, d, v, live=None: {"id": i, "duration": d, "view_count": v, "live_status": live}
    es = [E("a", 600, 10), E("b", 900, 500), E("c", 30, 9999), E("d", 1200, 300, "is_live"), E("e", 700, None),
          E("b", 900, 500), E("f", 800, 400)]
    assert [e["id"] for e in choose_videos(es, 2, set())] == ["b", "f"]           # most viewed, deduped
    assert [e["id"] for e in choose_videos(es, 9, {"b"})] == ["f", "a", "e"]      # skips used, short, live
    print("topics ok")
