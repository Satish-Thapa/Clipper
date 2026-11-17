import os
import subprocess
import sys
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import db
from . import config

"""Speech to text, with word timestamps. Engines (Settings → Clips → Transcription engine):
  auto   Apple Silicon Mac: the GPU through MLX (mlx-whisper). Windows/Linux: an NVIDIA GPU (CUDA) if it works, else CPU.
  mlx    Mac GPU.            cpu    faster-whisper on the CPU (slow; batched).
  cloud  any OpenAI-compatible speech-to-text API (Groq, OpenAI...): fast, paid per minute, audio leaves the computer.
YouTube videos usually skip all of this: their own captions are used when they exist (download.captions)."""
import json
import platform

# Transcriptions that may run at once (two jobs, or live chunks queuing up). Each engine shares the CPU/GPU between them.
_slots = threading.BoundedSemaphore(2)
_gpu = threading.Lock()  # MLX: one at a time on the Mac GPU
MLX_REPOS = {"tiny": "mlx-community/whisper-tiny-mlx", "base": "mlx-community/whisper-base-mlx",
             "small": "mlx-community/whisper-small-mlx", "medium": "mlx-community/whisper-medium-mlx",
             "large-v3": "mlx-community/whisper-large-v3-mlx", "large-v3-turbo": "mlx-community/whisper-large-v3-turbo"}


def _cuda_dlls():
    """Windows: make the pip-installed NVIDIA DLLs (pip install -e .[gpu]) findable by ctranslate2."""
    nv = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for b in nv.glob("*/bin") if os.name == "nt" else ():
        os.add_dll_directory(str(b))
        os.environ["PATH"] = f"{b}{os.pathsep}{os.environ['PATH']}"


DEVICE = ["Whisper (not loaded yet)"]


@lru_cache(maxsize=1)
def _has_mlx():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def engine():
    """The engine in use: "mlx", "cloud", or "local" (faster-whisper: CUDA if it works, else CPU)."""
    e = config.TRANSCRIBE_ENGINE
    if e == "cloud":
        return "cloud"
    if e == "mlx" or (e == "auto" and _has_mlx()):
        return "mlx" if _has_mlx() else "local"
    return "local"


def model_name():
    """WHISPER_MODEL, with "auto" = large-v3-turbo on a GPU (accurate, fast there), small on a CPU."""
    m = config.WHISPER_MODEL
    if m != "auto":
        return m
    if engine() == "mlx":
        return "large-v3-turbo"
    _model()  # local: loads, finds out whether CUDA works
    return _local_name[0]


def language():
    return (config.WHISPER_LANGUAGE or "").strip().lower() or None


def device():
    """"Whisper large-v3-turbo on Mac GPU" etc., for the log."""
    e = engine()
    if e == "mlx":
        return f"Whisper {model_name()} on Mac GPU"
    if e == "cloud":
        return f"cloud {config.TRANSCRIBE_API_MODEL}"
    _model()
    return DEVICE[0]


def workers():
    return max(1, min(int(config.TRANSCRIBE_WORKERS or 1), 4))


_local_name = ["small"]


@lru_cache(maxsize=1)
def _model():
    """faster-whisper: GPU (CUDA float16) if it works, else CPU int8. The warm-up catches missing CUDA/cuDNN, which
    otherwise only fails on the first real transcription. num_workers = how many transcriptions can run at once."""
    _cuda_dlls()
    from faster_whisper import WhisperModel
    n = workers()
    global _slots
    _slots = threading.BoundedSemaphore(n)
    want = config.WHISPER_MODEL
    try:
        name = "large-v3-turbo" if want == "auto" else want
        m = WhisperModel(name, device="cuda", compute_type="float16", num_workers=n)
        list(m.transcribe(np.zeros(16000, np.float32))[0])
        _local_name[0] = name
        DEVICE[0] = f"Whisper {name} on GPU"
        db.syslog(f"whisper: {name} on GPU (cuda float16)")
        return m
    except Exception as e:
        db.syslog(f"whisper: GPU unavailable ({e!r}), using CPU")
    name = "small" if want == "auto" else want
    _local_name[0] = name
    DEVICE[0] = f"Whisper {name} on CPU"
    return WhisperModel(name, device="cpu", compute_type="int8", num_workers=n,
                        cpu_threads=max(1, (os.cpu_count() or 4) // n))  # the cores, shared by the workers


@lru_cache(maxsize=1)
def _batched():
    """faster-whisper's batched pipeline (the speed-up WhisperX made popular): voice activity detection cuts the audio
    into chunks and several chunks are decoded together."""
    from faster_whisper import BatchedInferencePipeline
    return BatchedInferencePipeline(model=_model())


def reset():
    """Settings changed (engine, model, workers): load again on the next transcription."""
    _model.cache_clear()
    _batched.cache_clear()


def _decode(path, start, end):
    """[start, end) seconds as 16 kHz mono floats, decoded by our ffmpeg (Whisper's own PyAV decoder breaks on newer av)."""
    pcm = subprocess.run([config.FFMPEG, "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
                          "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.float32).copy()


def _local(audio):
    batch = int(config.TRANSCRIBE_BATCH or 0)
    if batch > 1 and len(audio) > 16000 * 30:  # short pieces gain nothing from batching
        try:
            return [(w.word, w.start, w.end) for s in _batched().transcribe(audio, word_timestamps=True, batch_size=batch,
                                                                             language=language())[0] for w in (s.words or [])]
        except Exception as e:  # never lose a job to the faster path: fall back to the plain one
            db.syslog(f"whisper: batched transcription failed ({e!r}), using the sequential one")
    return [(w.word, w.start, w.end) for s in _model().transcribe(audio, word_timestamps=True, vad_filter=True,
                                                                   language=language())[0] for w in (s.words or [])]


def _mlx(audio):
    import mlx_whisper
    with _gpu:
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=MLX_REPOS.get(model_name(), MLX_REPOS["large-v3-turbo"]),
                                   word_timestamps=True, language=language())
    return [(w["word"], float(w["start"]), float(w["end"])) for s in r.get("segments", []) for w in s.get("words", [])]


CLOUD_CHUNK = 20 * 60  # seconds per request: keeps every upload well under the APIs' 25 MB limit


def _cloud(path, start, end):
    """OpenAI-compatible /audio/transcriptions with word timestamps. The span goes up as a small mono MP3."""
    import httpx
    base = (config.TRANSCRIBE_API_URL or "").rstrip("/")
    key = config.TRANSCRIBE_API_KEY or config.DEEPSEEK_API_KEY
    if not base or not key:
        raise RuntimeError("cloud transcription needs an API address and key: Settings → Clips")
    out, t = [], start
    while t < end:
        e = min(end, t + CLOUD_CHUNK)
        mp3 = subprocess.run([config.FFMPEG, "-loglevel", "error", "-ss", str(t), "-t", str(e - t), "-i", str(path),
                              "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-f", "mp3", "-"],
                             capture_output=True, check=True).stdout
        data = {"model": config.TRANSCRIBE_API_MODEL, "response_format": "verbose_json", "timestamp_granularities[]": "word"}
        if language():
            data["language"] = language()
        r = httpx.post(f"{base}/audio/transcriptions", headers={"Authorization": f"Bearer {key}"}, data=data,
                       files={"file": ("audio.mp3", mp3, "audio/mpeg")}, timeout=httpx.Timeout(30, read=600))
        if r.is_error:
            raise RuntimeError(f"transcription API: HTTP {r.status_code} {r.text[:200]}")
        out += [(w["word"], w["start"] + (t - start), w["end"] + (t - start)) for w in r.json().get("words", [])]
        t = e
    return out


# Cloud APIs answer with the language's name ("english"); everything else here uses codes.
LANG_NAMES = {"english": "en", "hindi": "hi", "nepali": "ne", "urdu": "ur", "bengali": "bn", "arabic": "ar",
              "spanish": "es", "portuguese": "pt", "french": "fr", "german": "de", "italian": "it", "russian": "ru",
              "japanese": "ja", "korean": "ko", "chinese": "zh", "turkish": "tr", "indonesian": "id", "tamil": "ta",
              "telugu": "te", "marathi": "mr", "punjabi": "pa", "gujarati": "gu", "polish": "pl", "dutch": "nl",
              "ukrainian": "uk", "vietnamese": "vi", "thai": "th", "filipino": "tl", "tagalog": "tl", "malay": "ms"}


def _detect_one(path, t, seconds):
    """Whisper's guess for the speech in [t, t+seconds): -> (code, confidence 0..1)."""
    e = engine()
    if e == "cloud":
        import httpx
        base, key = (config.TRANSCRIBE_API_URL or "").rstrip("/"), config.TRANSCRIBE_API_KEY or config.DEEPSEEK_API_KEY
        mp3 = subprocess.run([config.FFMPEG, "-loglevel", "error", "-ss", str(t), "-t", str(seconds), "-i", str(path),
                              "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-f", "mp3", "-"], capture_output=True, check=True).stdout
        r = httpx.post(f"{base}/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                       data={"model": config.TRANSCRIBE_API_MODEL, "response_format": "verbose_json"},
                       files={"file": ("audio.mp3", mp3, "audio/mpeg")}, timeout=httpx.Timeout(30, read=120))
        r.raise_for_status()
        name = (r.json().get("language") or "").lower()
        return LANG_NAMES.get(name, name), 1.0
    audio = _decode(path, t, t + seconds)
    if e == "mlx":  # language ID only (one encoder pass), not a transcription
        from mlx_whisper.audio import N_FRAMES, log_mel_spectrogram, pad_or_trim
        from mlx_whisper.decoding import detect_language
        with _gpu:
            m = _mlx_model()
            _, probs = detect_language(m, pad_or_trim(log_mel_spectrogram(audio, n_mels=m.dims.n_mels), N_FRAMES, axis=-2))
        code = max(probs, key=probs.get)
        return code, float(probs[code])
    _, info = _model().transcribe(audio, vad_filter=True)  # the language is decided before any text is decoded
    return info.language, float(info.language_probability or 0)


@lru_cache(maxsize=1)
def _mlx_model():
    from mlx_whisper.load_models import load_model
    return load_model(MLX_REPOS.get(model_name(), MLX_REPOS["large-v3-turbo"]))


def detect(path, samples=3, seconds=30):
    """The language spoken in a video, from a few 30 s samples spread through it (one sample can land on music or
    silence). -> (code like "en", share of the votes 0..1), or (None, 0) if it can't tell."""
    from . import render
    dur = render.duration(path)
    if dur < 5:
        return None, 0.0
    points = [max(0.0, min(dur * f, dur - seconds)) for f in ((0.5,) if dur < seconds * 3 else (0.25, 0.5, 0.75)[:samples])]
    votes = {}
    for t in points:
        try:
            code, p = _detect_one(path, t, min(seconds, dur))
        except Exception as e:
            db.syslog(f"language detection failed at {t:.0f}s: {e!r}")
            continue
        if code:
            votes[code] = votes.get(code, 0) + p
    if not votes:
        return None, 0.0
    best = max(votes, key=votes.get)
    return best, votes[best] / sum(votes.values())


def _words(ws, start):
    return [{"w": w.strip(), "s": round(s + start, 2), "e": round(e + start, 2)} for w, s, e in ws if w.strip()]


def words(path, start, end):
    """Word-level transcript of [start, end) seconds of `path`, with absolute timestamps."""
    return words_many(path, [(start, end)])


def words_many(path, spans, before_each=None):
    """Several spans of one video. Local engines: their audio is decoded in parallel threads (one ffmpeg per span)
    while the first ones are being transcribed. before_each(): called before each span (the Stop checkpoint)."""
    from concurrent.futures import ThreadPoolExecutor
    eng, out = engine(), []
    if eng == "cloud":
        with ThreadPoolExecutor(min(4, max(1, len(spans)))) as ex:  # requests in parallel: the work is remote
            for (s, e), ws in zip(spans, ex.map(lambda sp: _cloud(path, *sp), spans)):
                if before_each:
                    before_each()
                out += _words(ws, s)
        return out
    with ThreadPoolExecutor(min(4, max(1, len(spans)))) as ex:
        audios = [ex.submit(_decode, path, s, e) for s, e in spans]
        for (s, e), a in zip(spans, audios):
            if before_each:
                before_each()
            with _slots:
                ws = _mlx(a.result()) if eng == "mlx" else _local(a.result())
            out += _words(ws, s)
    return out


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
