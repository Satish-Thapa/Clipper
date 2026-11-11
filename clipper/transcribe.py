import subprocess
import threading
from functools import lru_cache

import numpy as np

from .config import FFMPEG, WHISPER_MODEL

_lock = threading.Lock()  # ponytail: one transcription at a time; CPU-bound anyway


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


def words(path, start, end):
    """Word-level transcript of [start, end) seconds of `path`, with absolute timestamps."""
    # Decode with our ffmpeg and hand Whisper a float array (its own PyAV decoder breaks on newer av).
    pcm = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
                          "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    with _lock:
        segs, _ = _model().transcribe(np.frombuffer(pcm, np.float32), word_timestamps=True, vad_filter=True)
        return [{"w": w.word.strip(), "s": round(w.start + start, 2), "e": round(w.end + start, 2)}
                for seg in segs for w in (seg.words or [])]


def merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def text_with_times(ws, start, end, every=5.0):
    """'[12.0] hello there [17.1] next bit' — readable for the LLM, keeps timing info."""
    out, last = [], -1e9
    for w in ws:
        if start <= w["s"] < end:
            if w["s"] - last >= every:
                out.append(f"[{w['s']:.1f}]")
                last = w["s"]
            out.append(w["w"])
    return " ".join(out)
