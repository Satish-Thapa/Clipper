"""Background pipeline: download/record -> signals -> transcribe -> DeepSeek pick -> render -> upload."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db, download, pick, render, signals, transcribe, upload
from .config import DATA, LIVE_CHUNK_SECONDS, SHORT_HARD_MAX

STOPS = {}  # job_id -> threading.Event
WINDOW = {"short": (60, 40), "long": (120, 40)}  # (candidate length, transcript padding each side), seconds


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
        if job["url"].startswith("file:"):  # uploaded from the UI: already at data/jobs/<id>/source.*
            src = next(d.glob("source.*"))
            db.log(job_id, f"local file: {job['title']}")
            _process(job, src, job["title"] or src.name, d)
            db.update("jobs", job_id, status="done")
            return
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
    """Clip a stream WHILE it is live: record it in fixed-size chunks and process each chunk in the background.

    Big picture (one job, several threads):

        main loop (this function)   record chunk 0 --> record chunk 1 --> record chunk 2 --> ... until stream ends / Stop
                                          |                  |                  |
        worker threads                    +-> _process(0)    +-> _process(1)    +-> _process(2)    (clip, render, upload)

        chat thread (LiveChat)      collects the wall-clock time of every chat message the whole time

    So clips from chunk 0 can already be rendered/uploaded while chunk 1 is still being recorded.
    The job's shorts/longs counts apply PER CHUNK (default chunk = 10 min, LIVE_CHUNK_SECONDS in .env).
    Live streams have no "most replayed" heatmap and no comments yet, so only audio + chat are used.
    """
    jid, stop = job["id"], STOPS[job["id"]]  # stop = the Event set by the UI's Stop button (see stop())

    # Chat runs in its own thread for the whole stream. It only stores timestamps (seconds since 1970)
    # of messages, not their text; a burst of messages = something just happened.
    chat = download.LiveChat(job["url"], stop)
    chat.start()

    workers, k = [], 0  # k = chunk number
    while not stop.is_set():
        out = d / f"live_{k:04d}.ts"  # .ts = raw MPEG-TS, safe to write while streaming (no header to finalise)
        db.update("jobs", jid, status=f"recording chunk {k}")

        # Remember when this chunk starts (wall clock), so chat times can be turned into "seconds into this chunk".
        # Not exact: record_live_chunk first asks YouTube for fresh stream URLs (a few seconds) before ffmpeg
        # starts, so chat lands those few seconds late. CHAT_LAG and padding absorb most of it.
        t0 = time.time()

        # Blocks until the chunk is recorded: ffmpeg copies LIVE_CHUNK_SECONDS of the stream from the live edge
        # (no re-encode). Returns False if the stream is already over. If Stop is pressed mid-chunk, ffmpeg is
        # told to quit gracefully and the partial chunk is kept and still processed below.
        # Small gaps (a few seconds) between chunks are lost while the next recording starts.
        if not download.record_live_chunk(job["url"], out, LIVE_CHUNK_SECONDS, stop):
            db.log(jid, "stream ended")
            break
        if not out.exists() or out.stat().st_size == 0:  # ffmpeg produced nothing: stream died mid-chunk
            break
        db.log(jid, f"chunk {k} recorded, processing in background")

        # ponytail: chunks are processed independently, so a moment straddling a boundary gets split.
        # Upgrade: overlap chunks by ~60s.

        # Convert ALL chat timestamps so far to seconds relative to this chunk's start. Messages from earlier
        # chunks become negative and later ones fall past the end; signals.counts() simply ignores both.
        times = [t - t0 for t in chat.times]

        # Hand the chunk to _process in a background thread (same pipeline as a VOD: loudness + chat ->
        # hype curve -> transcribe -> DeepSeek -> render -> upload), then immediately go record the next chunk.
        # Transcription is behind a lock in transcribe.py, so chunks queue up there instead of fighting for CPU.
        w = threading.Thread(target=_process, args=(job, out, f"{i.get('title', '')} (part {k + 1})", d),
                             kwargs={"chat": times}, daemon=True)
        w.start()
        workers.append(w)
        k += 1

    # Stream is over (or Stop was pressed). Setting stop also ends the chat thread: it checks the flag after
    # each new message (if chat goes quiet it just lingers; it's a daemon thread so it dies with the server).
    stop.set()
    db.update("jobs", jid, status="finishing clips")
    for w in workers:  # wait for every chunk's clips to finish, then _run() marks the job done/stopped
        w.join()


def _process(job, src, title, d, heatmap=None, chat=None, comments=None):
    """Turn one video file (a whole VOD, or one recorded live chunk) into rendered clips.

    heatmap/chat/comments are optional audience signals (live chunks only have chat).
    job["mode"] decides who picks the moments:
      "hype"    - signals find candidate windows, DeepSeek picks among them (fast, cheap).
      "ai+hype" - DeepSeek reads the FULL transcript, signals are given as hints it may override.
      "ai"      - DeepSeek reads the FULL transcript only, no signals.
      "manual"  - only steps 1-2 (so the UI can show the curves); the user adds cuts via manual_cuts().
    """
    jid = job["id"]
    mode = job.get("mode") or "hype"
    try:
        # STEP 1: Loudness. ffmpeg's ebur128 filter measures "momentary loudness" in LUFS, the
        # broadcast unit for how loud audio *feels* to a person (not raw volume). We keep one value
        # per second, roughly -35 = calm talking, -10 = screaming/explosion, floored at -70 (silence).
        # len(audio) doubles as the video length in seconds.
        db.log(jid, f"analyzing audio of {src.name}")
        audio = signals.loudness(src)
        n = len(audio)

        # STEP 2: Hype curve. Each signal becomes a per-second array, gets normalised to 0..1
        # (median -> 0, 95th percentile -> 1, so different units are comparable), then they are summed
        # with weights:
        #   heatmap  x3    YouTube "most replayed": where real viewers rewatched (VODs only)
        #   chat     x2    live-chat messages per second, shifted 8s earlier (people type after the moment)
        #   comments x1.5  timestamps viewers wrote in comments ("4:05 lmao")
        #   audio    x1    loudness from step 1
        # curve[t] = how "hype" second t is. parts = each normalised signal, kept for the UI graph and the AI.
        curve, parts = signals.hype_curve(n, audio, heatmap, chat, comments)
        db.log(jid, f"{n}s analysed, signals used: {', '.join(parts) or 'none'}, mode: {mode}")
        _save_curves(d, src, curve, parts)  # UI can show the graph while we keep working
        if mode == "manual":
            db.log(jid, "manual mode: add your cuts in the UI")
            return

        # STEP 3: Candidate windows. Slide a fixed-length window (60s shorts, 120s longs) over
        # the curve and greedily take the highest-scoring non-overlapping ones, 3x as many as requested
        # so DeepSeek has choices. Kinds are searched separately, so the same moment can be a candidate for
        # both kinds (a tight short AND a long with the bigger story). Each is padded (40s each side) so the AI can find a clean
        # start and end. In "ai" modes these are only used as a fallback if DeepSeek fails.
        # First cap the requested counts by what this video can hold (a 1-min video -> 1 short, no long).
        asked = {"short": job["shorts"], "long": job["longs"]}
        counts = pick.fit_counts(asked, n)
        if counts != asked:
            db.log(jid, f"video is {n}s: making {counts['short']} short / {counts['long']} long "
                        f"(asked {asked['short']} / {asked['long']})")
        cands = []
        for kind, count in counts.items():
            length, pad = WINDOW[kind]
            if count <= 0:
                continue
            for s, e, sig in signals.top_windows(curve, length, count * 3):
                cands.append({"id": len(cands), "kind": kind, "start": max(0, s - pad),
                              "end": min(n, e + pad), "signal": round(sig, 3)})
        if not cands:
            db.log(jid, "video too short for requested clips")
            return

        # STEP 4: Transcribe with Whisper (word-level timestamps, CPU).
        # "hype": only the candidate windows (overlaps merged so nothing is transcribed twice), fast.
        # "ai" modes: the whole video, so DeepSeek can find moments the curves missed. Slow on long videos.
        spans = [[0, n]] if mode != "hype" else transcribe.merge([(c["start"], c["end"]) for c in cands])
        db.log(jid, f"transcribing {sum(e - s for s, e in spans):.0f}s")
        words = []
        for s, e in spans:
            words += transcribe.words(src, s, e)
        for c in cands:  # "[12.0] hello there [17.1] ..." text per window, absolute seconds
            c["transcript"] = transcribe.text_with_times(words, c["start"], c["end"])

        # STEP 5: DeepSeek picks the final clips, trims them to sentence boundaries and writes
        # title/description/hashtags. Its numbers are clamped by pick.validate(). If the API fails,
        # fall back to the strongest raw hype windows so the job still produces something.
        db.log(jid, "asking DeepSeek to pick the best moments")
        try:
            if mode == "hype":
                chosen = pick.choose(title, cands, counts)
            else:
                chosen = pick.choose_full(title, n, transcribe.text_with_times(words, 0, n),
                                          parts if mode == "ai+hype" else None, counts)
        except Exception as e:
            db.log(jid, f"DeepSeek failed ({e!r}), using raw hype windows")
            chosen = pick.fallback(cands, counts, title)
        # Snap every pick out to whole sentences (AI times are guesses from coarse markers), within the
        # video and never past the hard max (YouTube rejects Shorts over 3 min).
        for c in chosen:
            s, e = transcribe.snap(words, c["start"], c["end"])
            c["start"], c["end"] = s, min(e, n, s + pick.HARD_MAX[c["kind"]])
        _save_curves(d, src, curve, parts, chosen)  # now with the picked clips marked on the graph

        # STEP 6: Save each clip to the DB, render it with ffmpeg (every kind vertical 9:16), burn in the
        # headline (top) + captions (bottom) if the job asked for subtitles, then upload if auto-upload is on.
        _render_clips(job, src, d, chosen, words, job.get("subtitles", 1))
    except Exception as e:
        db.log(jid, f"ERROR processing {src.name}: {e!r}")


def _render_clips(job, src, d, chosen, words, subtitles):
    """subtitles: burn the headline (top) and word captions (bottom) into the video."""
    jid = job["id"]
    for c in chosen:
        cid = db.insert("clips", job_id=jid, kind=c["kind"], start=c["start"], end=c["end"], score=c["score"],
                        title=c["title"], description=c["description"], tags=json.dumps(c["tags"]),
                        reason=c["reason"], headline=c.get("headline", ""), status="rendering")
        out = d / "clips" / f"{cid}.mp4"
        db.log(jid, f"rendering {c['kind']} #{cid}: {c['title']}")
        render.cut(src, out, c["start"], c["end"], True, words if subtitles else (),
                   c.get("headline", "") if subtitles else "")
        db.update("clips", cid, path=str(out.relative_to(DATA)), status="rendered")
        if job["auto_upload"]:
            upload_clip(cid)


def manual_cuts(job_id, cuts, subtitles=True):
    """User-chosen cuts [{kind, start, end, title, headline, description, tags}] -> rendered clips, in the background.
    Times are used exactly as given (no sentence snapping): the user decided."""
    def run():
        job = db.get("jobs", job_id)
        d = DATA / "jobs" / str(job_id)
        try:
            src = next(d.glob("source.*"))
            words = []
            if subtitles:  # only transcribe when captions are wanted; it's the slow part
                for s, e in transcribe.merge([(c["start"], c["end"]) for c in cuts]):
                    words += transcribe.words(src, s, e)
            _render_clips(job, src, d, [{"score": 0, "reason": "manual cut", **c} for c in cuts], words, subtitles)
        except Exception as e:
            db.log(job_id, f"ERROR in manual cuts: {e!r}")
    threading.Thread(target=run, daemon=True).start()


def _save_curves(d, src, curve, parts, chosen=()):
    """Write per-second curves (all 0..1) + picked clips for the UI graph: data/jobs/<id>/<file>.curves.json"""
    top = float(curve.max()) if len(curve) and curve.max() > 0 else 1.0
    data = {"name": src.name, "n": len(curve),
            "signals": {k: [round(float(x), 2) for x in v] for k, v in {"hype": curve / top, **parts}.items()},
            "chosen": [{k: c[k] for k in ("kind", "start", "end", "title")} for c in chosen]}
    out = d / f"{src.stem}.curves.json"
    tmp = out.with_suffix(".tmp")  # write-then-rename so the UI never reads a half-written file
    tmp.write_text(json.dumps(data))
    tmp.replace(out)


def upload_clip(cid):
    c = db.get("clips", cid)
    if not upload.connected():
        db.update("clips", cid, status="not uploaded (connect YouTube)")
        return
    db.update("clips", cid, status="uploading")
    try:
        vid = upload.upload(DATA / c["path"], c["title"], c["description"], json.loads(c["tags"] or "[]"),
                            short=c["end"] - c["start"] <= SHORT_HARD_MAX)  # vertical + <= 3 min = a Short
        db.update("clips", cid, youtube_id=vid, status="uploaded")
        db.log(c["job_id"], f"uploaded #{cid} -> https://youtu.be/{vid}")
    except Exception as e:
        db.update("clips", cid, status=f"upload failed: {e}"[:300])
