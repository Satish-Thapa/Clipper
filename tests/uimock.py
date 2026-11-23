"""Look at the page without the server: seed a TEMP database through the real API (TestClient: no server, no loops,
never the real data/), save the answers as static fixtures, then serve them with a tiny static http.server.
Run: .venv/bin/python tests/uimock.py [port]   then open http://127.0.0.1:8765/#clips (or screenshot it with headless
Chrome: --force-prefers-reduced-motion; phone width: /phone.html?p=clips loads the page in a 390px iframe).
POSTs answer {} (nothing happens). Needs ffmpeg for the posters (FFMPEG=/opt/homebrew/bin/ffmpeg on the Mac)."""
import os, sys, tempfile, time, json, subprocess
os.environ["CLIPPER_DATA"] = tempfile.mkdtemp()          # ALWAYS first
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from fastapi.testclient import TestClient
from clipper import app, db, settings, quota, worker
from clipper.config import DATA, FFMPEG

OUT = Path(tempfile.mkdtemp())
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
worker.start = lambda jid: None
from clipper import auth
auth.DISABLED = True  # screenshots only: no login
c = TestClient(app.app)
now = time.time()
t = lambda h: time.strftime("%H:%M", time.localtime(now + h * 3600))
settings.save({"shorts_per_day": 3, "short_times": sorted({t(-3), t(1.5), t(4)}), "longs_per_day": 1, "long_times": [t(5)],
               "platforms": ["youtube", "instagram"], "long_platforms": ["youtube"], "auto_enabled": True, "weekend_days": []})
J = lambda **k: db.insert("jobs", created=now - 3600, auto_upload=0, **k)
j1 = J(url="https://www.youtube.com/watch?v=abc", title="IShowSpeed plays GTA 6 for the first time (12 hours)", mode="live", shorts=2,
       longs=0, layout="cam:top:auto", header=1, subtitles=1, fill=7, status="recording chunk 4",
       log="[12:01] job started\n[12:03] STEP 4 transcribing\n[12:04] STEP 5 asking")
j2 = J(url="file:podcast.mp4", title="Flagrant ep 412", mode="normal", shorts=3, longs=1, layout="", header=1, subtitles=1, status="done", log="done")
j3 = J(url="https://www.youtube.com/watch?v=def", title="NBA trade deadline reactions", mode="auto", shorts=0, longs=2, layout="",
       header=1, subtitles=0, status="failed", log="ERROR: YouTube bot check ('Sign in to confirm you're not a bot')")
n = 600
(DATA / "jobs" / str(j1)).mkdir(parents=True, exist_ok=True)
(DATA / "jobs" / str(j1) / "curves_live_0001.json").write_text(json.dumps({"name": "live_0001.ts", "n": n, "signals": {
    "hype": [abs(((i * 7) % 97) / 97 - .3) for i in range(n)], "chat": [1.0 if 200 < i < 230 else .1 for i in range(n)]},
    "chosen": [{"kind": "short", "start": 190, "end": 240, "title": "Speed fails"}]}))
rows = [("Speed Thinks NPCs Are Real People", "SPEED THINKS NPCS ARE REAL 😭", "short", 91, "laugh"),
        ("This GTA 6 Heist Went SO Wrong", "HEIST GOES WRONG 💀", "short", 84, "fail"),
        ("He Didn't Expect This Question", "HE WAS NOT READY 😳", "short", 77, "hot_take"),
        ("The Trade Nobody Saw Coming", "NOBODY SAW THIS COMING 🤯", "long", 72, "reaction"),
        ("You Can Take Dates In GTA 6", "DATES IN GTA 6 🔥", "long", 68, "hidden_detail"),
        ("Speed Rages At The Cops", "SPEED RAGES AT COPS 😤", "short", 63, "rage")]
colors = ["0x76B900", "0x00BFFF", "0xc77dff", "0xFF5A5F", "0xE8B339", "0x2A2A2A"]
ids = []
for i, (title, head, kind, score, moment) in enumerate(rows):
    job = (j1, j2, j3)[i % 3]
    p = DATA / "jobs" / str(job) / "clips" / f"m{i}.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"0")
    cid = db.insert("clips", job_id=job, kind=kind, start=100, end=100 + (34 if kind == "short" else 120), score=score, title=title,
                    headline=head, description="d", tags=json.dumps(["gta6"]), reason="the hook lands in 2s", path=str(p.relative_to(DATA)),
                    status="rendered", why=json.dumps({"by": "gpt-oss:20b", "moment": moment, "confirmed": ["chat"] * (i == 0),
                                                       "scores": {"hook": 9, "flow": 7, "emotion": 8, "share": 6, "trend": 5}}))
    ids.append(cid)
    (OUT / "api" / "clips" / str(cid)).mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={colors[i]}:s=270x480", "-frames:v", "1",
                    "-f", "image2", "-c:v", "png", str(OUT / "api" / "clips" / str(cid) / "poster")], check=True)
db.update("clips", ids[0], status="uploaded", post_at=quota.at(quota.day_of(now), t(-3)),
          uploads=json.dumps({"youtube": {"id": "x", "url": "https://youtu.be/x", "at": now - 3 * 3600}}), views=json.dumps({"youtube": 48210}))
db.update("clips", ids[3], status="upload failed",
          uploads=json.dumps({"youtube": {"error": "HTTP 403: The user has exceeded the number of videos they may upload. (uploadLimitExceeded)", "at": now - 3600}}))
free = [x for x in c.get("/api/slots?days=2").json()[0]["slots"] if x["state"] == "free" and x["kind"] == "short"]
if free:
    c.post(f"/api/clips/{ids[1]}/assign", json={"at": free[0]["at"]})
db.insert("auto_rounds", at=now - 600, data=json.dumps({"at": now - 600, "day": time.strftime("%Y-%m-%d"), "reason": "daily", "rows": [],
          "log": ["[09:00:01 AM] research started (daily): 2 topic(s)", "[09:00:02 AM] today's plan: 3 short + 1 long slot(s)",
                  "[09:00:40 AM] topic 1: the AI picked 'GTA 6 first look' (score 8/10): 5 big channels this week",
                  "[09:01:10 AM]    topic 1: OK  86/100 IShowSpeed GTA 6 | 1.2M views ..."]}))
# the Cheat page: a reel from an Instagram DM downloading, one from Messenger ready in a slot, a pasted one that failed
settings.save({"inbox_enabled": True, "inbox_sender": "Main Account", "inbox_ig_sender": "main.acct", "notify_uploads": True})
from clipper import inbox
inbox._save("facebook", {"since": now - 7200, "checked_at": now - 20, "last": {"at": now - 900, "links": ["x"], "jobs": [1], "skipped": []}})
inbox._save("instagram", {"since": now - 7200, "checked_at": now - 20})
r1 = J(url="https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1", title="Speed crashes the cop car", mode="repost", shorts=1,
       longs=0, layout="", header=0, subtitles=0, fill=1, status="downloading", via="instagram", log="")
worker.DL[r1] = {"pct": 46.3, "done": 61_000_000, "total": 132_000_000, "speed": 4_200_000, "eta": 17, "part": 1, "file": "v"}
r2 = J(url="https://www.facebook.com/reel/123", title="Kai Cenat Finally Meets Drake", mode="repost", shorts=1, longs=0, layout="",
       header=0, subtitles=0, fill=1, status="done", via="facebook", log="")
db.insert("clips", job_id=r2, kind="short", start=0, end=41, score=90, title="Kai Cenat Finally Meets Drake", headline="",
          description="Credit: kaicenat", tags="[]", reason="Reposted", path=str((DATA / "jobs" / str(j2) / "clips" / "m0.mp4").relative_to(DATA)),
          status="scheduled", post_at=now + 5400, why=json.dumps({"by": "repost"}))
J(url="https://www.instagram.com/reel/abc/", title=None, mode="repost", shorts=1, longs=0, layout="", header=0, subtitles=0, fill=1,
  status="failed", via="pasted", log="[10:00] ERROR: Instagram needs you to be logged in to download this reel")
FIX = {"state": "/api/state", "slots": "/api/slots?days=7", "settings": "/api/settings", "auto": "/api/auto", "reports": "/api/reports",
       "fillers": "/api/fillers", "logs": "/api/logs?lines=800", "curves": f"/api/jobs/{j1}/curves"}
for name, path in FIX.items():
    (OUT / "api" / f"{name}.json").write_text(json.dumps(c.get(path).json()))
(OUT / "index.html").write_text((Path(__file__).resolve().parent.parent / "clipper" / "static" / "index.html").read_text(encoding="utf-8"),
                                encoding="utf-8")
(OUT / "phone.html").write_text("<body style='margin:0;background:#000'><script>document.write(`<iframe src='/#${new URLSearchParams("
                                "location.search).get('p') || 'home'}' style='width:390px;height:844px;border:0'></iframe>`)</script>")
ROUTES = {"/api/" + k: k for k in ("state", "slots", "settings", "auto", "reports", "fillers", "logs")}


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(OUT), **k)

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ROUTES:
            return self._send((OUT / "api" / f"{ROUTES[path]}.json").read_bytes(), "application/json")
        if path.endswith("/curves"):
            return self._send((OUT / "api" / "curves.json").read_bytes(), "application/json")
        if path.endswith("/poster"):
            return self._send((OUT / path.lstrip("/")).read_bytes(), "image/png")
        if path.endswith("/preview") or path.startswith("/api/"):
            return self._send(b"{}", "application/json", 404)
        return super().do_GET()

    def do_POST(self):
        self._send(b"{}", "application/json")


print(f"mock ready on http://127.0.0.1:{PORT}/", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
