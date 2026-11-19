"""Learning from what actually worked on your channel(s).

Two things, both from clips that went up and got view counts (stats.refresh fills clips.views every hour):
  examples(category)  your best and worst clips (title, what happened, length, views) for the AI picking new clips:
                      "this is what this audience watched / skipped". Same category first, else the whole brand.
  weights()           how much each audience signal (heatmap, chat, reactions, loudness, visual change...) should count
                      in the hype curve: a signal whose strength inside a clip went along with more views gets more
                      weight, one that didn't gets less. Needs MIN_CLIPS clips with views; until then the defaults.
Every clip stores its signal strengths when it's made (clips.signals), which is what weights() learns from."""
import json
import math
import time

from . import db, signals

MIN_CLIPS = 8  # clips with views needed before the weights move
MAX_SHIFT = 0.6  # a weight moves at most this share up or down (default 2.0 -> 0.8..3.2)


def _views(c):
    try:
        return sum(int(v) for v in json.loads(c.get("views") or "{}").values())
    except (TypeError, ValueError):
        return 0


def _scored(clips):
    return [(c, _views(c)) for c in clips if c.get("uploads") and c["uploads"] != "{}" and _views(c) > 0]


def examples(category_id=None, k_top=5, k_bottom=3):
    """-> text for the AI prompt ("" when there's nothing to learn from yet)."""
    from . import categories
    cat = categories.get(category_id) if category_id else None
    pool = db.all("clips", "category_id=?", (category_id,)) if category_id else []
    scored = _scored(pool)
    if len(scored) < 4 and cat:  # too few in this category: use the whole brand's
        ids = {c["id"] for c in categories.all(brand_id=cat["brand_id"])}
        scored = _scored([c for c in db.all("clips") if c["category_id"] in ids])
    if len(scored) < 4:
        return ""
    scored.sort(key=lambda x: -x[1])
    line = lambda c, v: (f'- "{c["title"]}" ({round(c["end"] - c["start"])}s, {v:,} views): '
                         f'{(c.get("reason") or "").strip()[:160]}')
    top = [line(c, v) for c, v in scored[:k_top]]
    bottom = [line(c, v) for c, v in scored[-k_bottom:] if (c, v) not in scored[:k_top]]
    return ("What worked on this channel before (most viewed first): pick moments like these.\n" + "\n".join(top)
            + ("\n\nWhat didn't (fewest views): avoid moments like these.\n" + "\n".join(bottom) if bottom else ""))


def _corr(xs, ys):
    """Spearman rank correlation (robust to a few viral outliers)."""
    def ranks(a):
        order = sorted(range(len(a)), key=lambda i: a[i])
        r = [0.0] * len(a)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def update():
    """Recompute the learned weights from every clip with views and stored signals. Called after views refresh."""
    rows = [(json.loads(c.get("signals") or "{}"), v) for c, v in _scored(db.all("clips"))]
    rows = [(s, v) for s, v in rows if s]
    out = {"weights": {}, "clips": len(rows), "at": time.time(), "detail": {}}
    if len(rows) >= MIN_CLIPS:
        for name, base in signals.WEIGHTS.items():
            pts = [(s[name], v) for s, v in rows if name in s]
            if len(pts) < MIN_CLIPS:
                continue
            r = _corr([p[0] for p in pts], [math.log1p(p[1]) for p in pts])
            out["weights"][name] = round(base * (1 + max(-MAX_SHIFT, min(MAX_SHIFT, r))), 2)
            out["detail"][name] = {"correlation": round(r, 2), "clips": len(pts), "default": base}
    db.set_setting("learned", json.dumps(out))
    if out["weights"]:
        db.syslog(f"learned signal weights from {len(rows)} clips: {out['weights']}")
    try:  # facecam vs clean clips: shift each category's source mix toward what gets more views
        from . import page
        out["styles"] = page.learn_styles()
    except Exception as e:
        db.syslog(f"learning the facecam / clean mix failed: {e!r}")
    return out


def weights():
    """The signal weights to use now: learned ones where there's enough data, defaults otherwise."""
    raw = db.get_setting("learned")
    return (json.loads(raw).get("weights") if raw else None) or {}


def status():
    raw = db.get_setting("learned")
    d = json.loads(raw) if raw else {"weights": {}, "clips": 0, "detail": {}}
    return {**d, "defaults": signals.WEIGHTS, "min_clips": MIN_CLIPS}


if __name__ == "__main__":
    assert round(_corr([1, 2, 3, 4], [10, 20, 30, 40]), 2) == 1.0 and round(_corr([1, 2, 3, 4], [4, 3, 2, 1]), 2) == -1.0
    print("learn ok")
