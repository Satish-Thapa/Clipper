"""Background pipeline: download/record -> signals -> transcribe -> DeepSeek pick -> render -> upload."""
import json
import re
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db, download, engage, pick, render, signals, social, transcribe
from . import config
from .config import DATA

STOPS = {}  # job_id -> threading.Event
RENDER_JOBS = 2  # clips rendered at once (GPU encoder + CPU side by side)
WINDOW = {"short": (60, 40), "long": (120, 40)}  # (candidate length, transcript padding each side), seconds
FINISHED = ("done", "failed", "stopped", "interrupted")


class Stopped(Exception):
    """Raised at the next checkpoint after the Stop button: the job ends as 'stopped', not 'failed'."""


def check(job_id):
    """A checkpoint: between steps, transcription parts and clips. Raises Stopped if Stop was pressed."""
    if job_id in STOPS and STOPS[job_id].is_set():
        raise Stopped()


def start(job_id):
    STOPS[job_id] = threading.Event()
    threading.Thread(target=_run, args=(job_id,), daemon=True, name=f"job-{job_id}").start()


def run(job_id):
    """Same as start() but blocks until the job is finished (the topic queue clips one video at a time)."""
    STOPS[job_id] = threading.Event()
    _run(job_id)


def stop(job_id):
    """Stop takes effect within seconds: a running download is cancelled, and the pipeline quits at its next
    checkpoint (a clip already in the encoder finishes; the rest are skipped). The status shows 'stopping' at once."""
    job = db.get("jobs", job_id)
    if not job or job["status"] in FINISHED:
        return
    if job_id in STOPS and not STOPS[job_id].is_set():
        STOPS[job_id].set()
        db.update("jobs", job_id, status="stopping")
        db.log(job_id, "stop requested: stopping at the next step")
    elif job_id not in STOPS:  # no thread behind it (e.g. left over from before a restart): just mark it
        db.update("jobs", job_id, status="stopped")
        db.log(job_id, "stopped (it wasn't running)")


def restart(job_id):
    """Start over: stop the job if it's running (waits for it), delete its clips that went nowhere yet (posted ones
    stay), and run it again from scratch on the same video. The downloaded source is kept, so it doesn't download
    again; a live stream's old recording is dropped and it records again from now."""
    job = db.get("jobs", job_id)
    if not job:
        return
    stop(job_id)
    for _ in range(240):  # a clip already in the encoder finishes first: up to 2 min
        if (db.get("jobs", job_id) or {}).get("status") in FINISHED and not _running(job_id):
            break
        time.sleep(0.5)
    else:
        db.log(job_id, "start over: it didn't stop in time, try again in a minute")
        return
    d = DATA / "jobs" / str(job_id)
    gone = 0
    for c in db.all("clips", "job_id=?", (job_id,)):
        if any(u.get("id") for _, u in social.entries(json.loads(c.get("uploads") or "{}"))):
            continue  # already posted somewhere: kept
        for f in (d / "clips").glob(f"{c['id']}.*"):
            f.unlink(missing_ok=True)
        db.delete("clips", c["id"])
        gone += 1
    if job.get("is_live"):
        for f in list(d.glob("live_*.ts")) + list(d.glob("curves_live_*.json")):
            f.unlink(missing_ok=True)
    db.log(job_id, f"———— started over by you: {gone} unposted clip(s) deleted, finding clips again ————")
    db.update("jobs", job_id, status="queued", progress=0)
    start(job_id)


def _running(job_id):
    """Its pipeline thread is still alive (e.g. finishing a clip after a stop)."""
    return any(t.is_alive() and t.name == f"job-{job_id}" for t in threading.enumerate())


@config.in_brand(lambda job_id: config.brand_of_category((db.get("jobs", job_id) or {}).get("category_id")))
def _run(job_id):
    job = db.get("jobs", job_id)
    d = DATA / "jobs" / str(job_id)
    (d / "clips").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    db.log(job_id, f"job started: {job['url']} | {job['shorts']} shorts, {job['longs']} long, mode {job.get('mode')}, "
                   f"framing {job.get('aspect') or 'default'}, {'4K' if job.get('hq') else '1080p'}, "
                   f"subtitles {'on' if job.get('subtitles') else 'off'}, voice-over {'on' if job.get('voiceover') else 'off'}, "
                   f"upload to {job.get('platforms') if job.get('auto_upload') else 'nowhere'}"
                   + (f", topic T{job['topic_id']}" if job.get("topic_id") else ""))
    try:
        if job["url"].startswith("file:"):  # uploaded from the UI: already at data/jobs/<id>/source.*
            src = next(d.glob("source.*"))
            db.log(job_id, f"local file: {job['title']}")
            _process(job, src, job["title"] or src.name, d)
            check(job_id)
            db.update("jobs", job_id, status="done")
            db.log(job_id, f"job finished in {(time.time() - t0) / 60:.1f} min")
            return
        db.update("jobs", job_id, status="fetching info")
        with db.step(job_id, "fetching video info"):
            i = download.info(job["url"])
        db.update("jobs", job_id, title=i.get("title"), is_live=int(bool(i.get("is_live"))))
        db.log(job_id, f"{'LIVE' if i.get('is_live') else 'video'}: {i.get('title')} | channel {i.get('channel')}, "
                       f"{(i.get('duration') or 0) / 60:.1f} min, {i.get('view_count') or 0:,} views")
        (_live if i.get("is_live") else _vod)(job, i, d)
        status = "stopped" if STOPS[job_id].is_set() else "done"
        db.update("jobs", job_id, status=status)
        db.log(job_id, f"job {status} in {(time.time() - t0) / 60:.1f} min")
    except Stopped:
        db.update("jobs", job_id, status="stopped")
        db.log(job_id, f"job stopped by you after {(time.time() - t0) / 60:.1f} min")
    except Exception as e:
        if STOPS[job_id].is_set():  # e.g. the cancelled download raising: that's the stop, not a failure
            db.update("jobs", job_id, status="stopped")
            db.log(job_id, f"job stopped by you after {(time.time() - t0) / 60:.1f} min")
            return
        if download.age_restricted(e):
            db.log(job_id, "skipped: the video is age-restricted (YouTube only shows it to signed-in, age-verified "
                           "accounts). The topic moves on to the next video.")
        elif download.bot_checked(e):
            db.log(job_id, "ERROR: YouTube bot check ('Sign in to confirm you're not a bot'). YouTube wants a signed-in "
                           "browser: add YouTube cookies in Settings → Downloads, then run it again.")
        else:
            db.log(job_id, f"ERROR: {e!r}", exc=True)
        db.update("jobs", job_id, status="failed")


def _captions(jid, info, src):
    """YouTube's captions or None (= Whisper transcribes). The spoken language: Settings → Audio language if set, else
    the language YouTube has for the video (reliable; Whisper's own guess calls Hinglish "English"), else Whisper's
    guess from the audio. Languages in Settings → "Always transcribe these languages" (English by default) go to
    Whisper, which is more accurate there; others use YouTube's captions, but only ones in that same language
    (YouTube lists many "-orig" tracks, e.g. ar-orig on an English video: taking the first one was wrong)."""
    if not config.CAPTIONS_FIRST:
        return None
    whisper_for = {x.strip().lower() for x in (config.WHISPER_FOR or "").split(",") if x.strip()}
    spoken = transcribe.language() or (info.get("language") or "").split("-")[0].lower() or None
    if spoken and not transcribe.language():
        db.log(jid, f"spoken language: {spoken} (YouTube)")
    if not spoken:
        try:
            with db.step(jid, "detecting the spoken language"):
                spoken, share = transcribe.detect(src)
            db.log(jid, f"spoken language: {spoken or 'unknown'}" + (f" ({share:.0%} of samples)" if spoken else ""))
        except Exception as e:
            db.log(jid, f"language detection failed ({e!r}), using YouTube's own language guess")
    if spoken and spoken in whisper_for:
        db.log(jid, f"'{spoken}' speech: transcribing with Whisper (more accurate than YouTube's captions for it)")
        return None
    try:
        caps = download.captions(info, spoken)
    except Exception as e:
        db.log(jid, f"YouTube captions unavailable ({e!r}), will transcribe")
        return None
    if caps:
        db.log(jid, f"YouTube captions: {caps['source']}, {len(caps['words'])} words (no transcription needed)")
    else:
        db.log(jid, f"no YouTube captions in '{spoken or '?'}': transcribing with Whisper")
    return caps


def _vod(job, i, d):
    jid, url = job["id"], job["url"]
    db.update("jobs", jid, status="downloading")
    db.log(jid, "downloading video + comments + chat replay")
    with ThreadPoolExecutor(3) as ex:
        meta = ex.submit(download.info, url, True)
        chat = ex.submit(download.chat_replay_times, url)
        h = download.height_for(job.get("hq"))
        have = [f for f in d.glob("source.*") if f.name.count(".") == 1  # not source.f137.mp4 / .part leftovers
                and f.suffix not in (".part", ".ytdl") and f.stat().st_size > 1e6]
        if have:  # started over: the video is already here
            db.log(jid, f"video already downloaded ({have[0].name}): using it")
        else:
            with db.step(jid, f"downloading video (up to {h}p, {download.FRAGMENTS} chunks at once)"):
                download.download_vod(url, d / "source.%(ext)s", h, STOPS[jid], progress=_progress("jobs", jid))
        check(jid)
        with db.step(jid, "waiting for comments + chat replay"):
            comments = download.comment_times(meta.result().get("comments"))
            chat = chat.result()
    db.log(jid, f"heatmap: {'yes' if i.get('heatmap') else 'no'}, chat msgs: {len(chat)}, timestamped comments: {len(comments)}")
    src = next(d.glob("source.*"))
    db.log(jid, f"source file: {src.name}, {src.stat().st_size / 1e6:.0f} MB, {_describe(src)}")
    caps = _captions(jid, i, src) if job.get("mode") != "nostalgia" else None  # nostalgia clips have no words
    job["_channel"] = i.get("channel") or i.get("uploader") or ""  # its own overlay isn't another channel's watermark
    _process(job, src, i.get("title", ""), d, heatmap=i.get("heatmap"), chat=chat, comments=comments, captions=caps)


def _live(job, i, d):
    """Clip a stream WHILE it is live: record it in fixed-size chunks and process each chunk in the background.

    Big picture (one job, several threads):

        main loop (this function)   record chunk 0 --> record chunk 1 --> record chunk 2 --> ... until stream ends / Stop
                                          |                  |                  |
        worker threads                    +-> _process(0)    +-> _process(1)    +-> _process(2)    (clip, render, upload)

        chat thread (LiveChat)      collects the wall-clock time of every chat message the whole time

    So clips from chunk 0 can already be rendered/uploaded while chunk 1 is still being recorded.
    The job's shorts/longs counts apply PER CHUNK (default chunk = 10 min, config.LIVE_CHUNK_SECONDS in .env).
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

        # Blocks until the chunk is recorded: ffmpeg copies config.LIVE_CHUNK_SECONDS of the stream from the live edge
        # (no re-encode). Returns False if the stream is already over. If Stop is pressed mid-chunk, ffmpeg is
        # told to quit gracefully and the partial chunk is kept and still processed below.
        # Small gaps (a few seconds) between chunks are lost while the next recording starts.
        if not download.record_live_chunk(job["url"], out, config.LIVE_CHUNK_SECONDS, stop,
                                           download.height_for(job.get("hq"))):
            db.log(jid, "stream ended")
            break
        if not out.exists() or out.stat().st_size == 0:  # ffmpeg produced nothing: stream died mid-chunk
            break
        db.log(jid, f"chunk {k} recorded in {time.time() - t0:.0f}s ({out.stat().st_size / 1e6:.0f} MB), "
                    f"chat msgs so far: {len(chat.times)}, processing in background")

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


def _process(job, src, title, d, heatmap=None, chat=None, comments=None, captions=None):
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
    live = src.suffix == ".ts"  # a live chunk: the job's status is the recorder's ("recording chunk 3")

    def status(s):  # every status change is also a stop checkpoint
        check(jid)
        if not live:
            db.update("jobs", jid, status=s)
    try:
        status("analysing audio")
        # STEP 1: Loudness. ffmpeg's ebur128 filter measures "momentary loudness" in LUFS, the
        # broadcast unit for how loud audio *feels* to a person (not raw volume). We keep one value
        # per second, roughly -35 = calm talking, -10 = screaming/explosion, floored at -70 (silence).
        # len(audio) doubles as the video length in seconds.
        # ... and, at the same time, shot changes / how much the picture moves (2 small frames a second).
        with db.step(jid, f"STEP 1 analysing loudness + shot changes of {src.name}"):
            with ThreadPoolExecutor(2) as ex:
                loud_f, scene_f = ex.submit(signals.loudness, src), ex.submit(signals.scenes, src)
                audio = loud_f.result()
                try:
                    cuts, visual = scene_f.result()
                except Exception as e:
                    db.log(jid, f"shot detection failed ({e!r}), continuing without it")
                    cuts, visual = [], None
        n = len(audio)
        db.log(jid, f"shot changes: {len(cuts)}")
        topic = db.get("topics", job["topic_id"]) if job.get("topic_id") else None
        found = bool(topic and topic.get("source") == "auto")  # a video the automation found (not one you gave it)
        if found and mode != "manual" and _watermarked(jid, src, n, job.get("_channel", "")):  # another creator's video: not ours to post
            return
        if mode == "nostalgia":  # calm moments + a feelings caption + music: its own short path (nostalgia.py)
            return _nostalgia(job, src, title, d, n, cuts, visual, status)
        cam = None
        try:  # a streamer's facecam box: clips get it on top, full width, with the gameplay below
            from . import vision
            cam = vision.facecam_box(src)
            if cam:  # only stacked when it stays sharp: never enlarged past CAM_MAX_ZOOM; too small -> normal layout
                strip, _ = render.stack_geom(src, cam)
                cam_px = int(render._dims(src)[1] * cam[3])
                if not render.cam_fits(src, cam):
                    db.log(jid, f"facecam found at {cam} but only {cam_px}px tall: too small to show sharp, normal layout")
                    cam = None
                else:
                    db.log(jid, f"facecam: found at {cam}, {cam_px}px -> {strip}px strip "
                                f"({strip / max(cam_px, 1):.1f}x, stacked layout)")
            else:
                db.log(jid, "facecam: none")
        except Exception as e:
            db.log(jid, f"facecam check skipped ({e!r})")
        job = {**job, "_cam": cam}

        # STEP 2: Hype curve. Each signal becomes a per-second array, gets normalised to 0..1
        # (median -> 0, 95th percentile -> 1, so different units are comparable), then they are summed
        # with weights:
        #   heatmap  x3    YouTube "most replayed": where real viewers rewatched (VODs only)
        #   chat     x2    live-chat messages per second, shifted 8s earlier (people type after the moment)
        #   comments x1.5  timestamps viewers wrote in comments ("4:05 lmao")
        #   audio    x1    loudness from step 1
        # curve[t] = how "hype" second t is. parts = each normalised signal, kept for the UI graph and the AI.
        #   reactions x2  laughter / cheering / applause tags in YouTube's captions
        #   visual   x0.8 shot changes and motion
        # Those weights are where learning starts: learn.weights() moves them toward what got views on your channel.
        from . import learn
        reactions = signals.reaction_times(captions["words"]) if captions else None
        weights = learn.weights()
        curve, parts = signals.hype_curve(n, audio, heatmap, chat, comments, reactions, visual, weights)
        if weights:
            db.log(jid, f"signal weights learned from your clips' views: {weights}")
        db.log(jid, f"STEP 2 hype curve: {n}s analysed, signals used: {', '.join(parts) or 'none'}, mode: {mode}")
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
        if live:  # a live chunk: only its best few moments
            cap = max(1, int(config.LIVE_CLIPS_PER_CHUNK or 3))
            asked = {"short": min(asked["short"], cap), "long": min(asked["long"], 1)}
        counts = pick.fit_counts(asked, n)
        if counts != asked:
            db.log(jid, f"video is {n}s: making {counts['short']} short / {counts['long']} long "
                        f"(asked {asked['short']} / {asked['long']})")
        # Windows are built around the hottest peaks (setup before, payoff after) and snapped to shot changes.
        # Two-pass: many more candidates (cheap with YouTube captions, a bit more transcription otherwise), screened
        # by the fast model in STEP 4b so the thorough one only sees the best.
        two_pass = config.TWO_PASS and mode == "hype"
        mult = (6 if captions else 4) if two_pass else 3
        cands = []
        for kind, count in counts.items():
            length, pad = WINDOW[kind]
            if count <= 0:
                continue
            for s, e, sig in signals.peak_windows(curve, length, count * mult, cuts):
                cands.append({"id": len(cands), "kind": kind, "start": max(0, s - pad), "end": min(n, e + pad),
                              "signal": round(sig, 3), "signals": _sig_means(parts, s, e)})
        if not cands:
            db.log(jid, "video too short for requested clips")
            return
        db.log(jid, f"STEP 3 candidate windows: {len(cands)} ("
                    + ", ".join(f"{c['kind']} {_mmss(c['start'])}-{_mmss(c['end'])} hype {c['signal']}" for c in cands[:12])
                    + (" ..." if len(cands) > 12 else "") + ")")

        # STEP 4: Transcribe with Whisper (word-level timestamps, GPU if available, else CPU).
        # "hype": only the candidate windows (overlaps merged so nothing is transcribed twice), fast.
        # "ai" modes: the whole video, so DeepSeek can find moments the curves missed. Slow on long videos.
        spans = [[0, n]] if mode != "hype" else transcribe.merge([(c["start"], c["end"]) for c in cands])
        status("transcribing")
        words = []
        if captions:  # YouTube's own captions: just the words inside the spans
            words = [w for w in captions["words"] if any(s <= w["s"] < e for s, e in spans)]
            db.log(jid, f"STEP 4 transcript from YouTube captions ({captions['source']})")
        else:
            with db.step(jid, f"STEP 4 transcribing {sum(e - s for s, e in spans):.0f}s in {len(spans)} part(s) "
                              f"({transcribe.device()})"):
                words += transcribe.words_many(src, spans, before_each=lambda: check(jid))
        db.log(jid, f"transcript: {len(words)} words")
        for c in cands:  # "[12.0] hello there [17.1] ..." text per window, absolute seconds
            c["transcript"] = transcribe.text_with_times(words, c["start"], c["end"])

        # STEP 4b (two-pass): the fast model scores every candidate; the best 3 per clip wanted go on to STEP 5.
        from . import categories
        ctx = categories.scope(job.get("category_id"))  # its category (description + keywords) as a rule
        if two_pass and len(cands) > 3 * sum(counts.values()):
            status("AI screening candidates")
            try:
                with db.step(jid, f"STEP 4b fast model screening {len(cands)} candidates"):
                    scores = pick.prescore(title, cands, ctx)
                kept = []
                for kind, count in counts.items():
                    mine = sorted([c for c in cands if c["kind"] == kind],
                                  key=lambda c: (-scores.get(c["id"], 0), -c["signal"]))
                    kept += mine[:count * 3]
                db.log(jid, "screening kept: " + ", ".join(f"{c['kind']} {_mmss(c['start'])} ({scores.get(c['id'], '?')}/10)"
                                                         for c in kept))
                cands = kept
            except Exception as e:
                db.log(jid, f"screening failed ({e!r}), the thorough model sees all candidates")

        # STEP 5: DeepSeek picks the final clips, trims them to sentence boundaries and writes
        # title/description/hashtags. Its numbers are clamped by pick.validate(). If the API fails,
        # fall back to the strongest raw hype windows so the job still produces something.
        status("AI picking clips")
        try:
            with db.step(jid, f"STEP 5 asking DeepSeek to pick {counts['short']} short / {counts['long']} long"):
                examples = learn.examples(job.get("category_id"))  # this channel's past winners and losers
                if examples:
                    db.log(jid, "the AI also sees your best / worst clips so far")
                if mode == "hype":
                    images = _frames(src, cands) if config.VISION_MODEL.strip() else None
                    chosen = pick.choose(title, cands, counts, ctx, examples, images)
                else:
                    chosen = pick.choose_full(title, n, transcribe.text_with_times(words, 0, n),
                                              parts if mode == "ai+hype" else None, counts, ctx, examples)
        except Exception as e:
            db.log(jid, f"DeepSeek failed ({e!r}), using raw hype windows")
            chosen = pick.fallback(cands, counts, title)
        floor = int(config.MIN_CLIP_SCORE or 0)
        weak = [c for c in chosen if c["score"] < floor]
        if weak:  # pickier: only moments the AI rates as genuinely strong
            db.log(jid, f"dropped {len(weak)} weaker moment(s) under score {floor}: "
                        + ", ".join(f"\"{c['title']}\" ({c['score']:.0f})" for c in weak))
            chosen = [c for c in chosen if c["score"] >= floor]
        for c in chosen:
            db.log(jid, f"picked {c['kind']} {_mmss(c['start'])}-{_mmss(c['end'])} score {c['score']}: "
                        f"\"{c['title']}\" [{c.get('headline', '')}] {' '.join('#' + t for t in c['tags'])} | {c['reason']}")
        # Snap every pick out to whole sentences (AI times are guesses from coarse markers), within the
        # video and never past the hard max (YouTube rejects Shorts over 3 min).
        for c in chosen:
            s, e = transcribe.snap(words, c["start"], c["end"])
            s, e = s, min(e, n, s + pick.hard_max()[c["kind"]])
            if (s, e) != (c["start"], c["end"]):
                db.log(jid, f"snapped to sentences: {_mmss(c['start'])}-{_mmss(c['end'])} -> {_mmss(s)}-{_mmss(e)} "
                            f"({e - s:.0f}s) \"{c['title']}\"")
            c["start"], c["end"] = s, e
            c["signals"] = _sig_means(parts, s, e)  # kept with the clip: what learn.update() learns from
        # STEP 5b: the truth check: the AI watches each final clip and drops weak ones / fixes or drops a mismatch
        if chosen:
            status("AI checking clips")
            with db.step(jid, f"STEP 5b truth check on {len(chosen)} clip(s)"):
                chosen = _truth_check(jid, src, chosen, words, ctx, floor)
        _save_curves(d, src, curve, parts, chosen)  # now with the picked clips marked on the graph
        status(f"rendering {len(chosen)} clip(s)")

        # STEP 6: Save each clip to the DB, render it with ffmpeg (every kind vertical 9:16), burn in the
        # headline (top) + captions (bottom) if the job asked for subtitles, then upload if auto-upload is on.
        _render_clips(job, src, d, chosen, words, job.get("subtitles", 1))
    except Stopped:
        if not live:
            raise  # _run marks the job stopped
        db.log(jid, f"{src.name}: stopped")  # a live chunk's thread just ends
    except Exception as e:
        db.log(jid, f"ERROR processing {src.name}: {e!r}", exc=True)


WATERMARK_ASK = """These are 4 frames spread over one video. Is a creator's or channel's own name / watermark stamped over
the picture in most of them (e.g. semi-transparent text like "Gar Studio" across the middle, or "@somechannel")?
Answer false for: the game's own HUD and menus, esports broadcast graphics (scoreboard, tournament logo, player cams,
caster names), and subtitles. Reply as JSON: {"watermark": true or false, "text": "the watermark text, or empty"}"""


def _watermarked(jid, src, n, channel=""):
    """True (and logged) when the vision model sees another channel's watermark on the video: a reupload or someone
    else's edit. channel: who uploaded it (a streamer's own name / socials on their stream overlay are fine).
    No vision model / any error: False (the video goes ahead)."""
    if not config.VISION_MODEL.strip() or n < 20:
        return False
    ask = WATERMARK_ASK + (f'\nThis video was uploaded by the channel "{channel}" itself. Its OWN name, logo, socials or '
                           "stream overlay (a streamer's facecam frame, alerts, their @handle in a corner) are NOT "
                           "another channel's watermark: answer false for those. Only a DIFFERENT channel's name counts."
                           if channel else "")
    try:
        imgs = [(f"frame {k + 1}:", render.frame_b64(src, n * f, width=640)) for k, f in enumerate((0.15, 0.38, 0.62, 0.85))]
        r = pick._ask(ask, "You check videos before they are clipped.", "watermark", config.VISION_MODEL.strip(),
                      images=[x for x in imgs if x[1]])
        mark = r is True or (isinstance(r, str) and r.lower() == "true")
    except Exception as e:
        db.log(jid, f"watermark check skipped ({e!r})")
        return False
    if mark:
        db.log(jid, "skipped: another channel's watermark is on this video (a reupload or someone else's edit), "
                    "so nothing is clipped from it")
    return mark


FILLER = {"oh", "ah", "uh", "um", "umm", "hmm", "huh", "ha", "haha", "wow", "yeah", "yes", "no", "ok", "okay", "hey",
          "sorry", "here", "what", "go", "come", "on", "the", "a", "i", "you", "it", "so", "and", "music", "applause"}


def real_speech(words, seconds):
    """Is there actual talking in these caption words, or is it Whisper guessing at noise ("here. sorry. oh")?
    Talking: at least 6 real words and half a real word a second."""
    real = [w for w in words if re.sub(r"[^\w']", "", w["w"].lower()) not in FILLER and re.search(r"[^\W\d_]{2}", w["w"])]  # any script
    return len(real) >= 6 and len(real) >= 0.5 * max(seconds, 1)


def _sig_means(parts, s, e):
    """How strongly each signal spiked inside [s, e): {name: 0..1}."""
    a, b = int(max(s, 0)), int(max(e, s + 1))
    return {k: round(float(v[a:b].mean()), 3) for k, v in parts.items() if len(v[a:b])}


VERIFY_FRAMES = 6  # frames across a picked clip for its truth check
PICK_FRAMES = 16   # frames in all when the AI picks among the candidates


def _truth_check(jid, src, chosen, words, ctx, floor):
    """Each picked clip, as finally trimmed: the AI sees VERIFY_FRAMES frames across it + its speech + its headline /
    title / description (pick.verify). Weak moment -> dropped. Headline or title not matching what happens ->
    rewritten from what's really there and checked again; still not matching -> dropped. Nothing goes out with a
    caption the clip doesn't show. If the AI can't be reached, the clip keeps no headline (no claim to get wrong)."""
    bar = max(50, floor - 10)  # the checker scores harshly (average 55-70): 10 under the picker's floor
    vision = bool(config.VISION_MODEL.strip())
    kept = []
    for c in chosen:
        check(jid)
        span = c["end"] - c["start"]
        tr = transcribe.text_with_times(words, c["start"], c["end"])
        imgs = None
        if vision:
            ts = [c["start"] + span * (k + 0.5) / VERIFY_FRAMES for k in range(VERIFY_FRAMES)]
            with ThreadPoolExecutor(VERIFY_FRAMES) as ex:
                b64s = list(ex.map(lambda t: render.frame_b64(src, t, 512), ts))
            imgs = [(f"frame {k + 1} of {VERIFY_FRAMES} ({_mmss(t)})", b) for k, (t, b) in enumerate(zip(ts, b64s)) if b]
        name = f"{_mmss(c['start'])} \"{c.get('headline') or c['title']}\""
        v = None
        for attempt in range(2):
            try:
                v = pick.verify(c, tr, imgs, ctx)
                break
            except Exception as e:
                db.log(jid, f"truth check {name}: AI error ({e!r})" + (", trying again" if not attempt else ""))
        if v is None:
            db.log(jid, f"truth check {name}: couldn't check it, so it keeps no headline (nothing to get wrong)")
            kept.append({**c, "headline": ""})
            continue
        db.log(jid, f"truth check {name}: moment {v['moment']:.0f}, {'matches' if v['match'] else 'DOES NOT MATCH'}"
                    f" | seen: {str(v.get('happens', ''))[:200]}" + (f" | problem: {v['problems']}" if v.get("problems") else ""))
        if v["moment"] < bar:
            db.log(jid, f"dropped {name}: the clip itself is not strong enough ({v['moment']:.0f} < {bar})")
            continue
        if not v["match"]:
            new = {k: str(v.get(k) or "").strip() for k in ("headline", "title", "description")}
            if not new["headline"] or not new["title"]:
                db.log(jid, f"dropped {name}: its caption doesn't match the clip and no true one was offered")
                continue
            fixed = {**c, **new}
            try:
                v2 = pick.verify(fixed, tr, imgs, ctx)
            except Exception as e:
                v2 = {"match": False, "problems": repr(e)}
            if not v2["match"]:
                db.log(jid, f"dropped {name}: even the rewritten caption \"{new['headline']}\" doesn't match "
                            f"({v2.get('problems', '')})")
                continue
            db.log(jid, f"caption fixed to match the clip: \"{c.get('headline', '')}\" -> \"{new['headline']}\"; "
                        f"title \"{new['title']}\"")
            c = fixed
        c["reason"] = f"{c.get('reason', '')} | seen: {str(v.get('happens', ''))[:300]}".strip(" |")
        kept.append(c)
    return kept


def _frames(src, cands):
    """Small frames per candidate (up to 3, evenly inside it), fetched in parallel, for a vision model. At most
    PICK_FRAMES in all: more and the local model refuses the request ("Failed to tokenize prompt")."""
    per = max(1, min(3, PICK_FRAMES // max(len(cands), 1)))
    jobs = [(f"candidate {c['id']} at {_mmss(t)}", t) for c in cands
            for t in (c["start"] + (c["end"] - c["start"]) * k / (per + 1) for k in range(1, per + 1))][:PICK_FRAMES]
    with ThreadPoolExecutor(6) as ex:
        b64s = list(ex.map(lambda j: render.frame_b64(src, j[1]), jobs))
    return [(label, b) for (label, _), b in zip(jobs, b64s) if b]


def _render_clips(job, src, d, chosen, words, subtitles):
    """subtitles: burn the headline (top) and word captions (bottom) into the video."""
    # Two clips at a time: while one is in the GPU encoder, the other uses the CPU (decode, captions, voice-over).
    with ThreadPoolExecutor(RENDER_JOBS) as ex:
        for f in [ex.submit(_render_one, job, src, d, c, words, subtitles) for c in chosen]:
            f.result()  # re-raise the first failure so _process logs it


@config.in_brand(lambda job, *a, **k: config.brand_of_category(job.get("category_id")))
def _render_one(job, src, d, c, words, subtitles):
    jid = job["id"]
    check(jid)  # clips not started yet are skipped after Stop
    clip_words = [w for w in words if c["start"] <= w["s"] < c["end"]]  # kept: other shapes are rendered later
    if subtitles and clip_words and not real_speech(clip_words, c["end"] - c["start"]):
        # no real talking: Whisper's guesses at noise would be burned in ("HERE. SORRY. OH"): no captions, and a
        # headline built from them gives way to the title
        heard = " ".join(w["w"].lower() for w in clip_words)
        db.log(jid, f"{c['title']}: no real speech in it, so no captions")
        if c.get("headline") and all(t in heard for t in re.findall(r"[a-z']+", c["headline"].lower())):
            c["headline"] = " ".join(c["title"].split()[:6])
        words, clip_words = [w for w in words if not (c["start"] <= w["s"] < c["end"])], []
    cta = engage.clip_cta(job.get("category_id"))  # which follow prompt (learned from clips' views)
    cam = job.get("_cam")
    cid = db.insert("clips", job_id=jid, kind=c["kind"], start=c["start"], end=c["end"], score=c["score"],
                    features=json.dumps({"cta": cta, **({"cam": list(cam)} if cam else {})}),
                    title=c["title"], description=c["description"], tags=json.dumps(c["tags"]),
                    reason=c["reason"], headline=c.get("headline", ""), status="rendering",
                    category_id=job.get("category_id"), targets=job.get("targets") or "[]", words=json.dumps(clip_words),
                    signals=json.dumps(c.get("signals") or {}))
    out = d / "clips" / f"{cid}.mp4"
    voice = _voiceover(job, c, words, out) if job.get("voiceover") else []
    try:
        with db.step(jid, f"STEP 6 rendering {c['kind']} #{cid} ({c['end'] - c['start']:.0f}s, {render.encoder_name()}): "
                          f"{c['title']}"):
            args = (src, out, c["start"], c["end"], "9:16", words if subtitles else (),
                    c.get("headline", "") if subtitles else "", voice, job.get("aspect"))
            kw = dict(progress=_progress("clips", cid), end_text=end_text(db.get("clips", cid)), cam=cam,
                      side=side_logo(job.get("category_id"), out) if cam else None)
            render.cut(*args, **kw)
        # the finished video itself: a picture that stops moving (a decoder hiccup) never goes out
        bad = render.frozen(out)
        if bad:
            db.log(jid, f"clip #{cid}: the video FREEZES ({bad}): rendering it again on the CPU")
            render.cut(*args, **kw, force_cpu=True)
            bad = render.frozen(out)
            if bad:
                db.log(jid, f"clip #{cid}: still frozen after a second render ({bad}): dropped")
                raise RuntimeError(f"clip #{cid}: the rendered video freezes ({bad})")
            db.log(jid, f"clip #{cid}: second render plays fine")
    except Exception:
        db.update("clips", cid, status="render failed")
        raise
    finally:
        for wav, _ in voice:
            wav.unlink(missing_ok=True)  # voice lines are baked into the mp4 now
    if config.COLD_OPEN and c.get("peak") and c["kind"] == "short":
        try:  # open on the payoff for a moment ("WAIT FOR IT..."), then play from the start
            render.cold_open(out, c["peak"] - c["start"])
            db.log(jid, f"clip #{cid}: cold open from {c['peak'] - c['start']:.1f}s")
        except Exception as e:
            err = (getattr(e, "stderr", b"") or b"")
            err = err.decode(errors="replace") if isinstance(err, bytes) else str(err)
            db.log(jid, f"clip #{cid}: cold open skipped ({type(e).__name__}: {err.strip()[-300:] or e!r})")
        bad = render.frozen(out)
        if bad:  # the cold open's re-encode broke it: the clip without one is safer than none
            db.log(jid, f"clip #{cid}: frozen after the cold open ({bad}): rendering it again without one")
            try:
                render.cut(*args, **kw, force_cpu=True)
            except Exception as e:
                db.update("clips", cid, status="render failed")
                raise RuntimeError(f"clip #{cid}: re-render failed: {e!r}")
            if render.frozen(out):
                db.update("clips", cid, status="render failed")
                raise RuntimeError(f"clip #{cid}: the rendered video freezes")
    db.log(jid, f"clip #{cid} ready: {out.name}, {out.stat().st_size / 1e6:.1f} MB")
    _rendered(job, cid, out)


def _rendered(job, cid, out):
    """A clip's file is done: save it, then publish / schedule it the way the job asked."""
    jid = job["id"]
    db.update("clips", cid, path=str(out.relative_to(DATA)), status="rendered", progress=1)
    topic = db.get("topics", job["topic_id"]) if job.get("topic_id") else None
    by_hand = not (topic and topic.get("source") == "auto")
    if by_hand and (job["auto_upload"] or json.loads(job.get("targets") or "[]")) and _plan_on(job.get("category_id")):
        # a clip you started (a link, a live stream) on a page with a plan: the queue, ahead of the automatic clips,
        # so a stream's clips go out one slot at a time instead of all at once
        feats = json.loads(db.get("clips", cid).get("features") or "{}")
        db.update("clips", cid, status="scheduled", features=json.dumps({**feats, "priority": True}))
        db.log(jid, f"clip #{cid} queued first: it goes out on the page's next free slot (see Scheduled)")
        return
    if json.loads(job.get("targets") or "[]"):  # destinations chosen up front (Create → "publish when ready")
        dests = json.loads(db.get("clips", cid).get("targets") or "[]") or json.loads(job["targets"])
        if not _spaced(jid, cid, dests):
            publish_clip(cid, dests, skip_limited=True)
    elif job["auto_upload"]:
        if not by_hand:  # autopilot.upload_tick() posts it on its slot of the day
            db.update("clips", cid, status="scheduled")
            db.log(jid, f"clip #{cid} scheduled: the autopilot posts it on the next free slot")
        else:
            c = db.get("clips", cid)
            dests = destinations_for(c, json.loads(job.get("platforms") or '["youtube"]') or ["youtube"])
            if not _spaced(jid, cid, dests):
                publish_clip(cid, dests, skip_limited=True)


_CLAIMS, _CLAIM_LOCK = {}, threading.Lock()  # account id -> the latest time a just-rendered clip took for it


def _spaced(jid, cid, dests):
    """A clip that would go up the moment it's rendered (a stream makes several in a row): if its accounts got a post
    less than Settings → "Minutes between posts" ago, or another clip is already timed for them, give it the next free
    time instead (posted by publish_due), so a stream can't flood a page (Instagram blocks bursts as spam).
    -> True if it was given a later time."""
    gap = int(config.POST_GAP_MIN or 0) * 60
    ids = {d["account"] for d in dests}
    if not gap or not ids:
        return False
    with _CLAIM_LOCK:  # clips rendered at the same moment: each claims its time before the next one looks
        now = time.time()
        t = max(now, social.last_post_at(ids) + gap, social.paused_until(ids, now),
                *[_CLAIMS.get(a, 0) + gap for a in ids])
        taken = sorted(c["publish_at"] for c in db.all("clips", "status='scheduled' AND publish_at IS NOT NULL AND id != ?", (cid,))
                       if ids & {d.get("account") for d in json.loads(c.get("targets") or "[]")})
        for at in taken:  # step past every timed clip closer than the gap
            if abs(at - t) < gap:
                t = at + gap
        for a in ids:
            _CLAIMS[a] = t
    if t <= now + 5:
        return False
    db.update("clips", cid, status="scheduled", publish_at=t, targets=json.dumps(dests))
    db.log(jid, f"clip #{cid} timed for {time.strftime('%H:%M', time.localtime(t))}: keeps "
                f"{gap // 60} min between posts on the same accounts")
    return True


def side_logo(category_id, out):
    """What goes beside a streamer's facecam: the category's own logo (Categories → Upload its own logo), else its
    name drawn big (e.g. "GTA 6"). -> a PNG path, or None."""
    from . import categories
    c = categories.get(category_id) if category_id else None
    if not c or c["name"] == categories.UNCATEGORIZED:
        return None
    if c.get("logo_file") and (DATA / c["logo_file"]).exists():
        return DATA / c["logo_file"]
    from .nostalgia import _Writer, TEXT_FONTS, _wrap
    from PIL import Image
    name = render.plain(c["name"]).upper()
    img = Image.new("RGBA", (900, 520), (0, 0, 0, 0))
    for size in (170, 140, 110, 90, 70):
        wr = _Writer(size, TEXT_FONTS, stroke=max(4, size // 18))
        lines = _wrap(wr, name, 860)
        if len(lines) <= 3 and int(size * 1.05) * len(lines) <= 500:
            break
    y = (520 - int(size * 1.05) * len(lines)) // 2
    for ln in lines:
        wr.draw(img, (900 - wr.width(ln)) / 2, y, ln, fill=(255, 214, 10))
        y += int(size * 1.05)
    img = img.crop(img.getbbox() or (0, 0, 900, 520))
    path = out.with_name(out.stem + "_name.png")
    img.save(path)
    return path


def _plan_on(category_id):
    """Does this category's brand have its page plan switched on (so the planner posts its clips)?"""
    from . import page
    bid = config.brand_of_category(category_id)
    return bool(bid and page.plan(bid)["on"] and category_id in {c["id"] for c in page.cats(bid)})


def _nostalgia(job, src, title, d, n, cuts, motion, status):
    """Mode "nostalgia": the calmest unbroken stretches -> the AI picks the most scenic and writes a feelings caption
    for each -> rendered with a track from the music library (nostalgia.py)."""
    from . import categories, learn, nostalgia, page
    jid, want = job["id"], max(1, int(job.get("shorts") or 1))
    r = page.recipe(job["category_id"]) if job.get("category_id") else {}
    day_only, grade = r.get("light") == "day", r.get("look", "")
    cands = nostalgia.moments(n, cuts, motion, want * (10 if day_only else 5))
    if day_only:  # bright daytime only: night / dusk / dark moments never reach the AI
        bright = [c for c in cands if nostalgia.daylight(src, c["start"], c["end"])]
        db.log(jid, f"nostalgia: {len(bright)} of {len(cands)} calm moments are bright daytime")
        cands = bright[:want * 5]
    clean = [c for c in cands if not nostalgia.facecam(src, c["start"], c["end"])]
    if len(clean) < len(cands):
        db.log(jid, f"nostalgia: {len(cands) - len(clean)} moment(s) dropped: a face (facecam) on screen")
    cands = clean
    if not cands:
        db.log(jid, "nostalgia: no calm, unbroken moment of "
                    f"{nostalgia.LENGTH}s in this video (too short, or cut every few seconds)")
        return
    db.log(jid, f"nostalgia: {len(cands)} calm moments: " + ", ".join(f"{_mmss(c['start'])}" for c in cands))
    status("AI writing captions")
    images = None
    if config.VISION_MODEL.strip():
        images = [(f"candidate {c['id']} ({'start' if k == 0 else 'end'}):", render.frame_b64(src, t, width=512))
                  for c in cands for k, t in enumerate((c["start"] + 2.5, c["end"] - 2.5))]  # two frames each
        images = [x for x in images if x[1]]
    with db.step(jid, f"nostalgia: the AI picks {want} and writes the captions"):
        picks = nostalgia.write(title, cands, want, categories.scope(job.get("category_id")),
                                learn.examples(job.get("category_id")), images, [t["name"] for t in engage.tracks()], day_only)
    if not picks:
        db.log(jid, "nostalgia: the AI picked nothing usable, no clips from this video")
        return
    status(f"rendering {len(picks)} clip(s)")
    by_id = {c["id"]: c for c in cands}
    for p in picks:
        check(jid)
        c = by_id[p["id"]]
        t = nostalgia.track(job.get("category_id"), p.get("music", ""))
        desc = p["description"] + (f"\n\n{t['credit']}" if t and t.get("credit") else "")  # CC BY needs the credit
        if p.get("scene"):
            db.log(jid, f"on screen at {_mmss(c['start'])}: {p['scene']}")
        db.log(jid, f"picked {_mmss(c['start'])}-{_mmss(c['end'])}: \"{p['caption']}\" ({p['game']}) · music: "
                    f"{t['name'] if t else 'none (game sound)'}")
        cid = db.insert("clips", job_id=jid, kind="short", start=c["start"], end=c["end"], score=0,
                        features=json.dumps({"style": "nostalgia", "game": p["game"], "music": t["name"] if t else ""}),
                        title=p["title"], description=desc, tags=json.dumps(p["tags"]), reason="nostalgia: calm moment",
                        headline=p["caption"], status="rendering", category_id=job.get("category_id"),
                        targets=job.get("targets") or "[]", words="[]", signals="{}")
        out = d / "clips" / f"{cid}.mp4"
        try:
            with db.step(jid, f"rendering nostalgia clip #{cid} ({render.encoder_name()})"):
                nostalgia.render(src, out, c["start"], c["end"], p["caption"], p["game"],
                                 engage.MUSIC / t["file"] if t else None, progress=_progress("clips", cid), grade=grade)
        except Exception as e:
            db.update("clips", cid, status="render failed")
            db.log(jid, f"nostalgia render #{cid} failed: {e!r}", exc=True)
            continue
        db.log(jid, f"clip #{cid} ready: {out.name}, {out.stat().st_size / 1e6:.1f} MB")
        _rendered(job, cid, out)


def is_nostalgia(c):
    return json.loads(c.get("features") or "{}").get("style") == "nostalgia"


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
                words += transcribe.words_many(src, transcribe.merge([(c["start"], c["end"]) for c in cuts]))
            _render_clips(job, src, d, [{"score": 0, "reason": "manual cut", **c} for c in cuts], words, subtitles)
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


def _progress(table, rid):
    """A callback that writes a 0..1 progress into the row (at most every 2%, so the database isn't hammered)."""
    last = [-1.0]

    def report(f):
        if f - last[0] >= 0.02 or f >= 1:
            last[0] = f
            db.update(table, rid, progress=round(f, 3))
    return report


def end_text(c):
    """The clip's follow prompt (its end-card variant, with the category's handle and topic filled in)."""
    from . import categories
    variants = engage.cta_variants()
    if not variants:
        return ""
    k = json.loads(c.get("features") or "{}").get("cta") or 0
    return engage.fill(variants[min(int(k), len(variants) - 1)], categories.get(c.get("category_id")) or {})


@config.in_brand(lambda cid: config.brand_of_category(db.get("clips", cid)["category_id"]))
def recaption(cid):
    """Re-do a clip's burned-in captions with Whisper (e.g. made from wrong YouTube captions): the clip's span is
    transcribed again and re-rendered, replacing its file. The source video is used if it's still there, else just
    the clip's seconds are downloaded again. Title, description and schedule stay as they are."""
    c = db.get("clips", cid)
    job = db.get("jobs", c["job_id"]) or {}
    if json.loads(c.get("uploads") or "{}"):
        raise ValueError("this clip is already published somewhere: its captions can't be changed there")
    if not job.get("subtitles", 1) or is_nostalgia(c):
        raise ValueError("this clip was made without captions")
    d, pad = DATA / "jobs" / str(c["job_id"]), 2.0
    src, offset, tmp = next(d.glob("source.*"), None), 0.0, None
    if not src:
        if not str(job.get("url", "")).startswith("http"):
            raise ValueError("the source video was deleted and this clip didn't come from a link to download it again")
        tmp = d / f"recaption_{cid}"
        tmp.mkdir(parents=True, exist_ok=True)
        offset = max(c["start"] - pad, 0)
        with db.step(c["job_id"], f"#{cid}: downloading {c['end'] - c['start']:.0f}s of the source again for new captions"):
            download.download_section(job["url"], tmp / "part.%(ext)s", offset, c["end"] + pad, download.height_for(job.get("hq")))
        src = next(tmp.glob("part.*"))
    try:
        s, e = c["start"] - offset, c["end"] - offset
        with db.step(c["job_id"], f"#{cid}: transcribing with {transcribe.device()}"):
            words = transcribe.words(src, s, e)
        old = DATA / c["path"] if c.get("path") else None
        out = d / "clips" / f"{cid}_r{int(time.time())}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        with db.step(c["job_id"], f"#{cid}: rendering with the new captions"):
            render.cut(src, out, s, e, "9:16", words, c.get("headline", ""), (), job.get("aspect"),
                       progress=_progress("clips", cid), end_text=end_text(c),
                       cam=json.loads(c.get("features") or "{}").get("cam"),
                       side=side_logo(c.get("category_id"), out) if json.loads(c.get("features") or "{}").get("cam") else None)
        for f in [old] + [DATA / r for r in json.loads(c.get("renders") or "{}").values()]:
            if f and f != out:
                f.unlink(missing_ok=True)  # other shapes had the old captions: made again when needed
        db.update("clips", cid, path=str(out.relative_to(DATA)), renders="{}", progress=1,
                  words=json.dumps([{**w, "s": round(w["s"] + offset, 2), "e": round(w["e"] + offset, 2)} for w in words]))
        db.log(c["job_id"], f"#{cid}: new captions ({len(words)} words, Whisper): {out.name}")
        return db.get("clips", cid)
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


@config.in_brand(lambda c, *a: config.brand_of_category(c.get("category_id")))
def ensure_render(c, aspect):
    """The clip's file in `aspect` ("9:16", "4:5", "1:1", "16:9"), rendering it from the source if it isn't there yet.
    Needs the source video (autopilot deletes sources after clipping unless "keep source videos" is on)."""
    if (aspect == "9:16" or is_nostalgia(c)) and c["path"]:  # nostalgia clips are made for 9:16 only
        return DATA / c["path"]
    renders = json.loads(c.get("renders") or "{}")
    if renders.get(aspect) and (DATA / renders[aspect]).exists():
        return DATA / renders[aspect]
    job, d = db.get("jobs", c["job_id"]) or {}, DATA / "jobs" / str(c["job_id"])
    src = next(d.glob("source.*"), None)
    if not src:
        raise RuntimeError(f"can't make a {aspect} version: the source video was deleted after clipping "
                           "(turn on 'Keep source videos' in Autopilot to keep them)")
    out = d / "clips" / f"{c['id']}_{aspect.replace(':', 'x')}.mp4"
    words = json.loads(c.get("words") or "[]") if job.get("subtitles", 1) else ()
    db.log(c["job_id"], f"rendering #{c['id']} as {aspect}")
    render.cut(src, out, c["start"], c["end"], aspect, words, c.get("headline", "") if job.get("subtitles", 1) else "",
               (), job.get("aspect"), progress=_progress("clips", c["id"]), end_text=end_text(c))
    renders[aspect] = str(out.relative_to(DATA))
    db.update("clips", c["id"], renders=json.dumps(renders))
    return out


def _caption(c, platform):
    """Title + description for one platform: its own override (Create → Caption tabs), else the shared one."""
    o = json.loads(c.get("captions") or "{}").get(platform) or {}
    return (o.get("title") or c["title"] or "").strip(), (o.get("description") if o.get("description") is not None
                                                         else c["description"] or "").strip()


def dest_key(d):
    return f"{d['format']}@{d['account']}"


_publishing, _pub_guard = set(), threading.Lock()  # clips being uploaded right now: one attempt at a time


@config.in_brand(lambda cid, *a, **k: config.brand_of_category(db.get("clips", cid)["category_id"]))
def publish_clip(cid, dests=None, skip_limited=False):
    """Publish a clip to destinations [{"account": id, "format": "instagram_reel" | ...}] (default: the ones chosen for
    it). Each destination gets its own result in uploads["<format>@<account>"], so some can succeed while others
    fail; running it again retries only the ones that didn't work. Expired accounts are refused (reconnect first).
    One attempt per clip at a time: a second one (Retry pressed again, the autopilot's slot) is skipped, so two
    uploads never race and overwrite each other's results."""
    with _pub_guard:
        if cid in _publishing:
            c = db.get("clips", cid)
            db.log(c["job_id"], f"#{cid} is already uploading: this attempt was skipped")
            return c
        _publishing.add(cid)
    try:
        return _publish_clip(cid, dests, skip_limited)
    finally:
        with _pub_guard:
            _publishing.discard(cid)


def _publish_clip(cid, dests=None, skip_limited=False):
    from . import accounts
    c = db.get("clips", cid)
    dests = dests if dests is not None else json.loads(c.get("targets") or "[]")
    done, seconds = json.loads(c.get("uploads") or "{}"), c["end"] - c["start"]
    limited = social.limit_timers()
    db.update("clips", cid, status="uploading")
    for d in dests:
        key, fmt = dest_key(d), d["format"]
        if done.get(key, {}).get("id") or fmt not in social.FORMATS:
            continue
        a = accounts.get(d["account"], secret=True)
        platform = social.FORMATS[fmt]["platform"]
        if not a:
            done[key] = {"error": "that account was removed", "at": time.time(), "platform": platform, "format": fmt}
            continue
        entry = {"platform": platform, "format": fmt, "account": a["id"], "name": a["name"]}
        if a["platform"] != platform:  # e.g. an old destination whose account id now belongs to another platform
            done[key] = {**entry, "error": f"{a['name']} is a {a['platform']} account, not {platform}: pick the "
                                          f"destination again", "at": time.time()}
            db.log(c["job_id"], f"#{cid}: {done[key]['error']}")
            continue
        if a["error"]:
            done[key] = {**entry, "error": f"{a['name']} needs reconnecting on the Accounts page", "at": time.time()}
            continue
        if skip_limited and (platform in limited or accounts.limited(a)):
            db.log(c["job_id"], f"#{cid} not sent to {a['name']} ({fmt}) by autopilot: upload limit reached")
            continue
        cap = int(config.YT_MAX_PER_DAY or 0)
        if skip_limited and platform == "youtube" and cap and \
                len([x for x in social.upload_times("youtube") if x > time.time() - 86400]) >= cap:
            db.log(c["job_id"], f"#{cid} not sent to YouTube by autopilot: {cap} uploads in the last 24 h already "
                                "(Settings → YouTube → Most uploads a day)")
            continue
        if platform == "instagram":  # a Page of the brand can host the Reel as a link (needed / fallback)
            fb = next((x for x in accounts.all(secret=True, platform="facebook", brand_id=a.get("brand_id"))
                       if not x["error"]), None)
            a = {**a, "host_page": (fb["ext_id"], fb["token"])} if fb else a
        t0 = time.time()
        db.log(c["job_id"], f"uploading #{cid} to {a['name']} as {social.FORMATS[fmt]['label']}...")
        try:
            path = ensure_render(c, social.FORMATS[fmt]["aspect"])
            title, desc = _caption(c, platform)
            r = social.publish(a, fmt, path, title, desc, json.loads(c["tags"] or "[]"), seconds)
            done[key] = {**entry, **r, "at": time.time()}
            db.log(c["job_id"], f"uploaded #{cid} to {a['name']} in {time.time() - t0:.0f}s -> {r['url']}")
        except Exception as e:
            msg = social.error_text(e)[:500]
            done[key] = {**entry, "error": msg, "at": time.time()}  # shown as-is in the UI
            db.log(c["job_id"], f"upload #{cid} to {a['name']} ({fmt}) failed after {time.time() - t0:.0f}s: {msg}", exc=True)
            accounts.token_error(a["id"], msg)
            if social.note_limit(platform, str(e), a["id"]):
                db.log(c["job_id"], f"{platform} says the limit is reached: {a['name']} paused for 24 h "
                                    "(automatic posts skip it, other accounts carry on)")
        db.update("clips", cid, uploads=json.dumps(done))
    tried = [k for k in done]
    ok = [k for k in tried if done[k].get("id")]
    status = ("not uploaded (skipped)" if not tried else
              "uploaded" if len(ok) == len(tried) else f"uploaded {len(ok)}/{len(tried)}" if ok else "upload failed")
    yt = next((u.get("id") for p, u in social.entries(done) if p == "youtube" and u.get("id")), None)
    db.update("clips", cid, status=status, youtube_id=yt)
    if status == "uploaded":
        tidy_clip(cid)
    return db.get("clips", cid)


def _thumb(f):
    """The Library thumbnail next to a clip file (made now if missing), so it survives the video being deleted."""
    import subprocess
    out = f.with_suffix(".thumb.jpg")
    if f.exists() and not out.exists():
        subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-ss", "1", "-i", str(f), "-frames:v", "1",
                        "-vf", "scale=360:-2", "-q:v", "4", str(out)], capture_output=True)
    return out


def tidy_clip(cid):
    """Settings → "Delete clip files once published": a clip that's up on every destination loses its video files
    (its other shapes and Instagram copies too). The thumbnail and everything in the database stay."""
    if not config.DELETE_CLIPS_AFTER_UPLOAD:
        return 0
    c = db.get("clips", cid)
    if not c or c["status"] != "uploaded" or not c.get("path") or c.get("publish_at"):
        return 0
    f = DATA / c["path"]
    files = [f] + [DATA / r for r in json.loads(c.get("renders") or "{}").values()] + list(f.parent.glob(f"{f.stem}_ig*.mp4"))
    files = [x for x in files if x.exists()]
    if not files:
        return 0
    _thumb(f)
    freed = sum(x.stat().st_size for x in files)
    for x in files:
        x.unlink(missing_ok=True)
    db.update("clips", cid, renders="{}")
    db.log(c["job_id"], f"#{cid} published everywhere: video files deleted ({freed / 1e6:.0f} MB freed)")
    return freed


def sweep(now=None):
    """Every few minutes: delete published clips' files (tidy_clip) and source videos of finished jobs older than
    Settings → "Keep source videos for" (an autopilot with "Keep source videos" on keeps its own)."""
    from . import autopilot
    now = now or time.time()
    freed = 0
    for c in db.all("clips", "status='uploaded'"):
        try:
            freed += tidy_clip(c["id"])
        except Exception as e:
            db.syslog(f"cleanup of clip #{c['id']} failed: {e!r}")
    keep = float(config.SOURCE_KEEP_HOURS or 0) * 3600
    busy = {c["job_id"] for c in db.all("clips", "status IN ('rendering','uploading')")}
    for j in db.all("jobs", "status IN ('done','failed','stopped','interrupted')"):
        if j["id"] in busy:
            continue
        srcs = [f for f in (DATA / "jobs" / str(j["id"])).glob("source.*")]
        if not srcs:
            continue
        t = db.get("topics", j["topic_id"]) if j.get("topic_id") else None
        auto = bool(t and t.get("source") == "auto")
        if auto and (autopilot.settings(t.get("autopilot_id")) or {}).get("keep_sources"):
            continue
        if not auto and now - max(f.stat().st_mtime for f in srcs) < keep:  # yours: kept a while for cutting by hand
            continue
        size = sum(f.stat().st_size for f in srcs)
        for f in srcs:
            f.unlink(missing_ok=True)
        freed += size
        db.log(j["id"], f"source video deleted after the job finished ({size / 1e6:.0f} MB freed)")
    if freed:
        db.syslog(f"cleanup: {freed / 1e9:.2f} GB freed")
    return freed


def destinations_for(c, platforms, force=False):
    """Platform names (autopilot, topics, the old per-platform buttons) -> destinations: the clip's category's default
    account for each, in that platform's usual format. Instagram over its length limit is left out unless force."""
    from . import categories
    seconds, out = c["end"] - c["start"], []
    for a in categories.destinations(c.get("category_id"), platforms):
        p = a["platform"]
        if p == "instagram" and config.IG_MAX_SECONDS and seconds > config.IG_MAX_SECONDS:
            db.log(c["job_id"], f"#{c['id']} is {seconds:.0f}s: Instagram gets its last {config.IG_MAX_SECONDS:.0f}s "
                                f"(its API refuses longer Reels; Settings → Instagram: longest Reel)")
        fmt = "facebook_post" if p == "facebook" and seconds > social.FB_REEL_MAX else social.DEFAULT_FORMAT[p]
        out.append({"account": a["id"], "format": fmt})
    missing = set(platforms) - {social.FORMATS[d["format"]]["platform"] for d in out}
    for p in missing:
        db.log(c["job_id"], f"#{c['id']} not uploaded to {p}: no {p} account for this category (map one on Categories)")
    return out


def upload_clip(cid, platforms=None, force=False):
    """Older entry point: upload to `platforms` (default: the ones chosen for the job) through the clip's category's
    default accounts. Autopilot uploads skip platforms at their limit; manual ones always try."""
    c = db.get("clips", cid)
    job = db.get("jobs", c["job_id"]) or {}
    targets = json.loads(c.get("targets") or "[]")
    if platforms is None and targets:
        return publish_clip(cid)
    platforms = platforms or json.loads(job.get("platforms") or '["youtube"]') or ["youtube"]
    topic = db.get("topics", job["topic_id"]) if job.get("topic_id") else None
    auto = bool(topic and topic.get("source") == "auto")
    return publish_clip(cid, destinations_for(c, platforms, force), skip_limited=auto)


def publish_due(now=None):
    """Clips scheduled for a time (Create → Publish → Schedule): publish the ones whose time has come."""
    for c in db.all("clips", "status='scheduled' AND publish_at IS NOT NULL AND publish_at <= ?", (now or time.time(),)):
        until = social.paused_until([d.get("account") for d in json.loads(c.get("targets") or "[]")])
        if until:  # every account it goes to is paused by a limit: wait for the pause to end, don't lose it
            db.update("clips", c["id"], publish_at=until + 60)
            db.log(c["job_id"], f"#{c['id']}: its accounts are paused by a limit; moved to "
                                f"{time.strftime('%d %b %H:%M', time.localtime(until + 60))}")
            continue
        try:
            publish_clip(c["id"], skip_limited=True)
        except Exception as e:
            db.log(c["job_id"], f"scheduled clip #{c['id']} failed: {e!r}", exc=True)
            db.update("clips", c["id"], status="upload failed")
