import json
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, pick, social, stats, topics, upload, worker
from .config import CROP_ASPECT, DATA, SHORT_HARD_MAX

app = FastAPI(title="Clipper")
db.init()
db.syslog(f"server started, data in {DATA}")
topics.start_loop()  # picks up topics still queued from before a restart
stats.start_loop()  # hourly WhatsApp report, if WHATSAPP_PHONE/WHATSAPP_APIKEY are set
social.backfill_times()  # give old uploads a time so the 24h upload counter includes them
worker.start_retry_loop()  # every 10 min: upload clips that waited for a platform's daily limit
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
            "limits": social.limits_status()}


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: int):
    worker.stop(jid)
    return {"ok": True}


@app.post("/api/clips/{cid}/upload")
def upload_clip(cid: int, platforms: list[Platform] | None = None):
    """Body: optional list of platforms; default = the ones chosen for the clip's job."""
    worker.upload_clip(cid, platforms)
    return db.get("clips", cid)


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
    """Refresh view counts now and send the WhatsApp report. Returns the message text."""
    try:
        return {"text": stats.send_report()}
    except Exception as e:
        raise HTTPException(400, str(e))


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
