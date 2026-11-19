"""DeepSeek picks the final clips and writes titles/descriptions.

Two ways in:
  choose()       -> "hype" mode: DeepSeek only sees the candidate windows the hype curve found.
  choose_full()  -> "ai" / "ai+hype" modes: DeepSeek sees the WHOLE transcript (plus, optionally,
                    the signal curves as hints) and can pick any moment, overriding the curves.
Both return the same list of clip dicts, always passed through validate() first.
"""
import json
import re
import time

import httpx
from openai import BadRequestError, OpenAI

from . import db
from .config import (PC_LLM_BASE_URL, PC_LLM_MODEL, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, DEEPSEEK_API_KEY, DEEPSEEK_MODEL, LONG_MAX, LONG_MIN, SHORT_HARD_MAX, SHORT_MAX, SHORT_MIN,
                     STRETCH_LONG, STRETCH_SHORT)

# Clip kinds (all rendered vertical 9:16):
#   short = a full moment with its context (SHORT_MIN-SHORT_MAX, default 20-90s), long = a longer segment (<= 3 min).
# Every kind is capped at 180s, so every clip is a YouTube Short.
KINDS = ("short", "long")
# Target clip length in seconds: (min, max). Set in .env.
LIMITS = {"short": (SHORT_MIN, SHORT_MAX), "long": (LONG_MIN, LONG_MAX)}
# How far over the max DeepSeek may go when a moment needs a bit more context (setup/payoff),
# but never past YouTube's 180s Shorts limit.
HARD_MAX = {"short": min(SHORT_MAX + STRETCH_SHORT, SHORT_HARD_MAX), "long": min(LONG_MAX + STRETCH_LONG, SHORT_HARD_MAX)}
# What each kind is for, told to DeepSeek.
KIND_DESC = {
    "short": "a complete moment WITH its context: the setup a stranger needs, the payoff and the reaction.",
    "long": "a longer segment: a whole story, argument, mission or arc that stays gripping the whole way.",
}

# System prompt: who DeepSeek is and what a good clip looks like. Shared by both modes.
# Works for any content (gaming like GTA, IRL streams, podcasts, commentary, tutorials...).
SYSTEM = """You are a top short-form clip editor. You have cut thousands of viral clips from gaming
streams (GTA V / GTA Online / GTA RP, shooters, horror, Minecraft...), IRL streams, podcasts,
interviews, commentary and tutorials. First work out what kind of video this is from the title and
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
Gaming extras (e.g. GTA): physics glitches, cars flying, NPCs doing something insane, police chases
going off the rails, heist fails, RP characters staying in role in absurd situations, trolling.

What does NOT pop:
- Dead air: menus, loading screens, driving/walking with nothing happening, setup and housekeeping,
  "let me check chat", sponsor reads, intros and outros.
- Loud but repetitive noise: constant gunfire, explosions, music. Loud audio alone does not mean
  a good clip; a quiet line can be the best moment of the video.
- Anything that needs minutes of context to understand.

How to judge a clip:
EVERY CLIP IS A COMPLETE PIECE: the viewer gets the WHOLE thing (a hook, the good content, and its ending) and
watches it to the last second. Build each clip from three parts, all three required:
    1. THE HOOK (first 1-3 seconds): the line or moment that stops the scroll AND tells a stranger what's going
       on: a bold line, a question, a shout, a crash, "watch this", the start of the conflict. It must be clear
       without context (who / what they're trying to do). Slow setup before it? Start later, at the hook. Never open
       on dead air, "um", greetings, or a line pointing back ("and", "but", "so", "that's why", "he", "it").
    2. THE CONTENT (the middle): the actual good part, with nothing wasted: the build-up, the play, the story, the
       argument, the explanation. Every few seconds something moves it forward (a new line, a twist, rising tension);
       cut repetition and filler, but keep every line the payoff depends on.
    3. THE ENDING (last seconds): the payoff fully delivered and the thought CLOSED: the punchline lands, the
       question is answered, the play finishes, the reaction plays out, the story reaches its end. The viewer
       should feel "that was the whole thing", not "wait, what happened next?".
  Ending mid-thought, mid-action ("he's about to...") or before the payoff is the worst mistake a clip can make;
  a clip that needs the part before it to make sense is broken too. When in doubt, start one sentence earlier and
  end one sentence later: a slightly longer complete clip always beats a shorter broken one (within the max).
- Content that LASTS: it holds attention to the end (no slow stretch where people swipe away), still makes sense
  to someone who finds it weeks later with zero context, and is worth a rewatch or a share. Skip moments that only
  work live or need inside jokes to understand.
- Must make sense to someone scrolling who has never seen this creator.
- Start and end exactly on sentence boundaries; never cut someone off mid-word. Every sentence in the
  transcript starts with its [seconds] marker: use those times for "start", and the start of the sentence
  AFTER your last one for "end".
- SELF-CHECK before you answer, for every clip: read its first sentence (is it a hook, and would someone who just
  scrolled in understand it?), skim the middle (is every part worth watching?), and read its last sentence (does the
  thought close, is the question answered, has the reaction finished?). If not, move the start or the end until it
  does. Write the clip's actual first and last lines into "hook" and "ending" so you've checked them.
- Loop-friendly ending: end right after the payoff/reaction lands, not after a fade-out of chatter, so the
  replay starts straight back on the hook (replays count heavily on Shorts/Reels/TikTok).
- Formats that keep winning: "wait for it" build-ups with a clear payoff, fails and near-misses, clutch plays,
  chaos escalating, a reaction bigger than the event, a one-liner roast, a story with a twist, "POV" moments.

You only get the transcript (speech) and sometimes audience signals, not the picture. Infer the
action from what people say and how they react: "WHAT", screaming, laughing, sudden silence,
"no no no", "we're dead", panicked swearing.

Titles: punchy, curiosity-driven, under 60 chars, in the style of that niche on YouTube
(gaming: "This Heist Went SO Wrong"; podcast: "He Didn't Expect This Question"), never lying about
what happens. Put the searchable keyword (game or topic name, e.g. "GTA 5") in the title naturally: on Shorts
the title and description work as search signals more than hashtags do. Headline: the yellow box burned onto the
top of the video for the whole clip. It MUST match the title: the title's own hook in 3-6 words, same subject and
same promise, reusing its key words, so the video and its title read as one (title "This GTA Heist Went SO Wrong"
-> headline "HEIST WENT SO WRONG"; title "Cops Remember You In GTA 5" -> "COPS REMEMBER YOU"). Never a different
idea from the title.
Description: 1-2 lines. First line keyword-rich (game/topic + what happens); end with a short question that makes
people comment (e.g. "Would you have bailed?"). Hashtags: exactly 4-5, specific beats broad: the game or topic
(#gta5, #gtaonline, #gtarp only for roleplay, #podcast), the moment type (#gtafails, #funnymoments, #clutch),
one niche/community tag, and at most one broad tag (#gaming). No #shorts/#fyp/#viral (added per platform).

Return JSON only:
{"clips": [{"candidate": <id>, "kind": "short"|"long", "start": <sec>, "end": <sec>,
"virality": <0-100>, "title": "...", "headline": "...", "description": "...", "hashtags": ["..."],
"hook": "the clip's first line, as said", "ending": "the clip's last line, as said",
"reason": "one line: hook -> content -> ending, and why people will watch it to the end"}]}"""

# "hype" mode: only pre-found windows.
WINDOWS_TASK = """Video title: "{title}"
Below are candidate windows found by hype signals (loud audio, chat spikes, YouTube most-replayed,
timestamped comments). "signal" = how strongly they spiked. Each has a transcript with [seconds]
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
- reaction: where the speaker says surprise / hype words ("oh my god", "no way", "bro", "what the", shouting,
  laughing). Strong evidence when it lines up with chat or audio in the same spot.
These are HINTS. If the transcript shows a better moment where the signals are flat, pick it;
you have the final say.

{table}
"""
NO_SIGNALS = "\nNo audience signals: judge from the transcript alone.\n"


def fit_counts(counts, n):
    """Don't ask for more clips than an n-second video can hold.
    Roughly one short per SHORT_MAX seconds and one long per LONG_MAX seconds (at least 1 if it fits at all).
    No long at all if the whole video already fits in a single short: a 1-min video gives 1 short, 0 long."""
    (smin, smax), (lmin, lmax) = LIMITS["short"], LIMITS["long"]
    cap = {"short": max(1, n // smax) if n >= smin else 0,
           "long": max(1, n // lmax) if n > HARD_MAX["short"] and n >= lmin else 0}
    return {k: min(v, cap[k]) for k, v in counts.items()}


def pick_text(counts):
    """The "what to pick" part of the prompt, only for the kinds the user asked for. counts: {kind: how many}."""
    lines = [f'- {counts[k]} x {k.upper()} ("kind": "{k}"): target {LIMITS[k][0]}-{LIMITS[k][1]}s, hard max {HARD_MAX[k]}s. '
             f'{KIND_DESC[k]}' for k in KINDS if counts.get(k, 0) > 0]
    return "Pick:\n" + "\n".join(lines) + """
The max is a ceiling, not a goal: make each clip only as long as the moment needs (a 1-2 min LONG or a
30s SHORT is great if that tells it fully). Don't pad with filler to reach the max.
All clips are vertical 9:16. Go over the target max (never past the hard max) ONLY if cutting there would lose
the setup or the payoff; then start "reason" with "STRETCHED:" and say why.
Clips of the same kind must not overlap. A SHORT and a LONG MAY reuse the same moment (e.g. a tight short of the
best moment AND a long with the bigger story around it), but only for the very best moments."""


LIVE_MIN = 55  # live pieces: extra picks DeepSeek scores below this are dropped; the best one is always kept


STREAM_NOTE = """
THE TRANSCRIPT HAS TWO KINDS OF VOICES mixed together: the STREAMER (talks to chat and to themselves: first person,
casual, "bro", "chat", "guys", "look at this", reacting, commenting, swearing) and the GAME (characters' scripted
dialogue, radio, NPCs, cutscenes: names, story lines, full polished sentences). Work out who says each line.
- The best stream clips are the STREAMER REACTING to the GAME: something happens in the game and the streamer is
  surprised, shocked, hyped, laughing, or says something interesting / funny / a hot take about it. Clip the cause
  AND the reaction: start on the game moment that triggers it (the hook), end after the reaction lands.
- Use the signal graph to confirm: a reaction where chat / audio / reaction spike in the same spot is a real
  moment; pick it when it also makes sense from the transcript (you understand what happened and why they react).
- A streamer saying something genuinely interesting (an opinion, a discovery, explaining a detail) is a clip too,
  even if the graph is flat, as long as it stands on its own.
- Don't build a clip only from game dialogue with no streamer in it, unless it's a big reveal (see below)."""

FIRST_LOOK_NOTE = """
This looks like a FIRST LOOK at new gameplay (premiere / reveal / first time playing): viewers want to SEE THE NEW
GAME. New features, mechanics, the map / city, vehicles, weapons, characters, graphics, and the streamer's first
reaction to them are clip-worthy on their own, even when the streamer is calm and the graph is flat. Show the peak
features and gameplay: clip the moment a new thing is shown or tried, and say what it is in the title and headline
(e.g. "GTA 6 Map Is HUGE", "New Wanted System In GTA 6")."""
FIRST_LOOK = re.compile(r"premi[eè]re|first look|first time|reveal|gameplay|new game|early access|launch|day one|"
                        r"\bdemo\b|leak|trailer|first impression|blind", re.I)


WOW_NOTE = """
WOW MOMENTS COME FIRST: for streams the owner wants the moments that are AMAZING to watch, more than the best
story. A "wow, you can do THAT?!" moment is a top clip on its own: something impressive or unexpected the game lets
you do (a new mechanic, a crazy stunt, physics, a huge jump, an insane car / weapon / ability), stunning graphics
or a breathtaking view, a big explosion, chase, crash or fight, a jaw-dropping detail. You can't see the picture,
so read it from the words: the streamer or chat going "wow", "look at this", "you can do that?", "no way",
"this looks insane", "the graphics", "how is this real", "bro look", describing what's on screen, laughing in
disbelief; the game's sounds and lines around it. Prefer these over talky moments even when the talk is good.
A wow clip doesn't need a punchline: start just before the amazing thing happens, keep it while it plays out,
end once the reaction lands. Score (virality) it by how impressive it is to watch."""


def live_note(boundary, title="", piece=1):
    """Extra instructions for one piece of a live stream (or of a long video cut into pieces). boundary = seconds at
    the start that are the END of the previous piece (0 for the first piece)."""
    note = (f"\nThis is one piece of a LIVE stream, not a whole video. ALWAYS return AT LEAST ONE clip: the single "
            f"best complete moment in this piece, even if the piece is quiet (score it honestly). The counts above are a "
            f"MAXIMUM, not a quota: any clip beyond that first one must be genuinely worth a clip (virality {LIVE_MIN}+).")
    note += STREAM_NOTE + WOW_NOTE
    if FIRST_LOOK.search(title or ""):
        note += FIRST_LOOK_NOTE
        if piece == 1:
            note += ("\nThis is the FIRST piece: the opening of the new gameplay. Your at-least-one clip should be the "
                     "first proper look at the game here (the first new feature, area or moment that grabs).")
    if boundary:
        note += (f"\nThe first {boundary:.0f} seconds (0-{boundary:.0f}s) are the END of the PREVIOUS piece, which was "
                 f"already clipped. Only use that part for a moment that STARTS there and CONTINUES past {boundary:.0f}s "
                 f"(it was cut in half between the two pieces): then the clip may start before {boundary:.0f}s; say "
                 f"\"JOINED:\" at the start of its reason. Never pick a clip that ends before {boundary + 3:.0f}s.")
    return note


def choose(title, candidates, counts, note="", out=None):
    """hype mode. candidates: [{id, kind, start, end, signal, transcript}] -> list of clip dicts.
    note: extra instructions (live pieces); out: dict that receives DeepSeek's "skip" reason, if any."""
    task = WINDOWS_TASK.format(title=title, pick=pick_text(counts), candidates=json.dumps(candidates, indent=1)) + note
    r = _ask(task, full=True)
    if out is not None:
        out["skip"] = str(r.get("skip") or "")[:300]
    return validate(r.get("clips") or [], candidates, counts)


def choose_full(title, n, transcript, parts, counts, note="", out=None):
    """ai modes. transcript: whole video with [sec] markers. parts: {signal: 0..1 per second} or None."""
    # ponytail: whole transcript in one request; a 3h video is ~40k tokens. Chunk it if videos get much longer.
    signals = SIGNALS_HINT.format(step=10, names=" ".join(parts), table=curve_table(parts, n)) if parts else NO_SIGNALS
    task = FULL_TASK.format(title=title, n=n, pick=pick_text(counts), signals=signals, transcript=transcript) + note
    r = _ask(task, full=True)
    if out is not None:
        out["skip"] = str(r.get("skip") or "")[:300]
    clips = r.get("clips") or []
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


VOICE_SYSTEM = """You write voice-over commentary for short vertical clips (any content: gaming like GTA,
streams, podcasts, commentary...). The narrator is the channel's own voice: confident, funny, hyped,
talking to the viewer like a friend.
Rules:
- 2-5 lines, each at most 12 words, plain spoken English. No emojis, hashtags or stage directions.
- Line 1 at 0-1s is a hook that makes people stay ("This cop had NO idea what was coming.").
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


def user_note(notes):
    """Your own instructions for the AI (Create / Topics / Auto "Tell the AI"), as a prompt block ('' when none)."""
    notes = str(notes or "").strip()[:1000]
    return ("\n\nEXTRA INSTRUCTIONS FROM THE CHANNEL OWNER. Follow them; where they conflict with the general guidance "
            "above, they win (but keep the JSON format and the length limits):\n" + notes + "\n") if notes else ""


def search_queries(topic, notes=""):
    """DeepSeek: topic -> 4-6 YouTube search queries (just the topic itself if the answer is unusable)."""
    qs = [str(q).strip()[:100] for q in _ask(f"Topic: {topic}" + user_note(notes), QUERIES_SYSTEM, "queries") if str(q).strip()]
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


def clippable(topic, videos, notes=""):
    """DeepSeek judges flat search results -> {id: (score 0-10, reason)}. Unscored ids are left out."""
    rows = [{"id": v["id"], "title": v.get("title"), "channel": v.get("channel"),
             "minutes": round((v.get("duration") or 0) / 60), "views": v.get("view_count"),
             "description": (v.get("description") or "")[:200]} for v in videos]
    out = {}
    for r in _ask(f"Topic: {topic}\nVideos:\n{json.dumps(rows, indent=1)}" + user_note(notes), CLIPPABLE_SYSTEM, "videos"):
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
    for ln in _ask(task, VOICE_SYSTEM, "lines"):
        if not isinstance(ln, dict) or not str(ln.get("text", "")).strip():
            continue
        try:
            at = float(ln.get("at", 0))
        except (TypeError, ValueError):
            continue
        out.append({"at": min(max(at, 0.0), max(length - 1, 0.0)), "text": str(ln["text"]).strip()[:120]})
    return sorted(out, key=lambda x: x["at"])[:6]


PROVIDERS = {"deepseek": "DeepSeek", "local": "Local (Ollama)", "pc": "This PC (Ollama)"}


def _missing(p):
    """Why provider p can't be used right now ('' = ready)."""
    if p == "deepseek":
        return "" if DEEPSEEK_API_KEY else "DEEPSEEK_API_KEY missing in .env"
    if p == "local":
        return "" if LLM_BASE_URL and _model("local") else "LLM_BASE_URL / LLM_MODEL missing in .env"
    if p == "pc":
        return "" if PC_LLM_BASE_URL and _model("pc") else "PC_LLM_BASE_URL / PC_LLM_MODEL missing in .env"
    return "unknown provider"


def _model(p):
    return {"deepseek": DEEPSEEK_MODEL,
            "local": db.get_setting("ai_model") or LLM_MODEL, "pc": db.get_setting("pc_model") or PC_LLM_MODEL}.get(p, "")


def ai_order():
    """Providers to try, first = main, the rest = fallbacks in order. Saved in Settings → AI models (setting ai_order);
    default: local first when LLM_BASE_URL is set, then DeepSeek, then the small model on this PC."""
    saved = [p for p in (db.get_setting("ai_order") or "").split(",") if p in PROVIDERS]
    return saved or (["local", "deepseek", "pc"] if LLM_BASE_URL else ["deepseek", "local", "pc"])


def ai_choice():
    """The main AI -> {"provider", "model", "order": [{provider, model, name, missing}]} (first ready one is main)."""
    order = [{"provider": p, "model": _model(p), "name": PROVIDERS[p], "missing": _missing(p)} for p in ai_order()]
    main = next((o for o in order if not o["missing"]), order[0] if order else {"provider": "deepseek", "model": ""})
    return {"provider": main["provider"], "model": main["model"], "order": order}


def ai_name():
    """The main AI as the logs name it, e.g. 'gpt-oss:20b (Local (Ollama))'."""
    ch = ai_choice()
    return f"{ch['model']} ({PROVIDERS.get(ch['provider'], ch['provider'])})"


_models = {}  # url -> (time, value)


def local_models(url=None):
    """Models on an Ollama/LM Studio server (default LLM_BASE_URL) -> {"models": [names]} or {"error": ...}.
    Cached 30s, never raises. Ollama's /api/tags first (every pulled model), then the OpenAI-style /models."""
    url = LLM_BASE_URL if url is None else url
    if not url:
        return {"error": "no server address in .env"}
    if time.time() - _models.get(url, (0, None))[0] < 30:
        return _models[url][1]
    try:
        try:
            r = httpx.get(re.sub(r"/v1$", "", url) + "/api/tags", timeout=5)
            r.raise_for_status()
            names = [m["name"] for m in r.json().get("models", [])]
        except Exception:
            r = httpx.get(url + "/models", timeout=5,
                          headers={"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY and url == LLM_BASE_URL else {})
            r.raise_for_status()
            names = [m["id"] for m in r.json().get("data", [])]
        value = {"models": sorted(names)}
    except Exception as e:
        value = {"error": f"can't reach {url}: {e}"[:200]}
    _models[url] = (time.time(), value)
    return value


def set_ai(order, local_model="", pc_model=""):
    """Save the provider order (main first, then fallbacks) for every AI call; takes effect on the next call."""
    order = list(dict.fromkeys(p for p in order if p in PROVIDERS))
    if not order:
        raise ValueError("Pick at least one AI")
    db.set_setting("ai_order", ",".join(order))
    if local_model:
        db.set_setting("ai_model", local_model)
    if pc_model:
        db.set_setting("pc_model", pc_model)
    db.syslog("AI order: " + " -> ".join(f"{PROVIDERS[p]} ({_model(p)})" for p in order))


_balance = {"at": 0, "value": None}


def balance():
    """DeepSeek credit left, e.g. {"available": True, "balances": [{"currency": "USD", "total": "10.59", ...}]}.
    Cached 60s (the UI polls every 2s). Never raises: errors come back as {"error": ...}."""
    ch = ai_choice()
    if ch["provider"] != "deepseek":  # local model: no DeepSeek balance, just say which model is in use
        return {"model": ch["model"], "url": {"local": LLM_BASE_URL, "pc": PC_LLM_BASE_URL}.get(ch["provider"], "")}
    if time.time() - _balance["at"] < 60:
        return _balance["value"]
    try:
        if not DEEPSEEK_API_KEY:
            raise RuntimeError("DEEPSEEK_API_KEY missing in .env")
        r = httpx.get("https://api.deepseek.com/user/balance", timeout=10,
                      headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"})
        r.raise_for_status()
        d = r.json()
        value = {"available": d.get("is_available", False),
                 "balances": [{"currency": b["currency"], "total": b["total_balance"], "granted": b["granted_balance"],
                               "topped_up": b["topped_up_balance"]} for b in d.get("balance_infos", [])]}
    except Exception as e:
        value = {"error": str(e)[:200]}
    _balance.update(at=time.time(), value=value)
    return value


last_used = {"provider": "", "model": ""}  # which AI answered the latest call


def _client(p):
    if p in ("local", "pc"):
        # ponytail: local models can be slow on full transcripts -> 30 min read timeout, but a dead server fails in 10s
        return OpenAI(api_key=(LLM_API_KEY if p == "local" else "") or "none",
                      base_url=LLM_BASE_URL if p == "local" else PC_LLM_BASE_URL, timeout=httpx.Timeout(1800, connect=10),
                      max_retries=1)
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com", max_retries=1)


def _ask_one(p, task, system):
    kw = dict(model=_model(p), messages=[{"role": "system", "content": system}, {"role": "user", "content": task}])
    if p == "pc":  # small model on a 4 GB GPU: skip its "thinking" (1s instead of 12s per answer, same JSON)
        kw["reasoning_effort"] = "none"
    client = _client(p)
    try:
        r = client.chat.completions.create(response_format={"type": "json_object"}, **kw)  # forces valid JSON
    except BadRequestError:  # provider/model without JSON mode: ask plainly, the prompts already demand JSON
        r = client.chat.completions.create(**kw)
    text = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S)  # reasoning models
    if "{" in text:
        text = text[text.index("{"):text.rindex("}") + 1]  # drop ```json fences or chatter around the object
    return json.loads(text)


def test(p):
    """Ask one AI (no fallback) "What model are you?" -> {reply, model, seconds}. ValueError when it isn't set up."""
    if p not in PROVIDERS:
        raise ValueError("unknown AI")
    if _missing(p):
        raise ValueError(_missing(p))
    t = time.time()
    a = _ask_one(p, 'What model are you? One short sentence. Answer in JSON: {"reply": "your answer"}', "You are a helpful assistant.")
    reply = a.get("reply", a) if isinstance(a, dict) else a
    db.syslog(f"🤖 AI test: {_model(p)} ({PROVIDERS[p]}) answered in {time.time() - t:.1f}s")
    return {"reply": str(reply)[:500], "model": _model(p), "seconds": round(time.time() - t, 1)}


PROMPT_KEEP_DAYS = 7  # saved prompts (data/logs/prompts/<day>/) older than this are deleted
PURPOSE = {"clips": "picking the clips", "queries": "writing search queries", "videos": "judging which videos clip well",
           "topics": "finding today's trends", "lines": "writing the voice-over", "reply": "a test",
           "rules": "reading the campaign brief", "plan": "planning the campaign clips", "caption": "writing a campaign caption"}


def _save_prompt(system, task, key):
    """Every prompt goes to its own text file (system prompt + task, the reply is added after), linked from the log
    line, so you can read exactly what the AI was told. -> (file, its /media/ link)."""
    from .config import DATA
    root, now = DATA / "logs" / "prompts", time.localtime()
    day = time.strftime("%Y-%m-%d", now)
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{time.strftime('%H%M%S', now)}_{key}_{int(time.time() * 1000) % 100000:05d}.txt"
    f.write_text(f"=== SYSTEM PROMPT ===\n{system}\n\n=== TASK ===\n{task}\n", encoding="utf-8")
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - PROMPT_KEEP_DAYS * 86400))
    for old in root.iterdir():
        if old.is_dir() and old.name < cutoff:
            import shutil
            shutil.rmtree(old, ignore_errors=True)
    return f, f"/media/logs/prompts/{day}/{f.name}"


import threading
LAST = threading.local()  # .model / .link of the last answered call in THIS thread (a clip's "picked by" + prompt link)


def _ask(task, system=SYSTEM, key="clips", full=False):
    """Send one request in JSON mode; return the raw `key` list (full=True: the whole answer as a dict).
    Every AI call in the app goes through here. Tries the AIs in the saved order (Settings → AI models): when one
    down, errors or returns broken JSON, the next one gets the same request.
    The prompt is saved to data/logs/prompts/ and the log line links to it (see _save_prompt)."""
    errors, failed = [], 0
    try:
        pf, link = _save_prompt(system, task, key)
        db.here(f"🧠 prompt for {PURPOSE.get(key, key)} → {ai_name()}: \"{' '.join(task.split())[:220]}…\" "
                f"({len(system) + len(task):,} chars{', with your instructions' if 'FROM THE CHANNEL OWNER' in task else ''}) "
                f"full prompt + reply: {link}")
    except OSError:
        pf, link = None, ""
    for p in ai_order():
        why = _missing(p)
        if why:
            errors.append(f"{PROVIDERS[p]}: {why}")
            continue
        t = time.time()
        try:
            answer = _ask_one(p, task, system)
        except Exception as e:
            errors.append(f"{PROVIDERS[p]}: {e}"[:300])
            failed += 1
            db.here(f"🤖 AI {_model(p)} ({PROVIDERS[p]}) failed after {time.time() - t:.1f}s, trying the next one: {e}"[:400])
            continue
        last_used.update(provider=p, model=_model(p))
        LAST.model, LAST.link = f"{_model(p)} ({PROVIDERS[p]})" + (" as a fallback" if failed else ""), link
        db.here(f"🤖 AI: {_model(p)} ({PROVIDERS[p]}) answered in {time.time() - t:.1f}s"
                + (" (fallback)" if failed else ""))
        if pf:
            try:
                with open(pf, "a", encoding="utf-8") as fh:
                    fh.write(f"\n=== REPLY from {_model(p)} ({PROVIDERS[p]}), {time.time() - t:.1f}s"
                             + (f", after: {' | '.join(errors)}" if errors else "") + " ===\n"
                             + json.dumps(answer, indent=1, ensure_ascii=False) + "\n")
            except OSError:
                pass
        if full:
            return answer if isinstance(answer, dict) else {}
        return answer.get(key, []) if isinstance(answer, dict) else []
    raise RuntimeError("Every AI failed: " + " | ".join(errors))


def headline_for(headline, title):
    """The yellow headline: the AI's (3-6 words asked, hard cap 6), or, when it gave none, the title's first words."""
    words = str(headline or "").split() or re.sub(r"[^\w\s'!?&-]", " ", str(title or "")).split()
    return " ".join(words[:6])


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
        lo = LIMITS[kind][0]
        # Clamp start/end inside the candidate window.
        s = max(cand["start"], num(c.get("start"), cand["start"]))
        e = min(cand["end"], num(c.get("end"), cand["end"]))
        if e - s < lo:  # too short: grow it to the minimum length, staying inside the window
            e = min(cand["end"], s + lo)
            s = max(cand["start"], e - lo)
        e = min(e, s + HARD_MAX[kind])  # too long: cut to max length (+ the allowed stretch)
        if e - s < lo * 0.8:  # window itself too small to fit a real clip
            continue
        want[kind] -= 1
        out.append({"kind": kind, "start": s, "end": e, "score": num(c.get("virality"), 0),
                    "title": str(c.get("title", ""))[:100],
                    "headline": headline_for(c.get("headline"), c.get("title")),
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
            out.append({"kind": c["kind"], "start": c["start"], "end": min(c["end"], c["start"] + LIMITS[c["kind"]][1]),
                        "score": round(c["signal"] * 10, 1), "title": f"{title} highlight"[:100], "headline": headline_for("", title),
                        "description": "", "tags": [], "reason": "hype signal only (LLM unavailable)"})
    return out


if __name__ == "__main__":
    LIMITS.update(short=(15, 59), long=(60, 600))
    HARD_MAX.update(short=74, long=660)
    C = lambda short, long: {"short": short, "long": long}
    cands = [{"id": 0, "kind": "short", "start": 100, "end": 150, "signal": 1},
             {"id": 1, "kind": "long", "start": 0, "end": 400, "signal": 1}]
    got = validate([{"candidate": 0, "kind": "short", "start": 90, "end": 200, "virality": 90},
                    {"candidate": 0, "kind": "short", "start": 120, "end": 122, "virality": 80},
                    {"candidate": 1, "kind": "long", "start": 10, "end": 900, "virality": 70},
                    {"candidate": 9, "kind": "short", "virality": 99}], cands, C(1, 1))
    assert [(g["kind"], g["start"], g["end"]) for g in got] == [("short", 100, 150), ("long", 10, 400)], got
    got = validate([{"candidate": 0, "kind": "short", "start": 0, "end": 70, "headline": "one two three four five six seven"},     # small stretch: kept
                    {"candidate": 1, "kind": "short", "start": 100, "end": 300},  # way over: cut to 59+15
                    {"candidate": 0, "kind": "long", "start": 0, "end": 900}],    # long cut to 600+60
                   [{"id": 0, "start": 0, "end": 1000}, {"id": 1, "start": 0, "end": 1000}], C(2, 1))
    assert [(g["start"], g["end"]) for g in got] == [(0, 70), (100, 174), (0, 660)], got
    assert got[0]["headline"] == "one two three four five six", got[0]
    assert headline_for("", "This GTA Heist Went SO Wrong! (part 2)") == "This GTA Heist Went SO Wrong!", headline_for("", "This GTA Heist Went SO Wrong! (part 2)")
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
