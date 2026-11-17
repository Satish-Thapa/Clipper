"""Everything that talks to YouTube for input: metadata, video, chat, comments."""
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import yt_dlp

from .config import FFMPEG, SEARCH_REGION, YT_COOKIES, YT_COOKIES_FROM_BROWSER


BOT_CHECK = "Sign in to confirm"  # YouTube's anti-bot wall: needs cookies (see config.YT_COOKIES)


def ydl(opts):
    """yt_dlp.YoutubeDL with the YouTube cookies added, when there are any (every YouTube call goes through this)."""
    if YT_COOKIES_FROM_BROWSER:
        opts = {**opts, "cookiesfrombrowser": (YT_COOKIES_FROM_BROWSER,)}
    elif YT_COOKIES.exists():
        opts = {**opts, "cookiefile": str(YT_COOKIES)}
    return yt_dlp.YoutubeDL(opts)


def bot_checked(e):
    """True when an error is YouTube's 'Sign in to confirm you're not a bot'; remembers when, for the UI banner."""
    if BOT_CHECK not in str(e):
        return False
    from . import db
    db.set_setting("yt_bot_check", str(time.time()))
    return True


def cookies_state():
    """For the UI: where cookies come from, and the last bot check (if newer than the cookies)."""
    src = (f"browser: {YT_COOKIES_FROM_BROWSER}" if YT_COOKIES_FROM_BROWSER
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


def search(query, n=20, recent=RECENT[0]):
    """YouTube search results (flat: id, title, duration, view_count, live_status...), no download."""
    # gl/hl = the country and language YouTube ranks results for (US English by default: US-trending videos).
    url = (f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}&gl={SEARCH_REGION}&hl=en"
           + (f"&sp={recent}" if recent else ""))
    with ydl({"quiet": True, "extract_flat": True, "playlistend": n, "skip_download": True}) as y:
        es = y.extract_info(url, download=False).get("entries") or []
    return [e for e in es if e and e.get("id") and e.get("ie_key") == "Youtube"]  # videos only, no channels/playlists


def download_vod(url, out_path, height=1080):
    form, sort = fmt(height)
    opts = {"quiet": True, "noprogress": True, "outtmpl": str(out_path), "merge_output_format": "mp4", "format": form,
            "format_sort": sort, "concurrent_fragment_downloads": FRAGMENTS, "ffmpeg_location": FFMPEG}
    with ydl(opts) as y:
        y.download([url])


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
    p = subprocess.Popen([FFMPEG, "-y", "-loglevel", "error", *inputs, *maps, "-t", str(seconds),
                          "-c", "copy", str(out_path)], stdin=subprocess.PIPE)
    while p.poll() is None:
        if stop.wait(1):
            p.communicate(b"q", timeout=30)  # 'q' = graceful ffmpeg quit, file stays valid
    return True


def frame(url, out_path):
    """One still frame as a JPEG, for drawing the facecam box: the live edge of a live stream, or a bit into a video
    (past any intro). Only that one frame is fetched, nothing is downloaded."""
    form, sort = fmt(720)
    # Some formats of a video won't open in ffmpeg (e.g. an ended stream's mp4 pieces): try the next kind then.
    for opts in ({"format": form}, {"format": "bv*[height<=720][ext=webm]/bv*[height<=720]"}, {"format": form, "format_sort": sort}):
        with ydl({"quiet": True, **opts}) as y:
            i = y.extract_info(url, download=False)
        f = (i.get("requested_formats") or [i])[0]
        headers = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())  # YouTube wants these
        at = f"{min((i.get('duration') or 0) * 0.3, 600):.1f}"
        # Jump straight there (fast on most videos), else read 20s from the start (long single-file streams hang on a
        # jump), else the very first frame (a stream that just ended can't be seeked yet). Live: the live edge.
        tries = [([], [], 30)] if i.get("is_live") else [(["-ss", at], [], 12), ([], ["-ss", "20"], 30), ([], [], 30)]
        for before, after, secs in tries:
            try:
                r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-headers", headers, *before, "-i", f["url"], *after,
                                    "-frames:v", "1", "-vf", "format=yuvj420p", "-q:v", "3", str(out_path)],
                                   capture_output=True, timeout=secs)
            except subprocess.TimeoutExpired:  # too slow: the next way (the error would show the signed URL)
                continue
            if r.returncode == 0 and Path(out_path).exists():
                return out_path
    raise RuntimeError("ffmpeg couldn't read a frame of this video")  # not ffmpeg's error: it has the signed video URL


# Chat comes from yt-dlp's "live_chat" subtitle track: one JSON line per chat event, for a live stream (followed
# while it runs) and for a past stream's replay. Only real messages count (text, Super Chats, memberships, gifts),
# not YouTube's placeholders / banners / "welcome" notices.
MESSAGE = ("TextMessage", "PaidMessage", "PaidSticker", "Membership", "gift", "Gift")


def chat_events(line):
    """One live_chat.json line -> [(wall-clock seconds or None, seconds into the video or None)] of its messages."""
    try:
        d = json.loads(line)
    except ValueError:
        return []
    r = d.get("replayChatItemAction", d) if isinstance(d, dict) else {}
    offset = r.get("videoOffsetTimeMsec")
    out = []
    for a in r.get("actions", []):
        add = a.get("addChatItemAction") if isinstance(a, dict) else None
        for kind, item in ((add or {}).get("item") or {}).items():
            if isinstance(item, dict) and any(m in kind for m in MESSAGE):
                ts = item.get("timestampUsec")
                out.append((int(ts) / 1e6 if ts else None, int(offset) / 1000 if offset else None))
    return out


def _chat_cmd(url, out):
    cookies = (["--cookies-from-browser", YT_COOKIES_FROM_BROWSER] if YT_COOKIES_FROM_BROWSER
               else ["--cookies", str(YT_COOKIES)] if YT_COOKIES.exists() else [])
    return [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--skip-download", "--write-subs",
            "--sub-langs", "live_chat", *cookies, "-o", str(out), url]


def chat_replay_times(url, timeout=900):
    """Seconds-into-video of every chat message in a past stream's replay ([] if the video has no chat).
    -> (times, error or None)."""
    with tempfile.TemporaryDirectory() as d:
        try:
            p = subprocess.run(_chat_cmd(url, Path(d) / "chat"), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return [], f"chat replay took over {timeout // 60} min, skipped"
        files = list(Path(d).glob("chat*.json*"))
        if not files:
            err = (p.stderr or "").strip().splitlines()
            return [], (err[-1][:200] if err and p.returncode else None)  # None = the video simply has no chat
        times = []
        with open(files[0], encoding="utf-8", errors="replace") as f:
            for line in f:
                times += [o for _, o in chat_events(line) if o is not None and o >= 0]
        return times, None


class LiveChat(threading.Thread):
    """Collects the wall-clock time of every live chat message until `stop` is set: yt-dlp follows the chat into a
    file in the background, and this thread reads the new lines every 2 seconds. `error` says why if it couldn't."""

    def __init__(self, url, stop):
        super().__init__(daemon=True)
        self.url, self.stop, self.times, self.error = url, stop, [], None

    def run(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.Popen(_chat_cmd(self.url, Path(d) / "chat"), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
            pos = {}  # file -> bytes already read (yt-dlp writes .part, then renames it when the stream ends)
            try:
                while True:
                    for f in Path(d).glob("chat*.json*"):
                        try:
                            with open(f, "rb") as fh:
                                fh.seek(pos.get(f.name, 0))
                                data = fh.read()
                        except OSError:
                            continue
                        done = data.rfind(b"\n") + 1  # only complete lines; the rest is read next time
                        pos[f.name] = pos.get(f.name, 0) + done
                        for line in data[:done].decode("utf-8", "replace").splitlines():
                            self.times += [t if t else time.time() for t, _ in chat_events(line)]
                    if self.stop.wait(2) or p.poll() is not None:
                        break
                if p.poll() is not None and p.returncode and not self.times:
                    self.error = ((p.stderr.read() or "").strip().splitlines() or ["yt-dlp exited"])[-1][:200]
            finally:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(10)
                    except subprocess.TimeoutExpired:
                        p.kill()


TS = re.compile(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b")


def comment_times(comments):
    """Timestamps people typed in comments, like '12:34 lmao' -> 754."""
    out = []
    for c in comments or []:
        for h, m, s in TS.findall(c.get("text", "")):
            out.append(int(h or 0) * 3600 + int(m) * 60 + int(s))
    return out
