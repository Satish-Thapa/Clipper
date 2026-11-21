"""The pipeline for one job: download / record -> signals -> transcribe -> AI pick -> render -> slots.

Three modes (jobs.mode):
  normal  a YouTube video or a file you upload: clipped once.
  live    a YouTube stream that is live now: recorded in pieces, each piece clipped while the next one records.
  auto    made by Auto mode (autopilot.py) for one of its topics (jobs.auto_row); otherwise like normal.

Every clip has the reel look: the full-screen 9:16 framing you chose (full / streamer on top / split), the topic in
big letters across the middle (jobs.header) and word-by-word subtitles at the bottom (jobs.subtitles). The AI always
reads the whole transcript and uses the audience signals (heatmap, chat, comments, loudness, reactions) as hints.

Nothing here posts by itself: a job with "fill today's free slots" (jobs.fill) hands its best clips to quota.fill when
they're made; Auto topics are filled by autopilot.py; everything else waits in Clips. Every upload goes through
upload_clip, which never lets a platform go over the day's quota (quota.can_post)."""
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import ai, db, download, pick, quota, render, settings, signals, social, transcribe
from .config import DATA

STOPS = {}  # job_id -> threading.Event
RENDER_JOBS = 2  # clips rendered at once (GPU encoder + CPU side by side)
WINDOW = {"short": (60, 40), "long": (120, 40)}  # (candidate length, transcript padding each side), seconds
JOIN_SECONDS = 60  # live / pieces: how much of the previous piece's end is put in front of the next piece
HEAD_WORDS = 7  # the header on the video: at most this many words incl. its emoji
FINISHED = ("done", "failed", "stopped", "interrupted")


def start(job_id):
    STOPS[job_id] = threading.Event()
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def run(job_id):
    """Same as start() but blocks until the job is finished (Auto clips one video at a time)."""
    STOPS[job_id] = threading.Event()
    _run(job_id)


class Stopped(Exception):
    """Raised at the next step boundary after Stop was pressed (see _check)."""


def _check(job_id):
    if STOPS.get(job_id) is not None and STOPS[job_id].is_set():
        raise Stopped()


def stop(job_id):
    """Stop a job now: the download is aborted, and every step checks the flag before it starts. Clips already
    rendered stay. A job with no thread any more (server restarted) is just marked stopped."""
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
            raise ValueError("the uploaded file was already deleted (Settings → Keep source videos keeps it); upload it again")
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


def describe_job(job):
    lay = job.get("layout") or ""
    frame = ("streamer on top" if lay.startswith("cam:") else f"split with {lay[6:]}" if lay.startswith("split:")
             else "full screen")
    return (f"{job.get('mode') or 'normal'} mode | {job['shorts']} shorts + {job['longs']} long"
            + (" per piece" if job.get("mode") == "live" else "") + f", {frame}, "
            f"header {'on' if job.get('header', 1) else 'off'}, subtitles {'on' if job.get('subtitles', 1) else 'off'}, "
            f"4K source + 4K clip, "
            + ("fills today's free slots with its best clips" if job.get("fill") else "clips wait in Clips"))


def _run(job_id):
    job = db.get("jobs", job_id)
    d = DATA / "jobs" / str(job_id)
    (d / "clips").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    db.log(job_id, f"job started: {job['url']} | {describe_job(job)}")
    if (job.get("notes") or "").strip():
        db.log(job_id, f"your instructions for the AI: {job['notes'].strip()}")
    try:
        if job["url"].startswith("file:"):  # uploaded from the UI: already at data/jobs/<id>/source.*
            src = next(d.glob("source.*"))
            db.log(job_id, f"local file: {job['title']}")
            _fill(job, _process(job, src, job["title"] or src.name, d))
            db.update("jobs", job_id, status="done")
            db.log(job_id, f"job finished in {(time.time() - t0) / 60:.1f} min")
            return
        db.update("jobs", job_id, status="fetching info")
        with db.step(job_id, "fetching video info"):
            i = download.info(job["url"])
        live = bool(i.get("is_live"))
        db.update("jobs", job_id, title=i.get("title"), is_live=int(live), channel=i.get("channel") or "")
        job.update(is_live=int(live), channel=i.get("channel") or "", title=i.get("title"))
        db.log(job_id, f"{'LIVE' if live else 'video'}: {i.get('title')} | channel {i.get('channel')}, "
                       f"{(i.get('duration') or 0) / 60:.1f} min, {i.get('view_count') or 0:,} views")
        mode = job.get("mode") or "normal"
        if i.get("live_status") == "is_upcoming":
            raise ValueError("this stream hasn't started yet")
        if mode == "live" and not live:
            raise ValueError("this video isn't live right now: Live mode only records live streams (use Normal mode)")
        if mode != "live" and live:
            raise ValueError("this is a live stream: use Live mode to clip it while it streams")
        if live:
            stopped = _live(job, i, d)
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


def _fill(job, cids):
    """Normal / live jobs with "fill today's free slots": their best new clips take today's free slots."""
    if job.get("fill") and job.get("mode") != "auto" and cids:
        quota.fill(cids, None, lambda m: db.log(job["id"], m))


def drop_sources(job_id):
    """Delete the job's source video (+ half-downloaded parts) and live recordings once it's finished: 0.5-2 GB each.
    Clips and their master copies stay. Settings → Keep source videos turns this off."""
    if settings.get().keep_sources:
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
        db.log(job_id, f"source video deleted ({freed / 1e6:.0f} MB freed)")


def _fail(job_id, e):
    if download.bot_checked(e):
        db.log(job_id, "ERROR: YouTube bot check ('Sign in to confirm you're not a bot'). YouTube wants a signed-in "
                       "browser: add YouTube cookies on the Socials page, then run it again.")
    elif isinstance(e, ValueError):
        db.log(job_id, f"ERROR: {e}")
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
    long_min = settings.get().long_video_minutes
    if long_min and total >= long_min * 60:
        made = _pieces(job, src, total, d, i.get("title", ""), i.get("heatmap"), chat, comments)
    else:
        made = _process(job, src, i.get("title", ""), d, heatmap=i.get("heatmap"), chat=chat, comments=comments)
    _fill(job, made)


def _dur(f):
    out = subprocess.run([render.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _pieces(job, src, total, d, title, heatmap, chat, comments):
    """A long video (Settings → long_video_minutes+) clipped like a live stream: cut into pieces of the live piece
    length, each with the previous piece's last JOIN_SECONDS in front (a moment on a boundary can still be one clip),
    and each piece processed on its own (two at a time), with shorts/longs PER PIECE and at least one clip per piece.
    The video's heatmap, chat replay and comment times are shifted to each piece's own clock. -> the clip ids."""
    from .config import FFMPEG
    jid, stop = job["id"], STOPS[job["id"]]
    size = int((job.get("chunk_minutes") or settings.get().live_chunk_minutes) * 60)
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

    made = []
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
            made += f.result() or []
    db.log(jid, f"ALL {n} PIECE(S) FINISHED: {len(made)} clip(s) from this video")
    return made


def _live(job, i, d):
    """Clip a stream WHILE it is live: record it in fixed-size chunks and process each chunk in the background.

        main loop (this function)   record chunk 0 --> record chunk 1 --> record chunk 2 --> ... until stream ends / Stop
                                          |                  |                  |
        worker threads                    +-> _process(0)    +-> _process(1)    +-> _process(2)    (clip, render, slots)
        chat thread (LiveChat)      collects the wall-clock time of every chat message the whole time

    The job's shorts/longs apply PER CHUNK. Chunk length / how many chunks are per job; the job row is read again
    before every chunk, so changes made on the Jobs page while it records apply from the next chunk.
    -> True only when Stop was pressed."""
    jid, stop = job["id"], STOPS[job["id"]]
    chat_stop = threading.Event()  # its own flag: `stop` means "you pressed Stop" to the pieces still processing
    chat = download.LiveChat(job["url"], chat_stop)
    chat.start()
    workers, k = [], 0

    def piece(job, src, title, times, live):
        _fill(job, _process(job, src, title, d, chat=times, live=live))

    while not stop.is_set():
        job = db.get("jobs", jid)  # fresh: chunk length / chunk count / clips per chunk can change while it records
        if job.get("max_chunks") and k >= job["max_chunks"]:
            db.log(jid, f"recorded {k} chunk(s), the number asked for: stopping the recording")
            break
        seconds = int((job.get("chunk_minutes") or settings.get().live_chunk_minutes) * 60)
        out = d / f"live_{k:04d}.ts"  # .ts = raw MPEG-TS, safe to write while streaming (no header to finalise)
        db.update("jobs", jid, status=f"recording chunk {k + 1}" + (f"/{job['max_chunks']}" if job.get("max_chunks") else ""))
        db.log(jid, f"recording chunk {k + 1}: {seconds / 60:g} min, then {job['shorts']} shorts + {job['longs']} long from it")
        t0 = time.time()  # chat times -> seconds into this chunk (a few seconds late; CHAT_LAG and padding absorb it)
        if not download.record_live_chunk(job["url"], out, seconds, stop, download.height_for(job.get("hq"))):
            db.log(jid, "stream ended")
            break
        if not out.exists() or out.stat().st_size == 0:  # ffmpeg produced nothing: stream died mid-chunk
            break
        db.log(jid, f"chunk {k + 1} recorded in {time.time() - t0:.0f}s ({out.stat().st_size / 1e6:.0f} MB), "
                    f"chat msgs so far: {len(chat.times)}"
                    + (f" (live chat not readable: {chat.error})" if chat.error else "") + ", processing in background")
        # From the 2nd piece on: processed with the last JOIN_SECONDS of the previous piece in front, so a moment cut
        # in half by the boundary can become one clip (joined across exactly two pieces, never more).
        src, boundary = out, 0
        if k > 0:
            try:
                src, boundary = _join_tail(d / f"live_{k - 1:04d}.ts", out, d / f"live_join_{k:04d}.mp4")
            except Exception as e:
                db.log(jid, f"[piece {k + 1}] couldn't join the previous piece's end ({e!r}), processing this piece alone")
        times = [t - (t0 - boundary) for t in chat.times]
        w = threading.Thread(target=piece, args=(db.get("jobs", jid), src, f"{i.get('title', '')} (part {k + 1})", times,
                                                 {"piece": k + 1, "boundary": boundary}), daemon=True)
        w.start()
        workers.append(w)
        db.log(jid, f"[piece {k + 1}] processing in the background"
                    + (f" with the previous piece's last {boundary:.0f}s joined in front" if boundary else "")
                    + f"; {len([x for x in workers if x.is_alive()])} piece(s) being processed while the next one records")
        k += 1
    stopped = stop.is_set()
    chat_stop.set()
    db.update("jobs", jid, status="finishing clips")
    for w in workers:
        w.join()
    db.log(jid, f"ALL {len(workers)} PIECE(S) FINISHED: {len(db.all('clips', 'job_id=?', (jid,)))} clip(s) from this stream")
    return stopped


def _join_tail(prev, cur, out):
    """[last JOIN_SECONDS of prev] + [cur] -> out (no re-encode). -> (out, seconds of prev at its start)."""
    from .config import FFMPEG
    dur = lambda f: float(subprocess.run([render.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                                          "csv=p=0", str(f)], capture_output=True, text=True).stdout.strip() or 0)
    pd = dur(prev)
    tail = min(JOIN_SECONDS, pd / 2)
    # The tail must start ON a video keyframe. `inpoint` is in the file's own timestamps (a live .ts starts at ~1.4s,
    # not 0); any other point keeps the audio from there but the video only from the next keyframe.
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
    """Turn one video file (a whole video, or one piece of a stream / long video) into rendered clips. -> clip ids.

    The AI reads the WHOLE transcript and gets the audience signals as hints (heatmap, chat, comments, loudness,
    streamer reactions); the strongest raw hype windows are the fallback when the AI fails.
    live = {"piece": N, "boundary": seconds} for a piece: its first `boundary` seconds are the end of the previous
    piece (so a moment cut in half can be joined), and every piece makes at least one clip."""
    jid = job["id"]
    tag = f"[piece {live['piece']}] " if live else ""
    B = live["boundary"] if live else 0
    s = settings.get()

    def say(m, exc=False):
        db.log(jid, tag + m, exc=exc)
    try:
        # STEP 1: loudness (ebur128 momentary LUFS, one value per second; its length = the video's seconds).
        _check(jid)
        with db.step(jid, tag + f"STEP 1 analysing loudness of {src.name}"):
            audio = signals.loudness(src)
        n = len(audio)
        # STEP 2: the hype curve: heatmap x3, chat x2, comments x1.5, loudness x1, each normalised 0..1.
        curve, parts = signals.hype_curve(n, audio, heatmap, chat, comments)
        say(f"STEP 2 hype curve: {n}s analysed, signals used: {', '.join(parts) or 'none'}")
        _save_curves(d, src, curve, parts)  # the Jobs page draws it while the rest runs
        # STEP 3: candidate windows from the curve: only the fallback when the AI fails.
        asked = {"short": job["shorts"], "long": job["longs"]}
        counts = pick.fit_counts(asked, n)
        if counts != asked:
            say(f"video is {n}s: making {counts['short']} short / {counts['long']} long (asked {asked['short']} / {asked['long']})")
        cands = []
        for kind, count in counts.items():
            length, pad = WINDOW[kind]
            for st, e, sig in signals.top_windows(curve, length, count * 3) if count > 0 else []:
                if e + pad > B + 5:  # a window entirely in the previous piece's tail was clipped already
                    cands.append({"id": len(cands), "kind": kind, "start": max(0, st - pad), "end": min(n, e + pad),
                                  "signal": round(sig, 3)})
        if not cands:
            say("video too short for the clips asked")
            return []
        # STEP 4: Whisper on the whole video (word timestamps), then where the streamer reacts.
        _check(jid)
        with db.step(jid, tag + f"STEP 4 transcribing {n}s ({transcribe.device()})"):
            words = transcribe.words(src, 0, n)
        say(f"transcript: {len(words)} words")
        react = signals.reactions(words, n)
        if react.any():
            parts = {**parts, "reaction": react}
            peaks = sorted({int(t // 10 * 10) for t in np.flatnonzero(react >= 0.6)})
            say(f"streamer reactions: {len(peaks)} spot(s) with surprise / hype words"
                + (f" ({', '.join(_mmss(t) for t in peaks[:10])}{' ...' if len(peaks) > 10 else ''})" if peaks else ""))
        # STEP 5: the AI picks from the full transcript, the curves as hints; numbers clamped by pick.validate.
        _check(jid)
        ai.LAST.model, ai.LAST.link = "", ""
        verdict = {}
        try:
            with db.step(jid, tag + f"STEP 5 asking {ai.ai_name()} to pick {counts['short']} short / {counts['long']} long"):
                note = ((pick.live_note(B, title, live["piece"]) if live else "")
                        + pick.reel_note(job.get("channel") or "") + pick.category_note(job.get("category"))
                        + pick.learned_note() + ai.user_note(_notes(job, s)))
                chosen = pick.choose_full(title, n, transcribe.text_with_times(words, 0, n), parts, counts, note, verdict)
        except Exception as e:
            say(f"AI failed ({e!r}), using raw hype windows")
            chosen = pick.fallback(cands, counts, title)
        pick.confirm(chosen, parts)  # +5 where 2+ audience signals (heatmap / chat / comments) peak inside the clip
        if live:
            chosen = _keep_for_piece(chosen, cands, counts, title, B, verdict, say)
            if not chosen:
                return []
        for c in chosen:
            say(f"picked {c['kind']} {_mmss(c['start'])}-{_mmss(c['end'])} score {c['score']}"
                + (f" (+5: {' + '.join(c['confirmed'])} agree)" if c.get("confirmed") else "")
                + (" (+3: opens on a scream)" if c.get("scream_hook") else "")
                + (f", {(c.get('meta') or {}).get('moment')}" if (c.get("meta") or {}).get("moment") else "")
                + f": \"{c['title']}\" [{c.get('headline', '')}] {' '.join('#' + t for t in c['tags'])} | {c['reason']}")
        # Snap every pick out to whole sentences, then breathing room, never past the hard max (Shorts: 3 min).
        for c in chosen:
            st, e = transcribe.snap(words, c["start"], c["end"], reach=s.snap_reach)
            st, e = max(0.0, st - s.pad_before), e + s.pad_after
            e = min(e, n, st + pick.hard_max()[c["kind"]])
            if (st, e) != (c["start"], c["end"]):
                say(f"snapped to sentences: {_mmss(c['start'])}-{_mmss(c['end'])} -> {_mmss(st)}-{_mmss(e)} "
                    f"({e - st:.0f}s) \"{c['title']}\"")
            c["start"], c["end"] = st, e
        by = ai.LAST.model or "", ai.LAST.link or ""
        for c in chosen:
            c["why"] = _why(c, parts, words, by, live, B, title)
        _save_curves(d, src, curve, parts, chosen)  # now with the picked clips marked on the graph
        # STEP 6: render each clip (two at a time).
        return _render_clips(job, src, d, chosen, words)
    except Stopped:
        say("stopped by you: no more clips from this part")
    except Exception as e:
        say(f"ERROR processing {src.name}: {e!r}", exc=True)
    return []


def _notes(job, s):
    """Tell the AI: the Settings text, then this video's own (the more specific one last, so it wins)."""
    return "\n".join(x for x in ((s.notes or "").strip(), (job.get("notes") or "").strip()) if x)


def _keep_for_piece(chosen, cands, counts, title, B, verdict, say):
    """A live / long-video piece: drop what ends inside the previous piece, keep the best one always and the others
    only at virality LIVE_MIN+; nothing usable -> the piece's strongest hype window."""
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
                say(f"DECISION: kept \"{c['title']}\" (virality {c['score']:.0f} < {pick.LIVE_MIN}): the best moment "
                    f"of this piece, every piece makes at least one clip")
        else:
            say(f"DECISION: dropped \"{c['title']}\": virality {c['score']:.0f} < {pick.LIVE_MIN}, not worth a clip")
    if not kept:
        one = {"short": 1, "long": 0} if counts.get("short") else {"short": 0, "long": 1}
        kept = pick.fallback([c for c in cands if c["end"] > B + 5], one, title)
        if kept:
            kept[0]["reason"] = "the strongest moment of this piece by the hype signal (the AI picked none)"
            say("DECISION: the AI picked nothing" + (f" ({verdict.get('skip')})" if verdict.get("skip") else "")
                + ", using this piece's strongest hype window so it still makes one clip")
        else:
            say("DECISION: no usable moment in this piece (too short), skipped")
    joined = [c for c in kept if c["start"] < B]
    if kept:
        say(f"DECISION: {len(kept)} clip(s) from this piece" + (f", {len(joined)} JOINED across the previous piece and "
                                                              f"this one" if joined else ""))
    return kept


def _render_clips(job, src, d, chosen, words):
    """Two clips at a time: while one is in the GPU encoder, the other uses the CPU. -> the new clip ids."""
    with ThreadPoolExecutor(RENDER_JOBS) as ex:
        futures = [ex.submit(_render_one, job, src, d, c, words) for c in chosen]
        return [cid for cid in (f.result() for f in futures) if cid]  # re-raises the first failure for _process


def _sentences(ws):
    """Words -> sentences (Whisper's punctuation)."""
    out, cur = [], []
    for w in ws:
        cur.append(w["w"])
        if w["w"].endswith((".", "?", "!")):
            out.append(" ".join(cur))
            cur = []
    return out + ([" ".join(cur)] if cur else [])


def _why(c, parts, words, by, live, boundary, title):
    """Why this clip was chosen, for the Clips page: the AI's reasoning + score, who picked it (and its prompt), how
    the audience signals peaked inside it (1.0 = the top 5% of this video), and its opening and closing lines."""
    s, e = int(c["start"]), max(int(c["start"]) + 1, int(c["end"]))
    sig = {k: round(float(v[s:e].max()), 2) for k, v in (parts or {}).items() if len(v) > s}
    lines = _sentences([w for w in words if c["start"] <= w["s"] < c["end"]])
    model, link = by
    return {"by": model or ("hype signals only (the AI was unavailable)" if "LLM unavailable" in c.get("reason", "") else ""),
            "prompt": link, "mode": "AI read the full transcript, the signals as hints",
            "score": c.get("score"), "signals": sig, "stretched": str(c.get("reason", "")).startswith("STRETCHED"),
            "opening": " ".join(lines[0].split()[:40]) if lines else "", "closing": " ".join(lines[-1].split()[:40]) if len(lines) > 1 else "",
            "piece": live["piece"] if live else None, "joined": bool(live and c["start"] < boundary), "source": title,
            **(c.get("meta") or {}), "confirmed": c.get("confirmed") or [], "scream_hook": bool(c.get("scream_hook"))}


def _render_one(job, src, d, c, words):
    jid = job["id"]
    _check(jid)  # Stop pressed: don't start the next render
    head = pick.reel_headline(c.get("headline"), c.get("title"), HEAD_WORDS) if job.get("header", 1) else ""
    cid = db.insert("clips", job_id=jid, kind=c["kind"], start=c["start"], end=c["end"], score=c["score"],
                    title=c["title"], description=c["description"], tags=json.dumps(c["tags"]),
                    reason=c["reason"], headline=head, why=json.dumps(c.get("why") or {}), status="rendering")
    out = d / "clips" / f"{cid}.mp4"
    subs = words if job.get("subtitles", 1) else ()
    with db.step(jid, f"STEP 6 cutting {c['kind']} #{cid} ({c['end'] - c['start']:.0f}s, "
                      f"header \"{head.upper() or 'off'}\", subtitles {'on' if subs else 'off'}): {c['title']}"):
        try:
            render.cut(src, out, c["start"], c["end"], subs, head, job.get("layout") or "")
        except Exception:
            db.update("clips", cid, status="render failed")
            raise
    db.log(jid, f"clip #{cid} ready: {out.name}, {out.stat().st_size / 1e6:.1f} MB")
    keep_master(jid, src, out, c["start"], c["end"], words)  # so its header can be changed later
    try:  # the light copy + poster the app plays, made now so scrolling the reels never waits for an encode
        render.preview(out, d / "previews", str(cid))
        render.preview(out, d / "previews", str(cid), poster=True)
    except Exception as e:
        db.log(jid, f"clip #{cid}: couldn't make its preview now ({e!r}); it's made when first played")
    db.update("clips", cid, path=str(out.relative_to(DATA)), status="rendered")
    return cid


def keep_master(jid, src, out, start, end, words):
    """Keep <id>_master.mp4 (the clip's stretch of the source, no text) + <id>_words.json (its words, from 0), so the
    header can be changed later from the Clips page even after the source video is deleted."""
    try:
        render.save_master(src, out.with_name(f"{out.stem}_master.mp4"), start, end)
        out.with_name(f"{out.stem}_words.json").write_text(json.dumps(
            [{**w, "s": round(w["s"] - start, 3), "e": round(w["e"] - start, 3)} for w in words if start <= w["s"] < end]))
    except Exception as e:
        db.log(jid, f"couldn't keep the clip's master copy ({e!r}): its header can't be changed later")


EDIT_FIELDS = ("title", "description", "tags", "headline")


def edit_clip(cid, changes):
    """Your edits from the Clips page: title / description / hashtags are saved as they are; a new header (the text
    burned on the video) re-renders the clip in the background from its master copy. Raises ValueError when the
    header can't be re-rendered (then nothing is saved)."""
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
        if quota.posted_ok(c):
            raise ValueError("this clip is already posted: the header on the posted copies can't change")
        if not c.get("path"):
            raise ValueError("the clip's file is gone")
        if not _rerender_source(c, job):
            raise ValueError("this clip has no master copy and its source video was deleted: title, description and "
                             "hashtags can still be changed")
        new["headline"] = new["headline"].strip()[:60]
    if new:
        db.update("clips", cid, **new)
    if redo:
        before = c["status"]
        db.update("clips", cid, status="rendering")
        threading.Thread(target=_rerender, args=(cid, before), daemon=True).start()
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
        with db.step(jid, f"re-rendering #{cid} with the new header \"{c['headline']}\""):
            if words is None:
                words = transcribe.words(src, s, e)
                keep_master(jid, src, out, s, e, words)  # next time it renders from the master
                words = [w for w in words if s <= w["s"] < e]
            render.cut(src, tmp, s, e, words if job.get("subtitles", 1) else (), c["headline"], job.get("layout") or "")
        tmp.replace(out)
        out.with_suffix(".ass").unlink(missing_ok=True)
        if tmp.with_suffix(".ass").exists():
            tmp.with_suffix(".ass").replace(out.with_suffix(".ass"))
        db.update("clips", cid, status=before)
    except Exception as ex:
        tmp.unlink(missing_ok=True)
        db.update("clips", cid, status=before)
        db.log(jid, f"re-rendering #{cid} failed, the old video stays: {ex!r}", exc=True)


def _save_curves(d, src, curve, parts, chosen=()):
    """Per-second curves (all 0..1) + the picked clips for the Jobs page graph: data/jobs/<id>/curves_<file>.json
    (not "source.*.json": that would match the glob that finds the source video)."""
    top = float(curve.max()) if len(curve) and curve.max() > 0 else 1.0
    data = {"name": src.name, "n": len(curve),
            "signals": {k: [round(float(x), 2) for x in v] for k, v in {"hype": curve / top, **parts}.items()},
            "chosen": [{k: c[k] for k in ("kind", "start", "end", "title")} for c in chosen]}
    out = d / f"curves_{src.stem}.json"
    tmp = out.with_suffix(".tmp")  # write-then-rename so the page never reads a half-written file
    tmp.write_text(json.dumps(data))
    tmp.replace(out)


def _mmss(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _describe(src):
    """'3840x2160 vp9 60fps, 13.6 min' for the log ('' if ffprobe fails)."""
    try:
        out = subprocess.run([render.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=codec_name,width,height,r_frame_rate:format=duration", "-of", "json", str(src)],
                             capture_output=True, text=True, timeout=30).stdout
        j = json.loads(out)
        v, dur = j["streams"][0], float(j["format"]["duration"])
        num, den = (int(x) for x in v["r_frame_rate"].split("/"))
        return f"{v['width']}x{v['height']} {v['codec_name']} {num / den:.0f}fps, {dur / 60:.1f} min"
    except Exception:
        return ""


def upload_clip(cid, platforms=None, force=False, bypass=False):
    """Post a clip to `platforms` (default: where its kind posts, Settings: shorts / longs). Platforms that already have it are skipped, so
    pressing it again only retries the failed ones.

    The daily quota is checked here for EVERY platform (quota.can_post): this is the one path every upload takes
    (slots, Post now, retries), so no platform ever gets more clips of a kind in a day than that day's slots.
    A platform at its own API limit (social.limit_timers) is skipped unless force. force also tries Instagram on
    clips over Settings → ig_max_seconds. If nothing went up anywhere, the clip gives its slot back.
    bypass (the Clips page's Force upload, your explicit choice): skips the daily quota too; it's logged as such."""
    c = db.get("clips", cid)
    s = settings.get()
    platforms = platforms or settings.platforms_for(c["kind"], s)
    done = json.loads(c.get("uploads") or "{}")
    limited = social.limit_timers()
    db.update("clips", cid, status="uploading")
    for p in platforms:
        if done.get(p, {}).get("id"):
            continue
        why = "" if bypass else quota.can_post(c, p)
        if bypass and quota.can_post(c, p):
            db.log(c["job_id"], f"#{cid} FORCE UPLOAD to {p}, past the daily quota ({quota.can_post(c, p)})")
        if why:
            db.log(c["job_id"], f"#{cid} not posted to {p}: {why}")
            continue
        if p in limited and not force:
            db.log(c["job_id"], f"#{cid} not posted to {p}: its upload limit was hit, the timer ends "
                                f"{time.strftime('%H:%M', time.localtime(limited[p]['until']))}")
            continue
        if social.is_photo(c.get("path") or "") and p not in social.PHOTO_PLATFORMS:
            db.log(c["job_id"], f"#{cid} not posted to {p}: it's a photo, and {p}'s API only posts videos")
            continue
        if p == "instagram" and s.ig_max_seconds and c["end"] - c["start"] > s.ig_max_seconds and not force:
            db.log(c["job_id"], f"#{cid} not posted to instagram: {c['end'] - c['start']:.0f}s is over the "
                                f"{s.ig_max_seconds:.0f}s Instagram limit in Settings")
            continue
        t = time.time()
        db.log(c["job_id"], f"uploading #{cid} to {p}...")
        try:
            done[p] = {**social.upload(p, DATA / c["path"], c["title"], c["description"],
                                       json.loads(c["tags"] or "[]"), c["end"] - c["start"]), "at": time.time()}
            db.log(c["job_id"], f"uploaded #{cid} to {p} in {time.time() - t:.0f}s -> {done[p]['url']}")
        except Exception as e:
            done[p] = {"error": social.error_text(e)[:500], "at": time.time()}  # shown as-is in the UI
            db.log(c["job_id"], f"upload #{cid} to {p} failed after {time.time() - t:.0f}s: {done[p]['error']}", exc=True)
            if social.note_limit(p, str(e)):
                db.log(c["job_id"], f"{p} says the upload limit is exceeded: 24h timer started")
        db.update("clips", cid, uploads=json.dumps(done))
    ok = [p for p in done if done[p].get("id")]
    status = ("uploaded" if ok and len(ok) == len(done) else f"uploaded {len(ok)}/{len(done)}" if ok else
              "upload failed" if done else "rendered")
    db.update("clips", cid, status=status, youtube_id=done.get("youtube", {}).get("id"),
              **({} if ok else {"post_at": None}))  # nothing went up: the slot is free again
    if not ok:
        db.log(c["job_id"], f"#{cid} went up nowhere: it waits in Clips again"
                            + (" and its slot is free" if c.get("post_at") else ""))
    if (db.get("jobs", c["job_id"]) or {}).get("kind") in ("news", "fact"):  # also into the News page's run log
        from . import news
        news.note_upload(cid, {p: done[p] for p in platforms if p in done})
