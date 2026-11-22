"""Auto mode regression test: two topics with slot shares on a weekend day, a manual clip that may only take a slot no
topic keeps, weekend +1 leftovers, the researcher reading today's decisions. Fakes the finding and the clipping.
Temp data only. Run before 8 PM: .venv/bin/python tests/autotest.py   (prints "autotest OK")"""
import os, sys, tempfile, time
os.environ["CLIPPER_DATA"] = tempfile.mkdtemp()          # ALWAYS first
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, datetime, timedelta
from clipper import db, autopilot, scout, worker, quota, settings, stats, insights
from clipper.config import DATA
db.init()
now = datetime.now()
if now.hour >= 20:
    raise SystemExit("run before 8 PM (the test needs today's slots to be ahead)")
base = now + timedelta(minutes=10)
mk = lambda k: (base + timedelta(minutes=12 * k)).strftime("%H:%M")
settings.save({"shorts_per_day": 3, "short_times": [mk(0), mk(1), mk(2)], "longs_per_day": 1, "long_times": [mk(3)],
               "weekend_days": [now.weekday()], "weekend_short_time": mk(4), "weekend_long_time": mk(5),
               "platforms": ["youtube"], "auto_enabled": True, "research_time": "00:00",
               "auto_rows": [{"category": "gaming", "topic": "GTA 6", "videos": 2, "short_slots": 2, "long_slots": 0},
                             {"category": "sports", "topic": "", "videos": 1, "short_slots": 1, "long_slots": 1}]})
mj = db.insert("jobs", url="u", mode="normal", shorts=1, longs=0, fill=1, created=time.time())


def clip(job, score, kind="short"):
    p = DATA / "jobs" / str(job) / "clips" / f"{score}{kind}.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"0")
    return db.insert("clips", job_id=job, kind=kind, start=0, end=20, score=score, title=f"s{score}", description="",
                     tags="[]", reason="", path=str(p.relative_to(DATA)), status="rendered")


m1 = clip(mj, 99)
quota.set_row_state(0, "running")
quota.set_row_state(1, "running")
got = quota.fill([m1])
assert list(got.values()) == [quota.at(date.today(), mk(4))], "4 short slots, the first 3 kept for topics: it gets the 4th"

stats.daily = lambda: {}
insights.decide = lambda: {"scout": "gaming wins", "cutter": "fails win", "summary": "s", "suggestions": []}
trend_calls, found = [], []


def trend(niche, k, research, history, notes, step):
    trend_calls.append((niche, history))
    return [{"topic": "NBA trade drama", "why": "big", "score": 8}]


def find(t, used, say):
    found.append((t["topic"], t["category"], t["shorts"], t["longs"]))
    return [{"id": f"{t['topic'][:3]}{i}", "title": f"{t['topic']} video {i}", "scout_score": 70, "verdict": {"why": "w"}}
            for i in range(t["videos"])], {"queries": [t["topic"]]}


scores = iter([50, 95, 60, 40, 85, 30, 75, 20, 65])


def run(jid):  # each "video" makes its clips
    job = db.get("jobs", jid)
    for _ in range(job["shorts"]):
        clip(jid, next(scores))
    for _ in range(job["longs"]):
        clip(jid, next(scores), "long")
    db.update("jobs", jid, status="done")


scout.trending_topics, scout.find_videos, worker.run = trend, find, run
rec = autopilot.run_day("test")
assert not rec.get("error"), rec
assert found == [("GTA 6", "gaming", 2, 0), ("NBA trade drama", "sports", 2, 2)], found
assert trend_calls and "gaming wins" in trend_calls[0][1], "the researcher reads today's decisions"
jobs = {j["id"]: j for j in db.all("jobs")}
sched = db.all("clips", "status='scheduled'")
by_row = {r: sorted((c["kind"], c["score"]) for c in sched if jobs[c["job_id"]].get("auto_row") == r) for r in (0, 1, None)}
# topic 0: 2 videos x 2 shorts (50, 95, 60, 40) -> its 2 short slots get 95 and 60
# topic 1: shorts 85, 30 + longs 75, 20 -> its 1 short (85) and 1 long (75); the weekend's extra long gets 20
assert [x for x in by_row[0] if x[0] == "short"][:2] == [("short", 60), ("short", 95)], by_row
assert ("short", 85) in by_row[1] and ("long", 75) in by_row[1] and ("long", 20) in by_row[1], by_row
assert rec.get("weekend") == 1 and by_row[None] == [("short", 99)], (rec.get("weekend"), by_row)
cal = quota.calendar(1)[0]
assert [x["state"] for x in cal["slots"]].count("scheduled") == 6 and cal["limit"] == {"short": 4, "long": 2}, cal
assert autopilot.due() is None, "ran today already"
lay = {j["auto_row"]: j["layout"] for j in jobs.values() if j.get("mode") == "auto"}
assert lay[0].startswith("cam:top:") and lay[1] == "", lay  # gaming: streamer on top; sports: the default (full)
print("autotest OK")
