"""Topic queue: topic -> DeepSeek search queries -> trending YouTube videos -> one normal clipping job per video.

Topics run one at a time, and inside a topic the videos are clipped one after another (worker.run), so the
GPU/CPU is never shared between jobs. Queued topics survive a server restart; loop() picks them up again."""
import json
import threading
import time

from . import db, download, pick, vision, worker
from .config import TOPIC_MAX_MINUTES, TOPIC_MIN_MINUTES

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


def _loop():
    while True:
        queued = db.all("topics", "status='queued'")
        if not queued:
            WAKE.wait(10)
            WAKE.clear()
            continue
        _run(queued[-1])  # db.all is newest first -> oldest queued topic first


def choose_videos(entries, n, used):
    """Most-viewed eligible videos: right length, not live/upcoming, not clipped before. entries: flat yt-dlp results."""
    lo, hi = TOPIC_MIN_MINUTES * 60, TOPIC_MAX_MINUTES * 60
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
            verdicts = pick.clippable(topic, videos)
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


def _run(t):
    tid = t["id"]
    db.log(tid, f"topic started: '{t['topic']}' | {t['videos']} videos x ({t['shorts']} shorts + {t['longs']} long), "
                f"mode {t['mode']}, face check {'on' if t.get('face_check', 1) else 'off'}, "
                f"{'4K' if t.get('hq') else '1080p'}, upload to {t['platforms']}", "topics")
    STOPS[tid] = threading.Event()
    try:
        db.update("topics", tid, status="searching")
        try:
            queries = pick.search_queries(t["topic"])
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
            for q in queries:
                try:
                    found = download.search(q, recent=recent)
                    entries += found
                    db.log(tid, f"search '{q}' ({['this week', 'this month', 'any time'][download.RECENT.index(recent)]}): "
                                f"{len(found)} videos", "topics")
                except Exception as e:
                    db.log(tid, f"search '{q}' failed: {e!r}", "topics", exc=True)
            rejected = {i for i, (score, _) in judged.items() if score < MIN_CLIPPABLE}
            pool = choose_videos(entries, t["videos"] * 3, used | rejected)
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

        platforms = json.loads(t["platforms"] or "[]")
        for k, v in enumerate(videos, 1):
            if STOPS[tid].is_set():
                break
            db.update("topics", tid, status=f"clipping {k}/{len(videos)}")
            jid = db.insert("jobs", url=f"https://www.youtube.com/watch?v={v['id']}", title=v.get("title"),
                            shorts=t["shorts"], longs=t["longs"], auto_upload=int(bool(platforms)),
                            platforms=json.dumps(platforms), mode=t["mode"], subtitles=t["subtitles"],
                            voiceover=t["voiceover"], aspect=t.get("aspect"), hq=t.get("hq", 0), topic_id=tid, created=time.time())
            db.log(tid, f"video {k}/{len(videos)} -> job #{jid}", "topics")
            CURRENT[tid] = jid
            worker.run(jid)
        db.update("topics", tid, status="stopped" if STOPS[tid].is_set() else "done")
    except Exception as e:
        db.log(tid, f"ERROR: {e!r}", "topics", exc=True)
        db.update("topics", tid, status="failed")
    finally:
        CURRENT.pop(tid, None)


if __name__ == "__main__":
    E = lambda i, d, v, live=None: {"id": i, "duration": d, "view_count": v, "live_status": live}
    es = [E("a", 600, 10), E("b", 900, 500), E("c", 30, 9999), E("d", 1200, 300, "is_live"), E("e", 700, None),
          E("b", 900, 500), E("f", 800, 400)]
    assert [e["id"] for e in choose_videos(es, 2, set())] == ["b", "f"]           # most viewed, deduped
    assert [e["id"] for e in choose_videos(es, 9, {"b"})] == ["f", "a", "e"]      # skips used, short, live
    print("topics ok")
