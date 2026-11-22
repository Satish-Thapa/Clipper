"""The web app: the API the page (static/index.html) talks to. Every route validates what it gets and answers with a
plain message (HTTP 400 / 404 / 409) the page shows as it is."""
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

from . import ai, autopilot, db, download, insights, news, quota, render, settings, social, stats, upload, worker
from .config import DATA, YT_COOKIES

app = FastAPI(title="Clipper")
db.init()
db.syslog(f"server started, data in {DATA}")


@app.on_event("startup")
def _start():
    """Background loops start with the server only (importing this module, e.g. in a test, starts nothing)."""
    autopilot.start_loop()  # posts every slot on time + the daily report / Auto research
    social.backfill_times()  # give old uploads a time so the daily quota counts them
    social.backfill_limit_hits()


app.mount("/media", StaticFiles(directory=DATA), name="media")
INDEX = Path(__file__).parent / "static" / "index.html"
Platform = Literal["youtube", "facebook", "instagram", "tiktok"]
YOUTUBE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com/(watch\?.*v=|live/|shorts/)|youtu\.be/)[\w-]{6,}", re.I)
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".ts", ".avi", ".m4v")


def _bad(code, msg):
    raise HTTPException(code, msg)


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/privacy")
def privacy():
    """Privacy policy (Facebook/Instagram app review asks for one; host the same file publicly, see README)."""
    return FileResponse(INDEX.parent / "privacy.html")


# ---------- jobs: Normal (YouTube link or file) and Live ----------

class Look(BaseModel):
    """How the clips look (every field defaults to Settings)."""
    layout: Literal["full", "cam", "split"] | None = None
    cam_corner: Literal["auto", "tl", "tr", "bl", "br"] | None = None
    filler: str | None = Field(None, max_length=200)
    subtitles: bool | None = None
    header: bool | None = None


def _look(j: Look):
    """-> (layout string for render, header, subtitles) with the Settings defaults filled in, validated."""
    s = settings.get()
    lay, corner, filler = j.layout or s.layout, j.cam_corner or s.cam_corner, (j.filler if j.filler is not None else s.filler)
    if lay == "split":
        if not filler:
            _bad(400, "the split layout needs a background video: add one in Settings → Background videos")
        if not (FILLERS / _filler_name(filler)).is_file():
            _bad(400, f"the background video {filler} isn't there any more: pick another one")
    layout = {"full": "", "cam": f"cam:top:{corner}", "split": f"split:{_filler_name(filler) if filler else ''}"}[lay]
    pick = lambda v, d: int(d if v is None else v)
    return layout, pick(j.header, s.header), pick(j.subtitles, s.subtitles)


class NewJob(Look):
    url: str = Field(max_length=500)
    mode: Literal["normal", "live"] = "normal"
    shorts: int = Field(3, ge=0, le=20)  # clips to make (live: per piece)
    longs: int = Field(0, ge=0, le=10)
    notes: str = Field("", max_length=1000)  # Tell the AI (this video)
    fill: Literal[0, 1, 7, 30] = 0  # put the best clips in free slots: 0 off, 1 today, 7 this week, 30 this month
    chunk_minutes: int | None = Field(None, ge=1, le=120)  # live: minutes per piece (default: Settings)
    max_chunks: int = Field(0, ge=0, le=500)  # live: pieces to clip, 0 = until the stream ends


def _check_counts(shorts, longs):
    if shorts + longs == 0:
        _bad(400, "ask for at least one short or long clip")


@app.post("/api/jobs")
def create_job(j: NewJob):
    url = j.url.strip()
    if not YOUTUBE.match(url):
        _bad(400, "only YouTube links work here (youtube.com/watch?v=..., youtu.be/..., youtube.com/live/...); "
                  "for a video on your computer use Upload a file")
    _check_counts(j.shorts, j.longs)
    busy = [x for x in db.all("jobs", "url=?", (url,)) if x["status"] not in worker.FINISHED]
    if busy:
        _bad(409, f"this video is already being clipped (job #{busy[0]['id']})")
    layout, header, subtitles = _look(j)
    jid = db.insert("jobs", url=url, mode=j.mode, shorts=j.shorts, longs=j.longs, layout=layout, header=header,
                    subtitles=subtitles, notes=j.notes.strip(), fill=int(j.fill), auto_upload=0,
                    chunk_minutes=j.chunk_minutes if j.mode == "live" else None,
                    max_chunks=j.max_chunks if j.mode == "live" else 0, created=time.time())
    worker.start(jid)
    return {"id": jid}


@app.post("/api/jobs/file")
async def create_file_job(request: Request, filename: str = Query(max_length=300), shorts: int = Query(3, ge=0, le=20),
                          longs: int = Query(0, ge=0, le=10), layout: Literal["full", "cam", "split"] | None = None,
                          cam_corner: Literal["auto", "tl", "tr", "bl", "br"] | None = None, filler: str | None = None,
                          subtitles: bool | None = None, header: bool | None = None, notes: str = Query("", max_length=1000),
                          fill: int = Query(0, ge=0, le=30)):
    """Normal mode, a video file from your computer: raw bytes in the body (no multipart needed)."""
    ext = Path(filename).suffix.lower()
    if ext not in VIDEO_EXT:
        _bad(400, f"unsupported file type {ext or '(none)'}: use {', '.join(VIDEO_EXT)}")
    _check_counts(shorts, longs)
    if fill not in (0, 1, 7, 30):
        _bad(400, "fill is 0 (off), 1 (today), 7 (this week) or 30 (this month)")
    lay, head, subs = _look(Look(layout=layout, cam_corner=cam_corner, filler=filler, subtitles=subtitles, header=header))
    jid = db.insert("jobs", url=f"file:{Path(filename).name}", title=Path(filename).stem, mode="normal", shorts=shorts,
                    longs=longs, layout=lay, header=head, subtitles=subs, notes=notes.strip(), fill=int(fill),
                    auto_upload=0, status="uploading", created=time.time())
    d = DATA / "jobs" / str(jid)
    d.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with open(d / f"source{ext}", "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                size += len(chunk)
    except Exception as e:
        db.update("jobs", jid, status="failed")
        db.log(jid, f"the upload broke off: {e!r}")
        _bad(400, "the upload broke off: try again")
    if size < 10_000:
        db.update("jobs", jid, status="failed")
        db.log(jid, f"the file is only {size} bytes: not a video")
        _bad(400, "that file is empty or too small to be a video")
    worker.start(jid)
    return {"id": jid}


class LiveControls(BaseModel):
    """Change a live job while it records; applies from the next piece."""
    chunk_minutes: int = Field(ge=1, le=120)
    max_chunks: int = Field(ge=0, le=500)
    shorts: int = Field(ge=0, le=20)
    longs: int = Field(ge=0, le=10)


@app.post("/api/jobs/{jid}/live")
def live_controls(jid: int, c: LiveControls):
    j = db.get("jobs", jid) or _bad(404, "job not found")
    if j.get("mode") != "live" or j["status"] in worker.FINISHED:
        _bad(409, "only a live job that's still recording can be changed")
    _check_counts(c.shorts, c.longs)
    db.update("jobs", jid, **c.model_dump())
    db.log(jid, f"live settings changed: {c.chunk_minutes} min pieces, "
                f"{f'{c.max_chunks} pieces' if c.max_chunks else 'until the stream ends'}, "
                f"{c.shorts} shorts + {c.longs} long per piece (from the next piece)")
    return db.get("jobs", jid)


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: int):
    return {"ok": worker.stop(jid)}


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: int):
    try:
        return {"id": worker.retry(jid)}
    except ValueError as e:
        _bad(400, str(e))


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
    return {"stopped": [j["id"] for j in db.all("jobs") if j["status"] not in worker.FINISHED and worker.stop(j["id"])]}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: int):
    """A finished job's record (its clips stay in Clips)."""
    j = db.get("jobs", jid) or _bad(404, "job not found")
    if j["status"] not in worker.FINISHED:
        _bad(409, "the job is still running: stop it first")
    db.delete("jobs", jid)
    return {"ok": True}


class Ids(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)


@app.post("/api/jobs/delete")
def delete_jobs(b: Ids):
    """Several finished jobs' records at once (running ones are skipped and named); their clips stay."""
    done, skipped = [], []
    for jid in b.ids:
        j = db.get("jobs", jid)
        if j and j["status"] in worker.FINISHED:
            db.delete("jobs", jid)
            done.append(jid)
        elif j:
            skipped.append({"id": jid, "why": f"still {j['status']}: stop it first"})
    return {"deleted": done, "skipped": skipped}


@app.get("/api/jobs/{jid}/curves")
def curves(jid: int):
    """The hype curves of a job (one per video / piece), with its picked clips: the Jobs page graph."""
    return [json.loads(p.read_text()) for p in sorted((DATA / "jobs" / str(jid)).glob("curves_*.json"))]


# ---------- state ----------

@app.get("/api/state")
def state():
    """Everything the page polls (every few seconds)."""
    s = settings.get()
    today = quota.calendar(1)[0]
    return {"jobs": db.all("jobs"), "clips": db.all("clips"), "platforms": social.connected(), "ai": ai.balance(),
            "uploads": social.upload_status(), "limit_timers": social.limit_timers(),
            "youtube_cookies": download.cookies_state(), "youtube_scopes": upload.granted() if upload.connected() else {},
            "today": today, "auto": {"enabled": s.auto_enabled, "running": autopilot.LOCK.locked(),
                                     "stage": autopilot.STAGE[0]},
            "post_to": {"short": s.platforms, "long": s.long_platforms}, "ig_max_seconds": s.ig_max_seconds, "now": time.time()}


# ---------- clips ----------

def _clip_preview(cid, poster=False):
    c = db.get("clips", cid)
    if not c or not c.get("path") or not (DATA / c["path"]).exists():
        _bad(404, "no video for this clip")
    try:
        return render.preview(DATA / c["path"], DATA / "jobs" / str(c["job_id"]) / "previews", str(cid), poster)
    except Exception as e:
        _bad(500, f"couldn't make the preview: {e!r}"[:300])


CACHE = {"Cache-Control": "private, max-age=86400"}  # the page asks with ?v=<clip version>, so a new render is fetched


@app.get("/api/clips/{cid}/preview")
def clip_preview(cid: int):
    """Light copy of the clip for the app's players (fast over a tunnel). Download / Open use the full file."""
    return FileResponse(_clip_preview(cid), media_type="video/mp4", headers=CACHE)


@app.get("/api/clips/{cid}/poster")
def clip_poster(cid: int):
    return FileResponse(_clip_preview(cid, poster=True), media_type="image/jpeg", headers=CACHE)


class SlotIn(BaseModel):
    at: float  # the slot's time (seconds since 1970), from /api/slots


@app.post("/api/clips/{cid}/assign")
def assign_clip(cid: int, b: SlotIn):
    """Put a ready clip into a free slot (any day)."""
    try:
        return quota.assign(cid, b.at)
    except ValueError as e:
        _bad(409, str(e))


class MoveIn(BaseModel):
    at: float  # the slot it's dropped on


@app.post("/api/clips/{cid}/move")
def move_clip(cid: int, b: MoveIn):
    """Slots page drag and drop: into that slot; a clip already there swaps places (or goes back to Clips)."""
    try:
        return quota.assign(cid, b.at, swap=True)
    except ValueError as e:
        _bad(409, str(e))


@app.post("/api/clips/{cid}/unassign")
def unassign_clip(cid: int):
    c = db.get("clips", cid) or _bad(404, "clip not found")
    if c["status"] != "scheduled":
        _bad(409, f"the clip isn't in a slot ({c['status']})")
    return quota.unassign(cid)


class PostIn(BaseModel):
    platforms: list[Platform] = Field([], max_length=4)  # [] = where its kind posts (Slots page)


@app.post("/api/clips/{cid}/post")
def post_now(cid: int, b: PostIn | None = None):
    """Post now, to the platforms you pick (default: where its kind posts): it takes the next upcoming free slot of
    its kind today (refused when none is left), then uploads; the daily limit is still checked per platform."""
    c0 = db.get("clips", cid) or _bad(404, "clip not found")
    plats = (b.platforms if b else []) or settings.platforms_for(c0["kind"])
    if not plats:
        _bad(400, f"no platform to post {c0['kind']}s to: pick them on the Slots page")
    try:
        slot = quota.claim_now(cid)
    except ValueError as e:
        _bad(409, str(e))
    threading.Thread(target=worker.upload_clip, args=(cid, plats), daemon=True).start()
    return {"slot": slot, "clip": db.get("clips", cid)}


@app.post("/api/clips/{cid}/retry")
def retry_upload(cid: int, force: bool = False):
    """Retry the platforms that failed for a posted clip (the daily quota still applies). force: also Instagram over
    the length limit / a platform at its own upload limit."""
    c = db.get("clips", cid) or _bad(404, "clip not found")
    if not quota.posted_ok(c) and not json.loads(c.get("uploads") or "{}"):
        _bad(409, "this clip was never posted: use Post now or put it in a slot")
    if c["status"] in ("rendering", "uploading"):
        _bad(409, f"the clip is {c['status']}")
    if not quota.posted_ok(c):  # it went up nowhere: like Post now, it needs one of today's upcoming free slots
        try:
            quota.claim_now(cid)
        except ValueError as e:
            _bad(409, str(e))
    threading.Thread(target=worker.upload_clip, args=(cid, None, force), daemon=True).start()
    return {"ok": True}


class ForceIn(BaseModel):
    platforms: list[Platform] = Field(min_length=1, max_length=4)


@app.get("/api/clips/{cid}/force")
def force_check(cid: int):
    """What Force upload would skip, per platform: the warnings shown before you confirm."""
    c = db.get("clips", cid) or _bad(404, "clip not found")
    s, limited, on = settings.get(), social.limit_timers(), social.connected()
    done = json.loads(c.get("uploads") or "{}")
    out = {}
    for p in social.PLATFORMS:
        w = []
        if not on.get(p):
            w.append("not connected: it will fail")
        if (done.get(p) or {}).get("id"):
            w.append("already posted there: skipped")
        if quota.can_post(c, p):
            w.append(quota.can_post(c, p) + ": this goes past it, and later slots today may then be held back")
        if p in limited:
            w.append(f"{p} said its upload limit was reached at {time.strftime('%I:%M %p', time.localtime(limited[p]['hit_at']))}")
        if p == "instagram" and s.ig_max_seconds and c["end"] - c["start"] > s.ig_max_seconds:
            w.append(f"{c['end'] - c['start']:.0f}s is over Instagram's {s.ig_max_seconds:.0f}s limit: it often fails")
        out[p] = w
    return {"warnings": out, "default": settings.platforms_for(c["kind"], s)}


@app.post("/api/clips/{cid}/force")
def force_upload(cid: int, b: ForceIn):
    """Force upload: post now to the platforms you pick, past the daily quota, slots and platform limits (warned
    first by GET). Logged as forced."""
    c = db.get("clips", cid) or _bad(404, "clip not found")
    if not c.get("path") or c["status"] in ("rendering", "uploading"):
        _bad(409, f"the clip is {c['status']}")
    if c["status"] == "scheduled":  # it leaves its slot: posting now instead
        quota.unassign(cid, "force-uploaded now")
    db.log(c["job_id"], f"#{cid} FORCE UPLOAD by you to {', '.join(b.platforms)}")
    threading.Thread(target=worker.upload_clip, args=(cid, b.platforms, True, True), daemon=True).start()
    return {"ok": True}


class ClipEdit(BaseModel):
    title: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=5000)
    tags: list[str] | None = Field(None, max_length=30)
    headline: str | None = Field(None, max_length=60)  # the header on the video: changing it re-renders the clip


@app.patch("/api/clips/{cid}")
def edit_clip(cid: int, e: ClipEdit):
    if e.title is not None and not e.title.strip():
        _bad(400, "the title can't be empty")
    try:
        return worker.edit_clip(cid, e.model_dump())
    except ValueError as ex:
        _bad(409, str(ex))


def _drop_clip(c):
    """Delete a clip's files and row (a slot it held is free again). Copies already posted stay online."""
    if c["path"]:
        mp4 = DATA / c["path"]
        for f in (mp4, mp4.with_suffix(".ass"), mp4.with_name(f"{mp4.stem}_master.mp4"), mp4.with_name(f"{mp4.stem}_words.json")):
            f.unlink(missing_ok=True)
    db.delete("clips", c["id"])
    prev = DATA / "jobs" / str(c["job_id"]) / "previews"
    for f in (prev / f"{c['id']}.mp4", prev / f"{c['id']}.jpg"):
        f.unlink(missing_ok=True)


@app.delete("/api/clips/{cid}")
def delete_clip(cid: int):
    c = db.get("clips", cid) or _bad(404, "clip not found")
    if c["status"] in ("rendering", "uploading"):
        _bad(409, f"clip is {c['status']}, try again when it finishes")
    _drop_clip(c)
    return {"ok": True}


@app.post("/api/clips/delete")
def delete_clips(b: Ids):
    """Several clips at once (rendering / uploading ones are skipped and named)."""
    done, skipped = [], []
    for cid in b.ids:
        c = db.get("clips", cid)
        if c and c["status"] in ("rendering", "uploading"):
            skipped.append({"id": cid, "why": f"still {c['status']}"})
        elif c:
            _drop_clip(c)
            done.append(cid)
    return {"deleted": done, "skipped": skipped}


@app.post("/api/clips/{cid}/clear-errors")
def clear_errors(cid: int):
    """Hide a clip's failed-upload messages (the platforms that worked stay)."""
    c = db.get("clips", cid) or _bad(404, "clip not found")
    ups = {p: u for p, u in json.loads(c.get("uploads") or "{}").items() if u.get("id")}
    db.update("clips", cid, uploads=json.dumps(ups), **({"status": "rendered"} if c["status"] == "upload failed" else {}))
    return db.get("clips", cid)


# ---------- slots ----------

@app.get("/api/slots")
def slots(days: int = Query(7, ge=1, le=31)):
    """Every slot from today on, with its clip and state (free / scheduled / posted / missed, kept for Auto)."""
    return quota.calendar(days)


# ---------- settings ----------

@app.get("/api/settings")
def get_settings():
    return {"values": settings.get().model_dump(), "schema": settings.schema(),
            "fillers": [f["name"] for f in list_fillers() if not f.get("downloading")]}


@app.post("/api/settings")
def save_settings(changes: dict):
    if "filler" in changes and changes["filler"] and not (FILLERS / _filler_name(changes["filler"])).is_file():
        _bad(400, f"the background video {changes['filler']} isn't there")
    try:
        new = settings.save(changes)
    except ValueError as e:
        _bad(400, str(e))
    quota.reconcile()  # clips in slots that no longer exist go back to Clips
    return new.model_dump()


# ---------- background videos (split layout) ----------
FILLERS = DATA / "fillers"
FILLER_DL = {}  # url -> {"title", "status", "error"} for YouTube downloads in progress / failed


def _filler_name(name):
    n = re.sub(r"[^\w .()-]", "_", Path(name).name).strip() or "video.mp4"
    return n if n.lower().endswith((".mp4", ".mov", ".mkv", ".webm")) else n + ".mp4"


@app.get("/api/fillers")
def list_fillers():
    FILLERS.mkdir(parents=True, exist_ok=True)
    done = [{"name": f.name, "mb": round(f.stat().st_size / 1e6, 1), "seconds": round(render._duration(f))}
            for f in sorted(FILLERS.iterdir()) if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]
    return done + [{"name": d["title"], "url": u, "downloading": d["status"], "error": d.get("error")} for u, d in FILLER_DL.items()]


class FillerLink(BaseModel):
    url: str = Field(max_length=500)
    name: str = Field("", max_length=120)


@app.post("/api/fillers/youtube")
def filler_from_youtube(f: FillerLink):
    """Download a YouTube video as a background video (video only, up to 1080p; it plays muted) in the background."""
    if not YOUTUBE.match(f.url.strip()):
        _bad(400, "that isn't a YouTube video link")
    if f.url in FILLER_DL and FILLER_DL[f.url]["status"] != "failed":
        _bad(409, "already downloading that one")
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
async def upload_filler(request: Request, name: str = Query(max_length=200)):
    FILLERS.mkdir(parents=True, exist_ok=True)
    out = FILLERS / _filler_name(name)
    with open(out, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    if out.stat().st_size < 10_000:
        out.unlink(missing_ok=True)
        _bad(400, "that file is empty or too small to be a video")
    db.syslog(f"background video added: {out.name} ({out.stat().st_size / 1e6:.0f} MB)")
    return {"name": out.name}


@app.delete("/api/fillers/{name}")
def delete_filler(name: str):
    name = _filler_name(name)
    if settings.get().filler == name and settings.get().layout == "split":
        _bad(409, "this is the background video your split layout uses: pick another one in Settings first")
    busy = [j["id"] for j in db.all("jobs") if j["status"] not in worker.FINISHED and (j.get("layout") or "") == f"split:{name}"]
    if busy:
        _bad(409, f"job #{busy[0]} is using it right now")
    (FILLERS / name).unlink(missing_ok=True)
    return list_fillers()


# ---------- YouTube cookies (downloads: the bot check) ----------

def _cookie_name(name):
    stem = re.sub(r"[^\w .()-]", "", Path(name or "").stem).strip(" .")[:40] or "cookies"
    return stem + ".txt"


@app.post("/api/cookies")
async def save_cookies(request: Request, name: str = ""):
    """Body: a cookies.txt (Netscape format) exported from a logged-in youtube.com tab. Each file is ADDED as its own
    set (same name = replaced); when YouTube's bot check hits one set, downloads switch to the next."""
    text = (await request.body()).decode("utf-8", "replace")
    if ".youtube.com" not in text or chr(9) not in text:
        _bad(400, "that doesn't look like a cookies.txt with youtube.com cookies (Netscape format)")
    fname = _cookie_name(name)
    path = YT_COOKIES if fname == YT_COOKIES.name else download.COOKIE_DIR / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.startswith("# Netscape") else "# Netscape HTTP Cookie File" + chr(10) + text, encoding="utf-8")
    download.use_cookies(path.name)
    db.syslog(f"YouTube cookies saved as {path.name}, in use now; {len(download.cookie_sets())} set(s) in all")
    return download.cookies_state()


@app.post("/api/cookies/use")
def use_cookies(name: str):
    if name not in download.cookie_sets():
        _bad(404, "no cookies set with that name")
    download.use_cookies(name)
    return download.cookies_state()


@app.delete("/api/cookies")
def delete_cookies(name: str = ""):
    sets = download.cookie_sets()
    for n in ([name] if name else list(sets)):
        if n in sets:
            sets[n].unlink(missing_ok=True)
    return download.cookies_state()


@app.post("/api/youtube/connect")
def youtube_connect():
    try:
        upload.connect()  # blocks until you finish Google sign-in in the browser tab it opens
    except Exception as e:
        _bad(400, str(e))
    return {"ok": True, "scopes": upload.granted()}


# ---------- logs ----------

@app.get("/api/logs")
def logs(lines: int = 400):
    return {"text": db.tail(min(max(lines, 50), 5000)), "file": str(db.LOG_FILE)}


# ---------- reports ----------

@app.get("/api/reports")
def reports():
    """Every number we have: totals, views per day, by category / moment / length / hour..., YouTube channel
    analytics, the AI's decisions of the day, and what failed."""
    return {"performance": insights.performance(), "series": stats.series(30), "totals": stats.totals(),
            "channel": json.loads(db.get_setting("yt_channel") or "null"), "decisions": insights.decisions(),
            "errors": stats.errors(), "at": float(db.get_setting("views_at") or 0),
            "scopes": upload.granted() if upload.connected() else {}, "refreshing": REFRESHING.locked()}


REFRESHING = threading.Lock()


@app.post("/api/reports/refresh")
def refresh_reports(decide: bool = True):
    """Fetch every number now (and let the AI decide again), in the background."""
    if REFRESHING.locked():
        _bad(409, "already refreshing")

    def run():
        with REFRESHING:
            stats.daily()
            if decide:
                insights.decide()
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


# ---------- AI models ----------

@app.get("/api/ai/models")
def ai_models():
    from .config import LLM_BASE_URL, PC_LLM_BASE_URL
    pc = ai.local_models(PC_LLM_BASE_URL)
    return {**ai.ai_choice(), "providers": ai.PROVIDERS, "tests": ai.TESTS, "local_url": LLM_BASE_URL,
            **ai.local_models(), "pc_url": PC_LLM_BASE_URL, "pc_models": pc.get("models", []), "pc_error": pc.get("error", "")}


class AiUse(BaseModel):
    order: list[Literal["deepseek", "local", "pc"]] = Field(..., min_length=1, max_length=3)  # main first
    local_model: str = Field("", max_length=200)
    pc_model: str = Field("", max_length=200)


@app.post("/api/ai/use")
def ai_use(u: AiUse):
    try:
        ai.set_ai(u.order, u.local_model.strip(), u.pc_model.strip())
    except ValueError as e:
        _bad(400, str(e))
    ai._balance["at"] = 0
    return ai.ai_choice()


class AiTest(BaseModel):
    provider: Literal["deepseek", "local", "pc"]
    kind: Literal["ping", "json", "clip"] = "ping"
    model: str = Field("", max_length=200)  # test another model on that server before switching to it


@app.post("/api/ai/test")
def ai_test(t: AiTest):
    try:
        return ai.test(t.provider, t.kind, t.model)
    except ValueError as e:
        _bad(400, str(e))
    except Exception as e:
        _bad(502, f"{ai.PROVIDERS[t.provider]} failed: {e}"[:400])


# ---------- Auto ----------

@app.get("/api/auto")
def auto_status():
    return autopilot.status()


@app.post("/api/auto/run")
def auto_run():
    """Run today's research now (even when Auto is off), in the background."""
    s = settings.get()
    if autopilot.LOCK.locked():
        _bad(409, "the research is already running")
    if not s.auto_rows:
        _bad(400, "add at least one Auto topic first")
    threading.Thread(target=autopilot.run_day, args=("run now",), daemon=True).start()
    return {"ok": True}


@app.post("/api/auto/stop")
def auto_stop():
    return autopilot.stop_all()


@app.get("/api/auto/rounds")
def auto_rounds(limit: int = Query(10, ge=1, le=50)):
    return autopilot.rounds(limit)


@app.get("/api/topics/{tid}")
def auto_topic(tid: int):
    """One Auto topic's research log."""
    return db.get("topics", tid) or _bad(404, "not found")


# ---------- News & facts: switched off (Settings → news_enabled); the code stays for later ----------

def _news_on():
    if not settings.get().news_enabled:
        _bad(403, "News & facts are switched off")


class NewsFind(BaseModel):
    topic: str = Field("", max_length=200)
    kind: Literal["news", "facts"] = "news"
    n: int = Field(3, ge=1, le=10)


@app.post("/api/news/find")
def news_find(f: NewsFind):
    _news_on()
    topic = f.topic.strip() or "trending"
    rid = news.new_run(f.kind, topic)
    say = news.run_logger(rid)

    def run():
        with db.log_to(say):
            try:
                if f.kind == "facts":
                    stories = news.facts(topic, f.n, say)
                    data = {"topic": topic, "stories": [{**x, "pick": True} for x in stories]}
                else:
                    queries = list(dict.fromkeys([topic] + news.queries_for(topic)))
                    stories = news.find_news(queries, say)
                    picked = news.pick_stories(topic, stories, f.n, [r["headline"] for r in db.all("news")]) if stories else []
                    why = {x["link"]: x.get("why", "") for x in picked}
                    data = {"topic": topic, "queries": queries,
                            "stories": [{**x, "pick": x["link"] in why, "why": why.get(x["link"], "")} for x in stories[:40]]}
                db.update("news_runs", rid, status="found", data=json.dumps(data))
            except Exception as e:
                say(f"failed: {e!r}"[:300])
                db.update("news_runs", rid, status="failed")
    threading.Thread(target=run, daemon=True).start()
    return {"run_id": rid}


@app.get("/api/news/runs")
def news_runs(limit: int = 15):
    _news_on()
    return db.all("news_runs")[:limit]


class NewsMake(BaseModel):
    stories: list[dict] = Field(min_length=1, max_length=10)
    kind: Literal["news", "fact"] = "news"
    picture: bool = True
    run_id: int | None = None
    format: Literal["video", "photo"] = "video"


@app.post("/api/news/make")
def news_make(m: NewsMake):
    """Render the posts; they wait in Clips like any clip (posted through the slots)."""
    _news_on()
    rid = m.run_id or news.new_run(m.kind, "manual")
    say = news.run_logger(rid)

    def run():
        with news.LOCK:
            for st in m.stories:
                news.make_post({"age_h": None, "snippet": "", **st}, m.kind, [], False, m.picture, say,
                               status="rendered", run_id=rid, fmt=m.format, follow=False)
        db.update("news_runs", rid, status="made")
    threading.Thread(target=run, daemon=True).start()
    return {"queued": len(m.stories), "run_id": rid}

