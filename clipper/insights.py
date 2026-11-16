"""What works on this channel, learned from the view counts of everything we posted (stats.refresh fills clips.views).

performance()  -> per clip / per topic / per platform / per clip length: views, views per clip, best and worst.
history_text() -> the same, compact, for DeepSeek: the autopilot passes it when it picks trending topics, so it leans
                  toward what got views here and away from what flopped (while still trying something new).
learn_niche()  -> when the autopilot has no niche, DeepSeek turns that history into one (e.g. "GTA 6 NPC and
                  gameplay moments"), saved as setting 'auto_learned' and shown on the Auto page.

Clips keep their DB row (with views) after their file is deleted, so the history survives disk cleanups."""
import json
import time

from . import db, pick

MIN_CLIPS = 5  # fewer posted clips with view counts than this = not enough to learn from yet
FLOP_AGE = 24 * 3600  # a clip only counts as a flop once it's had a day to get views
LENGTHS = ((0, 20, "under 20s"), (20, 35, "20-35s"), (35, 60, "35-60s"), (60, 10_000, "over 60s"))


def _clips():
    """Every clip that went up somewhere, with its views, topic and source video."""
    jobs = {j["id"]: j for j in db.all("jobs")}
    topics = {t["id"]: t for t in db.all("topics")}
    out = []
    for c in db.all("clips"):
        ups = {p: u for p, u in json.loads(c.get("uploads") or "{}").items() if u.get("id")}
        if not ups:
            continue
        views = {p: int(v or 0) for p, v in json.loads(c.get("views") or "{}").items() if p in ups}
        job = jobs.get(c["job_id"]) or {}
        topic = topics.get(job.get("topic_id"))
        at = min((u.get("at") or time.time()) for u in ups.values())
        if job.get("kind") in ("news", "fact"):  # news/fact posts: one group per kind, to compare with the clips
            topic = {"topic": f"{job['kind']} posts", "source": "news"}
        out.append({"id": c["id"], "title": c["title"], "headline": c.get("headline") or "", "kind": c["kind"],
                    "seconds": round(c["end"] - c["start"]), "views": views, "total": sum(views.values()),
                    "platforms": list(ups), "at": at, "age_h": round((time.time() - at) / 3600, 1),
                    "topic": topic["topic"] if topic else None, "auto": bool(topic and topic.get("source") == "auto"),
                    "source": job.get("title") or job.get("url") or ""})
    return out


def performance():
    clips = _clips()
    measured = [c for c in clips if c["views"]]
    by = {}
    for c in measured:
        key = c["topic"] or f"video: {c['source'][:80]}"
        g = by.setdefault(key, {"name": key, "topic": bool(c["topic"]), "clips": 0, "views": 0, "best": None})
        g["clips"] += 1
        g["views"] += c["total"]
        if not g["best"] or c["total"] > g["best"]["total"]:
            g["best"] = {"title": c["title"], "total": c["total"]}
    groups = sorted(({**g, "per_clip": round(g["views"] / g["clips"])} for g in by.values()),
                    key=lambda g: g["per_clip"], reverse=True)
    plats = {}
    for c in measured:
        for p, v in c["views"].items():
            x = plats.setdefault(p, {"platform": p, "clips": 0, "views": 0})
            x["clips"] += 1
            x["views"] += v
    plats = sorted(({**x, "per_clip": round(x["views"] / x["clips"])} for x in plats.values()),
                   key=lambda x: x["per_clip"], reverse=True)
    lengths = []
    for lo, hi, name in LENGTHS:
        cs = [c for c in measured if lo <= c["seconds"] < hi]
        if cs:
            lengths.append({"length": name, "clips": len(cs), "per_clip": round(sum(c["total"] for c in cs) / len(cs))})
    ranked = sorted(measured, key=lambda c: c["total"], reverse=True)
    old = [c for c in ranked if time.time() - c["at"] > FLOP_AGE]
    return {"at": time.time(), "posted": len(clips), "measured": len(measured),
            "views": sum(c["total"] for c in measured), "enough": len(measured) >= MIN_CLIPS,
            "topics": groups, "platforms": plats, "lengths": lengths,
            "top": ranked[:8], "flops": old[-5:][::-1] if len(old) > 8 else []}


def history_text(perf=None, n=8):
    """Compact channel history for DeepSeek ('' when there isn't enough yet)."""
    perf = perf or performance()
    if not perf["enough"]:
        return ""
    t = lambda c: f'"{c["title"]}" ({c["total"]:,} views, {c["seconds"]}s, topic: {c["topic"] or c["source"][:60]})'
    lines = [f"Our channel so far: {perf['measured']} clips with view counts, {perf['views']:,} views in total.",
             "Best clips: " + "; ".join(t(c) for c in perf["top"][:n]),
             "Topics by views per clip: " + "; ".join(f'{g["name"]} = {g["per_clip"]:,}/clip over {g["clips"]} clips'
                                                     for g in perf["topics"][:n])]
    if perf["flops"]:
        lines.append("Weakest clips (a day or more old): " + "; ".join(t(c) for c in perf["flops"]))
    if perf["platforms"]:
        lines.append("Views per clip by platform: " + ", ".join(f'{p["platform"]} {p["per_clip"]:,}' for p in perf["platforms"]))
    if len(perf["lengths"]) > 1:
        lines.append("Views per clip by length: " + ", ".join(f'{x["length"]} {x["per_clip"]:,}' for x in perf["lengths"]))
    return "\n".join(lines)


NICHE_SYSTEM = """You run a viral short-clip channel and decide what it should post next, based on how its past clips
did. You get the channel's history: best clips, topics ranked by views per clip, weakest clips, platforms, lengths.
Name the niche to search for trending videos in: double down on what clearly gets views, drop what flopped, and keep
it broad enough to find new trending videos every day (a game/creator/subject area, not one single video). If the
history is thin or flat, stay close to the best topic but allow related subjects. Reply as JSON:
{"plan": {"niche": "3-10 words, the way you'd describe the channel", "why": "one line citing the numbers",
"avoid": "one line: what to stay away from, or empty"}}"""


def learn_niche(perf=None):
    """No niche set: DeepSeek picks one from the channel's history. -> {niche, why, avoid, at} or None (too little data)."""
    perf = perf or performance()
    text = history_text(perf)
    if not text:
        return None
    plan = pick._ask(text, NICHE_SYSTEM, "plan")
    if not isinstance(plan, dict) or not str(plan.get("niche", "")).strip():
        return None
    out = {"niche": str(plan["niche"]).strip()[:120], "why": str(plan.get("why", ""))[:300],
           "avoid": str(plan.get("avoid", ""))[:200], "at": time.time(), "clips": perf["measured"], "views": perf["views"]}
    db.set_setting("auto_learned", json.dumps(out))
    db.syslog(f"autopilot learned niche from {perf['measured']} clips / {perf['views']:,} views: '{out['niche']}' ({out['why']})")
    return out


def learned():
    raw = db.get_setting("auto_learned")
    return json.loads(raw) if raw else None


if __name__ == "__main__":
    import os
    if "CLIPPER_DATA" not in os.environ:  # it writes test rows: never into the real data/
        raise SystemExit("run as: CLIPPER_DATA=<empty temp dir> python -m clipper.insights")
    db.init()
    j = db.insert("jobs", url="u", title="src", shorts=1, longs=0, auto_upload=1, created=time.time())
    for i, (v, s) in enumerate([(1500, 18), (200, 25), (5, 30), (3, 45), (0, 50), (900, 15)]):
        db.insert("clips", job_id=j, kind="short", start=0, end=s, score=1, title=f"c{i}", description="", tags="[]",
                  reason="", uploads=json.dumps({"instagram": {"id": "x", "at": time.time() - 2 * 86400}}),
                  views=json.dumps({"instagram": v}))
    p = performance()
    assert p["enough"] and p["top"][0]["title"] == "c0" and p["flops"] == [] and p["platforms"][0]["platform"] == "instagram"
    assert p["lengths"][0] == {"length": "under 20s", "clips": 2, "per_clip": 1200}
    assert "c0" in history_text(p)
    print("insights ok")
