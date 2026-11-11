"""Everything that talks to YouTube for input: metadata, video, chat, comments."""
import re
import subprocess
import threading
import time

import yt_dlp

from .config import FFMPEG


def info(url, comments=False):
    opts = {"quiet": True, "skip_download": True, "getcomments": comments,
            "extractor_args": {"youtube": {"max_comments": ["500"]}}}
    with yt_dlp.YoutubeDL(opts) as y:
        return y.extract_info(url, download=False)


def download_vod(url, out_path):
    opts = {"quiet": True, "outtmpl": str(out_path), "merge_output_format": "mp4",
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b", "ffmpeg_location": FFMPEG}
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([url])


def record_live_chunk(url, out_path, seconds, stop):
    """Record `seconds` of a live stream from the live edge. Returns False if the stream is over.
    If `stop` is set mid-chunk, ffmpeg is told to quit and the partial chunk is kept."""
    with yt_dlp.YoutubeDL({"quiet": True, "format": "bv*[height<=1080]+ba/b[height<=1080]/b"}) as y:
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


TS = re.compile(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b")


def comment_times(comments):
    """Timestamps people typed in comments, like '12:34 lmao' -> 754."""
    out = []
    for c in comments or []:
        for h, m, s in TS.findall(c.get("text", "")):
            out.append(int(h or 0) * 3600 + int(m) * 60 + int(s))
    return out
