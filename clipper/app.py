import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, upload, worker
from .config import DATA

app = FastAPI(title="Clipper")
db.init()
app.mount("/media", StaticFiles(directory=DATA), name="media")
INDEX = Path(__file__).parent / "static" / "index.html"


class NewJob(BaseModel):
    url: str = Field(pattern=r"^https?://")
    shorts: int = Field(3, ge=0, le=20)
    longs: int = Field(1, ge=0, le=10)
    auto_upload: bool = True


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.post("/api/jobs")
def create_job(j: NewJob):
    if j.shorts + j.longs == 0:
        raise HTTPException(400, "ask for at least one clip")
    jid = db.insert("jobs", url=j.url, shorts=j.shorts, longs=j.longs, auto_upload=int(j.auto_upload), created=time.time())
    worker.start(jid)
    return {"id": jid}


@app.get("/api/state")
def state():
    return {"jobs": db.all("jobs"), "clips": db.all("clips"), "youtube_connected": upload.connected()}


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: int):
    worker.stop(jid)
    return {"ok": True}


@app.post("/api/clips/{cid}/upload")
def upload_clip(cid: int):
    worker.upload_clip(cid)
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


@app.post("/api/youtube/connect")
def youtube_connect():
    try:
        upload.connect()  # blocks until you finish Google sign-in in the browser tab it opens
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
