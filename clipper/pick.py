"""DeepSeek picks the final clips and writes titles/descriptions.

Two ways in:
  choose()       -> "hype" mode: DeepSeek only sees the candidate windows the hype curve found.
  choose_full()  -> "ai" / "ai+hype" modes: DeepSeek sees the WHOLE transcript (plus, optionally,
                    the signal curves as hints) and can pick any moment, overriding the curves.
Both return the same list of clip dicts, always passed through validate() first.
"""
import json
import time

import httpx
from openai import OpenAI

from . import config
from .config import SHORT_HARD_MAX

# Clip kinds (all rendered vertical 9:16):
#   short = a full moment with its context (<= 1 min), long = a longer segment (<= 3 min).
# Every kind is capped at 180s, so every clip is a YouTube Short.
KINDS = ("short", "long")
# Target clip length in seconds: (min, max), from Settings.
def limits():
    return {"short": (config.SHORT_MIN, config.SHORT_MAX), "long": (config.LONG_MIN, config.LONG_MAX)}
# How far over the max DeepSeek may go when a moment needs a bit more context (setup/payoff),
# but never past YouTube's 180s Shorts limit.
def hard_max():
    return {"short": min(config.SHORT_MAX + config.STRETCH_SHORT, SHORT_HARD_MAX),
            "long": min(config.LONG_MAX + config.STRETCH_LONG, SHORT_HARD_MAX)}
# What each kind is for, told to DeepSeek.
KIND_DESC = {
    "short": "a complete moment WITH its context: the setup a stranger needs, the payoff and the reaction.",
    "long": "a longer segment: a whole story, argument, mission or arc that stays gripping the whole way.",
}

# System prompt: who DeepSeek is and what a good clip looks like. Shared by both modes.
# Works for any content; Settings → "What your channel is about" adds the channel's focus (system_prompt()).
SYSTEM = """You are a top short-form clip editor. You have cut thousands of viral clips from gaming
streams, IRL streams, sports, podcasts, interviews, commentary and tutorials. First work out what kind of video this is from the title and
transcript, then judge it the way that audience would.

What pops, in any content:
- Chaos with a payoff: a plan going horribly wrong, a fail at the last second, a ridiculous crash.
- Strong reactions: screaming, crying laughing, stunned silence then "BRO", genuine shock or rage.
- Clutch / skill moments: last-second escapes, insane plays, a perfect shot, a comeback.
- A perfectly timed one-liner, roast, or punchline.
- Hot takes, bold claims, confessions, surprising facts, a story with a twist ending.
- Drama: arguments, betrayals, awkward moments, someone getting called out.
- Big satisfying moments: a win, a reveal, a huge payout, finally beating something hard.
- Clear, useful "aha" moments in educational content: one tip that stands on its own.
Gaming extras: physics glitches, absurd AI/NPC behaviour, plans going off the rails, roleplay in absurd
situations, trolling.

What does NOT pop:
- Dead air: menus, loading screens, driving/walking with nothing happening, setup and housekeeping,
  "let me check chat", sponsor reads, intros and outros.
- Loud but repetitive noise: constant gunfire, explosions, music. Loud audio alone does not mean
  a good clip; a quiet line can be the best moment of the video.
- Anything that needs minutes of context to understand.

How to judge a clip:
- Hook in the first 1-2 seconds (a shout, a crash, a weird line, a question). Slow setup? Start later.
- A clear payoff (the crash, the punchline, the reaction, the answer), then end soon after.
- Must make sense to someone scrolling who has never seen this creator.
- The clip must be COMPLETE. Include the setup a stranger needs to understand it, and let the payoff
  AND the reaction to it finish. Ending mid-thought or before the punchline is the worst mistake;
  when in doubt, start a sentence earlier and end a sentence later.
- Start and end on sentence boundaries; never cut someone off mid-word.
- Loop-friendly ending: end right after the payoff/reaction lands, not after a fade-out of chatter, so the
  replay starts straight back on the hook (replays count heavily on Shorts/Reels/TikTok).
- Formats that keep winning: "wait for it" build-ups with a clear payoff, fails and near-misses, clutch plays,
  chaos escalating, a reaction bigger than the event, a one-liner roast, a story with a twist, "POV" moments.

You only get the transcript (speech) and sometimes audience signals, not the picture. Infer the
action from what people say and how they react: "WHAT", screaming, laughing, sudden silence,
"no no no", "we're dead", panicked swearing.

Titles: punchy, curiosity-driven, under 60 chars, in the style of that niche on YouTube
(e.g. "This Plan Went SO Wrong", "He Didn't Expect This Question"), never lying about
what happens. Put the searchable keyword (the game, sport, show or topic name) in the title naturally: on Shorts
the title and description work as search signals more than hashtags do. Headline = the HOOK, burned onto the top
of the video for the whole clip: it must stop the scroll in the first second. 3-6 words that open a question the
clip answers (a curiosity gap), never a flat label of the topic. Ways that work: stakes or conflict ("HE BET HIS WHOLE
TEAM"), a bold claim the clip proves ("GTA 6 LOOKS TOO REAL"), a specific number or name ("$162M FOR THIS?",
"1 HP CLUTCH"), a direct question to the viewer ("WOULD YOU TAKE THIS?"), something to watch for ("WATCH THE LEFT
DOOR"). Specific beats generic: "MINI-GAME CHAOS" is weak, "THIS MINI-GAME BROKE THEM" is a hook. Don't give away the
payoff, don't promise what the clip doesn't show, no "wait for it". Not the same as the title.
Description: 1-2 lines. First line keyword-rich (game/topic + what happens); end with a short question that makes
people comment (e.g. "Would you have bailed?"). Hashtags: exactly 4-5, specific beats broad: the game, sport or
topic itself, the moment type (#funnymoments, #fails, #clutch), one niche/community tag, and at most one broad tag.
No #shorts/#fyp/#viral (added per platform).

Return JSON only:
{"clips": [{"candidate": <id>, "kind": "short"|"long", "start": <sec>, "end": <sec>,
"virality": <0-100>, "title": "...", "headline": "...", "description": "...", "hashtags": ["..."],
"reason": "one line: what happens and why it will pop"}]}"""

def system_prompt(context="", examples=""):
    """The clip editor prompt + the channel's focus from Settings + the clip's category (description, keywords)
    + what worked / didn't on this channel before (learn.examples)."""
    focus, lang = config.CHANNEL_FOCUS.strip(), config.CONTENT_LANGUAGE.strip() or "English"
    return (SYSTEM + (f"\n\nThis channel is about: {focus}. Judge every moment, title, headline and hashtag for that "
                      "audience." if focus else "")
            + (f"\n\n{context}\nA candidate that isn't about this category gets virality 0, even if it's funny. "
               "Use the category's keywords in titles, descriptions and hashtags where they fit." if context else "")
            + (f"\n\n{examples}" if examples else "")
            + ("" if lang.lower() == "english" else f"\n\nWrite title, headline, description and hashtags in {lang}."))


# "hype" mode: only pre-found windows.
WINDOWS_TASK = """Video title: "{title}"
Below are candidate windows found by hype signals (loudness jumps, chat spikes, YouTube most-replayed,
timestamped comments, laughter/cheering caption tags, shot changes). "signal" = how strongly they spiked overall,
"signals" = each one (0-1). Each has a transcript with [seconds]
markers (absolute time in the source).

{pick}
A high signal is a hint, not a verdict: prefer a quieter window with a great moment over a loud one
with nothing happening. Each clip must lie inside its candidate's window.

Candidates:
{candidates}"""

# "ai" / "ai+hype" modes: the whole video.
FULL_TASK = """Video title: "{title}" ({n} seconds long)
You get the FULL transcript; [seconds] markers are absolute times in the video.
{pick}
Pick them from ANYWHERE in the video. Use "candidate": 0 for every clip.
{signals}
Full transcript:
{transcript}"""

SIGNALS_HINT = """
Signals per {step}s bucket (0 = normal for this video, 9 = biggest spike). Columns: t (start second), {names}.
- audio: loudness. Weak evidence (in games it is often just gunfire/explosions).
- heatmap: YouTube "most replayed". Strong evidence, real viewers rewatched it.
- chat: live-chat bursts. Strong evidence of a reaction-worthy moment.
- comments: timestamps viewers wrote in comments. Strong evidence.
These are HINTS. If the transcript shows a better moment where the signals are flat, pick it;
you have the final say.

{table}
"""
NO_SIGNALS = "\nNo audience signals: judge from the transcript alone.\n"


def fit_counts(counts, n):
    """Don't ask for more clips than an n-second video can hold.
    Roughly one short per config.SHORT_MAX seconds and one long per config.LONG_MAX seconds (at least 1 if it fits at all).
    No long at all if the whole video already fits in a single short: a 1-min video gives 1 short, 0 long."""
    (smin, smax), (lmin, lmax) = limits()["short"], limits()["long"]
    cap = {"short": max(1, n // smax) if n >= smin else 0,
           "long": max(1, n // lmax) if n > hard_max()["short"] and n >= lmin else 0}
    return {k: min(v, cap[k]) for k, v in counts.items()}


def pick_text(counts):
    """The "what to pick" part of the prompt, only for the kinds the user asked for. counts: {kind: how many}."""
    lines = [f'- {counts[k]} x {k.upper()} ("kind": "{k}"): target {limits()[k][0]}-{limits()[k][1]}s, hard max {hard_max()[k]}s. '
             f'{KIND_DESC[k]}' for k in KINDS if counts.get(k, 0) > 0]
    return "Pick:\n" + "\n".join(lines) + """
The max is a ceiling, not a goal: make each clip only as long as the moment needs (a 1-2 min LONG or a
30s SHORT is great if that tells it fully). Don't pad with filler to reach the max.
All clips are vertical 9:16. Go over the target max (never past the hard max) ONLY if cutting there would lose
the setup or the payoff; then start "reason" with "STRETCHED:" and say why.
Clips of the same kind must not overlap. A SHORT and a LONG MAY reuse the same moment (e.g. a tight short of the
best moment AND a long with the bigger story around it), but only for the very best moments."""


def choose(title, candidates, counts, context="", examples="", images=None):
    """hype mode. candidates: [{id, kind, start, end, signal, signals, transcript}] -> list of clip dicts.
    images: [(label, jpeg base64)] a few frames per candidate, sent to Settings' vision model when one is set
    (if that call fails, it's asked again without pictures)."""
    task = WINDOWS_TASK.format(title=title, pick=pick_text(counts),
                               candidates=json.dumps(candidates, indent=1, ensure_ascii=False))
    system = system_prompt(context, examples)
    if images and config.VISION_MODEL.strip():
        try:
            return validate(_ask(task + "\n\nFrames from the candidates follow (2 per candidate, labelled): use what you "
                                        "see (action, faces, on-screen text) as well as what is said.", system,
                                 model=config.VISION_MODEL.strip(), images=images), candidates, counts)
        except Exception as e:
            from . import db
            db.syslog(f"vision model failed ({e!r}), picking from text only")
    return validate(_ask(task, system), candidates, counts)


PRESCORE_SYSTEM = """You screen candidate moments for short viral clips. For each candidate you get its transcript
(speech with [seconds] markers, plus caption tags like [Laughter]) and how strongly audience signals spiked in it
(0-1 each: heatmap = viewers rewatched, chat, reactions = laughter/cheering tags, audio = loudness jumps,
visual = shot changes / motion). Score 0-10 how likely it holds a complete, gripping moment a stranger would watch to
the end: a hook, a payoff, a reaction. Be harsh: most candidates are average (4-6).
Reply as JSON: {"scores": [{"id": <id>, "score": <0-10>}]} with every id."""


def prescore(title, candidates, context=""):
    """First pass with the fast model: {candidate id: score 0-10}, to keep only the best for the thorough pass."""
    rows = [{"id": c["id"], "kind": c["kind"], "signals": c.get("signals", {}), "transcript": c["transcript"][:800]}
            for c in candidates]
    out = {}
    for r in _ask(f'Video title: "{title}"\n{context}\nCandidates:\n{json.dumps(rows, ensure_ascii=False)}',
                  PRESCORE_SYSTEM, "scores", config.POST_MODEL):
        try:
            out[int(r["id"])] = float(r["score"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def choose_full(title, n, transcript, parts, counts, context="", examples=""):
    """ai modes. transcript: whole video with [sec] markers. parts: {signal: 0..1 per second} or None."""
    # ponytail: whole transcript in one request; a 3h video is ~40k tokens. Chunk it if videos get much longer.
    signals = SIGNALS_HINT.format(step=10, names=" ".join(parts), table=curve_table(parts, n)) if parts else NO_SIGNALS
    task = FULL_TASK.format(title=title, n=n, pick=pick_text(counts), signals=signals, transcript=transcript)
    clips = _ask(task, system_prompt(context, examples))
    for c in clips:
        if isinstance(c, dict):
            c["candidate"] = 0  # one "candidate" = the whole video, so validate() only clamps to [0, n]
    return validate(clips, [{"id": 0, "start": 0, "end": n}], counts)


def curve_table(parts, n, step=10):
    """Shrink per-second 0..1 curves to one 0-9 digit per signal per `step` seconds (peak in the bucket).
    Way fewer tokens than raw floats, and easy for the LLM to scan."""
    names = list(parts)
    rows = [" ".join(["t", *names])]
    for t in range(0, n, step):
        rows.append(" ".join([str(t), *(str(round(9 * float(parts[k][t:t + step].max()))) for k in names)]))
    return "\n".join(rows)


VOICE_SYSTEM = """You write voice-over commentary for short vertical clips (any content: gaming,
streams, sports, podcasts, commentary...). The narrator is the channel's own voice: confident, funny, hyped,
talking to the viewer like a friend.
Rules:
- 2-5 lines, each at most 12 words, plain spoken language. No emojis, hashtags or stage directions.
- Line 1 at 0-1s is a hook that makes people stay ("He had NO idea what was coming.").
- Later lines add context, a reaction or a joke. Put them where nobody important is speaking (use the
  transcript timing), NEVER over the punchline or the key line. Fewer lines is better than talking over it.
- Don't just repeat what is said in the clip, and don't invent facts that contradict the transcript.
- "at" = seconds from the clip start.
Return JSON only: {"lines": [{"at": <sec>, "text": "..."}]}"""


QUERIES_SYSTEM = """You find source videos for a clip channel that cuts viral Shorts out of longer videos.
Given a topic, write YouTube search queries that surface RECENT, POPULAR, clip-worthy videos about it:
streams, gameplay, funny moments, reactions, podcasts, interviews, news breakdowns, whatever fits the topic.
Mix angles (the main term, a reaction/funny-moments angle, a news/update angle, names of the big creators
or events in that topic if you know them). Short queries, the way people actually type them; no hashtags,
no quotes, no years unless the topic is about one. Reply as JSON: {"queries": ["...", "..."]} with 4-6 queries."""


def search_queries(topic, scope=""):
    """DeepSeek: topic -> 4-6 YouTube search queries (just the topic itself if the answer is unusable).
    scope: the category rule (categories.scope): every query stays inside it."""
    qs = [str(q).strip()[:100] for q in _ask(f"Topic: {topic}" + (f"\n{scope}\nEvery query must find videos of this "
                                             "category." if scope else ""), QUERIES_SYSTEM, "queries") if str(q).strip()]
    return list(dict.fromkeys(qs))[:6] or [topic]


CLIPPABLE_SYSTEM = """You decide which YouTube videos are worth cutting into short vertical clips (15s-3min)
for a viral clip channel. You only see the title, channel, length, views and maybe a description snippet.

Good sources (moments that stand on their own for a stranger scrolling):
- streams and gameplay (a corner facecam is fine) full of reactions, chaos, fails, funny moments, clutch plays, RP drama
- gameplay commentary and reactions where the game/scene fills the screen: hot takes, stories, punchlines
- compilations and "funny moments" videos, challenge videos, events with big reveals

Bad sources (people need the whole video for any part to make sense, or there is nothing to cut):
- step-by-step tutorials, guides, walkthroughs where each part depends on the previous one
- story-driven content: lore explanations, documentaries, a plot told from start to finish
- trailers, music videos, official teasers, very short or already-edited highlight videos
- slideshow/news-reading videos with no reactions, static "tier list" or ranking videos that need the full list
- anything that is mostly menus, ads, or one long calm explanation
- videos where the PICTURE is mostly a person: talking-head vlogs, "my thoughts on...", sit-down podcasts and
  interviews, people reacting on camera with little of the thing they react to. The channel wants clips that are
  mostly gameplay or real scenes; a small streamer facecam in the corner over gameplay is fine.

Score every video 0-10 for how well it would clip (7+ = clearly good, 4 or less = needs the full context).
Reply as JSON: {"videos": [{"id": "...", "score": 0-10, "reason": "short why"}]} with every id you were given."""


def clippable(topic, videos, scope=""):
    """DeepSeek judges flat search results -> {id: (score 0-10, reason)}. Unscored ids are left out.
    scope: the category rule: a video outside it scores 0 (a meme compilation for a CS2 / GTA channel)."""
    rows = [{"id": v["id"], "title": v.get("title"), "channel": v.get("channel"),
             "minutes": round((v.get("duration") or 0) / 60), "views": v.get("view_count"),
             "description": (v.get("description") or "")[:200]} for v in videos]
    out = {}
    rule = f"\n{scope}\nA video that isn't about this category scores 0." if scope else ""
    for r in _ask(f"Topic: {topic}{rule}\nVideos:\n{json.dumps(rows, indent=1)}", CLIPPABLE_SYSTEM, "videos"):
        try:
            out[str(r["id"])] = (min(max(float(r.get("score", 0)), 0), 10), str(r.get("reason", ""))[:150])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def voice_script(video_title, clip_title, length, transcript):
    """AI commentary lines for one clip -> [{"at": sec from clip start, "text": str}], sorted, sane."""
    task = (f'Video: "{video_title}". Clip title: "{clip_title}". Clip length: {length:.0f}s.\n'
            f"Transcript ([seconds] from clip start):\n{transcript or '(no speech)'}")
    out = []
    lang = config.CONTENT_LANGUAGE.strip() or "English"
    for ln in _ask(task, VOICE_SYSTEM + ("" if lang.lower() == "english" else f"\nWrite the lines in {lang}."), "lines"):
        if not isinstance(ln, dict) or not str(ln.get("text", "")).strip():
            continue
        try:
            at = float(ln.get("at", 0))
        except (TypeError, ValueError):
            continue
        out.append({"at": min(max(at, 0.0), max(length - 1, 0.0)), "text": str(ln["text"]).strip()[:120]})
    return sorted(out, key=lambda x: x["at"])[:6]


_balance = {"at": 0, "value": None}


def balance():
    """DeepSeek credit left, e.g. {"available": True, "balances": [{"currency": "USD", "total": "10.59", ...}]}.
    Cached 60s (the UI polls every 2s). Never raises: errors come back as {"error": ...}."""
    if time.time() - _balance["at"] < 60:
        return _balance["value"]
    try:
        if "deepseek.com" not in (config.LLM_BASE_URL or ""):  # only DeepSeek has a balance endpoint
            value = {"provider": config.LLM_PROVIDER, "set": bool(config.DEEPSEEK_API_KEY)}
            _balance.update(at=time.time(), value=value)
            return value
        if not config.DEEPSEEK_API_KEY:
            raise RuntimeError("no AI API key: add it in Settings → AI")
        r = httpx.get("https://api.deepseek.com/user/balance", timeout=10,
                      headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"})
        r.raise_for_status()
        d = r.json()
        value = {"available": d.get("is_available", False),
                 "balances": [{"currency": b["currency"], "total": b["total_balance"], "granted": b["granted_balance"],
                               "topped_up": b["topped_up_balance"]} for b in d.get("balance_infos", [])]}
    except Exception as e:
        value = {"error": str(e)[:200]}
    _balance.update(at=time.time(), value=value)
    return value


_client = []


def _local():
    """Your own Ollama server: no API key, and longer answers are fine (it's not billed, a big prompt takes a while)."""
    return config.LLM_PROVIDER == "ollama"


def _deepseek():
    """One client per provider setting (keeps the connection open between calls; a changed address / key / provider
    in Settings takes effect on the next request). 3 min timeout instead of the SDK's 10 min (10 for a local server),
    so a hung request doesn't stall a job; the SDK retries twice on its own."""
    local = _local()
    key = "ollama" if local else config.DEEPSEEK_API_KEY  # never send your provider key to another server
    url = config.LLM_BASE_URL.strip() or "https://api.deepseek.com"
    sig = (key, url, local)
    if not _client or _client[0][0] != sig:
        _client[:] = [(sig, OpenAI(api_key=key, base_url=url, timeout=600 if local else 180))]
    return _client[0][1]


def _ask(task, system=SYSTEM, key="clips", model=None, images=None):
    """Send one request to the AI provider (OpenAI-compatible API) in JSON mode; return the raw `key` list.
    model: default config.DEEPSEEK_MODEL (clip picking); posts pass the faster POST_MODEL.
    images: [(label, jpeg base64)] for a vision model: each sent as a labelled picture after the text."""
    if not config.DEEPSEEK_API_KEY and not _local():
        raise RuntimeError("no AI API key: add it in Settings → AI")
    content = task
    if images:
        content = [{"type": "text", "text": task}]
        for label, b64 in images:
            content += [{"type": "text", "text": label},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}}]
    r = _deepseek().chat.completions.create(
        model=model or config.DEEPSEEK_MODEL,
        response_format={"type": "json_object"},  # forces valid JSON back
        messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
    )
    return json.loads(r.choices[0].message.content).get(key, [])


def validate(clips, candidates, counts):
    """Never trust LLM numbers: clamp to window and duration limits, drop junk, cap counts."""
    by_id = {c["id"]: c for c in candidates}
    out, want = [], dict(counts)  # want = how many of each kind we still need

    def num(v, default):  # LLM may return "12.5", null, or garbage
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # Best first, so if the LLM returns too many, we keep the highest-virality ones.
    for c in sorted((c for c in clips if isinstance(c, dict)), key=lambda c: -num(c.get("virality"), 0)):
        cand = by_id.get(c.get("candidate"))
        kind = c.get("kind")
        if not cand or kind not in want or want[kind] <= 0:  # unknown window, bad kind, or quota full
            continue
        lo = limits()[kind][0]
        # Clamp start/end inside the candidate window.
        s = max(cand["start"], num(c.get("start"), cand["start"]))
        e = min(cand["end"], num(c.get("end"), cand["end"]))
        if e - s < lo:  # too short: grow it to the minimum length, staying inside the window
            e = min(cand["end"], s + lo)
            s = max(cand["start"], e - lo)
        e = min(e, s + hard_max()[kind])  # too long: cut to max length (+ the allowed stretch)
        if e - s < lo * 0.8:  # window itself too small to fit a real clip
            continue
        want[kind] -= 1
        out.append({"kind": kind, "start": s, "end": e, "score": num(c.get("virality"), 0),
                    "title": str(c.get("title", ""))[:100],
                    "headline": " ".join(str(c.get("headline", "")).split()[:6]),  # 3-6 words asked; hard cap 6
                    "description": str(c.get("description", "")),
                    "tags": [str(t).lstrip("#") for t in c.get("hashtags", [])][:15],
                    "reason": str(c.get("reason", ""))})
    return out


def fallback(candidates, counts, title):
    """No LLM? Use the raw hype windows, strongest signal first."""
    out, want = [], dict(counts)
    for c in sorted(candidates, key=lambda c: -c["signal"]):
        if want[c["kind"]] > 0:
            want[c["kind"]] -= 1
            out.append({"kind": c["kind"], "start": c["start"], "end": min(c["end"], c["start"] + limits()[c["kind"]][1]),
                        "score": round(c["signal"] * 10, 1), "title": f"{title} highlight"[:100], "headline": "",
                        "description": "", "tags": [], "reason": "hype signal only (LLM unavailable)"})
    return out


if __name__ == "__main__":
    limits = lambda: {"short": (15, 59), "long": (60, 600)}  # test values, independent of Settings
    hard_max = lambda: {"short": 74, "long": 660}
    C = lambda short, long: {"short": short, "long": long}
    cands = [{"id": 0, "kind": "short", "start": 100, "end": 150, "signal": 1},
             {"id": 1, "kind": "long", "start": 0, "end": 400, "signal": 1}]
    got = validate([{"candidate": 0, "kind": "short", "start": 90, "end": 200, "virality": 90},
                    {"candidate": 0, "kind": "short", "start": 120, "end": 122, "virality": 80},
                    {"candidate": 1, "kind": "long", "start": 10, "end": 900, "virality": 70},
                    {"candidate": 9, "kind": "short", "virality": 99}], cands, C(1, 1))
    assert [(g["kind"], g["start"], g["end"]) for g in got] == [("short", 100, 150), ("long", 10, 400)], got
    got = validate([{"candidate": 0, "kind": "short", "start": 0, "end": 70, "headline": "one two three four five six"},     # small stretch: kept
                    {"candidate": 1, "kind": "short", "start": 100, "end": 300},  # way over: cut to 59+15
                    {"candidate": 0, "kind": "long", "start": 0, "end": 900}],    # long cut to 600+60
                   [{"id": 0, "start": 0, "end": 1000}, {"id": 1, "start": 0, "end": 1000}], C(2, 1))
    assert [(g["start"], g["end"]) for g in got] == [(0, 70), (100, 174), (0, 660)], got
    assert got[0]["headline"] == "one two three four five six", got[0]
    import numpy as np
    assert curve_table({"audio": np.array([0, 1, 0.5, 0])}, 4, step=2) == "t audio\n0 9\n2 4", curve_table(
        {"audio": np.array([0, 1, 0.5, 0])}, 4, step=2)
    got = validate([{"candidate": 0, "kind": "short", "start": 0, "end": 60},
                    {"candidate": 0, "kind": "long", "start": 0, "end": 90}], [{"id": 0, "start": 0, "end": 100}],
                   C(1, 1))
    assert [(g["kind"], g["end"]) for g in got] == [("short", 60), ("long", 90)], got  # same moment, 2 kinds
    t = pick_text(C(0, 1))
    assert "x SHORT" not in t and "1 x LONG" in t, t
    assert fit_counts(C(3, 1), 60) == C(1, 0)     # 1-min video: one short, no long
    assert fit_counts(C(3, 1), 10) == C(0, 0)     # shorter than any clip
    assert fit_counts(C(3, 1), 100) == C(1, 1)    # too long for one short: a long fits
    assert fit_counts(C(3, 2), 300) == C(3, 1)    # 5 min: plenty of shorts, longs capped by length
    print("pick ok")
