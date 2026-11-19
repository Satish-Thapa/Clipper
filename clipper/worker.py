"""Background pipeline: download/record -> signals -> transcribe -> DeepSeek pick -> render -> upload."""
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import db, download, pick, render, signals, social, transcribe
from .config import (CLIP_PAD_AFTER, CLIP_PAD_BEFORE, DATA, IG_MAX_SECONDS, KEEP_SOURCES, LIVE_CHUNK_SECONDS,
                     LONG_VIDEO_MINUTES, SNAP_REACH)

STOPS = {}  # job_id -> threading.Event
RENDER_JOBS = 2  # clips rendered at once (GPU encoder + CPU side by side)
WINDOW = {"short": (60, 40), "long": (120, 40)}  # (candidate length, transcript padding each side), seconds


def start(job_id):
    STOPS[job_id] = threading.Event()
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def run(job_id):
    """Same as start() but blocks until the job is finished (the topic queue clips one video at a time)."""
    STOPS[job_id] = threading.Event()
    _run(job_id)


class Stopped(Exception):
    """Raised at the next step boundary after Stop was pressed (see _check)."""


def _check(job_id):
    if STOPS.get(job_id) is not None and STOPS[job_id].is_set():
        raise Stopped()


FINISHED = ("done", "failed", "stopped", "interrupted")


def stop(job_id):
    """Stop a job now: the download is aborted, and every step (loudness, transcript, AI pick, each render) checks the
    flag before it starts. Clips already rendered stay. A job with no thread any more (server restarted) is just
    marked stopped."""
    job = db.get("jobs", job_id)
    if not job or job["status"] in FINISHED:
        return False
    if job_id in STOPS:
        STOPS[job_id].set()
        db.log(job_id, "stop requested: stopping at the next step (clips already made stay)")
    else:
        db.update("jobs", job_id, status="stopped")
        db.log(job_id, "stopped (it wasn't running any more)")
    return True


RETRY_SKIP = ("id", "status", "log", "created", "is_live", "title")


def retry(job_id):
    """Run a failed / interrupted / stopped job again as a NEW job with the same settings (the old one stays as a
    record). An uploaded file is copied over if it's still there. -> the new job's id."""
    import shutil
    old = db.get("jobs", job_id)
    if not old:
        raise ValueError("job not found")
    if old["status"] not in FINISHED or old["status"] == "done":
        raise ValueError(f"job #{job_id} is {old['status']}: only failed, interrupted or stopped jobs can be retried")
    src = None
    if str(old["url"]).startswith("file:"):
        src = next((DATA / "jobs" / str(job_id)).glob("source.*"), None)
        if not src:
            raise ValueError("the uploaded file was already deleted (KEEP_SOURCES=1 in .env keeps it); upload it again")
    new = db.insert("jobs", **{k: v for k, v in old.items() if k not in RETRY_SKIP}, title=old.get("title"),
                    created=time.time())
    if src:
        d = DATA / "jobs" / str(new)
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, d / src.name)
    db.log(new, f"retry of job #{job_id} (it was {old['status']})")
    db.log(job_id, f"retried as job #{new}")
    start(new)
    return new


def _run(job_id):
    job = db.get("jobs", job_id)
    d = DATA / "jobs" / str(job_id)
    (d / "clips").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    db.log(job_id, f"job started: {job['url']} | {job['shorts']} shorts, {job['longs']} long, mode {job.get('mode')}, "
                   f"framing {job.get('aspect') or 'default'}, {'4K' if job.get('hq') else 'up to 1440p'}, "
                   + ("clean style (original sound, short title on top, like live clips), " if job.get("style") == "clean" else
                      f"subtitles {'on' if job.get('subtitles') else 'off'}, follow message {'on' if job.get('follow', 1) else 'off'}, voice-over {'on' if job.get('voiceover') else 'off'}, ")
                   + (f"upload only the best {'all' if job.get('upload_best') is None else job['upload_best']} short(s) + "
                      f"{'all' if job.get('upload_best_long') is None else job['upload_best_long']} long(s), "
                      if job.get("upload_best") is not None or job.get("upload_best_long") is not None else "") +
                   f"upload to {job.get('platforms') if job.get('auto_upload') else 'nowhere'}"
                   + (f", topic T{job['topic_id']}" if job.get("topic_id") else ""))
    if (job.get("notes") or "").strip():
        db.log(job_id, f"📝 your instructions for the AI: {job['notes'].strip()}")
    try:
        if job["url"].startswith("file:"):  # uploaded from the UI: already at data/jobs/<id>/source.*
            src = next(d.glob("source.*"))
            db.log(job_id, f"local file: {job['title']}")
            _process(job, src, job["title"] or src.name, d)
            db.update("jobs", job_id, status="done")
            db.log(job_id, f"job finished in {(time.time() - t0) / 60:.1f} min")
            return
        db.update("jobs", job_id, status="fetching info")
        with db.step(job_id, "fetching video info"):
            i = download.info(job["url"])
        db.update("jobs", job_id, title=i.get("title"), is_live=int(bool(i.get("is_live"))), channel=i.get("channel") or "")
        job.update(is_live=int(bool(i.get("is_live"))), channel=i.get("channel") or "")
        db.log(job_id, f"{'LIVE' if i.get('is_live') else 'video'}: {i.get('title')} | channel {i.get('channel')}, "
                       f"{(i.get('duration') or 0) / 60:.1f} min, {i.get('view_count') or 0:,} views")
        if i.get("is_live"):
            stopped = _live(job, i, d)  # True only when Stop was pressed (not when the pieces asked for are done)
        else:
            _vod(job, i, d)
            stopped = STOPS[job_id].is_set()
        status = "stopped" if stopped else "done"
        db.update("jobs", job_id, status=status)
        db.log(job_id, f"job {status} in {(time.time() - t0) / 60:.1f} min")
    except Exception as e:
        if STOPS.get(job_id) is not None and STOPS[job_id].is_set():  # Stop pressed: aborted download/step, not a failure
            db.update("jobs", job_id, status="stopped")
            db.log(job_id, f"job stopped by you after {(time.time() - t0) / 60:.1f} min")
        else:
            _fail(job_id, e)
    finally:
        drop_sources(job_id)


def drop_sources(job_id):
    """Delete the job's source video (+ half-downloaded parts) and live recordings once it's finished: 0.5-2 GB each.
    Clips, subtitles and the hype-curve data stay. KEEP_SOURCES=1 in .env turns this off."""
    if KEEP_SOURCES:
        return
    freed = 0
    for f in [*(DATA / "jobs" / str(job_id)).glob("source.*"), *(DATA / "jobs" / str(job_id)).glob("live_*")]:
        try:
            size = f.stat().st_size
            f.unlink()
            freed += size
        except OSError as e:
            db.log(job_id, f"could not delete {f.name}: {e!r}")
    if freed:
        db.log(job_id, f"source video deleted ({freed / 1e6:.0f} MB freed; KEEP_SOURCES=1 in .env keeps it)")


def _fail(job_id, e):
    if download.bot_checked(e):
        db.log(job_id, "ERROR: YouTube bot check ('Sign in to confirm you're not a bot'). YouTube wants a signed-in "
                       "browser: add YouTube cookies on the Socials page, then run it again.")
    else:
        db.log(job_id, f"ERROR: {e!r}", exc=True)
    db.update("jobs", job_id, status="failed")


def _vod(job, i, d):
    jid, url = job["id"], job["url"]
    db.update("jobs", jid, status="downloading")
    db.log(jid, "downloading video + comments + chat replay")
    ex = ThreadPoolExecutor(2)  # not a `with`: on Stop it must not wait for the chat replay (up to 15 min)
    try:
        meta = ex.submit(download.info, url, True)
        chat = ex.submit(download.chat_replay_times, url)
        h = download.height_for(job.get("hq"))
        with db.step(jid, f"downloading video (up to {h}p, {download.FRAGMENTS} chunks at once)"):
            download.download_vod(url, d / "source.%(ext)s", h, stop=STOPS.get(jid))
        _check(jid)
        with db.step(jid, "waiting for comments + chat replay"):
            comments = download.comment_times(meta.result().get("comments"))
            chat, chat_err = chat.result()
        if chat_err:
            db.log(jid, f"chat replay not read: {chat_err}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    db.log(jid, f"heatmap: {'yes' if i.get('heatmap') else 'no'}, chat msgs: {len(chat)}, timestamped comments: {len(comments)}")
    src = next(d.glob("source.*"))
    db.log(jid, f"source file: {src.name}, {src.stat().st_size / 1e6:.0f} MB, {_describe(src)}")
    total = i.get("duration") or _dur(src)
    if (LONG_VIDEO_MINUTES and total >= LONG_VIDEO_MINUTES * 60 and (job.get("mode") or "hype") != "manual"
            and not job.get("whole")):
        _pieces(job, src, total, d, i.get("title", ""), i.get("heatmap"), chat, comments)
    else:
        _process(job, src, i.get("title", ""), d, heatmap=i.get("heatmap"), chat=chat, comments=comments)


def _dur(f):
    out = subprocess.run([render.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _pieces(job, src, total, d, title, heatmap, chat, comments):
    """A long video (LONG_VIDEO_MINUTES+) clipped like a live stream: cut into pieces of the job's piece length
    (default LIVE_CHUNK_SECONDS), each with the previous piece's last JOIN_SECONDS in front (a moment on a boundary
    can still be one clip), and each piece processed on its own (two at a time): its own loudness, transcript,
    AI pick and renders, with shorts/longs PER PIECE and at least one clip per piece, exactly like live pieces.
    The video's heatmap, chat replay and comment times are shifted to each piece's own clock."""
    from .config import FFMPEG
    jid, stop = job["id"], STOPS[job["id"]]
    size = int((job.get("chunk_minutes") or 0) * 60) or LIVE_CHUNK_SECONDS
    n = -(-int(total) // size)
    if job.get("max_chunks"):
        n = min(n, int(job["max_chunks"]))
    db.log(jid, f"long video ({total / 60:.0f} min): clipping it like a stream, in {n} piece(s) of {size / 60:g} min, "
                f"{job['shorts']} shorts + {job['longs']} long per piece, 2 pieces at a time")

    def shift(off, length):
        heat = [{**h, "start_time": max(0.0, h["start_time"] - off), "end_time": min(length, h["end_time"] - off)}
                for h in heatmap or [] if h["end_time"] > off and h["start_time"] < off + length]
        inside = lambda ts: [t - off for t in ts or [] if 0 <= t - off < length]
        return heat, inside(chat), inside(comments)

    with ThreadPoolExecutor(2) as ex:
        futures = []
        for k in range(n):
            if stop.is_set():
                db.log(jid, f"stopped: {k} of {n} piece(s) handed over")
                break
            start, end = k * size, min((k + 1) * size, total)
            s0 = max(0, start - (JOIN_SECONDS if k else 0))
            out = d / f"live_vod_{k:04d}.mp4"
            db.update("jobs", jid, status=f"cutting piece {k + 1}/{n}")
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{s0:.2f}", "-i", str(src), "-t", f"{end - s0:.2f}",
                            "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)], check=True, capture_output=True)
            length = _dur(out)
            real0 = max(0.0, end - length)  # keyframes: the piece really starts a little before s0
            boundary = max(0.0, start - real0) if k else 0.0
            heat, ch, co = shift(real0, length)
            db.log(jid, f"[piece {k + 1}] {_mmss(real0)}-{_mmss(end)} of the video"
                        + (f" (the previous piece's last {boundary:.0f}s in front)" if boundary else ""))
            futures.append(ex.submit(_process, db.get("jobs", jid), out, f"{title} (part {k + 1}/{n})", d,
                                     heatmap=heat, chat=ch, comments=co, live={"piece": k + 1, "boundary": boundary}))
        db.update("jobs", jid, status="clipping pieces")
        for f in futures:
            f.result()
        db.log(jid, f"ALL {n} PIECE(S) FINISHED: {len(db.all('clips', 'job_id=?', (jid,)))} clip(s) from this video")


def _live(job, i, d):
    """Clip a stream WHILE it is live: record it in fixed-size chunks and process each chunk in the background.

    Big picture (one job, several threads):

        main loop (this function)   record chunk 0 --> record chunk 1 --> record chunk 2 --> ... until stream ends / Stop
                                          |                  |                  |
        worker threads                    +-> _process(0)    +-> _process(1)    +-> _process(2)    (clip, render, upload)

        chat thread (LiveChat)      collects the wall-clock time of every chat message the whole time

    So clips from chunk 0 can already be rendered/uploaded while chunk 1 is still being recorded.
    The job's shorts/longs counts apply PER CHUNK. Chunk length (chunk_minutes, default LIVE_CHUNK_SECONDS in .env)
    and how many chunks to clip (max_chunks, 0 = until the stream ends) are per job, set in the UI. The job row is read
    again before every chunk, so changes made on the Jobs page while it records apply from the next chunk.
    Live streams have no "most replayed" heatmap and no comments yet, so only audio + chat are used.
    """
    jid, stop = job["id"], STOPS[job["id"]]  # stop = the Event set by the UI's Stop button (see stop())

    # Chat runs in its own thread for the whole stream. It only stores timestamps (seconds since 1970)
    # of messages, not their text; a burst of messages = something just happened.
    # The chat thread gets its OWN stop flag: `stop` means "the user pressed Stop" to every piece still being
    # processed (_check), so it must never be set just because the recording is over.
    db.log(jid, "live: clips keep the job's layout with the original sound in sync and a short title on top matching the scene, no subtitles; "
                "the AI only picks the moments")
    chat_stop = threading.Event()
    chat = download.LiveChat(job["url"], chat_stop)
    chat.start()

    workers, k = [], 0  # k = chunk number
    while not stop.is_set():
        job = db.get("jobs", jid)  # fresh: chunk length / chunk count / clips per chunk can change while it records
        if job.get("max_chunks") and k >= job["max_chunks"]:
            db.log(jid, f"recorded {k} chunk(s), the number asked for: stopping the recording")
            break
        seconds = int((job.get("chunk_minutes") or 0) * 60) or LIVE_CHUNK_SECONDS
        out = d / f"live_{k:04d}.ts"  # .ts = raw MPEG-TS, safe to write while streaming (no header to finalise)
        db.update("jobs", jid, status=f"recording chunk {k + 1}" + (f"/{job['max_chunks']}" if job.get("max_chunks") else ""))
        db.log(jid, f"recording chunk {k + 1}: {seconds / 60:g} min, then {job['shorts']} shorts + {job['longs']} long from it")

        # Remember when this chunk starts (wall clock), so chat times can be turned into "seconds into this chunk".
        # Not exact: record_live_chunk first asks YouTube for fresh stream URLs (a few seconds) before ffmpeg
        # starts, so chat lands those few seconds late. CHAT_LAG and padding absorb most of it.
        t0 = time.time()

        # Blocks until the chunk is recorded: ffmpeg copies LIVE_CHUNK_SECONDS of the stream from the live edge
        # (no re-encode). Returns False if the stream is already over. If Stop is pressed mid-chunk, ffmpeg is
        # told to quit gracefully and the partial chunk is kept and still processed below.
        # Small gaps (a few seconds) between chunks are lost while the next recording starts.
        if not download.record_live_chunk(job["url"], out, seconds, stop, download.height_for(job.get("hq"))):
            db.log(jid, "stream ended")
            break
        if not out.exists() or out.stat().st_size == 0:  # ffmpeg produced nothing: stream died mid-chunk
            break
        db.log(jid, f"chunk {k + 1} recorded in {time.time() - t0:.0f}s ({out.stat().st_size / 1e6:.0f} MB), "
                    f"chat msgs so far: {len(chat.times)}"
                    + (f" (live chat not readable: {chat.error})" if chat.error else "") + ", processing in background")

        # ponytail: chunks are processed independently, so a moment straddling a boundary gets split.
        # Upgrade: overlap chunks by ~60s.

        # From the 2nd piece on, process it together with the last JOIN_SECONDS of the previous piece, so a moment
        # cut in half by the boundary can become one clip (joined across exactly two pieces, never more).
        src, boundary = out, 0
        if k > 0:
            try:
                src, boundary = _join_tail(d / f"live_{k - 1:04d}.ts", out, d / f"live_join_{k:04d}.mp4")
            except Exception as e:
                db.log(jid, f"[piece {k + 1}] couldn't join the previous piece's end ({e!r}), processing this piece alone")
        # Chat timestamps -> seconds into `src` (which starts `boundary` seconds before this piece). Messages outside
        # it become negative / past the end; signals.counts() ignores those.
        times = [t - (t0 - boundary) for t in chat.times]

        # Hand the chunk to _process in a background thread (same pipeline as a VOD: loudness + chat ->
        # hype curve -> transcribe -> DeepSeek -> render -> upload), then immediately go record the next chunk.
        # Transcription is behind a lock in transcribe.py, so chunks queue up there instead of fighting for CPU.
        job = db.get("jobs", jid)  # clips-per-piece changed while this piece recorded apply to it too
        w = threading.Thread(target=_process, args=(job, src, f"{i.get('title', '')} (part {k + 1})", d),
                             kwargs={"chat": times, "live": {"piece": k + 1, "boundary": boundary}}, daemon=True)
        w.start()
        workers.append(w)
        busy = [x for x in workers if x.is_alive()]
        db.log(jid, f"[piece {k + 1}] processing in the background"
                    + (f" with the previous piece's last {boundary:.0f}s joined in front" if boundary else "")
                    + f"; {len(busy)} piece(s) being processed at once while the next one records")
        k += 1

    # Stream is over, the pieces asked for are recorded, or Stop was pressed. End the chat thread (it checks its
    # flag after each new message; if chat goes quiet it lingers, a daemon thread that dies with the server).
    # Pieces still processing keep going unless the user really pressed Stop.
    stopped = stop.is_set()
    chat_stop.set()
    db.update("jobs", jid, status="finishing clips")
    if workers:
        db.log(jid, f"recording finished: waiting for {sum(w.is_alive() for w in workers)} piece(s) still being clipped")
    for w in workers:  # wait for every chunk's clips to finish, then _run() marks the job done/stopped
        w.join()
    db.log(jid, f"ALL {len(workers)} PIECE(S) FINISHED: {len(db.all('clips', 'job_id=?', (jid,)))} clip(s) from this stream")
    return stopped


JOIN_SECONDS = 60  # live: how much of the previous piece's end is put in front of the next piece
LIVE_HEAD_WORDS = 7  # reel header (live / clean style): the AI's headline, at most this many words incl. its emoji


def _join_tail(prev, cur, out):
    """[last JOIN_SECONDS of prev] + [cur] -> out (no re-encode). -> (out, seconds of prev at its start)."""
    from .config import FFMPEG
    dur = lambda f: float(subprocess.run([render.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                                          "csv=p=0", str(f)], capture_output=True, text=True).stdout.strip() or 0)
    pd = dur(prev)
    tail = min(JOIN_SECONDS, pd / 2)
    # The tail must start ON a video keyframe. `inpoint` is in the file's own timestamps (a live .ts starts at ~1.4s,
    # not 0); any other point keeps the audio from there but the video only from the next keyframe, so clips starting
    # in the overlap had seconds of sound over a stuck/black first frame.
    probe = lambda *a: subprocess.run([render.FFPROBE, "-v", "error", *a, str(prev)], capture_output=True, text=True).stdout
    try:
        t0 = float(probe("-show_entries", "format=start_time", "-of", "csv=p=0").strip() or 0)
    except ValueError:
        t0 = 0.0
    want = t0 + max(0.0, pd - tail)
    keys = [float(ln.split(",")[0]) for ln in probe("-select_streams", "v", "-read_intervals", f"{max(0.0, want - 20):.2f}%{want + 0.5:.2f}",
                                                     "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0").splitlines()
            if ",K" in ln and ln.split(",")[0].replace(".", "", 1).isdigit()]
    inpoint = max([k for k in keys if k <= want] or [want])
    lst = out.with_suffix(".txt")
    lst.write_text(f"file '{prev.name}'\ninpoint {inpoint:.6f}\nfile '{cur.name}'\n", encoding="utf-8")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy",
                    "-avoid_negative_ts", "make_zero", out.name], check=True, cwd=out.parent, capture_output=True)
    lst.unlink(missing_ok=True)
    return out, max(0.0, dur(out) - dur(cur))  # keyframes: the real tail can be a little longer than asked


def _process(job, src, title, d, heatmap=None, chat=None, comments=None, live=None):
    """Turn one video file (a whole VOD, or one recorded live chunk) into rendered clips.

    heatmap/chat/comments are optional audience signals (live chunks only have chat).
    job["mode"] decides who picks the moments:
      "hype"    - signals find candidate windows, DeepSeek picks among them (fast, cheap).
      "ai+hype" - DeepSeek reads the FULL transcript, signals are given as hints it may override.
      "ai"      - DeepSeek reads the FULL transcript only, no signals.
      "manual"  - only steps 1-2 (so the UI can show the curves); the user adds cuts via manual_cuts().
    """
    jid = job["id"]
    if job.get("campaign_id"):  # paid campaign: its own flow and prompts (campaign.process), not the viral-clip one
        from . import campaign
        try:
            return campaign.process(job, src, title, d)
        except Stopped:
            return db.log(jid, "stopped by you: no more clips from this part")
        except Exception as e:
            return db.log(jid, f"ERROR in the campaign flow for {src.name}: {e!r}", exc=True)
    mode = job.get("mode") or "hype"
    # live = {"piece": N, "boundary": seconds} for a live piece: the first `boundary` seconds of `src` are the end of
    # the previous piece (so a moment cut in half can be joined), and every piece makes at least one clip.
    tag = f"[piece {live['piece']}] " if live else ""
    B = live["boundary"] if live else 0

    def say(m, exc=False):
        db.log(jid, tag + m, exc=exc)
    try:
        # STEP 1: Loudness. ffmpeg's ebur128 filter measures "momentary loudness" in LUFS, the
        # broadcast unit for how loud audio *feels* to a person (not raw volume). We keep one value
        # per second, roughly -35 = calm talking, -10 = screaming/explosion, floored at -70 (silence).
        # len(audio) doubles as the video length in seconds.
        _check(jid)
        with db.step(jid, tag + f"STEP 1 analysing loudness of {src.name}"):
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
        say(f"STEP 2 hype curve: {n}s analysed, signals used: {', '.join(parts) or 'none'}, mode: {mode}")
        _save_curves(d, src, curve, parts)  # UI can show the graph while we keep working
        if mode == "manual":
            say("manual mode: add your cuts in the UI")
            return
        if job.get("whole") and not live:
            return _whole(job, src, title, d, parts, n, say)

        # STEP 3: Candidate windows. Slide a fixed-length window (60s shorts, 120s longs) over
        # the curve and greedily take the highest-scoring non-overlapping ones, 3x as many as requested
        # so DeepSeek has choices. Kinds are searched separately, so the same moment can be a candidate for
        # both kinds (a tight short AND a long with the bigger story). Each is padded (40s each side) so the AI can find a clean
        # start and end. In "ai" modes these are only used as a fallback if DeepSeek fails.
        # First cap the requested counts by what this video can hold (a 1-min video -> 1 short, no long).
        asked = {"short": job["shorts"], "long": job["longs"]}
        counts = pick.fit_counts(asked, n)
        if counts != asked:
            say(f"video is {n}s: making {counts['short']} short / {counts['long']} long "
                        f"(asked {asked['short']} / {asked['long']})")
        cands = []
        for kind, count in counts.items():
            length, pad = WINDOW[kind]
            if count <= 0:
                continue
            for s, e, sig in signals.top_windows(curve, length, count * 3):
                cands.append({"id": len(cands), "kind": kind, "start": max(0, s - pad),
                              "end": min(n, e + pad), "signal": round(sig, 3)})
        if B:
            before = len(cands)
            cands = [c for c in cands if c["end"] > B + 5]
            for i, c in enumerate(cands):
                c["id"] = i
            if before != len(cands):
                say(f"{before - len(cands)} candidate window(s) lie entirely in the previous piece's last {B:.0f}s: dropped")
        if not cands:
            say("video too short for requested clips")
            return
        say(f"STEP 3 candidate windows: {len(cands)} ("
                    + ", ".join(f"{c['kind']} {_mmss(c['start'])}-{_mmss(c['end'])} hype {c['signal']}" for c in cands[:12])
                    + (" ..." if len(cands) > 12 else "") + ")")

        # STEP 4: Transcribe with Whisper (word-level timestamps, GPU if available, else CPU).
        # "hype": only the candidate windows (overlaps merged so nothing is transcribed twice), fast.
        # "ai" modes: the whole video, so DeepSeek can find moments the curves missed. Slow on long videos.
        spans = [[0, n]] if mode != "hype" else transcribe.merge([(c["start"], c["end"]) for c in cands])
        words = []
        _check(jid)
        with db.step(jid, tag + f"STEP 4 transcribing {sum(e - s for s, e in spans):.0f}s in {len(spans)} part(s) "
                          f"({transcribe.device()})"):
            for s, e in spans:
                words += transcribe.words(src, s, e)
        say(f"transcript: {len(words)} words")
        if mode != "hype":  # whole transcript: where the streamer reacts ("oh my god", "no way", shouting)
            react = signals.reactions(words, n)
            if react.any():
                parts = {**parts, "reaction": react}
                peaks = sorted({int(t // 10 * 10) for t in np.flatnonzero(react >= 0.6)})
                say(f"streamer reactions: {len(peaks)} spot(s) with surprise / hype words"
                    + (f" ({', '.join(_mmss(t) for t in peaks[:10])}{' ...' if len(peaks) > 10 else ''})" if peaks else ""))
        for c in cands:  # "[12.0] hello there [17.1] ..." text per window, absolute seconds
            c["transcript"] = transcribe.text_with_times(words, c["start"], c["end"])

        # STEP 5: DeepSeek picks the final clips, trims them to sentence boundaries and writes
        # title/description/hashtags. Its numbers are clamped by pick.validate(). If the API fails,
        # fall back to the strongest raw hype windows so the job still produces something.
        _check(jid)
        pick.LAST.model, pick.LAST.link = "", ""
        try:
            with db.step(jid, tag + f"STEP 5 asking {pick.ai_name()} to pick {counts['short']} short / {counts['long']} long"):
                note, verdict = ((pick.live_note(B, title, live["piece"]) if live else "")
                                 + (pick.reel_note(job.get("channel") or "") if clean_style(job) else "")
                                 + pick.user_note(job.get("notes"))), {}
                if mode == "hype":
                    chosen = pick.choose(title, cands, counts, note, verdict)
                else:
                    chosen = pick.choose_full(title, n, transcribe.text_with_times(words, 0, n),
                                              parts if mode == "ai+hype" else ({"reaction": parts["reaction"]} if "reaction" in parts else None),
                                              counts, note, verdict)
        except Exception as e:
            say(f"AI failed ({e!r}), using raw hype windows")
            chosen = pick.fallback(cands, counts, title)
        if live:  # live pieces: DeepSeek's decision, spelled out. Every piece makes at least one clip.
            fresh = []
            for c in chosen:
                if c["end"] <= B + 3:
                    say(f"DECISION: dropped \"{c['title']}\": it ends inside the previous piece (already covered)")
                else:
                    fresh.append(c)
            best = max(fresh, key=lambda c: c["score"], default=None)
            kept = []
            for c in fresh:
                if c is best or c["score"] >= pick.LIVE_MIN:
                    kept.append(c)
                    if c is best and c["score"] < pick.LIVE_MIN:
                        say(f"DECISION: kept \"{c['title']}\" (virality {c['score']:.0f} < {pick.LIVE_MIN}): "
                            f"the best moment of this piece, every piece makes at least one clip")
                else:
                    say(f"DECISION: dropped \"{c['title']}\": virality {c['score']:.0f} < {pick.LIVE_MIN}, not worth a clip")
            chosen = kept
            if not chosen:  # the AI gave nothing usable: the strongest hype window of this piece instead
                one = {"short": 1, "long": 0} if counts.get("short") else {"short": 0, "long": 1}
                chosen = pick.fallback([c for c in cands if c["end"] > B + 5], one, title)
                if chosen:
                    chosen[0]["reason"] = "the strongest moment of this piece by the hype signal (the AI picked none)"
                    say(f"DECISION: the AI picked nothing"
                        + (f" ({verdict.get('skip')})" if verdict.get("skip") else "")
                        + ", using this piece's strongest hype window so it still makes one clip")
            if not chosen:
                say("DECISION: no usable moment in this piece (too short), skipped")
                _save_curves(d, src, curve, parts)
                _piece_done(jid, live["piece"], say)
                return
            lim = _best_limits(job)
            if job.get("auto_upload") and lim:  # held: only this piece's best N shorts / M longs go up when it's done
                for c in chosen:
                    c["upload"] = False
                say("DECISION: when this piece is done, upload its best "
                    + " + ".join(f"{'all' if lim.get(k) is None else lim[k]} {k}(s)" for k in ("short", "long")))
            joined = [c for c in chosen if c["start"] < B]
            say(f"DECISION: {len(chosen)} clip(s) from this piece"
                + (f", {len(joined)} JOINED across the previous piece and this one (starts {B - joined[0]['start']:.0f}s "
                   f"before the boundary)" if joined else ""))
        for c in chosen:
            say(f"picked {c['kind']} {_mmss(c['start'])}-{_mmss(c['end'])} score {c['score']}: "
                        f"\"{c['title']}\" [{c.get('headline', '')}] {' '.join('#' + t for t in c['tags'])} | {c['reason']}")
        # Snap every pick out to whole sentences (AI times are guesses from coarse markers), within the
        # video and never past the hard max (YouTube rejects Shorts over 3 min).
        # Then breathing room: CLIP_PAD_BEFORE before the first word, CLIP_PAD_AFTER after the last (the reaction lands).
        for c in chosen:
            s, e = transcribe.snap(words, c["start"], c["end"], reach=SNAP_REACH)
            s, e = max(0.0, s - CLIP_PAD_BEFORE), e + CLIP_PAD_AFTER
            s, e = s, min(e, n, s + pick.HARD_MAX[c["kind"]])
            if (s, e) != (c["start"], c["end"]):
                say(f"snapped to sentences: {_mmss(c['start'])}-{_mmss(c['end'])} -> {_mmss(s)}-{_mmss(e)} "
                            f"({e - s:.0f}s) \"{c['title']}\"")
            c["start"], c["end"] = s, e
        lim = _best_limits(job)
        if not live and job.get("auto_upload") and lim:  # a video: only its best N shorts / M longs go up, once all are made
            for c in chosen:
                c["upload"] = False
            say("DECISION: when all clips are made, upload the best "
                + " + ".join(f"{'all' if lim.get(k) is None else lim[k]} {k}(s)" for k in ("short", "long"))
                + "; the rest stay in Clips")
        _save_curves(d, src, curve, parts, chosen)  # now with the picked clips marked on the graph
        by = pick.LAST.model or "", pick.LAST.link or ""
        for c in chosen:
            c["why"] = _why(c, parts, words, mode, by, live, B, title)

        # STEP 6: Save each clip to the DB, render it with ffmpeg (every kind vertical 9:16), burn in the
        # headline (top) + captions (bottom) if the job asked for subtitles, then upload if auto-upload is on.
        _render_clips(job, src, d, chosen, words, job.get("subtitles", 1))
        if live:
            _upload_best(jid, live["piece"], say)
            _piece_done(jid, live["piece"], say)
        elif lim:
            _upload_best(jid, None, say)
    except Stopped:
        say("stopped by you: no more clips from this part")
    except Exception as e:
        say(f"ERROR processing {src.name}: {e!r}", exc=True)


def _whole(job, src, title, d, parts, n, say):
    """A short video from a channel (shorter than its "clip videos longer than" limit): posted WHOLE as one clip,
    formatted like the others (framing / layout, header, subtitles). The AI only writes its title, header,
    description and hashtags."""
    kind = "short" if n <= pick.HARD_MAX["short"] else "long"
    with db.step(job["id"], f"STEP 4 transcribing the whole video ({n}s, {transcribe.device()})"):
        words = transcribe.words(src, 0, n)
    c = {"kind": kind, "start": 0.0, "end": float(n), "score": 0, "title": title[:100], "headline": "",
         "description": title, "tags": [], "reason": "the whole video: shorter than the channel's clip limit, posted in full"}
    pick.LAST.model, pick.LAST.link = "", ""
    try:
        with db.step(job["id"], f"STEP 5 asking {pick.ai_name()} for the title, header and hashtags of the whole video"):
            note = ((pick.reel_note(job.get("channel") or "") if clean_style(job) else "") + pick.user_note(job.get("notes"))
                    + f"\n\nTHIS WHOLE VIDEO IS POSTED AS ONE CLIP under its own title (\"{title}\"): return exactly one "
                      f"{kind} with start 0 and end {min(n, pick.HARD_MAX[kind])}; your job is its headline, description "
                      f"and hashtags, all about the same topic as that title.\n")
            got = pick.choose_full(title, n, transcribe.text_with_times(words, 0, n), None,
                                   {"short": int(kind == "short"), "long": int(kind == "long")}, note, {})
        if got:
            c.update({k: got[0][k] for k in ("headline", "description", "tags", "score") if got[0].get(k)})
    except Exception as e:
        say(f"AI failed ({e!r}): posting it with the video's own title")
    c["start"], c["end"], c["title"] = 0.0, float(n), title[:100]  # the whole video keeps its own title
    c["why"] = {**_why(c, parts, words, "ai", (pick.LAST.model or "", pick.LAST.link or ""), None, 0, title), "whole": True}
    say(f"WHOLE VIDEO as one {kind} ({n}s): \"{c['title']}\" [{c.get('headline', '')}]")
    _render_clips(job, src, d, [c], words, job.get("subtitles", 1))


_SLOT = threading.Lock()


def post_or_queue(job, cid):
    """Auto-upload a finished clip: now, or, when the job has post_every (minutes; channel runs), on the next free
    slot of that queue: one clip every post_every minutes, in the order they're handed over (best first), and never
    before post_from (the job's "start posting at")."""
    every, start = float(job.get("post_every") or 0), float(job.get("post_from") or 0)
    if not every and start <= time.time():
        return upload_clip(cid)
    with _SLOT:  # two renders can finish at once: each gets its own slot
        at = max(time.time() + 30, float(db.get_setting("post_queue_next") or 0), start)
        db.set_setting("post_queue_next", str(at + every * 60))
    db.update("clips", cid, status="timed", post_at=at, post_to=job.get("platforms") or "[]")
    db.log(job["id"], f"clip #{cid} queued: posts {time.strftime('%d %b %H:%M', time.localtime(at))}"
                      + (f" (one clip every {every:g} min)" if every else ""))


def _best_limits(job):
    """{"short": N|None, "long": M|None} when "upload the best" is set for shorts and/or longs (None = all of that
    kind), or {} when both are empty (every clip uploads as soon as it's made)."""
    s, lo = job.get("upload_best"), job.get("upload_best_long")
    return {} if s is None and lo is None else {"short": s, "long": lo}


def _upload_best(jid, piece, say):
    """"Upload the best": right after a piece is clipped, upload THAT piece's best N shorts and best M longs (by the
    AI's score, best first) while the next piece records; the rest stay in Clips."""
    job = db.get("jobs", jid)
    lim = _best_limits(job)
    if not job.get("auto_upload") or not lim:
        return
    _check(jid)  # Stop pressed: upload nothing more
    mine = [c for c in db.all("clips", "job_id=? AND status='rendered'", (jid,)) if _piece_of(c) == piece]
    for kind in ("short", "long"):
        ready = sorted([c for c in mine if c["kind"] == kind], key=lambda c: -(c.get("score") or 0))
        if not ready:
            continue
        best = ready if lim.get(kind) is None else ready[:lim[kind]]
        say(f"UPLOADING THE BEST {len(best)} of {'the video' if piece is None else 'this piece'}'s {len(ready)} {kind}(s): "
            + (", ".join(f"#{c['id']} \"{c['title']}\" (score {c['score']:.0f})" for c in best) or "none")
            + (f"; the other {len(ready) - len(best)} stay in Clips" if len(ready) > len(best) else ""))
        for c in best:
            try:
                post_or_queue(job, c["id"])
            except Exception as e:
                say(f"clip #{c['id']} upload failed: {e!r}", exc=True)


def _piece_of(c):
    try:
        return (json.loads(c.get("why") or "{}") or {}).get("piece")
    except ValueError:
        return None


def _piece_done(jid, piece, say):
    """Log line when a piece is fully clipped: how many clips it made and what the job does next."""
    mine = db.all("clips", "job_id=?", (jid,))
    made = [c for c in mine if c.get("status") != "render failed" and _piece_of(c) == piece]
    total = len(mine)
    st = (db.get("jobs", jid) or {}).get("status") or ""
    nxt = (f"piece {st.split()[-1].split('/')[0]} is recording" if st.startswith("recording chunk") else
           f"now {st}" if st.startswith(("cutting piece", "clipping pieces")) else
           "finishing the other pieces" if st == "finishing clips" else st or "done")
    say(f"FINISHED: {len(made)} clip(s) ready in Clips ({total} from this job so far). Next: {nxt}")


def _render_clips(job, src, d, chosen, words, subtitles):
    """subtitles: burn the headline (top) and word captions (bottom) into the video."""
    # Two clips at a time: while one is in the GPU encoder, the other uses the CPU (decode, captions, voice-over).
    with ThreadPoolExecutor(RENDER_JOBS) as ex:
        for f in [ex.submit(_render_one, job, src, d, c, words, subtitles) for c in chosen]:
            f.result()  # re-raise the first failure so _process logs it


def _sentences(ws):
    """Words -> sentences (Whisper's punctuation)."""
    out, cur = [], []
    for w in ws:
        cur.append(w["w"])
        if w["w"].endswith((".", "?", "!")):
            out.append(" ".join(cur))
            cur = []
    return out + ([" ".join(cur)] if cur else [])


def _why(c, parts, words, mode, by, live, boundary, title):
    """Why this clip was chosen, for the Clips page: the AI's reasoning + score, who picked it (and its prompt), how
    the audience signals peaked inside it (1.0 = the top 5% of this video), and its opening and closing lines."""
    s, e = int(c["start"]), max(int(c["start"]) + 1, int(c["end"]))
    sig = {k: round(float(v[s:e].max()), 2) for k, v in (parts or {}).items() if len(v) > s}
    lines = _sentences([w for w in words if c["start"] <= w["s"] < c["end"]])
    model, link = by
    return {"by": model or ("hype signals only (the AI was unavailable)" if "LLM unavailable" in c.get("reason", "") else ""),
            "prompt": link, "mode": {"hype": "AI picked from the hype-curve candidates", "ai+hype": "AI read the full transcript, curves as hints",
                                     "ai": "AI read the full transcript"}.get(mode, mode),
            "score": c.get("score"), "signals": sig, "stretched": str(c.get("reason", "")).startswith("STRETCHED"),
            "opening": " ".join(lines[0].split()[:40]) if lines else "", "closing": " ".join(lines[-1].split()[:40]) if len(lines) > 1 else "",
            "piece": live["piece"] if live else None, "joined": bool(live and c["start"] < boundary), "source": title}


def clean_style(job):
    """Live streams always, videos when the job's style is "clean": original sound, short title on top, nothing else.
    Campaign jobs have their own flow."""
    return bool(job.get("is_live")) or job.get("style") == "clean"


def _render_one(job, src, d, c, words, subtitles):
    jid = job["id"]
    _check(jid)  # Stop pressed: don't start the next render
    clean = clean_style(job)
    # Clean style: a short title at the top matching the scene (the AI's headline); saved as the clip's headline too,
    # so the Clips page shows (and edits) what's on the video.
    head = pick.reel_headline(c.get("headline"), c.get("title"), LIVE_HEAD_WORDS) if clean else c.get("headline", "")
    cid = db.insert("clips", job_id=jid, kind=c["kind"], start=c["start"], end=c["end"], score=c["score"],
                    title=c["title"], description=c["description"], tags=json.dumps(c["tags"]),
                    reason=c["reason"], headline=head, why=json.dumps(c.get("why") or {}), status="rendering")
    out = d / "clips" / f"{cid}.mp4"
    if clean:
        # Live streams, and videos in the clean style (the reel look): the job's framing / layout (e.g. streamer on
        # top, gameplay below) with the ORIGINAL sound in sync, cut sync-safe (exact); the funny header (streamer +
        # what they're doing) at the top, the streamer's words at the bottom with the word being said in yellow.
        # No follow line, logo, end card or voice-over.
        with db.step(jid, f"STEP 6 cutting {c['kind']} #{cid} ({c['end'] - c['start']:.0f}s, original audio, "
                          f"header \"{head.upper()}\", subtitles): {c['title']}"):
            try:
                render.cut(src, out, c["start"], c["end"], True, words, head, (), job.get("aspect"), job.get("layout") or "",
                           follow=False, hook_text="", clean=True, exact=True, reel=True)
            except Exception:
                db.update("clips", cid, status="render failed")
                raise
        db.log(jid, f"clip #{cid} ready: {out.name}, {out.stat().st_size / 1e6:.1f} MB")
        if not job.get("is_live"):
            keep_master(jid, src, out, c["start"], c["end"], words)  # so its header can be changed later
        db.update("clips", cid, path=str(out.relative_to(DATA)), status="rendered")
        if job["auto_upload"] and c.get("upload", True):
            post_or_queue(job, cid)
        elif job["auto_upload"]:
            db.log(jid, f"clip #{cid} ready, waiting for the rest: only the best ones go up")
        return
    voice = _voiceover(job, c, words, out) if job.get("voiceover") else []
    try:
        with db.step(jid, f"STEP 6 rendering {c['kind']} #{cid} ({c['end'] - c['start']:.0f}s, {render.encoder_name()}): "
                          f"{c['title']}"):
            render.cut(src, out, c["start"], c["end"], True, words if subtitles else (),
                       c.get("headline", "") if subtitles else "", voice, job.get("aspect"), job.get("layout") or "",
                       follow=bool(job.get("follow", 1)))
    except Exception:
        db.update("clips", cid, status="render failed")
        raise
    finally:
        for wav, _ in voice:
            wav.unlink(missing_ok=True)  # voice lines are baked into the mp4 now
    db.log(jid, f"clip #{cid} ready: {out.name}, {out.stat().st_size / 1e6:.1f} MB")
    keep_master(jid, src, out, c["start"], c["end"], words)
    db.update("clips", cid, path=str(out.relative_to(DATA)), status="rendered")
    if job["auto_upload"] and not c.get("upload", True):  # long video in pieces: held for upload_best at the end
        db.log(jid, f"clip #{cid} ready, waiting for the rest of this piece: only its best ones go up")
    elif job["auto_upload"]:
        topic = db.get("topics", job["topic_id"]) if job.get("topic_id") else None
        if topic and topic.get("source") == "auto":  # autopilot.upload_tick() posts it on its slot of the day
            db.update("clips", cid, status="scheduled")
            db.log(jid, f"clip #{cid} scheduled: the autopilot posts it on the next free slot")
        else:
            post_or_queue(job, cid)


def keep_master(jid, src, out, start, end, words):
    """Keep <id>_master.mp4 (the clip's stretch of the source, no text) + <id>_words.json (its words, from 0), so the
    headline can be changed later from the Clips page even after the source video is deleted."""
    try:
        render.save_master(src, out.with_name(f"{out.stem}_master.mp4"), start, end)
        out.with_name(f"{out.stem}_words.json").write_text(json.dumps(
            [{**w, "s": round(w["s"] - start, 3), "e": round(w["e"] - start, 3)} for w in words if start <= w["s"] < end]))
    except Exception as e:
        db.log(jid, f"couldn't keep the clip's master copy ({e!r}): its headline can't be changed later")


EDIT_FIELDS = ("title", "description", "tags", "headline", "caption")


def edit_clip(cid, changes):
    """Your edits from the Clips page: title / description / hashtags / caption are saved as they are; a new
    headline (the text burned on the video) re-renders the clip in the background, from its master copy (or the
    job's source if it's still there). Raises ValueError when the headline can't be re-rendered (nothing is saved)."""
    c = db.get("clips", cid)
    if not c:
        raise ValueError("clip not found")
    job = db.get("jobs", c["job_id"]) or {}
    new = {k: v for k, v in changes.items() if k in EDIT_FIELDS and v is not None}
    if "tags" in new:
        new["tags"] = json.dumps([t for t in dict.fromkeys(str(t).strip().lstrip("#") for t in new["tags"]) if t][:15])
    redo = "headline" in new and new["headline"].strip() != (c.get("headline") or "")
    if redo:
        if c["status"] in ("rendering", "uploading"):
            raise ValueError(f"the clip is {c['status']}: try again when it's done")
        if not c.get("path"):
            raise ValueError("the clip's file is gone")
        if job.get("voiceover"):
            raise ValueError("this clip has an AI voice-over, which can't be rebuilt: the headline stays")
        if not _rerender_source(c, job):
            raise ValueError("the headline is burned into the video and this clip has no master copy (made before "
                             "edits existed) and its source video was deleted (KEEP_SOURCES=1 keeps it): title, "
                             "description and hashtags can still be changed")
        new["headline"] = new["headline"].strip()[:60]
    if new:
        db.update("clips", cid, **new)
    if redo:
        before = c["status"]
        db.update("clips", cid, status="rendering")
        threading.Thread(target=_rerender, args=(cid, before), daemon=True).start()
    if job.get("campaign_id"):
        from . import campaign
        campaign.recheck(cid)
    return db.get("clips", cid)


def _rerender_source(c, job):
    """-> (video, start, end, words or None) to render the clip from again, or None."""
    out = DATA / c["path"]
    master = out.with_name(f"{out.stem}_master.mp4")
    if master.exists():
        wf = master.with_name(f"{out.stem}_words.json")
        words = json.loads(wf.read_text()) if wf.exists() else []
        return master, 0.0, render._duration(master), words
    why = json.loads(c.get("why") or "{}")
    src = next((DATA / "jobs" / str(c["job_id"])).glob("source.*"), None)
    if src and not job.get("is_live") and why.get("piece") is None:  # a live / long-video piece's times aren't the source's
        return src, c["start"], c["end"], None
    return None


def _rerender(cid, before):
    c = db.get("clips", cid)
    job = db.get("jobs", c["job_id"]) or {}
    jid, out = c["job_id"], DATA / c["path"]
    src, s, e, words = _rerender_source(c, job)
    tmp = out.with_name(f"{out.stem}_edit.mp4")
    try:
        with db.step(jid, f"re-rendering #{cid} with the new headline \"{c['headline']}\""):
            if words is None:
                words = transcribe.words(src, s, e) if job.get("subtitles", 1) or clean_style(job) else []
                keep_master(jid, src, out, s, e, words)  # next time it renders from the master
                words = [w for w in words if s <= w["s"] < e]
            subs = words if job.get("subtitles", 1) else ()
            why = json.loads(c.get("why") or "{}")
            if job.get("campaign_id"):
                render.cut(src, tmp, s, e, True, subs, c["headline"], (), job.get("aspect"), job.get("layout") or "",
                           follow=False, hook_text=(why.get("campaign") or {}).get("onscreen_line") or "", clean=True)
            elif clean_style(job):  # same reel look as when it was made (header, yellow-word subtitles)
                render.cut(src, tmp, s, e, True, words, c["headline"], (), job.get("aspect"), job.get("layout") or "",
                           follow=False, hook_text="", clean=True, exact=True, reel=True)
            else:
                render.cut(src, tmp, s, e, True, subs, c["headline"], (), job.get("aspect"), job.get("layout") or "",
                           follow=bool(job.get("follow", 1)))
        tmp.replace(out)
        out.with_suffix(".ass").unlink(missing_ok=True)
        tmp.with_suffix(".ass").replace(out.with_suffix(".ass")) if tmp.with_suffix(".ass").exists() else None
        db.update("clips", cid, status=before)
        if json.loads(c.get("uploads") or "{}"):
            db.log(jid, f"#{cid}: the new version is only here; the copies already posted don't change")
    except Exception as ex:
        tmp.unlink(missing_ok=True)
        db.update("clips", cid, status=before)
        db.log(jid, f"re-rendering #{cid} failed, the old video stays: {ex!r}", exc=True)


def _voiceover(job, c, words, out):
    """AI voice-over for one clip: DeepSeek writes the lines, render.tts speaks them.
    -> [(wav, seconds into clip)], pushed later where needed so lines never talk over each other.
    Any failure just means no voice-over; the clip still renders."""
    jid = job["id"]
    if not render.can_speak():
        db.log(jid, "voice-over skipped: no text-to-speech (macOS 'say' / Windows PowerShell) found")
        return []
    s, e = c["start"], c["end"]
    rel = [{**w, "s": w["s"] - s, "e": w["e"] - s} for w in words if s <= w["s"] < e]  # times from clip start
    try:
        lines = pick.voice_script(job.get("title") or "", c["title"], e - s,
                                  transcribe.text_with_times(rel, 0, e - s, every=2))
    except Exception as ex:
        db.log(jid, f"voice-over skipped for '{c['title']}': {ex!r}")
        return []
    voice, free_at = [], 0.0
    for k, ln in enumerate(lines):
        at = max(ln["at"], free_at)  # wait for the previous line to finish
        if at >= e - s - 1:
            break
        wav = out.with_name(f"{out.stem}_vo{k}.wav")
        free_at = at + render.tts(ln["text"], wav) + 0.3
        voice.append((wav, at))
    db.log(jid, f"voice-over: {len(voice)} lines: " + " | ".join(f"{at:.1f}s \"{ln['text']}\""
                                                            for (_, at), ln in zip(voice, lines)))
    return voice


def manual_cuts(job_id, cuts, subtitles=True):
    """User-chosen cuts [{kind, start, end, title, headline, description, tags}] -> rendered clips, in the background.
    Times are used exactly as given (no sentence snapping): the user decided."""
    def run():
        job = db.get("jobs", job_id)
        d = DATA / "jobs" / str(job_id)
        db.log(job_id, f"manual cuts: {len(cuts)} ("
                       + ", ".join(f"{c['kind']} {_mmss(c['start'])}-{_mmss(c['end'])}" for c in cuts) + ")")
        try:
            src = next(d.glob("source.*"))
            words = []
            if subtitles or job.get("voiceover"):  # transcribe only if captions/voice-over need it (slow part)
                for s, e in transcribe.merge([(c["start"], c["end"]) for c in cuts]):
                    words += transcribe.words(src, s, e)
            _render_clips(job, src, d, [{"score": 0, "reason": "manual cut", "why": {"by": "you (a manual cut on the Jobs page)"}, **c} for c in cuts], words, subtitles)
        except Exception as e:
            db.log(job_id, f"ERROR in manual cuts: {e!r}", exc=True)
    threading.Thread(target=run, daemon=True).start()


def _mmss(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _describe(src):
    """'3840x2160 vp9 60fps, 13.6 min' for the log (ffprobe next to ffmpeg; '' if that fails)."""
    try:
        import subprocess
        from .config import FFMPEG
        probe = str(Path(FFMPEG).with_name("ffprobe" + Path(FFMPEG).suffix)) if Path(FFMPEG).parent != Path(".") else "ffprobe"
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=codec_name,width,height,r_frame_rate:format=duration", "-of", "json", str(src)],
                             capture_output=True, text=True, timeout=30).stdout
        j = json.loads(out)
        v, dur = j["streams"][0], float(j["format"]["duration"])
        num, den = (int(x) for x in v["r_frame_rate"].split("/"))
        return f"{v['width']}x{v['height']} {v['codec_name']} {num / den:.0f}fps, {dur / 60:.1f} min"
    except Exception:
        return ""


def _save_curves(d, src, curve, parts, chosen=()):
    """Write per-second curves (all 0..1) + picked clips for the UI graph: data/jobs/<id>/curves_<file>.json
    (not "source.*.json": that would match the glob used to find the source video)."""
    top = float(curve.max()) if len(curve) and curve.max() > 0 else 1.0
    data = {"name": src.name, "n": len(curve),
            "signals": {k: [round(float(x), 2) for x in v] for k, v in {"hype": curve / top, **parts}.items()},
            "chosen": [{k: c[k] for k in ("kind", "start", "end", "title")} for c in chosen]}
    out = d / f"curves_{src.stem}.json"
    tmp = out.with_suffix(".tmp")  # write-then-rename so the UI never reads a half-written file
    tmp.write_text(json.dumps(data))
    tmp.replace(out)


def upload_clip(cid, platforms=None, force=False):
    """Upload to `platforms` (default: the ones chosen for the job). Platforms that already worked are skipped,
    so pressing Upload again only retries the failed ones. force=True: try Instagram even over IG_MAX_SECONDS
    (the UI's red "Instagram (may fail)" button)."""
    c = db.get("clips", cid)
    job = db.get("jobs", c["job_id"]) or {}
    platforms = platforms or json.loads(job.get("platforms") or '["youtube"]') or ["youtube"]
    done = json.loads(c.get("uploads") or "{}")
    topic = db.get("topics", job["topic_id"]) if job.get("topic_id") else None
    auto, limited = bool(topic and topic.get("source") == "auto"), social.limit_timers()
    db.update("clips", cid, status="uploading")
    for p in platforms:
        if done.get(p, {}).get("id"):
            continue
        if auto and p in limited:  # autopilot only: manual uploads are always attempted (user's rule)
            db.log(c["job_id"], f"#{cid} not uploaded to {p} by autopilot: upload limit reached, timer ends "
                                f"{time.strftime('%H:%M', time.localtime(limited[p]['until']))}. Upload it later from the clip card.")
            continue
        if social.is_photo(c.get("path") or "") and p not in social.PHOTO_PLATFORMS:
            db.log(c["job_id"], f"#{cid} not uploaded to {p}: it's a photo, and {p}'s API only posts videos")
            continue
        if p == "instagram" and IG_MAX_SECONDS and c["end"] - c["start"] > IG_MAX_SECONDS and not force:
            db.log(c["job_id"], f"#{cid} not uploaded to instagram: {c['end'] - c['start']:.0f}s is over IG_MAX_SECONDS "
                                f"({IG_MAX_SECONDS:.0f}s, Instagram's API rejects longer Reels right now)")
            continue
        t = time.time()
        db.log(c["job_id"], f"uploading #{cid} to {p}...")
        try:
            done[p] = {**social.upload(p, DATA / c["path"], c["title"], c["description"],
                                       json.loads(c["tags"] or "[]"), c["end"] - c["start"],
                                       caption=c.get("caption") or None), "at": time.time()}
            db.log(c["job_id"], f"uploaded #{cid} to {p} in {time.time() - t:.0f}s -> {done[p]['url']}")
        except Exception as e:
            done[p] = {"error": social.error_text(e)[:500], "at": time.time()}  # shown as-is in the UI
            db.log(c["job_id"], f"upload #{cid} to {p} failed after {time.time() - t:.0f}s: {done[p]['error']}", exc=True)
            if social.note_limit(p, str(e)):
                db.log(c["job_id"], f"{p} says the upload limit is exceeded: 24h timer started")
        db.update("clips", cid, uploads=json.dumps(done))
    tried = list(done)  # every platform this clip was ever sent to, not just this call's (e.g. later "↑ Facebook")
    ok = [p for p in tried if done[p].get("id")]
    status = ("not uploaded (skipped)" if not tried else  # every platform skipped (limit / too long): nothing went up
              "uploaded" if len(ok) == len(tried) else f"uploaded {len(ok)}/{len(tried)}" if ok else "upload failed")
    db.update("clips", cid, status=status, youtube_id=done.get("youtube", {}).get("id"))
    if job.get("kind") in ("news", "fact"):  # also into the News page's run log
        from . import news
        news.note_upload(cid, {p: done[p] for p in platforms if p in done})
