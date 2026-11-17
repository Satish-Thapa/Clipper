import json
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import accounts, autopilot, brands, categories, config, db, download, pick, posts, social, stats, topics, upload, worker
from .config import DATA, SHORT_HARD_MAX, YT_COOKIES

app = FastAPI(title="Clipper")
# The UI polls /api/state every 2s and it carries every job/topic log: gzip makes that ~10x smaller.
app.add_middleware(GZipMiddleware, minimum_size=2000)
db.init()
config.migrate()  # once: an old .env / client_secret.json -> the database (Settings page from then on)
accounts.migrate()  # tokens encrypted; the old single YouTube / TikTok logins -> accounts
posts.seed_templates()  # once: the starter post templates (yours to edit)
brands.migrate()  # a brand always exists ("Demo channel" on a fresh install); older things belong to the first one
categories.migrate()  # account lists -> the mapping table; anything without a category -> Uncategorized
db.syslog(f"server started, data in {DATA}")


@app.on_event("startup")
def _start():
    """Background loops start with the server only (importing this module, e.g. in a test, starts nothing)."""
    topics.start_loop()  # picks up topics still queued from before a restart
    autopilot.start_loop()  # posts scheduled clips + runs rounds, when switched on in the UI
    stats.start_loop()  # hourly WhatsApp report, if WA_PHONE_NUMBER_ID/WA_TOKEN/WA_TO are set
    posts.start_loop()  # photo/text posts on their own schedule, when switched on on the Posts page
    social.backfill_times()  # give old uploads a time so the 24h upload counter includes them
    social.backfill_limit_hits()  # start the 24h timer of a limit error logged before timers existed


INDEX = Path(__file__).parent / "static" / "index.html"
MEDIA_TYPES = {".mp4", ".webm", ".mov", ".jpg", ".jpeg", ".png"}


@app.get("/media/{path:path}")
def media(path: str):
    """Clips, post cards and logos from the data folder. Only pictures and videos: the database next to them holds
    your API keys and tokens, so nothing else in there is ever served."""
    f = (DATA / path).resolve()
    if DATA.resolve() not in f.parents or f.suffix.lower() not in MEDIA_TYPES or not f.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(f)


Platform = Literal["youtube", "facebook", "instagram", "tiktok"]
Mode = Literal["hype", "ai+hype", "ai", "manual"]


class NewJob(BaseModel):
    url: str = Field(pattern=r"^https?://")
    shorts: int = Field(3, ge=0, le=20)
    longs: int = Field(1, ge=0, le=10)
    platforms: list[Platform] = ["youtube"]  # where finished clips are uploaded; [] = don't upload
    aspect: float | None = Field(None, ge=0.56, le=1.78)  # framing: kept w/h of the frame; None = the Settings default
    hq: bool = False  # download up to 4K instead of 1080p (slower)
    mode: Mode = "hype"
    subtitles: bool = True
    voiceover: bool = False
    category_id: int | None = None  # None = Uncategorized
    # "review": clips wait for you (Create → Publish); "auto": upload to `platforms` as soon as each clip is ready
    publish: Literal["review", "auto"] = "auto"


class NewTopic(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    videos: int = Field(3, ge=1, le=20)  # how many trending videos to clip
    shorts: int = Field(3, ge=0, le=20)  # per video
    longs: int = Field(1, ge=0, le=10)   # per video
    platforms: list[Platform] = []
    aspect: float | None = Field(None, ge=0.56, le=1.78)
    hq: bool = False
    mode: Literal["hype", "ai+hype", "ai"] = "hype"  # no "manual": nobody is there to type the cuts
    category_id: int | None = None
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
    auto = j.publish == "auto" and bool(j.platforms)
    jid = db.insert("jobs", url=j.url, shorts=j.shorts, longs=j.longs, auto_upload=int(auto),
                    platforms=json.dumps(j.platforms if auto else []), aspect=j.aspect, hq=int(j.hq), mode=j.mode,
                    subtitles=int(j.subtitles), voiceover=int(j.voiceover),
                    category_id=j.category_id or categories.uncategorized(), created=time.time())
    worker.start(jid)
    return {"id": jid}


@app.post("/api/jobs/file")
async def create_file_job(request: Request, filename: str, shorts: int = 3, longs: int = 1,
                          platforms: str = "youtube", mode: Mode = "hype", aspect: float | None = None,
                          subtitles: bool = True, voiceover: bool = False, category_id: int | None = None):
    """Raw file bytes in the body (no multipart, so no python-multipart dependency)."""
    plats = [p for p in platforms.split(",") if p in social.PLATFORMS]  # comma-separated in the query string
    ext = Path(filename).suffix.lower()
    if ext not in (".mp4", ".mkv", ".mov", ".webm", ".ts", ".avi"):
        raise HTTPException(400, f"unsupported file type {ext or '(none)'}")
    jid = db.insert("jobs", url=f"file:{Path(filename).name}", title=Path(filename).stem, shorts=shorts,
                    longs=longs, auto_upload=int(bool(plats)), platforms=json.dumps(plats), aspect=min(max(aspect, 0.5625), 16 / 9) if aspect else None, mode=mode, subtitles=int(subtitles), voiceover=int(voiceover), status="uploading",
                    category_id=category_id or categories.uncategorized(), created=time.time())
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
                         category_id=t.category_id or categories.uncategorized(),
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


def _no_log(rows):
    """Rows without their log (it can be 100s of KB per job), with its size so the UI knows when to re-fetch it."""
    return [{**{k: v for k, v in r.items() if k != "log"}, "log_size": len(r.get("log") or "")} for r in rows]


@app.get("/api/log/{table}/{rid}")
def get_log(table: Literal["jobs", "topics"], rid: int):
    """One job's / topic's log, fetched by the UI only while you have it open."""
    r = db.get(table, rid)
    if not r:
        raise HTTPException(404, "not found")
    return {"log": r.get("log") or ""}


@app.get("/api/state")
def state():
    bid = brands.current()
    cats = categories.all(brand_id=bid)
    mine = {c["id"] for c in cats}  # this brand's work only (through its categories)
    clips = [{k: v for k, v in c.items() if k != "words"} for c in db.all("clips") if c["category_id"] in mine]
    return {"jobs": _no_log([j for j in db.all("jobs") if j["category_id"] in mine]), "clips": clips,
            "topics": _no_log([x for x in db.all("topics") if x.get("category_id") in mine or not x.get("category_id")]),
            "categories": cats, "accounts": accounts.all(brand_id=bid), "formats": formats(),
            "brands": brands.all(), "brand": bid,
            "platforms": social.connected(),
            "deepseek": pick.balance(), "whatsapp": stats.whatsapp_ready(), "report_minutes": config.REPORT_MINUTES,
            "view_errors": stats.LAST["errors"], "crop_aspect": config.CROP_ASPECT,
            "uploads": social.upload_status(), "limit_timers": social.limit_timers(), "ig_max_seconds": config.IG_MAX_SECONDS,
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
    ?force=true: try Instagram even when the clip is longer than config.IG_MAX_SECONDS (it may fail)."""
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
                   and not (p == "instagram" and config.IG_MAX_SECONDS and c["end"] - c["start"] > config.IG_MAX_SECONDS)]
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
    for rel in json.loads(c.get("renders") or "{}").values():  # the 4:5 / 1:1 / 16:9 versions too
        (DATA / rel).unlink(missing_ok=True)
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
    name: str | None = Field(None, max_length=60)
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
    category_id: int | None = None  # the category its clips belong to (its accounts get them)


@app.get("/api/auto")
def auto_status():
    """Every clip autopilot with its plan and state."""
    return autopilot.status_all()


@app.post("/api/auto/new")
def auto_new(s: AutoSettings):
    return autopilot.create(s.model_dump(exclude_none=True))


@app.delete("/api/auto/{aid}")
def auto_delete(aid: int):
    try:
        autopilot.remove(aid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/auto")
def auto_save(s: AutoSettings, id: int | None = None):
    new = s.model_dump(exclude_none=True)
    cur = autopilot.settings(id)
    if not cur:
        raise HTTPException(404, "no such autopilot")
    merged = {**cur, **new}
    if merged["shorts"] + merged["longs"] == 0:
        raise HTTPException(400, "ask for at least one clip per video")
    if merged["post_to"] <= merged["post_from"]:
        raise HTTPException(400, "the posting window must end after it starts")
    return autopilot.save(new, cur["id"])


@app.post("/api/auto/stop")
def auto_stop(id: int | None = None):
    """OFF + stop everything an autopilot (default: all) started: its round, topics and jobs, scheduled clips."""
    return autopilot.stop_all(id)


@app.get("/api/auto/rounds")
def auto_rounds(limit: int = 20, id: int | None = None):
    return autopilot.rounds(limit, id)


@app.get("/api/auto/activity")
def auto_activity(limit: int = 20, id: int | None = None):
    """The Auto page's activity view: past rounds + the logs of just the topics/jobs they started
    (the activity view reads its decisions out of those logs)."""
    rs = autopilot.rounds(limit, id)
    tids = {t["topic_id"] for r in rs for t in r.get("topics") or [] if t.get("topic_id")}
    logs = {}
    for tid in tids:
        t = db.get("topics", tid)
        if t:
            logs[f"t{tid}"] = t["log"] or ""
            for j in db.all("jobs", f"topic_id={int(tid)}"):
                logs[f"j{j['id']}"] = j["log"] or ""
    return {"rounds": rs, "logs": logs}


@app.post("/api/auto/run")
def auto_run(id: int | None = None, one: bool = False):
    """Run a round now in the background (finds trends + queues topics; the topic queue then clips and uploads).
    one=True: "Make one clip now", a single video for a single clip."""
    s = autopilot.settings(id)
    if not s:
        raise HTTPException(404, "no such autopilot")
    if autopilot._lock(s["id"]).locked():
        raise HTTPException(409, "a round is already running")
    threading.Thread(target=autopilot.run_round, args=("one clip" if one else "run now", s["id"], one), daemon=True).start()
    return {"ok": True}


# ---- photo / text posts (posts.py) ----

PostPlatform = Literal["facebook", "instagram"]


class Category(BaseModel):
    """Every field optional: the UI saves single fields (the ON/OFF switch) as well as the whole form."""
    name: str | None = Field(None, min_length=1, max_length=60)
    niche: str | None = Field(None, max_length=200)  # "" = general interest / top stories
    templates: list[int] | None = None  # post template ids, rotated in this order
    accounts: list[int] | None = None  # account ids mapped to it (all platforms)
    defaults: list[int] | None = None  # of those, the default one per platform (Create picks these)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    description: str | None = Field(None, max_length=500)  # what it's about: the AI reads this
    keywords: str | None = Field(None, max_length=300)  # comma-separated: the AI uses them in titles / hashtags
    posts_per_day: int | None = Field(None, ge=1, le=48)
    post_from: int | None = Field(None, ge=0, le=23)
    post_to: int | None = Field(None, ge=1, le=24)
    approve: bool | None = None  # True = drafts wait for you
    enabled: bool | None = None
    handle: str | None = Field(None, max_length=60)  # printed bottom-left on its cards
    logo: bool | None = None  # its own logo if uploaded, else the Settings logo


class Template(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    label: str | None = Field(None, max_length=30)  # the tag on the card ("" = none)
    source: Literal["ai", "news", "meme"] | None = None  # AI writes it / picks today's news / makes a meme
    picture: bool | None = None  # a gameplay frame behind the card
    instructions: str | None = Field(None, max_length=2000)  # what to write, in your words
    format: Literal["photo", "text", "reel", "auto"] | None = None  # see posts.FORMATS
    bg: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    accent: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    font: Literal["sans", "serif"] | None = None


class NewAccount(BaseModel):
    platform: PostPlatform
    ext_id: str = Field(min_length=3, max_length=40, pattern=r"^\d+$")  # Page id / Instagram user id
    token: str = Field(min_length=20)  # Page access token (for Instagram: its linked Page's token)
    page_id: str = Field("", max_length=40)  # Instagram only: the linked Facebook Page id


class MetaToken(BaseModel):
    token: str = Field(min_length=20)
    picks: list[tuple[PostPlatform, str]] = []  # import: which [platform, id] to add


class PostEdit(BaseModel):
    card: str | None = Field(None, min_length=1, max_length=300)
    footer: str | None = Field(None, max_length=100)
    caption: str | None = Field(None, max_length=2000)


def _post(pid):
    p = db.get("posts", pid)
    if not p:
        raise HTTPException(404, "post not found")
    return p


def _category(cid):
    c = posts.category(cid)
    if not c:
        raise HTTPException(404, "category not found")
    return c


def _meta_error(e):
    return HTTPException(400, f"Meta said: {str(e)[:300]}")


@app.get("/api/posts")
def posts_overview():
    """Categories (with counts), accounts (no tokens) and the post templates."""
    return posts.overview()


@app.get("/api/posts/auto")
def posts_auto():
    """Automate -> Posts: every category's auto-post settings, plan, problems, and the drafts to approve."""
    return posts.automation()


class PostAuto(BaseModel):
    name: str | None = Field(None, max_length=60)
    category_id: int | None = None
    enabled: bool | None = None
    niche: str | None = Field(None, max_length=200)  # what its posts are about ("" = the category's topic)
    keywords: str | None = Field(None, max_length=300)
    templates: list[int] | None = None
    posts_per_day: int | None = Field(None, ge=1, le=48)
    post_from: int | None = Field(None, ge=0, le=23)
    post_to: int | None = Field(None, ge=1, le=24)
    approve: bool | None = None


@app.post("/api/posts/autos")
def post_auto_create(a: PostAuto):
    try:
        return posts.auto_create(a.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/posts/autos/{aid}")
def post_auto_save(aid: int, a: PostAuto):
    new = a.model_dump(exclude_none=True)
    cur = posts.auto_get(aid)
    if not cur:
        raise HTTPException(404, "no such automation")
    if {**cur, **new}["post_to"] <= {**cur, **new}["post_from"]:
        raise HTTPException(400, "the posting window must end after it starts")
    try:
        return posts.auto_save(aid, new)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/posts/autos/{aid}")
def post_auto_delete(aid: int):
    posts.auto_delete(aid)
    return {"ok": True}


@app.post("/api/posts/autos/{aid}/make")
def post_auto_make(aid: int, template: int | None = None):
    """One post now from this automation (a draft), with its topic, keywords and templates."""
    a = posts.auto_get(aid)
    if not a:
        raise HTTPException(404, "no such automation")
    try:
        return posts.locked(posts.make, a["category_id"], template, "draft", "", a)
    except Exception as e:
        raise HTTPException(400, str(e)[:300])


@app.post("/api/posts/approve-all")
def posts_approve_all(category_id: int | None = None):
    return {"approved": posts.approve_all(category_id=category_id)}


@app.post("/api/categories")
def category_create(c: Category):
    return posts.save_category(None, c.model_dump(exclude_none=True))


@app.get("/api/categories/{cid}")
def category_status(cid: int):
    _category(cid)
    return posts.status(cid)


@app.post("/api/categories/{cid}")
def category_save(cid: int, c: Category):
    new = c.model_dump(exclude_none=True)
    merged = {**_category(cid), **new}
    if merged["post_to"] <= merged["post_from"]:
        raise HTTPException(400, "the posting window must end after it starts")
    return posts.save_category(cid, new)


@app.delete("/api/categories/{cid}")
def category_delete(cid: int, move_to: int | None = None):
    """Its clips, posts and jobs move to `move_to` (default Uncategorized): nothing is lost."""
    _category(cid)
    try:
        dest = posts.locked(categories.delete, cid, move_to)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "moved_to": dest}


@app.post("/api/categories/{cid}/make")
def posts_make(cid: int, template: int | None = None, topic: str = ""):
    """Write + render one post now (a draft, whatever the approval setting). Takes a few seconds (DeepSeek).
    topic: a keyword for this one post (empty = the category's topic)."""
    _category(cid)
    try:
        return posts.locked(posts.make, cid, template, "draft", topic[:200])
    except Exception as e:
        raise HTTPException(400, str(e)[:300])


IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")  # PNG, JPEG


async def _save_image(request, dest):
    """Raw image bytes in the body -> dest (PNG or JPEG only, up to 5 MB)."""
    data = await request.body()
    if not data.startswith(IMAGE_MAGIC) or len(data) > 5_000_000:
        raise HTTPException(400, "choose a PNG or JPEG image up to 5 MB (a transparent PNG looks best)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


@app.post("/api/categories/{cid}/logo")
async def category_logo_upload(cid: int, request: Request):
    """This category's own logo (on its cards instead of the Settings logo)."""
    c = _category(cid)
    rel = f"brand/category_{cid}_{int(time.time())}.png"  # new name: the browser shows the new one at once
    await _save_image(request, DATA / rel)
    if c["logo_file"]:
        (DATA / c["logo_file"]).unlink(missing_ok=True)
    db.update("post_categories", cid, logo_file=rel)
    return posts.category(cid)


@app.delete("/api/categories/{cid}/logo")
def category_logo_delete(cid: int):
    c = _category(cid)
    if c["logo_file"]:
        (DATA / c["logo_file"]).unlink(missing_ok=True)
    db.update("post_categories", cid, logo_file="")
    return posts.category(cid)


@app.post("/api/templates")
def template_create(t: Template):
    return posts.save_template(None, t.model_dump(exclude_none=True))


@app.post("/api/templates/preview")
def template_preview(t: Template):
    """A sample card for unsaved template settings (the editor's live preview)."""
    try:
        out = posts.preview(t.model_dump(exclude_none=True))
    except Exception as e:
        raise HTTPException(400, f"couldn't draw the card: {str(e)[:200]}")
    for old in sorted((DATA / "previews").glob("tpl_*.jpg"))[:-20]:  # keep the folder small
        old.unlink(missing_ok=True)
    return {"image": str(out.relative_to(DATA))}


@app.post("/api/templates/{tid}")
def template_save(tid: int, t: Template):
    if not posts.template(tid):
        raise HTTPException(404, "template not found")
    return posts.save_template(tid, t.model_dump(exclude_none=True))


@app.delete("/api/templates/{tid}")
def template_delete(tid: int):
    posts.delete_template(tid)  # also taken out of every category
    return {"ok": True}


# ---- settings (every option the app has; config.py) ----

@app.get("/api/settings")
def settings_get():
    """Every setting with its value; secrets only say whether one is saved. Plus the accounts (for the pickers)."""
    return {"fields": config.public(), "accounts": posts.accounts(),
            "logo": str(config.LOGO_FILE.relative_to(DATA)) if config.LOGO_FILE.exists() else ""}


@app.post("/api/settings")
def settings_save(new: dict):
    """Save settings: {KEY: value}. A secret sent as "" stays as it was; null clears a value."""
    changed = config.save(new)
    if {"WHISPER_MODEL", "TRANSCRIBE_WORKERS", "TRANSCRIBE_ENGINE"} & set(changed):
        from . import transcribe
        transcribe.reset()  # the next transcription loads the model with the new settings
    if "FFMPEG" in changed:
        from . import render
        render.encoder.cache_clear()
    if "DEEPSEEK_API_KEY" in changed:
        pick._client.clear()
        pick._balance["at"] = 0  # show the new key's balance at once
    warnings = []
    if "YT_CLIENT_SECRET_JSON" in changed and config.YT_CLIENT_SECRET_JSON:
        from . import upload as yt
        hint = yt.client_hint(config.YT_CLIENT_SECRET_JSON)
        if hint:
            warnings.append(hint)
    if {"META_APP_ID", "META_APP_SECRET"} & set(changed) and config.META_APP_ID and config.META_APP_SECRET:
        import httpx  # check the pair now, not when a token swap silently fails later
        r = httpx.get(f"{social.graph()}/oauth/access_token", timeout=30, params={
            "client_id": config.META_APP_ID, "client_secret": config.META_APP_SECRET, "grant_type": "client_credentials"})
        if not r.is_success:
            warnings.append("Meta didn't accept this App ID + App secret pair, so short tokens can't be made long-lived. "
                            "Use the App ID and the App secret from developers.facebook.com → your app → App settings → "
                            "Basic (not the Instagram app secret, and not an Instagram account id).")
    if changed:
        db.syslog(f"settings saved: {', '.join(changed)}")
    return {"changed": changed, "warnings": warnings, **settings_get()}


@app.post("/api/settings/logo")
async def settings_logo_upload(request: Request):
    await _save_image(request, config.LOGO_FILE)
    return settings_get()


@app.delete("/api/settings/logo")
def settings_logo_delete():
    config.LOGO_FILE.unlink(missing_ok=True)
    return settings_get()


@app.post("/api/accounts")
def account_add(a: NewAccount):
    """Add one Page / Instagram account by id + token (checked with Meta first)."""
    try:
        return {"id": posts.add_account(a.platform, a.ext_id, a.token, a.page_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _meta_error(e)


@app.post("/api/accounts/discover")
def account_discover(t: MetaToken):
    """Every Page (+ linked Instagram) a Meta token can post to, so you can pick which to add, plus what the token
    allows (permissions, expiry, Instagram accounts granted) so a missing account comes with a reason."""
    tok = social.meta_long_lived(t.token.strip())
    check = social.token_check(tok)
    check["long_lived_swap"] = tok != t.token.strip()
    check["app_set"] = bool(config.META_APP_ID and config.META_APP_SECRET)
    try:
        found = posts.discover(tok)
    except Exception as e:
        if not check.get("valid"):
            raise HTTPException(400, f"Meta says this token doesn't work: {check.get('error') or str(e)[:200]}")
        raise _meta_error(e)
    return {"accounts": found, "check": check}


@app.post("/api/accounts/import")
def account_import(t: MetaToken):
    try:
        return {"added": posts.import_accounts(t.token, t.picks)}
    except Exception as e:
        raise _meta_error(e)


@app.post("/api/accounts/check")
def account_check():
    """Ask Meta now whether every account's token still works (the loop also does it every 6 h)."""
    posts.check_accounts()
    return posts.accounts()


@app.delete("/api/accounts/{aid}")
def account_delete(aid: int):
    posts.remove_account(aid)  # also taken off every category
    return {"ok": True}


class CustomPost(BaseModel):
    category_id: int | None = None
    media: list[str] = []  # paths from /api/uploads/image, in carousel order
    caption: str = Field("", max_length=2200)
    tags: list[str] = []
    targets: list[int] = []  # account ids; [] = the category's defaults
    publish_at: float | None = None
    now: bool = False


@app.post("/api/posts/custom")
def post_custom(p: CustomPost):
    if not p.media and not p.caption.strip():
        raise HTTPException(400, "add a photo or write something")
    if any(not m.startswith("uploads/") or ".." in m for m in p.media):
        raise HTTPException(400, "unknown photo")
    expired = [a["name"] for a in (accounts.get(i) for i in p.targets) if a and a["status"] == "expired"]
    if expired:
        raise HTTPException(409, f"reconnect first: {', '.join(expired)} (Accounts page)")
    if p.now:
        out = posts.create_custom(p.category_id, p.media, p.caption, p.tags, p.targets)
        threading.Thread(target=posts.locked, args=(posts.publish, out["id"]), daemon=True).start()
        return out
    return posts.create_custom(p.category_id, p.media, p.caption, p.tags, p.targets, p.publish_at)


@app.post("/api/posts/{pid}")
def posts_edit(pid: int, e: PostEdit):
    if _post(pid)["status"] in ("posting", "posted", "making"):
        raise HTTPException(409, "already posted / busy: it can't be edited")
    return posts.edit(pid, **e.model_dump(exclude_none=True))


@app.post("/api/posts/{pid}/approve")
def posts_approve(pid: int):
    """Queue it for the next posting slot."""
    if _post(pid)["status"] not in ("draft", "skipped"):
        raise HTTPException(409, "only drafts can be approved")
    db.update("posts", pid, status="ready")
    return db.get("posts", pid)


@app.post("/api/posts/{pid}/skip")
def posts_skip(pid: int):
    if _post(pid)["status"] not in ("draft", "ready"):
        raise HTTPException(409, "only drafts and queued posts can be skipped")
    db.update("posts", pid, status="skipped")
    return db.get("posts", pid)


class PublishNow(BaseModel):
    accounts: list[int] | None = None  # empty / no body: the post's own targets, else its category's defaults


@app.post("/api/posts/{pid}/publish")
def posts_publish(pid: int, body: PublishNow | None = None):
    """Post it right now (outside the schedule), to the given accounts or its category's. Retries failed ones."""
    if _post(pid)["status"] in ("posting", "making"):
        raise HTTPException(409, "busy, try again in a moment")
    try:
        return posts.locked(posts.publish, pid, body.accounts if body else None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/posts/{pid}")
def posts_delete(pid: int):
    p = _post(pid)
    if p["status"] in ("posting", "making"):
        raise HTTPException(409, "busy, try again in a moment")
    if p["image"]:
        (DATA / p["image"]).unlink(missing_ok=True)
    db.delete("posts", pid)
    return {"ok": True}  # local only: a copy already posted stays up


@app.post("/api/views/refresh")
def refresh_views():
    """Just update the view counts shown in the UI (no WhatsApp message)."""
    return {"errors": stats.refresh()}


@app.post("/api/youtube/connect")
def youtube_connect():
    """Older name for POST /api/accounts/youtube: sign in a channel (becomes an account)."""
    return account_youtube()


@app.get("/api/learning")
def learning():
    """What Clipper learned: clips (signal weights from views) and posts (which choices earned engagement)."""
    from . import engage, learn
    return {**learn.status(), "posts": engage.status()}


@app.get("/api/music")
def music_list():
    from . import engage
    return engage.tracks()


@app.post("/api/music")
async def music_add(request: Request, name: str):
    """Raw audio bytes in the body; `name` = the file name (its words describe the mood for the AI)."""
    from . import engage
    try:
        return engage.save_track(name, await request.body())
    except ValueError as e:
        raise HTTPException(400, str(e))


class MusicFetch(BaseModel):
    moods: list[str] = Field(min_length=1, max_length=12)
    per: int = Field(2, ge=1, le=5)


@app.post("/api/music/fetch")
def music_fetch(m: MusicFetch):
    """Free Creative Commons tracks (CC BY / CC0) from Openverse for each mood; credits go into captions."""
    from . import engage
    return {"added": engage.fetch_music(m.moods, m.per), "tracks": engage.tracks()}


@app.delete("/api/music/{file}")
def music_delete(file: str):
    from . import engage
    return engage.delete_track(file)


@app.get("/api/thumb")
def thumb(path: str):
    """A small JPEG of a clip (made once, kept next to it): the Library shows these instead of loading every video."""
    import subprocess
    f = (DATA / path).resolve()
    if DATA.resolve() not in f.parents or f.suffix.lower() not in (".mp4", ".webm", ".mov"):
        raise HTTPException(404, "not found")
    out = f.with_suffix(".thumb.jpg")
    if not f.is_file():  # the video was deleted after it was published: its thumbnail stays
        if out.exists():
            return FileResponse(out, headers={"Cache-Control": "max-age=86400"})
        raise HTTPException(404, "not found")
    if not out.exists() or out.stat().st_mtime < f.stat().st_mtime:
        r = subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-ss", "1", "-i", str(f), "-frames:v", "1",
                            "-vf", "scale=360:-2", "-q:v", "4", str(out)], capture_output=True)
        if r.returncode or not out.exists():
            raise HTTPException(404, "no thumbnail")
    return FileResponse(out, headers={"Cache-Control": "max-age=3600"})


@app.get("/api/jobs/{jid}/source")
def job_source(jid: int):
    """The job's source video (for the trim timeline in Create), or 404 once it's been deleted."""
    src = next((DATA / "jobs" / str(jid)).glob("source.*"), None)
    if not src or src.suffix.lower() not in MEDIA_TYPES:
        raise HTTPException(404, "no source video for this job (deleted after clipping, or not a playable format)")
    return {"path": str(src.relative_to(DATA))}


# ---- the redesigned UI: formats, categories + account routing, accounts, publishing, library, schedule ----

def formats():
    """Where a clip can go: {format: {platform, label, aspect, max seconds or None}}."""
    return {k: {**v, "max": social.format_max(k)} for k, v in social.FORMATS.items()}


class CategoryRouting(BaseModel):
    account_id: int
    mapped: bool = True
    default: bool = False  # this account is the one Create picks for its platform


@app.get("/api/categories")
def categories_list():
    """Categories with their mapped accounts (and which is default per platform) and how many clips/posts each has."""
    n = categories.counts()
    return [{**c, "counts": n.get(c["id"], {"clips": 0, "posts": 0})} for c in categories.all(brand_id=brands.current())]


@app.post("/api/categories/{cid}/accounts")
def category_route(cid: int, r: CategoryRouting):
    _category(cid)
    if not accounts.get(r.account_id):
        raise HTTPException(404, "account not found")
    return categories.map_account(cid, r.account_id, r.mapped, r.default)


class Destination(BaseModel):
    account: int
    format: str


class ClipEdit(BaseModel):
    title: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=5000)
    tags: list[str] | None = None
    headline: str | None = Field(None, max_length=60)
    captions: dict[str, dict[str, str]] | None = None  # {platform: {"title", "description"}} overrides
    category_id: int | None = None
    targets: list[Destination] | None = None


@app.post("/api/clips/{cid}")
def clip_edit(cid: int, e: ClipEdit):
    """Edit a clip's text (shared + per-platform), category and destinations before it's published."""
    c = db.get("clips", cid)
    if not c:
        raise HTTPException(404, "clip not found")
    new = e.model_dump(exclude_none=True)
    row = {k: v for k, v in new.items() if k in ("title", "description", "headline", "category_id")}
    if "tags" in new:
        row["tags"] = json.dumps([t.strip().lstrip("#") for t in new["tags"] if t.strip()][:30])
    if "captions" in new:
        row["captions"] = json.dumps({p: v for p, v in new["captions"].items() if p in social.PLATFORMS})
    if "targets" in new:
        row["targets"] = json.dumps([d for d in new["targets"] if d["format"] in social.FORMATS])
    if row:
        db.update("clips", cid, **row)
    return db.get("clips", cid)


class Publish(BaseModel):
    destinations: list[Destination]
    publish_at: float | None = None  # seconds since 1970; None = now


@app.post("/api/clips/{cid}/publish")
def clip_publish(cid: int, p: Publish):
    """Publish now (in the background: watch the clip's per-destination status) or schedule for a time."""
    c = db.get("clips", cid)
    if not c or not c["path"]:
        raise HTTPException(404, "clip not ready")
    dests = [d.model_dump() for d in p.destinations if d.format in social.FORMATS]
    if not dests:
        raise HTTPException(400, "pick at least one destination")
    expired = [a["name"] for a in (accounts.get(d["account"]) for d in dests) if a and a["status"] == "expired"]
    if expired:
        raise HTTPException(409, f"reconnect first: {', '.join(expired)} (Accounts page)")
    db.update("clips", cid, targets=json.dumps(dests))
    if p.publish_at and p.publish_at > time.time() + 30:
        db.update("clips", cid, status="scheduled", publish_at=p.publish_at)
        db.log(c["job_id"], f"#{cid} scheduled for {time.strftime('%d %b %H:%M', time.localtime(p.publish_at))}")
        return db.get("clips", cid)
    db.update("clips", cid, status="uploading", publish_at=None)
    threading.Thread(target=worker.publish_clip, args=(cid, dests), daemon=True).start()
    return db.get("clips", cid)


@app.post("/api/clips/{cid}/recaption")
def clip_recaption(cid: int):
    """Transcribe the clip again with Whisper and re-render it with the new captions (runs in the background)."""
    c = db.get("clips", cid)
    if not c:
        raise HTTPException(404, "no such clip")
    if c["status"] in ("rendering", "uploading"):
        raise HTTPException(409, "busy, try again in a moment")
    def run():
        try:
            worker.recaption(cid)
        except Exception as e:
            db.log(c["job_id"], f"#{cid}: redoing captions failed: {e!r}", exc=True)
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@app.post("/api/clips/{cid}/retry")
def clip_retry(cid: int):
    """Retry the destinations that failed (the ones that worked are left alone)."""
    c = db.get("clips", cid)
    if not c:
        raise HTTPException(404, "clip not found")
    ups = json.loads(c["uploads"] or "{}")
    failed = [d for d in json.loads(c["targets"] or "[]") if not ups.get(worker.dest_key(d), {}).get("id")]
    if not failed:
        raise HTTPException(400, "nothing failed")
    db.update("clips", cid, status="uploading")
    threading.Thread(target=worker.publish_clip, args=(cid, failed), daemon=True).start()
    return db.get("clips", cid)


@app.post("/api/clips/{cid}/now")
def clip_post_now(cid: int):
    """Scheduled tab → Post now: a scheduled clip goes up right away, to its chosen destinations; an autopilot clip
    waiting for a slot goes to that autopilot's platforms (and counts toward its posts today)."""
    c = db.get("clips", cid)
    if not c or not c["path"]:
        raise HTTPException(404, "clip not ready")
    if c["status"] in ("uploading", "rendering"):
        raise HTTPException(409, "busy, try again in a moment")
    dests = json.loads(c.get("targets") or "[]")
    aid = autopilot.clip_aid(c) if not dests else None
    if not dests:
        s = autopilot.settings(aid) if aid else None
        platforms = (s or {}).get("platforms") or [p for p in ("youtube", "instagram", "facebook", "tiktok")]
        dests = worker.destinations_for(c, platforms, force=True)
    if not dests:
        raise HTTPException(400, "no accounts to post to: map accounts to its category on the Categories page")
    if aid:
        times = [x for x in autopilot._posts(aid) if x > time.time() - 3 * 86400] + [time.time()]
        db.set_setting(f"auto_posts_{aid}", json.dumps(times))
    db.update("clips", cid, targets=json.dumps(dests), status="uploading", publish_at=None)
    db.log(c["job_id"], f"#{cid} posted now from the Scheduled tab")
    threading.Thread(target=worker.publish_clip, args=(cid, dests), daemon=True).start()
    return db.get("clips", cid)


@app.post("/api/clips/{cid}/unschedule")
def clip_unschedule(cid: int):
    db.update("clips", cid, status="rendered", publish_at=None)
    return db.get("clips", cid)


# accounts for every platform

class TikTokAccount(BaseModel):
    refresh_token: str = Field(min_length=10)


@app.get("/api/accounts")
def accounts_list():
    """The accounts of the brand you're working in."""
    return accounts.all(brand_id=brands.current())


# brands (pages): each has its own accounts and categories

class BrandIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class BrandPick(BaseModel):
    id: int


@app.get("/api/brands")
def brands_list():
    return {"brands": brands.all(), "current": brands.current()}


@app.post("/api/brands")
def brand_create(b: BrandIn):
    """A new brand; you switch to it straight away (connect its accounts next)."""
    try:
        return {"id": brands.create(b.name), **brands_list()}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/brands/current")
def brand_switch(b: BrandPick):
    try:
        brands.set_current(b.id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return brands_list()


@app.post("/api/brands/{bid}")
def brand_rename(bid: int, b: BrandIn):
    try:
        brands.rename(bid, b.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return brands_list()


@app.delete("/api/brands/{bid}")
def brand_delete(bid: int, move_to: int | None = None):
    """Only when another brand exists. Its accounts and categories (and their clips and posts) move to `move_to`."""
    try:
        dest = brands.delete(bid, move_to)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"moved_to": dest, **brands_list()}


@app.post("/api/accounts/youtube")
def account_youtube():
    """Opens Google sign-in in a browser tab on this computer and waits for you to finish."""
    try:
        return {"id": accounts.add_youtube()}
    except Exception as e:
        raise HTTPException(400, str(e)[:700])


@app.post("/api/accounts/tiktok")
def account_tiktok(t: TikTokAccount):
    if not (config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET):
        raise HTTPException(400, "add your TikTok client key and secret in Settings → TikTok first")
    try:
        return {"id": accounts.add_tiktok(t.refresh_token)}
    except Exception as e:
        raise HTTPException(400, f"TikTok said: {str(e)[:300]}")


@app.post("/api/accounts/{aid}/check")
def account_check_one(aid: int):
    if not accounts.get(aid):
        raise HTTPException(404, "account not found")
    accounts.check(aid)
    return accounts.get(aid)


# your own photos for posts (cropped to 1:1 / 4:5), then a post from them

@app.post("/api/uploads/image")
async def upload_image(request: Request, aspect: Literal["1:1", "4:5"] = "4:5"):
    data = await request.body()
    if not data.startswith(IMAGE_MAGIC):
        raise HTTPException(400, "choose a JPEG or PNG photo")
    if len(data) > 20_000_000:
        raise HTTPException(400, "photos up to 20 MB")
    try:
        return {"path": posts.save_upload(data, aspect)}
    except Exception as e:
        raise HTTPException(400, f"couldn't read that photo: {str(e)[:200]}")


class PostSchedule(BaseModel):
    publish_at: float | None = None  # None = back to the category's posting slots
    targets: list[int] | None = None


@app.post("/api/posts/{pid}/schedule")
def post_schedule(pid: int, s: PostSchedule):
    p = _post(pid)
    row = {"publish_at": s.publish_at}
    if s.targets is not None:
        row["targets"] = json.dumps(s.targets)
    if p["status"] in ("draft", "skipped", "failed"):
        row["status"] = "ready"
    db.update("posts", pid, **row)
    return db.get("posts", pid)


@app.post("/api/posts/{pid}/retry")
def post_retry(pid: int):
    """Retry the accounts that failed (publish() skips the ones it's already on)."""
    _post(pid)
    threading.Thread(target=posts.locked, args=(posts.publish, pid), daemon=True).start()
    return {"ok": True}


# one list of everything, and what's coming up

def _status_clip(st):
    st = st or ""
    return ("processing" if st in ("rendering", "uploading") else "scheduled" if st == "scheduled"
            else "failed" if "failed" in st else "published" if st.startswith("uploaded") else "draft")


def _clip_item(c, cats_by_id):
    ups = json.loads(c.get("uploads") or "{}")
    targets = json.loads(c.get("targets") or "[]")
    plats = sorted({p for p, u in social.entries(ups)} |
                   {social.FORMATS[d["format"]]["platform"] for d in targets if d["format"] in social.FORMATS})
    return {"type": "clip", "id": c["id"], "title": c["title"], "media": c["path"], "kind": c["kind"],
            "file": bool(c["path"]) and (DATA / c["path"]).exists(),
            "duration": round(c["end"] - c["start"]), "platforms": plats, "status": _status_clip(c["status"]),
            "detail": c["status"], "category_id": c["category_id"],
            "category": (cats_by_id.get(c["category_id"]) or {}).get("name", ""), "publish_at": c["publish_at"],
            "progress": c["progress"], "uploads": ups, "targets": targets, "job_id": c["job_id"]}


def _post_item(p, cats_by_id):
    ups = json.loads(p.get("uploads") or "{}")
    status = {"posting": "processing", "making": "processing", "ready": "scheduled", "posted": "published",
              "failed": "failed"}.get(p["status"], "draft")
    return {"type": "post", "id": p["id"], "title": p["card"] or (p["caption"] or "")[:80], "media": p["image"],
            "video": p.get("video") or "", "music": json.loads(p.get("features") or "{}").get("music", ""),
            "kind": p["kind"], "duration": None,
            "platforms": sorted({u.get("platform") for u in ups.values() if u.get("platform")}),
            "status": status, "detail": p["status"], "category_id": p["category_id"],
            "category": (cats_by_id.get(p["category_id"]) or {}).get("name", ""), "publish_at": p["publish_at"],
            "uploads": ups, "targets": posts.targets_of(p), "media_count": len(json.loads(p.get("media") or "[]"))}


@app.get("/api/library")
def library(category: int | None = None, platform: str | None = None, status: str | None = None, q: str = "",
            kind: Literal["all", "clip", "post"] = "all", limit: int = 300):
    """Clips and posts together, newest first, filtered by category / platform / status / title search."""
    cats_by_id = {c["id"]: c for c in categories.all(brand_id=brands.current())}  # this brand's only
    items = ([_clip_item(c, cats_by_id) for c in db.all("clips") if c["category_id"] in cats_by_id] if kind != "post" else []) + \
            ([_post_item(p, cats_by_id) for p in db.all("posts") if p["category_id"] in cats_by_id] if kind != "clip" else [])
    q = q.strip().lower()
    items = [i for i in items if (not category or i["category_id"] == category) and (not status or i["status"] == status)
             and (not platform or platform in i["platforms"]) and (not q or q in (i["title"] or "").lower())]
    return items[:min(max(limit, 1), 1000)]


@app.get("/api/scheduled")
def scheduled():
    """What's going out next: clips and posts with a time, plus autopilot clips / approved posts waiting for a slot
    (time = the next slot of their schedule)."""
    cats_by_id, out = {c["id"]: c for c in categories.all(brand_id=brands.current())}, []
    nxt = {s["id"]: autopilot.schedule(s, posts=autopilot._posts(s["id"]))["next_at"] for s in autopilot.profiles()}
    for c in db.all("clips", "status='scheduled'"):
        if c["category_id"] not in cats_by_id:
            continue
        at = c["publish_at"] or nxt.get(autopilot.clip_aid(c) or 0) or min(nxt.values(), default=None)
        out.append({**_clip_item(c, cats_by_id), "at": at, "slot": not c["publish_at"]})
    for p in db.all("posts", "status='ready'"):
        cat = cats_by_id.get(p["category_id"])
        if not cat:
            continue
        at = p["publish_at"] or (autopilot.schedule(cat, posts=posts._times(cat["id"]))["next_at"] if cat else None)
        out.append({**_post_item(p, cats_by_id), "at": at, "slot": not p["publish_at"]})
    return sorted(out, key=lambda i: i["at"] or 9e12)
