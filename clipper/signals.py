"""Turns raw signals into one per-second 'hype' curve, then picks candidate windows from it."""
import re
import subprocess

import numpy as np

from .config import FFMPEG

CHAT_LAG = 8  # people type a few seconds after the moment happens

# Relative trust in each signal. YouTube's "most replayed" is real viewer behaviour, so it wins.
WEIGHTS = {"heatmap": 3.0, "chat": 2.0, "comments": 1.5, "audio": 1.0}


def loudness(path):
    """Per-second momentary loudness (LUFS) via ffmpeg's ebur128 filter."""
    p = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-vn",
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


def hype_curve(n, audio=None, heatmap=None, chat=None, comments=None):
    curve = np.zeros(n)
    used = []
    for name, sig in [("audio", smooth(_fit(audio, n), 3) if audio is not None else None),
                      ("heatmap", heatmap_curve(heatmap, n) if heatmap else None),
                      ("chat", smooth(counts(chat, n, CHAT_LAG), 10) if chat else None),
                      ("comments", smooth(counts(comments, n), 30) if comments else None)]:
        if sig is not None and sig.any():
            curve += WEIGHTS[name] * norm(_fit(sig, n))
            used.append(name)
    return curve, used


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
    curve, used = hype_curve(300, audio=c, chat=[110] * 50)
    assert used == ["audio", "chat"] and curve[105:115].mean() > curve[:50].mean()
    from .download import comment_times
    assert comment_times([{"text": "1:02:03 and 4:05 lol"}]) == [3723, 245]
    print("signals ok")
