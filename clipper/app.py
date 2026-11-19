import json
import re
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import autopilot, campaign, channels, db, download, insights, news, pick, render, reset, social, stats, topics, upload, worker
from .config import CROP_ASPECT, DATA, IG_MAX_SECONDS, LIVE_CHUNK_SECONDS, SHORT_HARD_MAX, YT_COOKIES

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
    layout: str = Field("", max_length=200, pattern=r"^(split:[^/\\\\:*?\"<>|]+|cam:(top|bottom):(auto|tl|tr|bl|br|[0-9.]+,[0-9.]+,[0-9.]+,[0-9.]+))?$")  # "split:<file>" = clip on top, that background video below; "cam:top|bottom:<where>" = facecam stack
    hq: bool = False  # download up to 4K instead of 1080p (slower)
    mode: Mode = "hype"
    subtitles: bool = True
    follow: bool = True  # the follow message at the top (with or without a headline)
    notes: str = Field("", max_length=1000)  # your own instructions for the AI (go into its prompts)
    voiceover: bool = False
    style: Literal["clean", "full"] = "full"  # clean = like live clips: original sound + a short title on top, nothing else
    chunk_minutes: int | None = Field(None, ge=1, le=120)  # live streams: length of each recorded piece
    max_chunks: int = Field(0, ge=0, le=500)  # live streams: pieces to record and clip; 0 = until the stream ends
    upload_best: int | None = Field(None, ge=0, le=100)  # upload only the best N shorts (per piece for live / long videos; None = all)
    upload_best_long: int | None = Field(None, ge=0, le=100)  # ... and the best N longs (None = all). Both None = upload every clip


class LiveControls(BaseModel):
    """Change a live job while it records; applies from the next chunk."""
    chunk_minutes: int = Field(ge=1, le=120)
    max_chunks: int = Field(ge=0, le=500)
    upload_best: int | None = Field(None, ge=0, le=100)
    upload_best_long: int | None = Field(None, ge=0, le=100)
    shorts: int = Field(ge=0, le=20)
    longs: int = Field(ge=0, le=10)


class NewTopic(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    videos: int = Field(3, ge=1, le=20)  # how many trending videos to clip
    shorts: int = Field(3, ge=0, le=20)  # per video
    longs: int = Field(1, ge=0, le=10)   # per video
    platforms: list[Platform] = []
    aspect: float = Field(CROP_ASPECT, ge=0.56, le=1.78)
    layout: str = Field("", max_length=200, pattern=r"^(split:[^/\\\\:*?\"<>|]+|cam:(top|bottom):(auto|tl|tr|bl|br|[0-9.]+,[0-9.]+,[0-9.]+,[0-9.]+))?$")
    hq: bool = False
    mode: Literal["hype", "ai+hype", "ai"] = "hype"  # no "manual": nobody is there to type the cuts
    subtitles: bool = True
    follow: bool = True  # the follow message at the top (with or without a headline)
    notes: str = Field("", max_length=1000)  # your own instructions for the AI (go into its prompts)
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


@app.get("/api/frame")
def video_frame(url: str = Query(..., pattern=r"^https?://", max_length=500)):
    """One frame of a video / the live edge of a stream, for drawing where the streamer's facecam is."""
    import hashlib
    d = DATA / "frames"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{hashlib.sha1(f'{url}{int(time.time() // 60)}'.encode()).hexdigest()[:16]}.jpg"  # fresh each minute
    if not out.exists():
        for old in d.glob("*.jpg"):
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink(missing_ok=True)
        try:
            download.frame(url, out)
        except Exception as e:
            raise HTTPException(502, f"Couldn't get a frame: {e}"[:300])
    return FileResponse(out, media_type="image/jpeg")


def _clip_preview(cid, poster=False):
    c = db.get("clips", cid)
    if not c or not c.get("path") or not (DATA / c["path"]).exists():
        raise HTTPException(404, "no video for this clip")
    try:
        return render.preview(DATA / c["path"], DATA / "jobs" / str(c["job_id"]) / "previews", str(cid), poster)
    except Exception as e:
        raise HTTPException(500, f"couldn't make the preview: {e!r}"[:300])


CACHE = {"Cache-Control": "private, max-age=86400"}  # the page asks with ?v=<clip version>, so a new render is fetched


@app.get("/api/clips/{cid}/preview")
def clip_preview(cid: int):
    """Light copy of the clip for the app's players (fast over a tunnel). Download / Open use the full file."""
    return FileResponse(_clip_preview(cid), media_type="video/mp4", headers=CACHE)


@app.get("/api/clips/{cid}/poster")
def clip_poster(cid: int):
    return FileResponse(_clip_preview(cid, poster=True), media_type="image/jpeg", headers=CACHE)


@app.get("/privacy")
def privacy():
    """Privacy policy (Facebook/Instagram app review asks for one; host the same file publicly, see README)."""
    return FileResponse(INDEX.parent / "privacy.html")


@app.post("/api/jobs")
def create_job(j: NewJob):
    if j.shorts + j.longs == 0:
        raise HTTPException(400, "ask for at least one clip")
    jid = db.insert("jobs", url=j.url, shorts=j.shorts, longs=j.longs, auto_upload=int(bool(j.platforms)),
                    platforms=json.dumps(j.platforms), aspect=j.aspect, hq=int(j.hq), mode=j.mode, subtitles=int(j.subtitles), follow=int(j.follow), notes=j.notes.strip(), voiceover=int(j.voiceover), style=j.style,
                    chunk_minutes=j.chunk_minutes, max_chunks=j.max_chunks, upload_best=j.upload_best, upload_best_long=j.upload_best_long, layout=j.layout, created=time.time())
    worker.start(jid)
    return {"id": jid}


class ChannelRun(NewJob):
    """Clip a channel: its best `count` videos by views and upload date. Longer than min_minutes = clipped,
    shorter = posted whole in the clip format. The rest = the same settings as a single video."""
    url: str = ""
    channel: str = Field(min_length=2, max_length=300)
    count: int = Field(5, ge=1, le=30)
    min_minutes: float = Field(10, ge=0, le=600)
    every_min: float = Field(0, ge=0, le=1440)  # upload one clip every N minutes (a queue); 0 = as soon as it's made
    start_at: float | None = None  # epoch seconds: no clip is posted before this (None = right away)


@app.post("/api/channel")
def channel_run(r: ChannelRun):
    job = dict(shorts=r.shorts, longs=r.longs, auto_upload=int(bool(r.platforms)), platforms=json.dumps(r.platforms),
               aspect=r.aspect, hq=int(r.hq), mode=r.mode, subtitles=int(r.subtitles), follow=int(r.follow),
               notes=r.notes.strip(), voiceover=int(r.voiceover), style=r.style, upload_best=r.upload_best,
               upload_best_long=r.upload_best_long, layout=r.layout, chunk_minutes=r.chunk_minutes, max_chunks=r.max_chunks,
               post_every=r.every_min or None, post_from=r.start_at)
    try:
        return {"videos": channels.start(r.channel, r.count, r.min_minutes, job)}
    except Exception as e:
        raise HTTPException(409 if "already" in str(e) else 502, str(e)[:300])


@app.get("/api/channel")
def channel_state():
    return channels.STATE


@app.delete("/api/channel")
def channel_stop():
    return {"was_running": channels.stop()}


def _best_text(s, lo):
    return f"{'all' if s is None else s} short(s) + {'all' if lo is None else lo} long(s)"


@app.post("/api/jobs/{jid}/live")
def live_controls(jid: int, c: LiveControls):
    if not db.get("jobs", jid):
        raise HTTPException(404, "job not found")
    if c.shorts + c.longs == 0:
        raise HTTPException(400, "ask for at least one clip per chunk")
    db.update("jobs", jid, **c.model_dump())
    db.log(jid, f"live settings changed: {c.chunk_minutes} min chunks, "
                f"{f'{c.max_chunks} chunks' if c.max_chunks else 'until the stream ends'}, "
                f"{c.shorts} shorts + {c.longs} long per chunk, "
                f"{'upload every clip' if c.upload_best is None and c.upload_best_long is None else f'after each piece upload its best {_best_text(c.upload_best, c.upload_best_long)}'} "
                f"(clip / upload counts from the piece being recorded, "
                f"length from the next one)")
    return db.get("jobs", jid)


@app.post("/api/jobs/file")
async def create_file_job(request: Request, filename: str, shorts: int = 3, longs: int = 1,
                          platforms: str = "youtube", mode: Mode = "hype", aspect: float = CROP_ASPECT,
                          subtitles: bool = True, voiceover: bool = False, layout: str = "", follow: bool = True,
                          notes: str = "", style: Literal["clean", "full"] = "full",
                          upload_best: int | None = Query(None, ge=0, le=100), upload_best_long: int | None = Query(None, ge=0, le=100)):
    """Raw file bytes in the body (no multipart, so no python-multipart dependency)."""
    plats = [p for p in platforms.split(",") if p in social.PLATFORMS]  # comma-separated in the query string
    ext = Path(filename).suffix.lower()
    if ext not in (".mp4", ".mkv", ".mov", ".webm", ".ts", ".avi"):
        raise HTTPException(400, f"unsupported file type {ext or '(none)'}")
    jid = db.insert("jobs", url=f"file:{Path(filename).name}", title=Path(filename).stem, shorts=shorts,
                    longs=longs, auto_upload=int(bool(plats)), platforms=json.dumps(plats), aspect=min(max(aspect, 0.5625), 16 / 9), layout=layout if layout.startswith("split:") or render.cam_layout(layout) else "", mode=mode, subtitles=int(subtitles), follow=int(follow), notes=notes.strip()[:1000], voiceover=int(voiceover), style=style, upload_best=upload_best, upload_best_long=upload_best_long,
                    status="uploading", created=time.time())
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
                         subtitles=int(t.subtitles), follow=int(t.follow), notes=t.notes.strip(), voiceover=int(t.voiceover), platforms=json.dumps(t.platforms),
                         face_check=int(t.face_check), aspect=t.aspect, hq=int(t.hq), layout=t.layout)
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
        raise HTTPException(409, "this job's source video is gone: it's deleted once clipping finishes "
                                 "(KEEP_SOURCES=1 in .env keeps it); live jobs can't be cut manually")
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
    return {"jobs": db.all("jobs"), "clips": db.all("clips"), "topics": db.all("topics"), "campaigns": db.all("campaigns"),
            "platforms": social.connected(),
            "deepseek": pick.balance(), "whatsapp": stats.whatsapp_ready(), "report_minutes": stats.REPORT_MINUTES,
            "view_errors": stats.LAST["errors"], "crop_aspect": CROP_ASPECT,
            "uploads": social.upload_status(), "limit_timers": social.limit_timers(), "ig_max_seconds": IG_MAX_SECONDS, "live_chunk_minutes": LIVE_CHUNK_SECONDS / 60,
            "youtube_cookies": download.cookies_state()}


def _cookie_name(name):
    """A safe file name for an added cookies.txt set (from the uploaded file's name, e.g. "cookies laptop.txt")."""
    stem = re.sub(r"[^\w .()-]", "", Path(name or "").stem).strip(" .")[:40] or "cookies"
    return stem + ".txt"


@app.post("/api/cookies")
async def save_cookies(request: Request, name: str = ""):
    """Body: a cookies.txt (Netscape format) exported from a logged-in youtube.com tab (this computer or another one).
    Fixes YouTube's bot check. Each file is ADDED as its own set (same name = replaced) and used from now on; when
    YouTube's bot check hits one set, downloads switch to the next."""
    text = (await request.body()).decode("utf-8", "replace")
    if ".youtube.com" not in text or chr(9) not in text:
        raise HTTPException(400, "that doesn't look like a cookies.txt with youtube.com cookies (Netscape format)")
    fname = _cookie_name(name)
    path = YT_COOKIES if fname == YT_COOKIES.name else download.COOKIE_DIR / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.startswith("# Netscape") else "# Netscape HTTP Cookie File" + chr(10) + text,
                    encoding="utf-8")
    download.use_cookies(path.name)
    db.syslog(f"YouTube cookies saved as {path.name} ({text.count('.youtube.com')} youtube.com cookies), in use now; "
              f"{len(download.cookie_sets())} set(s) in all")
    return download.cookies_state()


@app.post("/api/cookies/use")
def use_cookies(name: str):
    if name not in download.cookie_sets():
        raise HTTPException(404, "no cookies set with that name")
    download.use_cookies(name)
    db.syslog(f"YouTube cookies: using {name}")
    return download.cookies_state()


@app.delete("/api/cookies")
def delete_cookies(name: str = ""):
    sets = download.cookie_sets()
    for n in ([name] if name else list(sets)):  # no name: remove every set
        if n in sets:
            sets[n].unlink(missing_ok=True)
    db.syslog(f"YouTube cookies removed: {name or 'all'}")
    return download.cookies_state()


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: int):
    return {"ok": worker.stop(jid)}


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: int):
    """Run a failed / interrupted / stopped job again as a new job with the same settings."""
    try:
        return {"id": worker.retry(jid)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/jobs/retry-failed")
def retry_failed():
    """Retry every failed or interrupted job (not stopped ones: you stopped those) that wasn't retried already."""
    done, skipped = [], []
    for j in db.all("jobs", "status IN ('failed', 'interrupted')"):
        if "retried as job #" in (j.get("log") or ""):
            continue
        try:
            done.append(worker.retry(j["id"]))
        except ValueError as e:
            skipped.append({"id": j["id"], "why": str(e)})
    return {"started": done, "skipped": skipped}


@app.post("/api/jobs/stop-all")
def stop_all_jobs():
    """Stop every running job (live recordings included). Clips already made stay."""
    return {"stopped": [j["id"] for j in db.all("jobs") if j["status"] not in worker.FINISHED and worker.stop(j["id"])]}


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


class ClipEdit(BaseModel):
    title: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=5000)
    tags: list[str] | None = Field(None, max_length=30)
    headline: str | None = Field(None, max_length=60)  # the text on the video: changing it re-renders the clip
    caption: str | None = Field(None, max_length=2200)  # campaign clips: the exact caption posted


@app.patch("/api/clips/{cid}")
def edit_clip(cid: int, e: ClipEdit):
    try:
        return worker.edit_clip(cid, e.model_dump())
    except ValueError as ex:
        raise HTTPException(409, str(ex))


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
        mp4.with_name(f"{mp4.stem}_master.mp4").unlink(missing_ok=True)
        mp4.with_name(f"{mp4.stem}_words.json").unlink(missing_ok=True)
    db.delete("clips", cid)
    return {"ok": True}  # ponytail: local only; deleting the YouTube copy needs the broader youtube scope


# ---- Campaigns (paid clipping campaigns, e.g. Whop): campaign.py ----
CAMP_LAYOUT = r"^(cam:(top|bottom):(auto|tl|tr|bl|br|[0-9.]+,[0-9.]+,[0-9.]+,[0-9.]+))?$"  # no split: outside footage is rejected


class CampaignIn(BaseModel):
    brief: str = Field(min_length=20, max_length=20000)
    name: str = Field("", max_length=120)
    platforms: list[Platform] = []  # [] = the ones the brief names
    shorts: int = Field(3, ge=1, le=20)  # clips per official video
    max_seconds: int | None = Field(None, ge=10, le=SHORT_HARD_MAX)
    aspect: float = Field(CROP_ASPECT, ge=0.56, le=1.78)
    layout: str = Field("", max_length=200, pattern=CAMP_LAYOUT)
    subtitles: bool = True
    onscreen: bool = True  # the AI's on-screen text at the top
    trial_on_screen: bool = False  # the required line (e.g. the free trial) on screen too
    notes: str = Field("", max_length=1000)
    rules: dict | None = None  # from /api/campaigns/analyze (then the brief isn't read again)


class BriefIn(BaseModel):
    brief: str = Field(min_length=20, max_length=20000)


@app.post("/api/campaigns/analyze")
def analyze_brief(b: BriefIn):
    """The New campaign form: rules + every setting filled from the brief, or a question where the brief is silent."""
    return campaign.suggest(b.brief.strip())


class CampaignEdit(BaseModel):
    name: str | None = Field(None, max_length=120)
    rules: dict | None = None
    platforms: list[Platform] | None = None
    shorts: int | None = Field(None, ge=1, le=20)
    max_seconds: int | None = Field(None, ge=0, le=SHORT_HARD_MAX)
    aspect: float | None = Field(None, ge=0.56, le=1.78)
    layout: str | None = Field(None, max_length=200, pattern=CAMP_LAYOUT)
    subtitles: bool | None = None
    onscreen: bool | None = None
    trial_on_screen: bool | None = None
    notes: str | None = Field(None, max_length=1000)


def _camp(cid):
    c = db.get("campaigns", cid)
    if not c:
        raise HTTPException(404, "campaign not found")
    return c


@app.post("/api/campaigns")
def create_campaign(c: CampaignIn):
    d = c.model_dump()
    brief, name, rules = d.pop("brief").strip(), d.pop("name").strip(), d.pop("rules")
    d.update(platforms=json.dumps(d["platforms"]), subtitles=int(d["subtitles"]), onscreen=int(d["onscreen"]),
             trial_on_screen=int(d["trial_on_screen"]), notes=d["notes"].strip())
    return {"id": campaign.create(brief, name, rules, **d)}


@app.patch("/api/campaigns/{cid}")
def edit_campaign(cid: int, e: CampaignEdit):
    """Your edits to the settings or the rules (the rules are saved as you typed them, cleaned up)."""
    _camp(cid)
    d = {k: v for k, v in e.model_dump().items() if v is not None}
    if "rules" in d:
        d["rules"] = json.dumps(campaign.edited_rules(d["rules"]), ensure_ascii=False)
    if "platforms" in d:
        d["platforms"] = json.dumps(d["platforms"])
    for k in ("subtitles", "onscreen", "trial_on_screen"):
        if k in d:
            d[k] = int(d[k])
    if d.get("max_seconds") == 0:
        d["max_seconds"] = None
    if d:
        db.update("campaigns", cid, **d)
    return db.get("campaigns", cid)


@app.post("/api/campaigns/{cid}/reread")
def reread_campaign(cid: int):
    c = _camp(cid)
    db.update("campaigns", cid, status="reading the brief")

    def run():
        db.log_to(lambda m: db.log(cid, m, table="campaigns"))
        try:
            r, by_ai = campaign.read_brief(c["brief"])
            db.update("campaigns", cid, rules=json.dumps(r, ensure_ascii=False), status="ready")
            db.log(cid, f"brief read again {'by the AI' if by_ai else 'without the AI'}", table="campaigns")
        finally:
            db.log_to(None)
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@app.delete("/api/campaigns/{cid}")
def delete_campaign(cid: int):
    _camp(cid)
    if any(j["status"] not in worker.FINISHED for j in db.all("jobs", "campaign_id=?", (cid,))):
        raise HTTPException(409, "its videos are still being clipped: stop them first")
    db.delete("campaigns", cid)  # its jobs and clips stay (Clips page)
    return {"ok": True}


class CampaignVideos(BaseModel):
    urls: list[str] = Field([], max_length=50)
    folder: str = Field("", max_length=500)


@app.post("/api/campaigns/{cid}/videos")
def campaign_videos(cid: int, v: CampaignVideos):
    c = _camp(cid)
    urls = [u.strip() for u in v.urls if u.strip()]
    if any(not re.match(r"^https?://", u) for u in urls):
        raise HTTPException(400, "each link must start with http:// or https://")
    ids = []
    if v.folder.strip():
        try:
            ids += campaign.add_folder(c, v.folder)
        except ValueError as e:
            raise HTTPException(400, str(e))
    ids += campaign.add_urls(c, urls) if urls else []
    if not ids:
        raise HTTPException(400, "add at least one link or a folder")
    return {"jobs": ids}


@app.post("/api/campaigns/{cid}/file")
async def campaign_file(cid: int, request: Request, filename: str):
    """One official video file (raw bytes in the body, like /api/jobs/file)."""
    c = _camp(cid)
    ext = Path(filename).suffix.lower()
    if ext not in campaign.VIDEO_EXT:
        raise HTTPException(400, f"unsupported file type {ext or '(none)'}")
    jid = campaign.add_file_job(c, filename)
    d = DATA / "jobs" / str(jid)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"source{ext}", "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    db.update("jobs", jid, status="queued")
    campaign.enqueue(jid)
    db.log(cid, f"added the file {Path(filename).name}: job #{jid}", table="campaigns")
    return {"id": jid}


class CaptionIn(BaseModel):
    caption: str = Field(min_length=1, max_length=2200)


@app.put("/api/clips/{cid}/caption")
def set_caption(cid: int, b: CaptionIn):
    if not db.get("clips", cid):
        raise HTTPException(404, "clip not found")
    db.update("clips", cid, caption=b.caption.strip(), description=b.caption.strip())
    return {"checks": campaign.recheck(cid)}


@app.post("/api/clips/{cid}/caption/rewrite")
def rewrite_caption(cid: int):
    """A new AI caption for a campaign clip (built to the rules, not a repeat of the campaign's other captions)."""
    c = db.get("clips", cid)
    job = db.get("jobs", c["job_id"]) if c else None
    camp = db.get("campaigns", job["campaign_id"]) if job and job.get("campaign_id") else None
    if not camp:
        raise HTTPException(404, "not a campaign clip")
    why = json.loads(c.get("why") or "{}")
    used = [x["caption"].splitlines()[0] for x in db.all(
        "clips", "caption != '' AND id != ? AND job_id IN (SELECT id FROM jobs WHERE campaign_id=?)", (cid, camp["id"]))]
    talk = " ".join(x for x in (why.get("opening"), why.get("closing")) if x)  # the source video is gone by now
    cap = campaign.caption_for(camp, c, talk, used + [str(c.get("caption") or "").split("\n")[0]])[0]
    db.update("clips", cid, caption=cap, description=cap)
    return {"caption": cap, "checks": campaign.recheck(cid)}


class PostedIn(BaseModel):
    platform: Platform
    url: str = Field(pattern=r"^https?://", max_length=500)


@app.post("/api/clips/{cid}/posted")
def mark_posted(cid: int, b: PostedIn):
    """You posted it by hand: keep the link (shown on the clip, with the date it must stay up until)."""
    c = db.get("clips", cid)
    if not c:
        raise HTTPException(404, "clip not found")
    ups = json.loads(c.get("uploads") or "{}")
    ups[b.platform] = {"id": "manual", "url": b.url, "at": time.time(), "manual": True}
    db.update("clips", cid, uploads=json.dumps(ups), status="uploaded")
    return db.get("clips", cid)


@app.get("/api/logs")
def logs(lines: int = 400):
    """Tail of data/logs/clipper.log: every job, topic and system event, with error tracebacks."""
    return {"text": db.tail(min(max(lines, 50), 5000)), "file": str(db.LOG_FILE)}


@app.post("/api/report")
def send_report():
    """Refresh view counts, make a report (saved for the Reports page), send it on WhatsApp if set up."""
    return stats.send_report()


@app.get("/api/insights")
def get_insights():
    """What works: views per topic / platform / clip length, best and weakest clips, and the niche the autopilot
    learned from them (used when no niche is set)."""
    return {**insights.performance(), "learned": insights.learned(), "views_at": float(db.get_setting("views_at") or 0)}


@app.post("/api/insights/learn")
def learn_now():
    """Refresh views on every platform, then ask DeepSeek what the channel should post (no round is started)."""
    stats.refresh()
    out = insights.learn_niche()
    if not out:
        raise HTTPException(409, f"not enough data yet: needs {insights.MIN_CLIPS} posted clips with view counts")
    return out


@app.get("/api/ai/models")
def ai_models():
    """Settings → AI models: the order (main + fallbacks, each with its model and what's missing) and the local models."""
    from .config import LLM_BASE_URL, PC_LLM_BASE_URL
    pc = pick.local_models(PC_LLM_BASE_URL)
    return {**pick.ai_choice(), "providers": pick.PROVIDERS, "local_url": LLM_BASE_URL, **pick.local_models(),
            "pc_url": PC_LLM_BASE_URL, "pc_models": pc.get("models", []), "pc_error": pc.get("error", "")}


class AiUse(BaseModel):
    order: list[Literal["deepseek", "local", "pc"]] = Field(..., min_length=1, max_length=3)  # main first
    local_model: str = Field("", max_length=200)
    pc_model: str = Field("", max_length=200)


@app.post("/api/ai/use")
def ai_use(u: AiUse):
    """Set which AI every call uses (clips, titles, autopilot, news) and the fallbacks when it fails."""
    try:
        pick.set_ai(u.order, u.local_model.strip(), u.pc_model.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    pick._balance["at"] = 0  # header refreshes right away
    return pick.ai_choice()


class AiTest(BaseModel):
    provider: Literal["deepseek", "local", "pc"]


@app.post("/api/ai/test")
def ai_test(t: AiTest):
    """Settings → AI models → Test: ask that one AI "What model are you?" -> {reply, model, seconds}."""
    try:
        return pick.test(t.provider)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"{pick.PROVIDERS[t.provider]} failed: {e}"[:400])


class NewsFind(BaseModel):
    topic: str = Field("", max_length=200)  # "" = the autopilot's niche, else the niche learned from views, else trending
    kind: Literal["news", "facts"] = "news"
    n: int = Field(3, ge=1, le=10)  # how many posts DeepSeek should pick


@app.post("/api/news/find")
def news_find(f: NewsFind):
    """Start a search run in the background -> {run_id}. The News page follows its log and shows the stories when
    it's done (status 'found'): Google News + Bing News (or facts), then DeepSeek's picks."""
    topic, how = f.topic.strip(), "your topic"
    if not topic:
        learned = insights.learned()
        topic = autopilot.settings()["niche"].strip() or (learned or {}).get("niche", "")
        how = ("the autopilot's niche" if autopilot.settings()["niche"].strip() else
               "learned from your views" if topic else "whatever is trending")
    rid = news.new_run(f.kind, topic or "trending")
    say = news.run_logger(rid)

    def run():
        with db.log_to(say):  # the AI lines ("🤖 AI: gpt-oss:20b answered…") land in this run's log
            _run()

    def _run():
        try:
            say(f"▶ find {f.n} {f.kind} post(s) about '{topic or 'anything trending'}' (topic: {how})")
            if f.kind == "facts":
                stories = news.facts(topic or "video games", f.n, say)
                data = {"topic": topic, "how": how, "stories": [{**x, "pick": True} for x in stories]}
            else:
                say(f"🧠 {pick.ai_name()} writing search queries…")
                queries = list(dict.fromkeys(([topic] + news.queries_for(topic)) if topic else news.queries_for(autopilot.ANY_NICHE)))
                say("   queries: " + " | ".join(queries))
                db.update("news_runs", rid, status="searching")
                stories = news.find_news(queries, say)
                picked = []
                if stories:
                    db.update("news_runs", rid, status="picking")
                    say(f"🧠 {pick.ai_name()} picking the best {f.n} of {len(stories)}…")
                    picked = news.pick_stories(topic or "trending", stories, f.n, [r["headline"] for r in db.all("news")])
                    for x in picked:
                        say(f"   ★ {x['title'][:100]} ({x['source']}): {x.get('why', '')}")
                why = {x["link"]: x.get("why", "") for x in picked}
                data = {"topic": topic, "how": how, "queries": queries,
                        "stories": [{**x, "pick": x["link"] in why, "why": why.get(x["link"], "")} for x in stories[:40]]}
            db.update("news_runs", rid, status="found", data=json.dumps(data))
            say(f"✅ {len(data['stories'])} stor{'y' if len(data['stories']) == 1 else 'ies'} ready in '1 · Find' above: tick the ones to post, then Make posts")
        except Exception as e:
            say(f"❌ failed: {e!r}"[:300])
            db.update("news_runs", rid, status="failed")
    threading.Thread(target=run, daemon=True).start()
    return {"run_id": rid}


@app.get("/api/news/runs")
def news_runs(limit: int = 15):
    return db.all("news_runs")[:limit]


class NewsMake(BaseModel):
    stories: list[dict] = Field(min_length=1, max_length=10)
    kind: Literal["news", "fact"] = "news"
    platforms: list[Platform] = []
    voiceover: bool = False
    picture: bool = True
    follow: bool = True  # the follow message at the top of the post
    when: Literal["now", "hold", "at", "autopilot"] = "hold"  # post right away / keep / at a time / autopilot slots
    at: float | None = None  # "at": first post time (seconds since 1970); the rest every `every_min` after it
    every_min: int = Field(60, ge=0, le=1440)
    run_id: int | None = None  # the Find run these stories came from: making + posting go into its log
    format: Literal["video", "photo"] = "video"  # photo: 1080x1350 image, Facebook + Instagram only


@app.post("/api/news/make")
def news_make(m: NewsMake):
    """Write + render one post per story in the background; then post, keep, time or hand to the autopilot."""
    if m.when == "at" and not m.at:
        raise HTTPException(400, "pick a date and time")
    if m.when in ("now", "at") and not m.platforms:
        raise HTTPException(400, "pick where to post")

    rid = m.run_id or news.new_run(m.kind, "manual")
    say = news.run_logger(rid)
    when = {"now": "post each right away", "hold": "keep them for you to post", "autopilot": "on the autopilot's slots",
            "at": f"first at {time.strftime('%d %b %H:%M', time.localtime(m.at or 0))}, then every {m.every_min} min"}[m.when]
    say(f"▶ make {len(m.stories)} {m.format} post(s) for {', '.join(m.platforms) or 'no platform'}: {when}"
        + (" (photos only go to Facebook + Instagram)" if m.format == "photo" and set(m.platforms) - {"facebook", "instagram"} else ""))
    db.update("news_runs", rid, status="making")

    def run():
        with news.LOCK:
            for i, st in enumerate(m.stories):
                status = {"now": "rendered", "hold": "rendered", "at": "timed", "autopilot": "scheduled"}[m.when]
                post_at = m.at + i * m.every_min * 60 if m.when == "at" else None
                cid = news.make_post({"age_h": None, "snippet": "", **st}, m.kind, m.platforms, m.voiceover, m.picture,
                                     say, status=status, post_at=post_at, run_id=rid, fmt=m.format, follow=m.follow)
                if cid and m.when == "now":
                    say(f"   ⬆ posting #{cid} now…")
                    worker.upload_clip(cid, m.platforms, force=True)
        db.update("news_runs", rid, status="made")
        say("✅ done")
    threading.Thread(target=run, daemon=True).start()
    return {"queued": len(m.stories), "run_id": rid}


class ScheduleIn(BaseModel):
    at: float | None = None  # None = cancel the time (the post stays, unposted)
    platforms: list[Platform] | None = None


@app.post("/api/clips/{cid}/schedule")
def schedule_clip(cid: int, b: ScheduleIn):
    """Post a clip/post at a set time (news.timed_tick posts it), or cancel that."""
    c = db.get("clips", cid)
    if not c:
        raise HTTPException(404, "clip not found")
    if b.at:
        if c["status"] in ("rendering", "uploading") or not c.get("path"):
            raise HTTPException(400, "this clip isn't ready to schedule")
        if b.at < time.time() - 60:
            raise HTTPException(400, "that time has already passed")
        plats = b.platforms if b.platforms is not None else json.loads((db.get("jobs", c["job_id"]) or {}).get("platforms") or "[]")
        if not plats:
            raise HTTPException(400, "pick where to post")
        # the platforms go on the clip itself, so the job's other clips keep theirs
        db.update("clips", cid, status="timed", post_at=b.at, post_to=json.dumps(plats))
        db.log(c["job_id"], f"#{cid} scheduled for {time.strftime('%d %b %H:%M', time.localtime(b.at))} to {', '.join(plats)}")
    elif c["status"] in ("timed", "scheduled", "paused (streamer live)"):  # "Don't post": off the schedule, kept here
        db.update("clips", cid, status="rendered", post_at=None, post_to=None)
        db.log(c["job_id"], f"#{cid} taken off the schedule (won't be posted)")
    return db.get("clips", cid)


FILLERS = DATA / "fillers"  # background videos for the split layout (kept by "Reset everything")


def _filler_name(name):
    n = re.sub(r"[^\w .()-]", "_", Path(name).name).strip() or "video.mp4"
    return n if n.lower().endswith((".mp4", ".mov", ".mkv", ".webm")) else n + ".mp4"


FILLER_DL = {}  # url -> {"title", "status", "error"} for YouTube downloads in progress / failed (shown in the list)


@app.get("/api/fillers")
def list_fillers():
    """Background videos you can pick, then any YouTube downloads still running or failed ("downloading": status)."""
    FILLERS.mkdir(parents=True, exist_ok=True)
    from . import render
    done = [{"name": f.name, "mb": round(f.stat().st_size / 1e6, 1), "seconds": round(render._duration(f))}
            for f in sorted(FILLERS.iterdir()) if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]
    return done + [{"name": d["title"], "url": u, "downloading": d["status"], "error": d.get("error")}
                   for u, d in FILLER_DL.items()]


class FillerLink(BaseModel):
    url: str = Field(pattern=r"^https?://")
    name: str = Field("", max_length=120)  # optional; default = the video's title


@app.post("/api/fillers/youtube")
def filler_from_youtube(f: FillerLink):
    """Download a YouTube video as a background video (video only, up to 1080p; it plays muted) in the background."""
    if f.url in FILLER_DL and FILLER_DL[f.url]["status"] != "failed":
        raise HTTPException(409, "already downloading that one")
    FILLER_DL[f.url] = {"title": f.name or f.url, "status": "getting info"}

    def run():
        import shutil
        import tempfile
        from .config import FFMPEG
        d = FILLER_DL[f.url]
        try:
            i = download.info(f.url)
            if i.get("is_live"):
                raise RuntimeError("that's a live stream; use a normal video")
            name = _filler_name((f.name or i.get("title") or "background")[:100])
            d.update(title=name, status=f"downloading ({round((i.get('duration') or 0) / 60)} min video)")
            db.syslog(f"background video: downloading '{i.get('title')}' from {f.url}")
            with tempfile.TemporaryDirectory() as tmp:
                with download.ydl({"quiet": True, "noprogress": True, "outtmpl": f"{tmp}/v.%(ext)s",
                                   "format": "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b[height<=1080]/b",
                                   "concurrent_fragment_downloads": download.FRAGMENTS, "ffmpeg_location": FFMPEG}) as y:
                    y.download([f.url])
                got = next(p for p in Path(tmp).iterdir() if p.is_file() and not p.name.endswith(".part"))
                out = FILLERS / (Path(name).stem + got.suffix)
                shutil.move(str(got), out)
            db.syslog(f"background video added from YouTube: {out.name} ({out.stat().st_size / 1e6:.0f} MB)")
            FILLER_DL.pop(f.url, None)
        except Exception as e:
            download.bot_checked(e)
            d.update(status="failed", error=db.scrub(e)[:200])
            db.syslog(f"background video from {f.url} failed: {e!r}")
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@app.delete("/api/fillers/youtube")
def forget_failed_filler(url: str):
    FILLER_DL.pop(url, None)
    return list_fillers()


@app.post("/api/fillers")
async def upload_filler(request: Request, name: str):
    """Raw video bytes in the body (like /api/jobs/file)."""
    FILLERS.mkdir(parents=True, exist_ok=True)
    out = FILLERS / _filler_name(name)
    with open(out, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    db.syslog(f"background video added: {out.name} ({out.stat().st_size / 1e6:.0f} MB)")
    return {"name": out.name}


@app.delete("/api/fillers/{name}")
def delete_filler(name: str):
    (FILLERS / _filler_name(name)).unlink(missing_ok=True)
    return list_fillers()


@app.get("/api/storage")
def get_storage():
    return reset.storage()


class ResetIn(BaseModel):
    confirm: str  # must be "RESET"
    keep_settings: bool = True  # keep the autopilot settings (it's switched OFF either way)
    keep_logins: bool = True  # keep the YouTube/TikTok logins and YouTube cookies


@app.post("/api/reset")
def reset_everything(r: ResetIn):
    """Stop everything and delete every video, clip, post, topic, round, report, view count and log."""
    if r.confirm != "RESET":
        raise HTTPException(400, 'type RESET to confirm')
    return reset.reset_all(r.keep_settings, r.keep_logins)


@app.get("/api/reports")
def reports(limit: int = 50):
    return db.all("reports")[:limit]


class Framing(BaseModel):
    aspect: float = Field(CROP_ASPECT, ge=0.56, le=1.78)
    layout: str = Field("", max_length=200, pattern=r"^(split:[^/\\:*?\"<>|]+|cam:(top|bottom):(auto|tl|tr|bl|br|[0-9.]+,[0-9.]+,[0-9.]+,[0-9.]+))?$")


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
    mode: Literal["hype", "ai+hype", "ai"] | None = None
    aspect: float | None = Field(None, ge=0.56, le=1.78)
    layout: str | None = Field(None, max_length=200)
    hq: bool | None = None
    face_check: bool | None = None
    subtitles: bool | None = None
    follow: bool | None = None
    notes: str | None = Field(None, max_length=1000)  # your instructions for the AI
    voiceover: bool | None = None
    platforms: list[Platform] | None = None
    news_per_day: int | None = Field(None, ge=0, le=24)  # news/fact posts a day, on top of the clips
    news_kind: Literal["news", "facts", "both"] | None = None
    news_picture: bool | None = None
    news_format: Literal["video", "photo", "both"] | None = None
    mix: list[Framing] | None = Field(None, max_length=8)  # framings to rotate, one per topic ([] = just `aspect`/`layout`)
    streamers: list[str] | None = Field(None, max_length=30)  # YouTube channels to watch for live streams
    watch: bool | None = None
    live_layout: str | None = Field(None, max_length=200, pattern=r"^(cam:(top|bottom):(auto|tl|tr|bl|br))?$")
    live_chunk: int | None = Field(None, ge=2, le=60)  # minutes per recorded piece of a watched live stream
    live_shorts: int | None = Field(None, ge=1, le=10)  # clips per piece


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
