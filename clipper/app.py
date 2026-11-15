import json
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import autopilot, db, download, pick, social, stats, topics, upload, worker
from .config import CROP_ASPECT, DATA, IG_MAX_SECONDS, SHORT_HARD_MAX, YT_COOKIES

app = FastAPI(title="Clipper")
db.init()
db.syslog(f"server started, data in {DATA}")


@app.on_event("startup")
def _start():
    """Background loops start with the server only (importing this module, e.g. in a test, starts nothing)."""
    topics.start_loop()  # picks up topics still queued from before a restart
    autopilot.start_loop()  # posts scheduled clips + runs rounds, when switched on in the UI
    stats.start_loop()  # hourly WhatsApp report, if WA_PHONE_NUMBER_ID/WA_TOKEN/WA_TO are set
    social.backfill_times()  # give old uploads a time so the 24h upload counter includes them
    social.backfill_limit_hits()  # start the 24h timer of a limit error logged before timers existed


app.mount("/media", StaticFiles(directory=DATA), name="media")
INDEX = Path(__file__).parent / "static" / "index.html"


Platform = Literal["youtube", "facebook", "instagram", "tiktok"]
Mode = Literal["hype", "ai+hype", "ai", "manual"]


class NewJob(BaseModel):
    url: str = Field(pattern=r"^https?://")
    shorts: int = Field(3, ge=0, le=20)
    longs: int = Field(1, ge=0, le=10)
    platforms: list[Platform] = ["youtube"]  # where finished clips are uploaded; [] = don't upload
    aspect: float = Field(CROP_ASPECT, ge=0.56, le=1.78)  # framing: kept w/h of the frame (0.5625 = fills the phone)
    hq: bool = False  # download up to 4K instead of 1080p (slower)
    mode: Mode = "hype"
    subtitles: bool = True
    voiceover: bool = False


class NewTopic(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    videos: int = Field(3, ge=1, le=20)  # how many trending videos to clip
    shorts: int = Field(3, ge=0, le=20)  # per video
    longs: int = Field(1, ge=0, le=10)   # per video
    platforms: list[Platform] = []
    aspect: float = Field(CROP_ASPECT, ge=0.56, le=1.78)
    hq: bool = False
    mode: Literal["hype", "ai+hype", "ai"] = "hype"  # no "manual": nobody is there to type the cuts
    subtitles: bool = True
    voiceover: bool = False
    face_check: bool = True  # skip videos that are mostly a person on screen (vision.py)


class Cut(BaseModel):
    kind: Literal["short", "long"]
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field("", max_length=100)
    headline: str = Field("", max_length=60)
    description: str = ""
    tags: list[str] = []


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.post("/api/jobs")
def create_job(j: NewJob):
    if j.shorts + j.longs == 0:
        raise HTTPException(400, "ask for at least one clip")
    jid = db.insert("jobs", url=j.url, shorts=j.shorts, longs=j.longs, auto_upload=int(bool(j.platforms)),
                    platforms=json.dumps(j.platforms), aspect=j.aspect, hq=int(j.hq), mode=j.mode, subtitles=int(j.subtitles), voiceover=int(j.voiceover), created=time.time())
    worker.start(jid)
    return {"id": jid}


@app.post("/api/jobs/file")
async def create_file_job(request: Request, filename: str, shorts: int = 3, longs: int = 1,
                          platforms: str = "youtube", mode: Mode = "hype", aspect: float = CROP_ASPECT,
                          subtitles: bool = True, voiceover: bool = False):
    """Raw file bytes in the body (no multipart, so no python-multipart dependency)."""
    plats = [p for p in platforms.split(",") if p in social.PLATFORMS]  # comma-separated in the query string
    ext = Path(filename).suffix.lower()
    if ext not in (".mp4", ".mkv", ".mov", ".webm", ".ts", ".avi"):
        raise HTTPException(400, f"unsupported file type {ext or '(none)'}")
    jid = db.insert("jobs", url=f"file:{Path(filename).name}", title=Path(filename).stem, shorts=shorts,
                    longs=longs, auto_upload=int(bool(plats)), platforms=json.dumps(plats), aspect=min(max(aspect, 0.5625), 16 / 9), mode=mode, subtitles=int(subtitles), voiceover=int(voiceover), status="uploading", created=time.time())
    d = DATA / "jobs" / str(jid)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"source{ext}", "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    worker.start(jid)
    return {"id": jid}


@app.post("/api/topics")
def create_topic(t: NewTopic):
    if t.shorts + t.longs == 0:
        raise HTTPException(400, "ask for at least one clip per video")
    tid = topics.enqueue(topic=t.topic.strip(), videos=t.videos, shorts=t.shorts, longs=t.longs, mode=t.mode,
                         subtitles=int(t.subtitles), voiceover=int(t.voiceover), platforms=json.dumps(t.platforms),
                         face_check=int(t.face_check), aspect=t.aspect, hq=int(t.hq))
    return {"id": tid}


@app.post("/api/topics/{tid}/stop")
def stop_topic(tid: int):
    topics.stop(tid)
    return {"ok": True}


@app.delete("/api/topics/{tid}")
def delete_topic(tid: int):
    t = db.get("topics", tid)
    if not t:
        raise HTTPException(404, "topic not found")
    if t["status"] not in ("queued", "done", "failed", "stopped", "interrupted"):
        raise HTTPException(409, "topic is running, stop it first")
    db.delete("topics", tid)  # its jobs and clips stay
    return {"ok": True}


@app.post("/api/jobs/{jid}/cuts")
def add_cuts(jid: int, cuts: list[Cut], subtitles: bool = True):
    if not db.get("jobs", jid):
        raise HTTPException(404, "job not found")
    if not next((DATA / "jobs" / str(jid)).glob("source.*"), None):
        raise HTTPException(409, "no source video for this job yet (live jobs can't be cut manually)")
    if not cuts or any(c.end <= c.start for c in cuts):
        raise HTTPException(400, "each cut needs end > start")
    if any(c.end - c.start > SHORT_HARD_MAX for c in cuts):
        raise HTTPException(400, f"clips can be at most {SHORT_HARD_MAX}s (YouTube Shorts limit)")
    worker.manual_cuts(jid, [c.model_dump() for c in cuts], subtitles)
    return {"ok": True}


@app.get("/api/jobs/{jid}/curves")
def curves(jid: int):
    return [json.loads(p.read_text()) for p in sorted((DATA / "jobs" / str(jid)).glob("curves_*.json"))]


@app.get("/api/state")
def state():
    return {"jobs": db.all("jobs"), "clips": db.all("clips"), "topics": db.all("topics"), "platforms": social.connected(),
            "deepseek": pick.balance(), "whatsapp": stats.whatsapp_ready(), "report_minutes": stats.REPORT_MINUTES,
            "view_errors": stats.LAST["errors"], "crop_aspect": CROP_ASPECT,
            "uploads": social.upload_status(), "limit_timers": social.limit_timers(), "ig_max_seconds": IG_MAX_SECONDS,
            "youtube_cookies": download.cookies_state()}


@app.post("/api/cookies")
async def save_cookies(request: Request):
    """Body: a cookies.txt (Netscape format) exported from a logged-in youtube.com tab. Fixes YouTube's bot check."""
    text = (await request.body()).decode("utf-8", "replace")
    if ".youtube.com" not in text or chr(9) not in text:
        raise HTTPException(400, "that doesn't look like a cookies.txt with youtube.com cookies (Netscape format)")
    YT_COOKIES.write_text(text if text.startswith("# Netscape") else "# Netscape HTTP Cookie File" + chr(10) + text,
                          encoding="utf-8")
    db.syslog(f"YouTube cookies saved to {YT_COOKIES.name} ({text.count('.youtube.com')} youtube.com cookies)")
    return download.cookies_state()


@app.delete("/api/cookies")
def delete_cookies():
    YT_COOKIES.unlink(missing_ok=True)
    db.syslog("YouTube cookies removed")
    return download.cookies_state()


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: int):
    worker.stop(jid)
    return {"ok": True}


@app.post("/api/clips/{cid}/upload")
def upload_clip(cid: int, platforms: list[Platform] | None = None, force: bool = False):
    """Body: optional list of platforms; default = the ones chosen for the clip's job.
    ?force=true: try Instagram even when the clip is longer than IG_MAX_SECONDS (it may fail)."""
    worker.upload_clip(cid, platforms, force)
    return db.get("clips", cid)


@app.post("/api/clips/upload_all")
def upload_all(platforms: list[Platform]):
    """Upload every rendered clip to whichever of `platforms` it isn't on yet (the UI sends Facebook + Instagram
    together), oldest first, one clip at a time in the background."""
    todo = []
    for c in reversed(db.all("clips")):
        on = json.loads(c.get("uploads") or "{}")
        missing = [p for p in platforms if not on.get(p, {}).get("id")
                   and not (p == "instagram" and IG_MAX_SECONDS and c["end"] - c["start"] > IG_MAX_SECONDS)]
        if c["path"] and c["status"] not in ("rendering", "uploading") and missing:
            todo.append((c["id"], missing))

    def run():
        db.syslog(f"bulk upload to {', '.join(platforms)}: {len(todo)} clips")
        for cid, missing in todo:
            worker.upload_clip(cid, missing)
        db.syslog(f"bulk upload to {', '.join(platforms)} finished")
    threading.Thread(target=run, daemon=True).start()
    return {"queued": len(todo)}


@app.delete("/api/clips/{cid}")
def delete_clip(cid: int):
    c = db.get("clips", cid)
    if not c:
        raise HTTPException(404, "clip not found")
    if c["status"] in ("rendering", "uploading"):
        raise HTTPException(409, f"clip is {c['status']}, try again when it finishes")
    if c["path"]:
        mp4 = DATA / c["path"]
        mp4.unlink(missing_ok=True)
        mp4.with_suffix(".ass").unlink(missing_ok=True)
    db.delete("clips", cid)
    return {"ok": True}  # ponytail: local only; deleting the YouTube copy needs the broader youtube scope


@app.get("/api/logs")
def logs(lines: int = 400):
    """Tail of data/logs/clipper.log: every job, topic and system event, with error tracebacks."""
    return {"text": db.tail(min(max(lines, 50), 5000)), "file": str(db.LOG_FILE)}


@app.post("/api/report")
def send_report():
    """Refresh view counts, make a report (saved for the Reports page), send it on WhatsApp if set up."""
    return stats.send_report()


@app.get("/api/reports")
def reports(limit: int = 50):
    return db.all("reports")[:limit]


class AutoSettings(BaseModel):
    """Every field optional: the UI saves single fields live (e.g. the ON/OFF switch) as well as the whole form."""
    enabled: bool | None = None
    niche: str | None = Field(None, max_length=200)  # "" = anything trending, its choice
    posts_per_day: int | None = Field(None, ge=1, le=48)  # clips posted a day (each to every chosen platform)
    post_from: int | None = Field(None, ge=0, le=23)  # posting window, local hours: posts are spread evenly over it
    post_to: int | None = Field(None, ge=1, le=24)
    shorts: int | None = Field(None, ge=0, le=20)  # clips per video
    longs: int | None = Field(None, ge=0, le=10)
    choosy: int | None = Field(None, ge=1, le=10)  # videos researched per video clipped
    hour: int | None = Field(None, ge=0, le=23)  # when the day's first round starts
    keep_sources: bool | None = None
    mode: Literal["hype", "ai+hype", "ai"] | None = None
    aspect: float | None = Field(None, ge=0.56, le=1.78)
    hq: bool | None = None
    face_check: bool | None = None
    subtitles: bool | None = None
    voiceover: bool | None = None
    platforms: list[Platform] | None = None


@app.get("/api/auto")
def auto_status():
    return autopilot.status()


@app.post("/api/auto")
def auto_save(s: AutoSettings):
    new = s.model_dump(exclude_none=True)
    merged = {**autopilot.settings(), **new}
    if merged["shorts"] + merged["longs"] == 0:
        raise HTTPException(400, "ask for at least one clip per video")
    if merged["post_to"] <= merged["post_from"]:
        raise HTTPException(400, "the posting window must end after it starts")
    return autopilot.save(new)


@app.post("/api/auto/stop")
def auto_stop():
    """OFF + stop everything the autopilot started: the running round, its topics and jobs, scheduled posts."""
    return autopilot.stop_all()


@app.get("/api/auto/rounds")
def auto_rounds(limit: int = 20):
    return autopilot.rounds(limit)


@app.post("/api/auto/run")
def auto_run():
    """Run a round now (finds trends + queues topics) in the background; the topic queue then clips and uploads."""
    if autopilot.LOCK.locked():
        raise HTTPException(409, "a round is already running")
    threading.Thread(target=autopilot.run_round, args=("run now",), daemon=True).start()
    return {"ok": True}


@app.post("/api/views/refresh")
def refresh_views():
    """Just update the view counts shown in the UI (no WhatsApp message)."""
    return {"errors": stats.refresh()}


@app.post("/api/youtube/connect")
def youtube_connect():
    try:
        upload.connect()  # blocks until you finish Google sign-in in the browser tab it opens
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
