"""Everything that talks to YouTube for input: metadata, video, chat, comments."""
import json
import re
import subprocess
import threading
import time
import urllib.parse

import yt_dlp

from . import config
from .config import YT_COOKIES


BOT_CHECK = "Sign in to confirm you"  # YouTube's anti-bot wall ("...you're not a bot"): needs cookies (config.YT_COOKIES)
AGE_CHECK = "confirm your age"  # an age-restricted video: just skip it (only a signed-in, age-verified account gets it)


def ydl(opts, cookies=True):
    """yt_dlp.YoutubeDL with the YouTube cookies added, when there are any (every YouTube call goes through this).
    IPv4 only by default (config.YT_FORCE_IPV4): yt-dlp's --force-ipv4. cookies=False: without them (picture
    searches don't need your login, and reading a browser's cookies can wait on the macOS keychain prompt)."""
    if config.YT_FORCE_IPV4:
        opts = {**opts, "source_address": "0.0.0.0"}
    if not cookies:
        return yt_dlp.YoutubeDL(opts)
    if config.YT_COOKIES_FROM_BROWSER:
        opts = {**opts, "cookiesfrombrowser": (config.YT_COOKIES_FROM_BROWSER,)}
    elif YT_COOKIES.exists():
        opts = {**opts, "cookiefile": str(YT_COOKIES)}
    return yt_dlp.YoutubeDL(opts)


def age_restricted(e):
    return AGE_CHECK in str(e)


def bot_checked(e):
    """True when an error is YouTube's 'Sign in to confirm you're not a bot'; remembers when, for the UI banner.
    'Sign in to confirm your age' (an age-restricted video) is NOT a bot check: that video is skipped, nothing pauses."""
    s = str(e)
    if BOT_CHECK not in s or AGE_CHECK in s:
        return False
    from . import db
    db.set_setting("yt_bot_check", str(time.time()))
    return True


def cookies_state():
    """For the UI: where cookies come from, and the last bot check (if newer than the cookies)."""
    src = (f"browser: {config.YT_COOKIES_FROM_BROWSER}" if config.YT_COOKIES_FROM_BROWSER
           else "cookies.txt" if YT_COOKIES.exists() else "")
    from . import db
    hit = float(db.get_setting("yt_bot_check") or 0)
    fresh = YT_COOKIES.stat().st_mtime if YT_COOKIES.exists() else 0
    return {"source": src, "bot_check_at": hit if hit > fresh and time.time() - hit < 6 * 3600 else None}


FRAGMENTS = 8  # YouTube throttles each connection; fetching 8 chunks at once is several times faster


def height_for(hq):
    """Source quality: up to 1080p by default (fast), up to 4K when the job asks for it ("4K quality" box).
    4K is ~10x the data but sharper for full-screen 9:16, where a 1080p source's slice is only 608px wide."""
    return 2160 if hq else 1080


def fmt(height):
    """yt-dlp format + sort: best video up to `height` (highest fps first) + best audio."""
    return f"bv*[height<={height}]+ba/b[height<={height}]/b", [f"res:{height}", "fps", "vbr", "abr"]


def info(url, comments=False):
    opts = {"quiet": True, "skip_download": True, "getcomments": comments,
            "extractor_args": {"youtube": {"max_comments": ["500"]}}}
    with ydl(opts) as y:
        return y.extract_info(url, download=False)


# YouTube search filters ("sp" in the results URL): uploaded this week, then this month, then any time.
RECENT = ("EgIIAw%3D%3D", "EgIIBA%3D%3D", "")


def search(query, n=20, recent=RECENT[0], cookies=True):
    """YouTube search results (flat: id, title, duration, view_count, live_status...), no download."""
    # gl/hl = the country and language YouTube ranks results for (US English by default: US-trending videos).
    url = (f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}&gl={config.SEARCH_REGION}&hl=en"
           + (f"&sp={recent}" if recent else ""))
    with ydl({"quiet": True, "extract_flat": True, "playlistend": n, "skip_download": True}, cookies) as y:
        es = y.extract_info(url, download=False).get("entries") or []
    return [e for e in es if e and e.get("id") and e.get("ie_key") == "Youtube"]  # videos only, no channels/playlists


SEARCH_THREADS = 4  # a few at once: ~4x faster topic research, still gentle enough not to trip YouTube's bot check


def search_many(queries, n=20, recent=RECENT[0]):
    """search() for every query in parallel -> [(query, results, error or None)] in query order."""
    from concurrent.futures import ThreadPoolExecutor

    def one(q):
        try:
            return q, search(q, n, recent), None
        except Exception as e:
            return q, [], e
    with ThreadPoolExecutor(min(SEARCH_THREADS, max(1, len(queries)))) as ex:
        return list(ex.map(one, queries))


MIN_FREE_GB = 5  # a download doesn't start with less free space than this (a 4K source can be 2 GB+)


def disk_ok(path):
    """Raise a clear error when the disk is nearly full, instead of filling it up mid-download."""
    import shutil
    from pathlib import Path
    free = shutil.disk_usage(Path(path).parent if not Path(path).is_dir() else path).free / 1e9
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free on the disk: free some space (Clipper needs {MIN_FREE_GB} GB to "
                           "download a video safely)")


def download_vod(url, out_path, height=1080, stop=None, progress=None):
    """stop: a threading.Event; when it's set, the download is cancelled at the next progress update (~every chunk).
    progress: called with 0..1 as it downloads (video and audio are fetched one after the other: each 0..1)."""
    import threading
    disk_ok(out_path)
    form, sort = fmt(height)
    last, lock = [0.0], threading.Lock()

    def hook(d):
        # yt-dlp calls this from all FRAGMENTS download threads at once. Only the Stop button may raise here: any
        # other exception (e.g. the progress write failing) makes yt-dlp drop a fragment and fail the whole merge.
        if stop is not None and stop.is_set():
            raise yt_dlp.utils.DownloadCancelled("stopped by you")
        try:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if progress and d.get("status") == "downloading" and total:
                with lock:  # at most one progress write a second, from one thread at a time
                    if time.time() - last[0] < 1:
                        return
                    last[0] = time.time()
                progress(min(d.get("downloaded_bytes", 0) / total, 1.0))
        except Exception:
            pass
    opts = {"quiet": True, "noprogress": True, "outtmpl": str(out_path), "merge_output_format": "mp4", "format": form,
            "format_sort": sort, "concurrent_fragment_downloads": FRAGMENTS, "ffmpeg_location": config.FFMPEG,
            "progress_hooks": [hook], "fragment_retries": 10, "retries": 10}
    for attempt in (1, 2):
        try:
            with ydl(opts) as y:
                y.download([url])
            return
        except yt_dlp.utils.DownloadError:
            if attempt == 2 or (stop is not None and stop.is_set()):
                raise
            # a broken half-download (e.g. a missing fragment): clear its pieces and start once more from scratch
            from pathlib import Path
            folder = Path(out_path).parent
            for f in folder.glob(Path(out_path).name.split("%")[0] + "*.part*"):
                f.unlink(missing_ok=True)


def download_section(url, out_path, start, end, height=1080, tries=2, cookies=True):
    """Only [start, end) seconds of a video (cut exactly: keyframes are forced at the edges), e.g. to re-render a clip
    whose source was deleted."""
    form, sort = fmt(height)
    opts = {"quiet": True, "noprogress": True, "outtmpl": str(out_path), "merge_output_format": "mp4", "format": form,
            "format_sort": sort, "ffmpeg_location": config.FFMPEG, "retries": 10, "fragment_retries": 10,
            "download_ranges": yt_dlp.utils.download_range_func(None, [(max(start, 0), end)]),
            "force_keyframes_at_cuts": True, "socket_timeout": 20}  # a stalled connection fails instead of hanging
    from pathlib import Path
    for attempt in range(1, tries + 1):  # a stream that drops mid-cut ("ffmpeg exited with code 8") usually works the second time
        try:
            with ydl(opts, cookies) as y:
                y.download([url])
            return
        except yt_dlp.utils.DownloadError:
            if attempt == tries:
                raise
            stem = Path(out_path).name.split("%")[0]  # only this download's own partial files, nothing else there
            for f in Path(out_path).parent.glob(stem + "*"):
                if f.is_file():
                    f.unlink(missing_ok=True)
            time.sleep(3)


def record_live_chunk(url, out_path, seconds, stop, height=1080):
    """Record `seconds` of a live stream from the live edge. Returns False if the stream is over.
    If `stop` is set mid-chunk, ffmpeg is told to quit and the partial chunk is kept."""
    form, sort = fmt(height)
    with ydl({"quiet": True, "format": form, "format_sort": sort}) as y:
        i = y.extract_info(url, download=False)  # re-extract each chunk: HLS URLs expire
    if not i.get("is_live"):
        return False
    # YouTube live usually serves video and audio as separate HLS streams.
    inputs = [x for f in i.get("requested_formats") or [i] for x in ("-i", f["url"])]
    maps = ["-map", "0:v", "-map", "1:a"] if len(inputs) == 4 else []
    p = subprocess.Popen([config.FFMPEG, "-y", "-loglevel", "error", *inputs, *maps, "-t", str(seconds),
                          "-c", "copy", str(out_path)], stdin=subprocess.PIPE)
    while p.poll() is None:
        if stop.wait(1):
            p.communicate(b"q", timeout=30)  # 'q' = graceful ffmpeg quit, file stays valid
    return True


def chat_replay_times(url):
    """Seconds-into-video of every chat message in a past stream's replay ([] if none)."""
    from chat_downloader import ChatDownloader
    try:
        return [m["time_in_seconds"] for m in ChatDownloader().get_chat(url) if m.get("time_in_seconds", -1) >= 0]
    except Exception:
        return []


class LiveChat(threading.Thread):
    """Collects wall-clock timestamps of live chat messages until stopped."""

    def __init__(self, url, stop):
        super().__init__(daemon=True)
        self.url, self.stop, self.times = url, stop, []

    def run(self):
        from chat_downloader import ChatDownloader
        try:
            for m in ChatDownloader().get_chat(self.url):
                self.times.append(m.get("timestamp", time.time() * 1e6) / 1e6)
                if self.stop.is_set():
                    return
        except Exception:
            pass  # ponytail: chat is a bonus signal; audio still works without it


def captions(info, spoken=None):
    """YouTube's own captions for a video, as words with timestamps [{"w", "s", "e"}], or None.
    Prefers captions made by the uploader in the video's language, then YouTube's automatic ones in the ORIGINAL
    language ("hi-orig": not a machine translation). Instant, and usually better than Whisper for Hindi etc.
    json3 gives automatic captions word by word; uploaded ones come as lines, spread evenly over their time.
    spoken: the language detected from the audio (transcribe.detect). Given, only captions in that language are used:
    YouTube's automatic captioner sometimes guesses the language wrong (English commentary captioned as "ar-orig")."""
    lang = spoken or (info.get("language") or "").split("-")[0]
    subs, auto = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    pick = None
    manual = [k for k in subs if k != "live_chat"]
    same = lambda k: lang and k.split("-")[0] == lang
    for k in ([x for x in manual if same(x)] or (manual if len(manual) == 1 and not spoken else [])):
        pick, src = subs[k], k
        break
    if not pick:
        origs = [x for x in auto if x.endswith("-orig")]
        for k in ([x for x in origs if same(x)] if spoken else origs + ([lang] if lang in auto else [])):
            pick, src = auto[k], k
            break
    url = next((f["url"] for f in pick or [] if f.get("ext") == "json3"), None)
    if not url:
        return None
    with ydl({"quiet": True}) as y:
        data = json.loads(y.urlopen(url).read().decode("utf-8"))
    out = []
    for ev in data.get("events", []):
        segs, t0, dur = ev.get("segs") or [], ev.get("tStartMs", 0), ev.get("dDurationMs", 0)
        pieces = [(t0 + s.get("tOffsetMs", 0), s.get("utf8", "")) for s in segs if s.get("utf8", "").strip()]
        for n, (start_ms, text) in enumerate(pieces):
            end_ms = pieces[n + 1][0] if n + 1 < len(pieces) else t0 + dur
            ws = text.split()
            step = max(end_ms - start_ms, 1) / len(ws)
            for k, w in enumerate(ws):
                out.append({"w": w, "s": round((start_ms + k * step) / 1000, 2), "e": round((start_ms + (k + 1) * step) / 1000, 2)})
    if not out:
        return None
    out.sort(key=lambda w: w["s"])
    return {"words": out, "source": src}


TS = re.compile(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b")


def comment_times(comments):
    """Timestamps people typed in comments, like '12:34 lmao' -> 754."""
    out = []
    for c in comments or []:
        for h, m, s in TS.findall(c.get("text", "")):
            out.append(int(h or 0) * 3600 + int(m) * 60 + int(s))
    return out
