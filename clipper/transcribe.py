import os
import subprocess
import sys
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

from .config import FFMPEG, WHISPER_MODEL

_lock = threading.Lock()  # ponytail: one transcription at a time; one model, one GPU


def _cuda_dlls():
    """Windows: make the pip-installed NVIDIA DLLs (pip install -e .[gpu]) findable by ctranslate2."""
    nv = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for b in nv.glob("*/bin") if os.name == "nt" else ():
        os.add_dll_directory(str(b))
        os.environ["PATH"] = f"{b}{os.pathsep}{os.environ['PATH']}"


@lru_cache(maxsize=1)
def _model():
    """GPU (CUDA float16) if it works, else CPU int8 on every core. The warm-up catches missing CUDA/cuDNN,
    which otherwise only fails on the first real transcription."""
    _cuda_dlls()
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
        list(m.transcribe(np.zeros(16000, np.float32))[0])
        print(f"whisper: {WHISPER_MODEL} on GPU (cuda float16)")
        return m
    except Exception as e:
        print(f"whisper: GPU unavailable ({e!r}), using CPU")
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)


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


def snap(ws, start, end, reach=6.0):
    """Nudge [start, end] out to sentence boundaries (at most `reach` s each way) so a clip never starts
    or stops mid-sentence. The LLM only sees a marker every few seconds, so its times are guesses.
    Uses Whisper's punctuation; ws must be sorted by time."""
    ends = lambda w: w["w"].endswith((".", "?", "!"))
    i = next((k for k, w in enumerate(ws) if w["s"] >= start), len(ws))  # first word inside the clip
    while i > 0 and not ends(ws[i - 1]) and ws[i - 1]["s"] >= start - reach:  # walk back to sentence start
        i -= 1
    if i < len(ws) and ws[i]["s"] < start:
        start = max(0.0, ws[i]["s"] - 0.2)
    j = next((k for k in range(len(ws) - 1, -1, -1) if ws[k]["s"] < end), -1)  # last word inside the clip
    while 0 <= j < len(ws) - 1 and not ends(ws[j]) and ws[j + 1]["e"] <= end + reach:  # walk on to sentence end
        j += 1
    if j >= 0:
        end = max(end, ws[j]["e"] + 0.5)  # small tail so the last word/reaction isn't clipped
    return start, end


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


if __name__ == "__main__":
    W = lambda t, w: {"w": w, "s": t, "e": t + 0.4}
    ws = [W(0, "Hi."), W(1, "So"), W(2, "the"), W(3, "cops"), W(4, "came."), W(5, "Then"), W(6, "boom."), W(9, "Wow.")]
    assert snap(ws, 2.5, 5.5) == (0.8, 6.9), snap(ws, 2.5, 5.5)       # mid-sentence both ends -> widened
    assert snap(ws, 0.9, 4.6) == (0.9, 4.9), snap(ws, 0.9, 4.6)       # already clean: only the small tail
    assert snap(ws, 2.5, 5.5, reach=1)[0] == 1.8                      # never walks back more than reach
    print("transcribe ok")
