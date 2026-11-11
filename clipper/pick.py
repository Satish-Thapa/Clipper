"""DeepSeek picks the final clips from candidate windows and writes titles/descriptions."""
import json

from openai import OpenAI

from .config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL

LIMITS = {"short": (15, 59), "long": (60, 600)}

PROMPT = """You are a top YouTube clip editor. Video title: "{title}".
Below are candidate windows found by hype signals (loud audio, chat spikes, most-replayed, timestamped comments).
Each has a transcript with [seconds] markers (absolute time in the source).

Pick the best {shorts} SHORT clips (15-59s, vertical) and best {longs} LONG clips (60-600s).
A great clip: strong hook in the first 2 seconds, a clear payoff, makes sense with no context.
Start and end on sentence boundaries. Each clip must lie inside its candidate's window.
Titles: punchy, curiosity-driven, under 70 chars, no clickbait lies.

Return JSON: {{"clips": [{{"candidate": <id>, "kind": "short"|"long", "start": <sec>, "end": <sec>,
"virality": <0-100>, "title": "...", "description": "...", "hashtags": ["..."], "reason": "why it will pop"}}]}}

Candidates:
{candidates}"""


def choose(title, candidates, shorts, longs):
    """candidates: [{id, kind, start, end, signal, transcript}] -> list of clip dicts."""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY missing in .env")
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    r = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": PROMPT.format(
            title=title, shorts=shorts, longs=longs, candidates=json.dumps(candidates, indent=1))}],
    )
    return validate(json.loads(r.choices[0].message.content).get("clips", []), candidates, shorts, longs)


def validate(clips, candidates, shorts, longs):
    """Never trust LLM numbers: clamp to window and duration limits, drop junk, cap counts."""
    by_id = {c["id"]: c for c in candidates}
    out, want = [], {"short": shorts, "long": longs}
    def num(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    for c in sorted((c for c in clips if isinstance(c, dict)), key=lambda c: -num(c.get("virality"), 0)):
        cand = by_id.get(c.get("candidate"))
        kind = c.get("kind")
        if not cand or kind not in want or want[kind] <= 0:
            continue
        lo, hi = LIMITS[kind]
        s = max(cand["start"], num(c.get("start"), cand["start"]))
        e = min(cand["end"], num(c.get("end"), cand["end"]))
        if e - s < lo:
            e = min(cand["end"], s + lo)
            s = max(cand["start"], e - lo)
        e = min(e, s + hi)
        if e - s < lo * 0.8:
            continue
        want[kind] -= 1
        out.append({"kind": kind, "start": s, "end": e, "score": num(c.get("virality"), 0),
                    "title": str(c.get("title", ""))[:100], "description": str(c.get("description", "")),
                    "tags": [str(t).lstrip("#") for t in c.get("hashtags", [])][:15],
                    "reason": str(c.get("reason", ""))})
    return out


def fallback(candidates, shorts, longs, title):
    """No LLM? Use the raw hype windows."""
    out, want = [], {"short": shorts, "long": longs}
    for c in sorted(candidates, key=lambda c: -c["signal"]):
        if want[c["kind"]] > 0:
            want[c["kind"]] -= 1
            out.append({"kind": c["kind"], "start": c["start"], "end": min(c["end"], c["start"] + LIMITS[c["kind"]][1]),
                        "score": round(c["signal"] * 10, 1), "title": f"{title} highlight"[:100],
                        "description": "", "tags": [], "reason": "hype signal only (LLM unavailable)"})
    return out


if __name__ == "__main__":
    cands = [{"id": 0, "kind": "short", "start": 100, "end": 150, "signal": 1},
             {"id": 1, "kind": "long", "start": 0, "end": 400, "signal": 1}]
    got = validate([{"candidate": 0, "kind": "short", "start": 90, "end": 200, "virality": 90},
                    {"candidate": 0, "kind": "short", "start": 120, "end": 122, "virality": 80},
                    {"candidate": 1, "kind": "long", "start": 10, "end": 900, "virality": 70},
                    {"candidate": 9, "kind": "short", "virality": 99}], cands, 1, 1)
    assert [(g["kind"], g["start"], g["end"]) for g in got] == [("short", 100, 150), ("long", 10, 400)], got
    print("pick ok")
