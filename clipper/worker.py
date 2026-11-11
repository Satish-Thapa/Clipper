"""Background pipeline: download/record -> signals -> transcribe -> DeepSeek pick -> render -> upload."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db, download, pick, render, signals, transcribe, upload
from .config import DATA, LIVE_CHUNK_SECONDS

STOPS = {}  # job_id -> threading.Event
WINDOW = {"short": (40, 10), "long": (300, 60)}  # (candidate length, transcript padding each side), seconds


def start(job_id):
    STOPS[job_id] = threading.Event()
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def stop(job_id):
    if job_id in STOPS:
        STOPS[job_id].set()
        db.log(job_id, "stop requested")


def _run(job_id):
    job = db.get("jobs", job_id)
    d = DATA / "jobs" / str(job_id)
    (d / "clips").mkdir(parents=True, exist_ok=True)
    try:
        db.update("jobs", job_id, status="fetching info")
        i = download.info(job["url"])
        db.update("jobs", job_id, title=i.get("title"), is_live=int(bool(i.get("is_live"))))
        db.log(job_id, f"{'LIVE' if i.get('is_live') else 'video'}: {i.get('title')}")
        (_live if i.get("is_live") else _vod)(job, i, d)
        db.update("jobs", job_id, status="stopped" if STOPS[job_id].is_set() else "done")
    except Exception as e:
        db.log(job_id, f"ERROR: {e!r}")
        db.update("jobs", job_id, status="failed")


def _vod(job, i, d):
    jid, url = job["id"], job["url"]
    db.update("jobs", jid, status="downloading")
    db.log(jid, "downloading video + comments + chat replay")
    with ThreadPoolExecutor(2) as ex:
        meta = ex.submit(download.info, url, True)
        chat = ex.submit(download.chat_replay_times, url)
        download.download_vod(url, d / "source.%(ext)s")
        comments = download.comment_times(meta.result().get("comments"))
        chat = chat.result()
    db.log(jid, f"heatmap: {'yes' if i.get('heatmap') else 'no'}, chat msgs: {len(chat)}, timestamped comments: {len(comments)}")
    src = next(d.glob("source.*"))
    _process(job, src, i.get("title", ""), d, heatmap=i.get("heatmap"), chat=chat, comments=comments)


def _live(job, i, d):
    jid, stop = job["id"], STOPS[job["id"]]
    chat = download.LiveChat(job["url"], stop)
    chat.start()
    workers, k = [], 0
    while not stop.is_set():
        out = d / f"live_{k:04d}.ts"
        db.update("jobs", jid, status=f"recording chunk {k}")
        t0 = time.time()
        if not download.record_live_chunk(job["url"], out, LIVE_CHUNK_SECONDS, stop):
            db.log(jid, "stream ended")
            break
        if not out.exists() or out.stat().st_size == 0:
            break
        db.log(jid, f"chunk {k} recorded, processing in background")
        # ponytail: chunks are processed independently, so a moment straddling a boundary gets split.
        # Upgrade: overlap chunks by ~60s.
        times = [t - t0 for t in chat.times]
        w = threading.Thread(target=_process, args=(job, out, f"{i.get('title', '')} (part {k + 1})", d),
                             kwargs={"chat": times}, daemon=True)
        w.start()
        workers.append(w)
        k += 1
    stop.set()  # ends the chat thread
    db.update("jobs", jid, status="finishing clips")
    for w in workers:
        w.join()


def _process(job, src, title, d, heatmap=None, chat=None, comments=None):
    jid = job["id"]
    try:
        db.log(jid, f"analyzing audio of {src.name}")
        audio = signals.loudness(src)
        n = len(audio)
        curve, used = signals.hype_curve(n, audio, heatmap, chat, comments)
        db.log(jid, f"{n}s analysed, signals used: {', '.join(used) or 'none'}")

        cands = []
        for kind, count in (("short", job["shorts"]), ("long", job["longs"])):
            length, pad = WINDOW[kind]
            if count <= 0 or n < pick.LIMITS[kind][0]:
                continue
            for s, e, sig in signals.top_windows(curve, length, count * 3):
                cands.append({"id": len(cands), "kind": kind, "start": max(0, s - pad),
                              "end": min(n, e + pad), "signal": round(sig, 3)})
        if not cands:
            db.log(jid, "video too short for requested clips")
            return

        spans = transcribe.merge([(c["start"], c["end"]) for c in cands])
        db.log(jid, f"transcribing {sum(e - s for s, e in spans):.0f}s across {len(cands)} candidates")
        words = []
        for s, e in spans:
            words += transcribe.words(src, s, e)
        for c in cands:
            c["transcript"] = transcribe.text_with_times(words, c["start"], c["end"])

        db.log(jid, "asking DeepSeek to pick the best moments")
        try:
            chosen = pick.choose(title, cands, job["shorts"], job["longs"])
        except Exception as e:
            db.log(jid, f"DeepSeek failed ({e!r}), using raw hype windows")
            chosen = pick.fallback(cands, job["shorts"], job["longs"], title)

        for c in chosen:
            cid = db.insert("clips", job_id=jid, kind=c["kind"], start=c["start"], end=c["end"], score=c["score"],
                            title=c["title"], description=c["description"], tags=json.dumps(c["tags"]),
                            reason=c["reason"], status="rendering")
            out = d / "clips" / f"{cid}.mp4"
            db.log(jid, f"rendering {c['kind']} #{cid}: {c['title']}")
            render.cut(src, out, c["start"], c["end"], c["kind"] == "short", words)
            db.update("clips", cid, path=str(out.relative_to(DATA)), status="rendered")
            if job["auto_upload"]:
                upload_clip(cid)
    except Exception as e:
        db.log(jid, f"ERROR processing {src.name}: {e!r}")


def upload_clip(cid):
    c = db.get("clips", cid)
    if not upload.connected():
        db.update("clips", cid, status="not uploaded (connect YouTube)")
        return
    db.update("clips", cid, status="uploading")
    try:
        vid = upload.upload(DATA / c["path"], c["title"], c["description"], json.loads(c["tags"] or "[]"),
                            short=c["kind"] == "short")
        db.update("clips", cid, youtube_id=vid, status="uploaded")
        db.log(c["job_id"], f"uploaded #{cid} -> https://youtu.be/{vid}")
    except Exception as e:
        db.update("clips", cid, status=f"upload failed: {e}"[:300])
