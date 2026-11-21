"""Auto mode, and the loop that posts every slot.

Every minute (loop): quota.tick posts each clip whose slot time has come (Normal, Live and Auto clips alike).

Every day at Settings → research_time:
  1. the report: views and stats from every platform (stats.daily) -> what works here (insights.decide), which the
     researcher and the cutter read in their prompts today. Done even when Auto is off.
  2. if Auto is on, for each Auto topic (Settings → auto_rows, up to three: category, topic, videos, slots):
     topic = yours, or the AI's pick of what's trending in that category today (scout.trending_topics)
     -> scout.find_videos -> one job per video, clipped one at a time (clips per video = its slots / videos + 1 spare)
     -> its best clips fill ITS share of today's free slots (quota.fill; the share is kept free for it until it's done)
  3. on weekend days, the extra +1 slots get the best Auto clips left over.
Clips that don't get a slot wait in Clips. Nothing here posts more than the quota: every upload is checked in
worker.upload_clip.

Stop (stop_all) aborts the running research, stops its job, and Auto is switched off."""
import json
import math
import threading
import time
from datetime import date

from . import categories, db, download, insights, quota, scout, settings, stats, worker

LOCK = threading.Lock()  # one research run at a time (loop + "Run now")
STAGE = [""]  # what the running research is doing right now, for the UI
ABORT = threading.Event()  # set by stop_all(): the running research gives up at its next step
CURRENT = {}  # "job": the job being clipped right now
FINISHED = ("done", "failed", "stopped", "interrupted")
NICHE = {"gaming": "gaming: GTA 5, GTA 6, Fortnite, Minecraft, Call of Duty, big streamers playing",
         "podcast": "podcasts and talk shows: big-name guests, hot takes, drama, interviews",
         "sports": "sports talk: NBA, NFL, soccer, boxing, UFC debates, press conferences, reactions"}


class Stopped(Exception):
    pass


def _step(text):
    if ABORT.is_set():
        raise Stopped()
    STAGE[0] = text


def layout_of(s):
    """The default framing in Settings as render's layout string."""
    return {"full": "", "cam": f"cam:top:{s.cam_corner}", "split": f"split:{s.filler}"}[s.layout]


def per_video(slots, videos):
    """Clips to cut from each video so a topic can fill `slots` with its best: an even share + 1 to choose from."""
    return math.ceil(slots / videos) + 1 if slots else 0


def _today_runs():
    return [r for r in rounds(10) if r.get("day") == date.today().isoformat()]


def due(s=None, now=None):
    """'daily' when today's research should start now, else None."""
    s, now = s or settings.get(), now or time.time()
    if not s.auto_enabled or now < quota.at(date.fromtimestamp(now), s.research_time):
        return None
    return None if _today_runs() else "daily"


def report_due(s=None, now=None):
    s, now = s or settings.get(), now or time.time()
    return now >= quota.at(date.fromtimestamp(now), s.research_time) and \
        db.get_setting("report_day") != date.fromtimestamp(now).isoformat()


def daily_report():
    """Refresh every platform's stats, snapshot today's numbers, decide what works. -> the decisions (or None)."""
    db.set_setting("report_day", date.today().isoformat())
    try:
        stats.daily()
    except Exception as e:
        db.syslog(f"daily report: refreshing the stats failed ({e!r})", exc=True)
    try:
        return insights.decide()
    except Exception as e:
        db.syslog(f"daily report: deciding what works failed ({e!r})", exc=True)
        return None


def run_day(reason="daily"):
    """Today's research and clipping for every Auto topic. -> the run record (saved for the Auto page)."""
    if not LOCK.acquire(blocking=False):
        return {"error": "the research is already running"}
    ABORT.clear()
    s, t0 = settings.get(), time.time()
    rec = {"at": t0, "day": date.today().isoformat(), "reason": reason, "rows": [], "decisions": None}
    try:
        _step("refreshing the reports")
        rec["decisions"] = daily_report()
        history = "\n".join(x for x in (insights.history_text(), (rec["decisions"] or {}).get("scout", "")) if x)
        auto_cids = []
        for i, row in enumerate(s.auto_rows):
            quota.set_row_state(i, "running")
            try:
                rec["rows"].append(_row(i, row, s, history, auto_cids))
            finally:
                quota.set_row_state(i, "done")
        extra = [x for x in quota.plan(date.today(), s) if x["extra"]]
        if extra and auto_cids:  # weekend: the extra slots get the best Auto clips left over
            _step("filling the weekend's extra slots")
            rec["weekend"] = len(quota.fill(auto_cids, None, db.syslog, only=[x["at"] for x in extra]))
    except Stopped:
        rec["error"] = "stopped by you"
        db.syslog("Auto: research stopped by you")
    except Exception as e:
        rec["error"] = str(e)[:300]
        db.syslog(f"Auto: research failed: {e!r}", exc=True)
    finally:
        rec["seconds"] = round(time.time() - t0)
        STAGE[0] = ""
        ABORT.clear()
        LOCK.release()
    db.insert("auto_rounds", at=rec["at"], data=json.dumps(rec))
    return rec


def _row(i, row, s, history, auto_cids):
    """One Auto topic: find -> clip -> fill its slots. -> what happened, for the run record."""
    out = {"row": i, "category": row.category, "asked": row.topic, "videos": [], "slots": {}}
    topic, research = row.topic.strip(), {}
    if not topic:
        _step(f"topic {i + 1}: finding what's trending in {row.category}")
        found = scout.trending_topics(NICHE[row.category], 1, research, history, s.notes, _step)
        topic = found[0]["topic"]
        out.update(trend=found[0], research=research)
    shorts, longs = per_video(row.short_slots, row.videos), per_video(row.long_slots, row.videos)
    tid = db.insert("topics", topic=topic, videos=row.videos, shorts=shorts, longs=longs, mode="auto",
                    category=row.category, auto_row=i, source="auto", face_check=int(s.face_check), notes=s.notes,
                    status="searching", created=time.time())
    out.update(topic=topic, topic_id=tid)
    say = lambda m, exc=False: db.log(tid, m, "topics", exc=exc)
    say(f"Auto topic {i + 1} ({row.category}): '{topic}'" + (" (trending today)" if not row.topic.strip() else "")
        + f" | {row.videos} video(s), {shorts} short(s) + {longs} long(s) from each, for {row.short_slots} short + "
          f"{row.long_slots} long slot(s) today")
    _step(f"topic {i + 1}: finding videos for '{topic}'")
    used = {j["url"].rsplit("v=", 1)[-1] for j in db.all("jobs")}
    videos, plan = scout.find_videos({**db.get("topics", tid), "category": row.category}, used, say)
    db.update("topics", tid, queries=json.dumps(plan["queries"]))
    jobs = []
    for k, v in enumerate(videos, 1):
        _step(f"topic {i + 1}: clipping video {k}/{len(videos)}: {v.get('title')}")
        db.update("topics", tid, status=f"clipping {k}/{len(videos)}")
        jid = db.insert("jobs", url=f"https://www.youtube.com/watch?v={v['id']}", title=v.get("title"), mode="auto",
                        auto_row=i, topic_id=tid, category=row.category, shorts=shorts, longs=longs,
                        layout=layout_of(s), header=int(s.header), subtitles=int(s.subtitles),
                        notes="", fill=0, auto_upload=0, created=time.time())
        say(f"video {k}/{len(videos)} -> job #{jid}: {v.get('title')} (scout {v['scout_score']}/100)")
        CURRENT["job"] = jid
        t0 = time.time()
        try:
            worker.run(jid)
        finally:
            CURRENT.pop("job", None)
        jobs.append(jid)
        out["videos"].append({"id": v["id"], "title": v.get("title"), "job": jid, "score": v["scout_score"],
                              "why": v["verdict"]["why"]})
        hit = download.cookies_state()["bot_check_at"]
        if hit and hit >= t0:
            say("YouTube bot check: stopping this topic. Add YouTube cookies on the Socials page.")
            break
        _step(f"topic {i + 1}: next video")
    cids = [c["id"] for j in jobs for c in db.all("clips", "job_id=? AND status='rendered'", (j,))]
    auto_cids += cids
    given = quota.fill(cids, i, say)
    out.update(slots={str(cid): at for cid, at in given.items()}, clips=len(cids))
    db.update("topics", tid, status="done" if videos else "failed")
    say(f"topic done: {len(cids)} clip(s), {len(given)} in today's slots, {len(cids) - len(given)} wait in Clips")
    return out


def stop_all():
    """Stop the running research and its job; Auto is switched off (clips already in slots stay there)."""
    if LOCK.locked():
        ABORT.set()
    if CURRENT.get("job"):
        worker.stop(CURRENT["job"])
    try:
        settings.save({"auto_enabled": False})
    except ValueError:
        pass
    db.syslog("Auto stopped by you")
    return {"running": LOCK.locked()}


def rounds(limit=20):
    """Past research runs, newest first (Auto page)."""
    with db.conn() as c:
        return [{"id": r["id"], **json.loads(r["data"])}
                for r in c.execute("SELECT * FROM auto_rounds ORDER BY id DESC LIMIT ?", (limit,))]


def status():
    s = settings.get()
    runs = _today_runs()
    return {"enabled": s.auto_enabled, "running": LOCK.locked(), "stage": STAGE[0] if LOCK.locked() else "",
            "next_at": quota.at(date.today(), s.research_time) if not runs else None, "today": runs[:1],
            "rows": [{**r.model_dump(), "per_video": {"short": per_video(r.short_slots, r.videos),
                                                      "long": per_video(r.long_slots, r.videos)}} for r in s.auto_rows],
            "categories": categories.NAMES, "decisions": insights.decisions(),
            "topics": db.all("topics", "source='auto'")[:9]}


def loop():
    tick = 0
    while True:
        try:
            quota.tick(worker.upload_clip)  # every minute: is a slot due?
            if tick % 5 == 0:  # every 5 minutes: is today's report / research due?
                if due():
                    run_day("daily")  # its first step is the daily report
                elif report_due():
                    daily_report()
        except Exception as e:
            db.syslog(f"auto loop error: {e!r}", exc=True)
        tick += 1
        time.sleep(60)


def start_loop():
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    assert per_video(3, 2) == 3 and per_video(0, 2) == 0 and per_video(1, 1) == 2
    print("autopilot ok")
