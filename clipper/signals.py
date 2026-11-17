"""Turns raw signals into one per-second 'hype' curve, then picks candidate windows from it."""
import re
import subprocess

import numpy as np

from . import config

CHAT_LAG = 8  # people type a few seconds after the moment happens

# Relative trust in each signal. YouTube's "most replayed" is real viewer behaviour, so it wins. These are the
# starting values: learn.weights() adjusts them from which clips actually got views on your channel.
WEIGHTS = {"heatmap": 3.0, "chat": 2.0, "reactions": 2.0, "comments": 1.5, "audio": 1.0, "visual": 0.8}
SCENE_CUT = 0.3  # ffmpeg scene score (0..1) above which a frame starts a new shot

# Caption tags that mean the audience/people on screen reacted: YouTube puts them in automatic captions
# ("[Laughter]", "[Applause]", "[हंसी]", "[हौसला बढ़ाने की आवाज़]" = cheering). Music / noise tags are left out.
REACTION_RE = re.compile(r"laugh|chuckl|cheer|applau|clap|scream|gasp|whoop|crowd|hans|हंस|हँस|हौसला|ताली|तालि|चीख|"
                         r"ठहाक|रिसा|risa|aplau|ovaci", re.I)


def loudness(path):
    """Per-second momentary loudness (LUFS) via ffmpeg's ebur128 filter."""
    p = subprocess.run([config.FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-vn",
                        "-af", "ebur128=framelog=info", "-f", "null", "-"],
                       capture_output=True, text=True)
    per_sec = {}
    for t, m in re.findall(r"t:\s*([\d.]+)\s+.*?M:\s*(-?[\d.]+|-inf)", p.stderr):
        if m != "-inf":
            per_sec.setdefault(int(float(t)), []).append(float(m))
    if not per_sec:
        return np.zeros(0)
    out = np.full(max(per_sec) + 1, -70.0)
    for s, v in per_sec.items():
        out[s] = max(v)
    return np.maximum(out, -70)  # silence floor


def scenes(path):
    """Shot changes and how much the picture changes, from 2 small frames per second (ffmpeg's scene score).
    -> (cut times in seconds, per-second max scene score 0..1). Software decoding: on a Mac it's faster than
    VideoToolbox here because the frames would have to come back from the GPU."""
    p = subprocess.run([config.FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf",
                        "fps=2,scale=160:-2,select='gte(scene,0)',metadata=print:key=lavfi.scene_score", "-f", "null", "-"],
                       capture_output=True, text=True)
    per_sec, cuts, t = {}, [], None
    for line in p.stderr.splitlines():
        if "pts_time:" in line:
            m = re.search(r"pts_time:([\d.]+)", line)
            t = float(m.group(1)) if m else None
        elif "lavfi.scene_score=" in line and t is not None:
            s = float(line.rsplit("=", 1)[1])
            per_sec[int(t)] = max(per_sec.get(int(t), 0), s)
            if s >= SCENE_CUT:
                cuts.append(round(t, 2))
    out = np.zeros(max(per_sec) + 1 if per_sec else 0)
    for s, v in per_sec.items():
        out[s] = v
    return cuts, out


def reaction_times(words):
    """Times of reaction tags in caption words ([Laughter], [हंसी]...). Tags can span several words: "[a b c]"."""
    out, tag, start = [], [], None
    for w in words:
        if w["w"].startswith("[") or tag:
            if not tag:
                start = w["s"]
            tag.append(w["w"])
            if w["w"].endswith("]"):
                if REACTION_RE.search(" ".join(tag)):
                    out.append(start)
                tag = []
            elif len(tag) > 8:  # not a tag after all
                tag = []
    return out


def counts(times, n, shift=0):
    a = np.zeros(n)
    for t in times:
        i = int(t - shift)
        if 0 <= i < n:
            a[i] += 1
    return a


def heatmap_curve(heatmap, n):
    a = np.zeros(n)
    for h in heatmap or []:
        a[int(h["start_time"]):int(h["end_time"]) + 1] = h["value"]
    return a[:n]


def smooth(a, w):
    return np.convolve(a, np.ones(w) / w, mode="same") if len(a) else a


def norm(a):
    """Robust 0..1 scale: median -> 0, 95th percentile -> 1. Flat signals become all zeros."""
    if not len(a):
        return a
    lo, hi = np.median(a), np.percentile(a, 95)
    return np.clip((a - lo) / (hi - lo), 0, 1) if hi > lo else np.zeros_like(a)


def hype_curve(n, audio=None, heatmap=None, chat=None, comments=None, reactions=None, visual=None, weights=None):
    """-> (curve, parts). curve = weighted sum; parts = {name: 0..1 array} for each signal that had data.
    Audio counts its RISE as well as its level: constant gunfire / music is loud everywhere, a sudden jump isn't.
    weights: per-signal weights (learn.weights()); default WEIGHTS."""
    w = {**WEIGHTS, **(weights or {})}
    curve = np.zeros(n)
    parts = {}
    loud = _fit(audio, n) if audio is not None else None
    if loud is not None and len(loud):
        rise = np.maximum(loud - np.convolve(loud, np.ones(15) / 15, mode="same"), 0)  # above its own recent level
        loud = norm(smooth(loud, 3)) * 0.5 + norm(smooth(rise, 3)) * 0.5
    for name, sig in [("audio", loud),
                      ("heatmap", heatmap_curve(heatmap, n) if heatmap else None),
                      ("chat", smooth(counts(chat, n, CHAT_LAG), 10) if chat else None),
                      ("reactions", smooth(counts(reactions, n), 6) if reactions else None),
                      ("comments", smooth(counts(comments, n), 30) if comments else None),
                      ("visual", smooth(_fit(visual, n), 5) if visual is not None and len(visual) else None)]:
        if sig is not None and sig.any():
            parts[name] = norm(_fit(sig, n))
            curve += w.get(name, 1.0) * parts[name]
    return curve, parts


def peak_windows(curve, length, k, cuts=(), lead=0.6, snap=6):
    """k candidate windows built around the hottest peaks (not fixed blocks): each starts `lead` of its length before
    the peak (the setup) and runs on past it (the payoff), and both edges move to the nearest shot change within
    `snap` seconds, so a clip starts and ends on a cut. Peaks closer than half a window to a taken one are skipped.
    -> [(start, end, mean hype)]"""
    n = len(curve)
    length = min(length, n)
    if length <= 0:
        return []
    sm = smooth(curve, 7)
    cuts = np.asarray(sorted(cuts), dtype=float)
    blocked = np.zeros(n, bool)

    def to_cut(t):
        if not len(cuts):
            return t
        i = int(np.argmin(np.abs(cuts - t)))
        return float(cuts[i]) if abs(cuts[i] - t) <= snap else t
    out = []
    while len(out) < k and not blocked.all():
        peak = int(np.argmax(np.where(blocked, -np.inf, sm)))
        s = min(max(0.0, peak - lead * length), n - length)
        s = max(0.0, to_cut(s))
        e = min(float(n), to_cut(s + length))
        if e - s < length * 0.5:
            e = min(float(n), s + length)
        out.append((round(s, 2), round(e, 2), float(curve[int(s):int(e)].mean()) if e > s else 0.0))
        blocked[max(0, peak - length // 2):peak + length // 2 + 1] = True
    return out


def _fit(a, n):
    a = np.asarray(a, dtype=float)
    return np.pad(a, (0, max(0, n - len(a))), constant_values=a.min() if len(a) else 0)[:n]


def top_windows(curve, length, k):
    """Greedy pick of k non-overlapping windows with the highest summed hype."""
    n = len(curve)
    length = min(length, n)
    if length <= 0:
        return []
    sums = np.convolve(curve, np.ones(length), mode="valid")  # sums[i] = curve[i:i+length].sum()
    taken = np.zeros(len(sums), bool)
    out = []
    while len(out) < k and not taken.all():
        i = int(np.argmax(np.where(taken, -np.inf, sums)))
        out.append((i, i + length, float(sums[i] / length)))
        taken[max(0, i - length + 1):i + length] = True
    return out


if __name__ == "__main__":
    c = np.zeros(300)
    c[100:120] = 5
    c[250:260] = 3
    w = top_windows(c, 30, 2)
    assert w[0][0] <= 100 and w[0][1] >= 120, w
    assert w[1][0] <= 250 and w[1][1] >= 260, w
    assert counts([10, 10, 20], 30, shift=8)[2] == 2
    curve, parts = hype_curve(300, audio=c, chat=[110] * 50)
    assert list(parts) == ["audio", "chat"] and curve[105:115].mean() > curve[:50].mean()
    pw = peak_windows(c, 30, 2, cuts=[88.0, 117.0])
    assert pw[0][0] == 88.0 and pw[0][1] == 117.0 and pw[1][0] <= 250 <= pw[1][1], pw  # snapped to the cuts
    ws = [{"w": "[Laughter]", "s": 5}, {"w": "so", "s": 6}, {"w": "[हौसला", "s": 9}, {"w": "बढ़ाने", "s": 9.2},
          {"w": "की", "s": 9.3}, {"w": "आवाज़]", "s": 9.4}, {"w": "[Music]", "s": 12}]
    assert reaction_times(ws) == [5, 9], reaction_times(ws)
    from .download import comment_times
    assert comment_times([{"text": "1:02:03 and 4:05 lol"}]) == [3723, 245]
    print("signals ok")
