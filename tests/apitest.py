"""API regression test: settings validation, jobs, slots, Post now + the quota guard, edits, bulk delete, slot move,
AI tests. Temp data only, nothing uploaded. Run: .venv/bin/python tests/apitest.py   (prints "apitest OK")"""
import os, sys, tempfile, time, json, threading, types
os.environ["CLIPPER_DATA"] = tempfile.mkdtemp()          # ALWAYS first: never the real data/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from clipper import app, worker, db, social, ai
from clipper.config import DATA

started = []
worker.start = lambda jid: started.append(jid)


class Inline:  # the app's "background" work runs inline, so the test can check its result
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.f = (target, args, kwargs or {})

    def start(self):
        self.f[0](*self.f[1], **self.f[2])


app.threading = types.SimpleNamespace(Thread=Inline, Lock=threading.Lock)
sent = []
social.upload = lambda p, path, *a, **k: sent.append(p) or {"id": f"{p}-id", "url": f"https://x/{p}"}
c = TestClient(app.app)


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def err(r, code, text):
    assert r.status_code == code and text in r.json()["detail"], f"{r.status_code} {r.text}"


# settings
st = ok(c.get("/api/settings"))
assert st["values"]["shorts_per_day"] == 3 and "description" in st["schema"]["properties"]["shorts_per_day"]
err(c.post("/api/settings", json={"shorts_per_day": 4}), 400, "need 4 short time(s)")
err(c.post("/api/settings", json={"layout": "split"}), 400, "background video")
err(c.post("/api/settings", json={"filler": "nope.mp4"}), 400, "isn't there")
err(c.post("/api/settings", json={"short_max": 200}), 400, "short_max")         # a short is never over 180s
ok(c.post("/api/settings", json={"long_min": 200, "long_max": 900}))            # a long: your call
err(c.post("/api/settings", json={"topic_max_minutes": 90}), 400, "topic_max_minutes")  # research: an hour at most
now = time.time()
later = [time.strftime("%H:%M", time.localtime(now + h * 3600)) for h in (2, 3)]
if later[0] > later[1] or time.localtime(now).tm_hour >= 21:
    raise SystemExit("run before 9 PM (the test needs two short slots later today)")
ok(c.post("/api/settings", json={"shorts_per_day": 2, "short_times": later, "longs_per_day": 1, "long_times": ["23:30"],
                                 "platforms": ["youtube", "facebook"], "weekend_days": [], "weekend_short_time": "21:00"}))

# jobs: validation
err(c.post("/api/jobs", json={"url": "https://vimeo.com/123"}), 400, "only YouTube links")
err(c.post("/api/jobs", json={"url": "https://youtu.be/abcdefghijk", "shorts": 0, "longs": 0}), 400, "at least one")
err(c.post("/api/jobs", json={"url": "https://youtu.be/abcdefghijk", "layout": "split"}), 400, "background video")
j = ok(c.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=abcdefghijk", "mode": "live", "layout": "cam",
                                 "cam_corner": "tr", "header": False, "fill": True, "chunk_minutes": 5}))
j = db.get("jobs", j["id"])
assert (j["mode"], j["layout"], j["header"], j["subtitles"], j["fill"], j["chunk_minutes"]) == ("live", "cam:top:tr", 0, 1, 1, 5), j
err(c.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=abcdefghijk"}), 409, "already being clipped")
err(c.post("/api/jobs/file?filename=a.exe", content=b"x" * 20000), 400, "unsupported file type")
err(c.post("/api/jobs/file?filename=a.mp4", content=b"x"), 400, "too small")
ok(c.post("/api/jobs/file?filename=my%20clip.mp4&layout=full&shorts=2", content=b"x" * 20000))
err(c.post(f"/api/jobs/{j['id']}/live", json={"chunk_minutes": 5, "max_chunks": 0, "shorts": 0, "longs": 0}), 400, "at least one")
ok(c.post(f"/api/jobs/{j['id']}/live", json={"chunk_minutes": 7, "max_chunks": 3, "shorts": 1, "longs": 0}))
err(c.delete(f"/api/jobs/{j['id']}"), 409, "still running")


def clip(score, kind="short"):
    p = DATA / "jobs" / str(j["id"]) / "clips" / f"c{score}.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"0")
    return db.insert("clips", job_id=j["id"], kind=kind, start=0, end=20, score=score, title=f"t{score}", description="",
                     tags="[]", reason="", path=str(p.relative_to(DATA)), status="rendered")


a, b, d, e = clip(90), clip(80), clip(70), clip(65)
lng = clip(60, "long")
day0 = [x for x in ok(c.get("/api/slots?days=1"))[0]["slots"] if x["kind"] == "short"]
err(c.post(f"/api/clips/{lng}/assign", json={"at": day0[0]["at"]}), 409, "short slot")
ok(c.post(f"/api/clips/{a}/assign", json={"at": day0[0]["at"]}))
err(c.post(f"/api/clips/{b}/assign", json={"at": day0[0]["at"]}), 409, "already has that slot")
err(c.post(f"/api/clips/{b}/assign", json={"at": day0[0]["at"] + 60}), 409, "no slot at that time")
# drag and drop: b dropped on a's slot -> a goes back to Clips (b had no slot)
ok(c.post(f"/api/clips/{b}/move", json={"at": day0[0]["at"]}))
assert db.get("clips", a)["status"] == "rendered" and db.get("clips", b)["post_at"] == day0[0]["at"]
ok(c.post(f"/api/clips/{b}/unassign"))
err(c.post(f"/api/clips/{a}/unassign"), 409, "isn't in a slot")

# post now: today's two upcoming slots, then it refuses; the upload guard caps every platform
ok(c.post(f"/api/clips/{a}/post"))
ok(c.post(f"/api/clips/{b}/post"))
assert sorted(sent) == ["facebook", "facebook", "youtube", "youtube"], sent
err(c.post(f"/api/clips/{d}/post"), 409, "no free short slot left today")
err(c.post(f"/api/clips/{a}/post"), 409, "already posted")
worker.upload_clip(d)  # a sneaky direct third upload: refused per platform
assert sent.count("youtube") == 2 and db.get("clips", d)["status"] == "rendered", sent
assert "quota is used: 2/2" in db.get("jobs", j["id"])["log"]
cal = ok(c.get("/api/slots?days=1"))[0]
assert cal["used"]["short"] == 2 and cal["limit"]["short"] == 2, cal

# a failed upload: the error shows, can be cleared
db.update("clips", e, status="upload failed", uploads=json.dumps({"youtube": {"error": "HTTP 403: quotaExceeded", "at": now}}))
assert ok(c.post(f"/api/clips/{e}/clear-errors"))["status"] == "rendered"

# edit, delete, bulk delete
err(c.patch(f"/api/clips/{a}", json={"title": "  "}), 400, "can't be empty")
err(c.patch(f"/api/clips/{a}", json={"headline": "NEW"}), 409, "already posted")
ok(c.patch(f"/api/clips/{d}", json={"title": "better", "tags": ["#gta6", "gta6", "fails"]}))
assert json.loads(db.get("clips", d)["tags"]) == ["gta6", "fails"]
r = ok(c.post("/api/clips/delete", json={"ids": [d, e, lng]}))
assert sorted(r["deleted"]) == sorted([d, e, lng]) and not db.get("clips", d)
fin = db.insert("jobs", url="u", mode="normal", shorts=1, longs=0, status="done", created=now)
r = ok(c.post("/api/jobs/delete", json={"ids": [fin, j["id"]]}))
assert r["deleted"] == [fin] and r["skipped"][0]["id"] == j["id"], r
assert ok(c.get(f"/api/jobs/{j['id']}/curves")) == []

# force upload: past the daily quota, to the platforms you pick, warned first
f = clip(55)
w = ok(c.get(f"/api/clips/{f}/force"))["warnings"]
assert any("quota is used" in x for x in w["youtube"]), w
ok(c.post(f"/api/clips/{f}/force", json={"platforms": ["youtube"]}))
assert sent.count("youtube") == 3 and db.get("clips", f)["status"] == "uploaded", sent
assert "FORCE UPLOAD to youtube, past the daily quota" in db.get("jobs", j["id"])["log"]

# fill days: off / today / week / month only; Post to the platforms you pick
r = c.post("/api/jobs", json={"url": "https://youtu.be/zzzzzzzzzzz", "fill": 3})
assert r.status_code == 422, r.text
ok(c.post("/api/jobs", json={"url": "https://youtu.be/yyyyyyyyyyy", "fill": 30}))
err(c.post("/api/jobs/file?filename=a.mp4&fill=3", content=b"x" * 20000), 400, "fill is 0")
ok(c.post("/api/settings", json={"shorts_per_day": 3, "short_times": later + ["23:59"]}))
g = clip(45)
sent.clear()
ok(c.post(f"/api/clips/{g}/post", json={"platforms": ["facebook"]}))
assert sent == ["facebook"], sent  # only where you picked
# Auto topic: your own clips per video
ok(c.post("/api/settings", json={"auto_rows": [{"category": "gaming", "videos": 2, "short_slots": 1, "shorts_per_video": 4}]}))
assert ok(c.get("/api/auto"))["rows"][0]["per_video"] == {"short": 4, "long": 0}
ok(c.post("/api/settings", json={"auto_rows": [{"category": "gaming", "videos": 1, "short_slots": 1}]}))

# AI tests: stubbed model answers
ai._missing = lambda p: ""


def fake(p, task, system, model=None):
    ai.LAST.tokens = 120
    if "bank heist" in task:
        return {"clips": [{"start": 40, "end": 76, "title": "Worst Heist Ever", "headline": "HEIST GOES WRONG 😭",
                           "hashtags": ["gta6"], "scores": {"hook": 9, "flow": 8, "emotion": 9, "share": 7, "trend": 6}}]}
    if "items" in task:
        return {"items": [{"n": 1, "word": "one"}, {"n": 2, "word": "two"}], "done": True}
    return {"reply": "I am a test model"}


ai._ask_one = fake
for kind in ("ping", "json", "clip"):
    r = ok(c.post("/api/ai/test", json={"provider": "deepseek", "kind": kind, "model": "try-me"}))
    assert r["ok"] and r["model"] == "try-me", r
ai._ask_one = lambda p, task, system, model=None: {"clips": [{"start": 0, "end": 10}]}
bad = ok(c.post("/api/ai/test", json={"provider": "deepseek", "kind": "clip"}))
assert not bad["ok"] and [x["name"] for x in bad["checks"] if not x["ok"]][:1] == ["all 5 scores"], bad

s = ok(c.get("/api/state"))
assert s["today"]["limit"]["short"] == 3 and "jobs" in s  # raised to 3 a day above
assert ok(c.get("/api/reports"))["performance"]["posted"] == 4  # a, b, the forced one, the one posted to facebook
err(c.post("/api/news/find", json={"topic": "gta"}), 403, "switched off")
assert ok(c.get("/api/auto"))["rows"][0]["per_video"] == {"short": 2, "long": 0}
# an older saved settings blob (with the 0.8s lead-in) is migrated once to no lead-in
from clipper import settings as S_
db.set_setting("app", json.dumps({**S_.get().model_dump(), "pad_before": 0.8, "start_fix": False}))
S_.reset_cache()
assert S_.get().pad_before == 0.0 and S_.get().start_fix
print("apitest OK")
