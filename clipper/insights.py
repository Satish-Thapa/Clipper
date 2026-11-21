"""What works on this channel, from the numbers of everything we posted (stats.refresh fills clips.stats / views).

performance()  -> views per clip broken down by everything we can act on: category, topic, moment type, length,
                  short/long, the hour and weekday it posted, platform; YouTube retention; best and weakest clips.
history_text() -> the same, compact, for the AI.
decide()       -> once a day (autopilot.daily_report): the AI reads the history and writes today's decisions:
                    scout   - for the researcher (which categories / topics / creators to lean into or avoid)
                    cutter  - for the clip picker (which moment types, lengths and hooks work)
                    summary + suggestions for you (e.g. "move a slot to 19:00"; the app never changes your settings)
                  saved as setting "decisions"; scout.trending_topics and pick.learned_note put them in their prompts.

Clips keep their DB row (with numbers) after their file is deleted, so the history survives disk cleanups."""
import json
import time

from . import ai, db

MIN_CLIPS = 5  # fewer posted clips with numbers than this = not enough to learn from yet
FLOP_AGE = 24 * 3600  # a clip only counts as a flop once it's had a day to get views
LENGTHS = ((0, 20, "under 20s"), (20, 35, "20-35s"), (35, 60, "35-60s"), (60, 10_000, "over 60s"))
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _clips():
    """Every clip that went up somewhere, with its numbers and what it was."""
    jobs = {j["id"]: j for j in db.all("jobs")}
    topics = {t["id"]: t for t in db.all("topics")}
    out = []
    for c in db.all("clips"):
        ups = {p: u for p, u in json.loads(c.get("uploads") or "{}").items() if u.get("id")}
        if not ups:
            continue
        st = {p: v for p, v in json.loads(c.get("stats") or "{}").items() if p in ups}
        views = {p: int(v.get("views") or 0) for p, v in st.items()} or \
            {p: int(v or 0) for p, v in json.loads(c.get("views") or "{}").items() if p in ups}
        job = jobs.get(c["job_id"]) or {}
        topic = topics.get(job.get("topic_id"))
        why = json.loads(c.get("why") or "{}")
        at = min((u.get("at") or time.time()) for u in ups.values())
        lt = time.localtime(at)
        eng = sum(int(v.get(k) or 0) for v in st.values() for k in ("likes", "comments", "shares", "saves"))
        out.append({"id": c["id"], "title": c["title"], "headline": c.get("headline") or "", "kind": c["kind"],
                    "seconds": round(c["end"] - c["start"]), "views": views, "total": sum(views.values()),
                    "engagement": eng, "platforms": list(ups), "at": at, "age_h": round((time.time() - at) / 3600, 1),
                    "hour": f"{lt.tm_hour:02d}:00", "weekday": DAYS[lt.tm_wday],
                    "category": job.get("category") or "unknown", "moment": why.get("moment") or "unknown",
                    "mode": job.get("mode") or "normal", "topic": topic["topic"] if topic else None,
                    "source": job.get("title") or job.get("url") or "",
                    "yt_pct": (st.get("youtube") or {}).get("avg_view_pct"), "subs": (st.get("youtube") or {}).get("subs") or 0})
    return out


def _group(clips, key, label=None):
    by = {}
    for c in clips:
        k = key(c)
        g = by.setdefault(k, {"name": label(k) if label else k, "clips": 0, "views": 0, "engagement": 0})
        g["clips"] += 1
        g["views"] += c["total"]
        g["engagement"] += c["engagement"]
    return sorted(({**g, "per_clip": round(g["views"] / g["clips"])} for g in by.values()),
                  key=lambda g: g["per_clip"], reverse=True)


def performance():
    clips = _clips()
    m = [c for c in clips if c["views"]]
    plats = {}
    for c in m:
        for p, v in c["views"].items():
            x = plats.setdefault(p, {"name": p, "clips": 0, "views": 0})
            x["clips"] += 1
            x["views"] += v
    length = lambda c: next(n for lo, hi, n in LENGTHS if lo <= c["seconds"] < hi)
    ret = [c["yt_pct"] for c in m if c["yt_pct"]]
    ranked = sorted(m, key=lambda c: c["total"], reverse=True)
    old = [c for c in ranked if time.time() - c["at"] > FLOP_AGE]
    return {"at": time.time(), "posted": len(clips), "measured": len(m), "views": sum(c["total"] for c in m),
            "engagement": sum(c["engagement"] for c in m), "subs": sum(c["subs"] for c in m),
            "retention": round(sum(ret) / len(ret), 1) if ret else None, "enough": len(m) >= MIN_CLIPS,
            "by": {"category": _group(m, lambda c: c["category"]), "moment": _group(m, lambda c: c["moment"]),
                   "length": _group(m, length), "kind": _group(m, lambda c: c["kind"]),
                   "hour": _group(m, lambda c: c["hour"]), "weekday": _group(m, lambda c: c["weekday"]),
                   "mode": _group(m, lambda c: c["mode"]),
                   "topic": _group(m, lambda c: c["topic"] or f"video: {c['source'][:60]}")},
            "platforms": sorted(({**x, "per_clip": round(x["views"] / x["clips"])} for x in plats.values()),
                                key=lambda x: x["per_clip"], reverse=True),
            "top": ranked[:8], "flops": old[-5:][::-1] if len(old) > 8 else []}


def history_text(perf=None, n=6):
    """Compact channel history for the AI ('' when there isn't enough yet)."""
    perf = perf or performance()
    if not perf["enough"]:
        return ""
    t = lambda c: (f'"{c["title"]}" ({c["total"]:,} views, {c["seconds"]}s, {c["category"]}, {c["moment"]}'
                   + (f', {c["yt_pct"]:.0f}% watched' if c["yt_pct"] else "") + ")")
    g = lambda name: "; ".join(f'{x["name"]} {x["per_clip"]:,}/clip ({x["clips"]})' for x in perf["by"][name][:n])
    lines = [f"Our channel so far: {perf['measured']} clips with numbers, {perf['views']:,} views"
             + (f", {perf['retention']:.0f}% average watched on YouTube" if perf["retention"] else "") + ".",
             "Best clips: " + "; ".join(t(c) for c in perf["top"][:n])]
    for name, what in (("category", "category"), ("topic", "topic"), ("moment", "moment type"), ("length", "length"),
                       ("hour", "posting hour"), ("weekday", "weekday")):
        if len(perf["by"][name]) > 1:
            lines.append(f"Views per clip by {what}: {g(name)}")
    if perf["flops"]:
        lines.append("Weakest clips (a day or more old): " + "; ".join(t(c) for c in perf["flops"]))
    if perf["platforms"]:
        lines.append("Views per clip by platform: " + ", ".join(f'{p["name"]} {p["per_clip"]:,}' for p in perf["platforms"]))
    return "\n".join(lines)


DECIDE_SYSTEM = """You run the daily review of a short-clip channel (gaming, podcasts, sports). You get how every
posted clip did: views per clip by category, topic, moment type, length, short/long, posting hour and weekday,
platform; YouTube retention; the best and weakest clips. Decide for TODAY, citing the numbers:
- "scout": 2-4 short lines for the researcher who picks the topics and videos to clip (which categories, topics,
  creators to lean into or away from).
- "cutter": 2-4 short lines for the editor who picks the moments inside a video (which moment types, lengths and
  hooks got views, what flopped).
- "summary": 2-3 plain sentences for the owner: what is working and what isn't.
- "suggestions": up to 3 concrete changes the owner could make in Settings (e.g. "move the 10:00 short to 19:00:
  evening posts get 2x the views", "give gaming one more slot"). The app never changes settings by itself.
Never invent numbers. With thin or flat data say so and keep the advice light. Reply as JSON:
{"scout": "...", "cutter": "...", "summary": "...", "suggestions": ["..."]}"""


def decide(perf=None):
    """Today's decisions from the history (see the module doc), saved and returned. Not enough data -> a note only."""
    perf = perf or performance()
    text = history_text(perf)
    if not text:
        out = {"at": time.time(), "clips": perf["measured"], "scout": "", "cutter": "", "suggestions": [],
               "summary": f"Not enough to learn from yet: {perf['measured']} of {MIN_CLIPS} posted clips have numbers."}
    else:
        a = ai.ask(text, DECIDE_SYSTEM, "decide", full=True)
        out = {"at": time.time(), "clips": perf["measured"], "views": perf["views"], "by": getattr(ai.LAST, "model", "") or "",
               "scout": str(a.get("scout") or "")[:600], "cutter": str(a.get("cutter") or "")[:600],
               "summary": str(a.get("summary") or "")[:600],
               "suggestions": [str(x)[:200] for x in a.get("suggestions") or []][:3]}
    db.set_setting("decisions", json.dumps(out))
    db.syslog(f"daily decisions ({perf['measured']} clips): {out['summary']}")
    return out


def decisions():
    raw = db.get_setting("decisions")
    return json.loads(raw) if raw else None


if __name__ == "__main__":
    import os
    if "CLIPPER_DATA" not in os.environ:  # it writes test rows: never into the real data/
        raise SystemExit("run as: CLIPPER_DATA=<empty temp dir> python -m clipper.insights")
    db.init()
    j = db.insert("jobs", url="u", title="src", shorts=1, longs=0, auto_upload=0, created=time.time(), category="gaming")
    for i, (v, s, moment) in enumerate([(1500, 18, "fail"), (200, 25, "reaction"), (5, 30, "fail"), (3, 45, "story"),
                                        (0, 50, "story"), (900, 15, "fail")]):
        db.insert("clips", job_id=j, kind="short", start=0, end=s, score=1, title=f"c{i}", description="", tags="[]",
                  reason="", why=json.dumps({"moment": moment}),
                  uploads=json.dumps({"instagram": {"id": "x", "at": time.time() - 2 * 86400}}),
                  stats=json.dumps({"instagram": {"views": v, "likes": v // 10}}))
    p = performance()
    assert p["enough"] and p["top"][0]["title"] == "c0" and p["platforms"][0]["name"] == "instagram"
    assert p["by"]["length"][0] == {"name": "under 20s", "clips": 2, "views": 2400, "engagement": 240, "per_clip": 1200}
    assert p["by"]["moment"][0]["name"] == "fail" and p["by"]["category"][0]["name"] == "gaming"
    assert "moment type: fail" in history_text(p)
    ai.ask = lambda task, system, key="clips", full=False: {"scout": "more gaming", "cutter": "fails win", "summary": "ok",
                                                             "suggestions": ["a", "b", "c", "d"]}
    d = decide(p)
    assert decisions()["cutter"] == "fails win" and len(d["suggestions"]) == 3
    print("insights ok")
