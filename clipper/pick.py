"""CLIP-CUTTER: the AI picks the moments of ONE video that become clips, and writes their titles, headers,
descriptions and hashtags. (Finding the videos is scout.py; the AI plumbing is ai.py.)

The AI scores each clip on five parts (hook, flow, emotion, share, trend; 0-10 each) and virality() turns them
into the 0-100 score here, so every clip is scored the same way; confirm() adds +5 when 2+ audience signals
(heatmap / chat / comments) peak inside it. Category rules (categories.py) are added to the prompt.

choose_full() gives the AI the WHOLE transcript plus the signal curves as hints (it can pick any moment, overriding
the curves) and returns clip dicts, always passed through validate() first; fallback() is the raw hype windows.
"""
import re

from . import ai, categories, settings

# Clip kinds (all rendered vertical 9:16):
#   short = a full moment with its context (Settings: short_min-short_max, default 20-90s), long = a longer segment.
# A short is never over 180s (a YouTube Short); a long is as long as you set (over 180s: a normal YouTube video).
KINDS = ("short", "long")


def limits():
    """Target clip length in seconds per kind: (min, max), from Settings."""
    s = settings.get()
    return {"short": (s.short_min, s.short_max), "long": (s.long_min, s.long_max)}


FB_REEL_MAX = 90  # Facebook Reels are 90s at most (longer = a normal Page video): the owner's main platform


def hard_max():
    """How far over the max the AI may go (the stretch, Settings): a short never past 180s (YouTube Shorts' limit) and,
    while Facebook is ticked for shorts, never past 90s (so it still goes up as a Facebook Reel, unless your own short
    max is already longer); a long never past your max + the stretch."""
    s = settings.get()
    cap = settings.SHORT_HARD_MAX
    if "facebook" in s.platforms:
        cap = min(cap, max(FB_REEL_MAX, s.short_max))
    return {k: min(hi + s.stretch, cap) if k == "short" else hi + s.stretch for k, (_, hi) in limits().items()}


# What each kind is for, told to DeepSeek.
KIND_DESC = {
    "short": "a complete moment WITH its context: the setup a stranger needs, the payoff and the reaction.",
    "long": "a longer segment: a whole story, argument, mission or arc that stays gripping the whole way. In a game "
            "video, a great LONG is also a full showcase of something players can DO in the game (an activity, feature, "
            "mechanic or secret: taking someone on a date, robbing a store, a new vehicle or mission type...), shown from "
            "start to finish so it answers \"can you do X in this game?\". Title it that way, e.g. \"You Can Take Dates "
            "In GTA 6\", \"Everything You Can Do In The GTA 6 Gas Station\".",
}

# System prompt: who DeepSeek is and what a good clip looks like. Shared by both modes.
# Works for any content (gaming like GTA, IRL streams, podcasts, commentary, tutorials...).
SYSTEM = """You are a top short-form clip editor. You have cut thousands of viral clips from gaming
streams (GTA V / GTA Online / GTA RP, shooters, horror, Minecraft...), IRL streams, podcasts,
interviews, commentary and tutorials. First work out what kind of video this is from the title and
transcript, then judge it the way that audience would.

THE CHANNEL OWNER'S PRIORITIES, in this order:
1. FUNNY CONVERSATIONS: banter, roasts, people talking over each other, a casual chat that turns hilarious, someone
   saying something absurd with a straight face. A funny back-and-forth beats any action scene.
2. ATTENTION GRABBERS: a loud scream, a shocked "WHAT?!", genuine surprise, panic, a jump scare, rage. These make the
   best hooks: open the clip ON the scream / the shock (or one breath before it), never seconds earlier.
3. Everything else below.

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

Audience signals, when you get them (they are hints, the transcript decides):
- Heatmap ("most replayed", the strongest signal: real viewers rewound to watch it): the peak is usually the
  PAYOFF; the setup starts 5-20s before it. Build the clip so the payoff lands in its last 40%.
- Chat spikes: the moment is 5-15s BEFORE the spike (chat reacts late).
- Comment timestamps: several viewers naming the same spot = a strong moment.
- A spot where 2+ signals agree is a priority candidate.

How to judge a clip:
EVERY CLIP IS A COMPLETE PIECE: the viewer gets the WHOLE thing (a hook, the good content, and its ending) and
watches it to the last second. Build each clip from three parts, all three required:
    1. THE HOOK (first 1-3 seconds): the line or moment that stops the scroll AND tells a stranger what's going
       on: a bold line, a question, a shout, a crash, "watch this", the start of the conflict. It must be clear
       without context (who / what they're trying to do). Slow setup before it? Start later, at the hook. Never open
       on dead air, "um", "yeah", "anyway", "like I was saying", greetings, throat-clearing, a sponsor read, or a
       line pointing back ("and", "but", "so", "that's why", "he", "it"). The first spoken words should be the
       most attention-grabbing line: a bold claim, a shocking statement, a question that demands an answer, or
       mid-action.
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
- Must make sense to someone scrolling who has never seen this creator. If it needs a name or one fact, put it in
  the headline instead of making the clip longer.
- Length: most great SHORTS are 20-60s, and under 30s loops best (replays push retention past 100%). Go longer
  only for a complete story with a strong payoff.
- Spread the clips across the video: don't take most of them from one 10-minute stretch unless the signals
  clearly demand it. Never use times outside the transcript.
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

Score every clip honestly, 0-10 each ("scores"):
- hook: the first 2 seconds are specific and bold, open a curiosity gap, no soft open
- flow: a clear arc, a satisfying or shocking ending, makes sense alone
- emotion: laughter, anger, shock, awe, vulnerability, hype
- share: debate-worthy, quotable, relatable, "send this to a friend"
- trend: tied to what's hot this week (the game, person or event)
The app turns them into the clip's virality (hook counts most), so don't inflate them: a 5 is an average clip.

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
"scores": {"hook": 0-10, "flow": 0-10, "emotion": 0-10, "share": 0-10, "trend": 0-10},
"moment_type": "hot_take|story|reveal|debate|advice|laugh|emotional|rage|hype|glitch|fail|hidden_detail|reaction|prediction",
"title": "...", "headline": "...", "description": "...", "hashtags": ["..."],
"hook": "the clip's first line, as said", "ending": "the clip's last line, as said",
"framing": "gameplay_facecam|speaker_track|split_screen|center_crop" (what the picture most likely needs),
"copyright_risk": "low|medium|high" (high = mostly someone else's official / broadcast footage),
"reason": "one line: hook -> content -> ending, and why people will watch it to the end"}]}"""

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
    Roughly one short per short max seconds and one long per long max seconds (at least 1 if it fits at all).
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
All clips are vertical 9:16. You MAY go over the target max (never past the hard max) when the content is better
for it: the moment keeps building, a bigger reaction or a second payoff lands, or the funniest line comes right after
the max; or when cutting there would lose the setup or the payoff. A great moment 10-20s longer beats one cut short;
but never pad a weak moment. Then start "reason" with "STRETCHED:" and say what the extra seconds add.
Clips of the same kind must not overlap. A SHORT and a LONG MAY reuse the same moment (e.g. a tight short of the
best moment AND a long with the bigger story around it), but only for the very best moments."""


LIVE_MIN = 60  # live pieces: extra picks scored below this (virality) are dropped; the best one is always kept


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


def choose_full(title, n, transcript, parts, counts, note="", out=None):
    """ai modes. transcript: whole video with [sec] markers. parts: {signal: 0..1 per second} or None."""
    # ponytail: whole transcript in one request; a 3h video is ~40k tokens. Chunk it if videos get much longer.
    signals = SIGNALS_HINT.format(step=10, names=" ".join(parts), table=curve_table(parts, n)) if parts else NO_SIGNALS
    task = FULL_TASK.format(title=title, n=n, pick=pick_text(counts), signals=signals, transcript=transcript) + note
    r = ai.ask(task, SYSTEM, full=True)
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


REEL_EMOJI = "😭💀🤣😂😳🔥😱🤯😤🥶"


def reel_note(channel=""):
    """The header on the video: what happens, a bit funny, with an emoji; the creator's name only for a big individual
    creator (the owner's rule: "SPEED DID THIS" yes, a small channel's or a show's name no)."""
    who = (f' The video\'s channel is "{channel}".' if channel else " The channel isn't known: don't name anyone.")
    return ("\n\nHEADLINE FOR THIS CHANNEL'S REELS (overrides the headline rule above): what is happening in this moment, a "
            "bit funny / teasing, 4-7 words, CAPS, ending with ONE emoji from "
            f"{' '.join(REEL_EMOJI)}.\nNAMES (header AND title): use the creator's name ONLY when the channel is a big, "
            "well-known INDIVIDUAL creator or streamer fans know by name, in the short form fans use (IShowSpeed -> SPEED, "
            "Kai Cenat -> KAI, xQc -> XQC, MrBeast -> MRBEAST): \"SPEED THINKS ANIMALS ARE HUMAN 😭\". When the channel is "
            "a smaller creator, or not one person (a podcast / show / network / brand / clip channel), don't name the "
            "channel or anyone in it: just say what happens: \"COPS CHASE A GARBAGE TRUCK 💀\", \"HE DID NOT EXPECT THAT "
            "QUESTION 😳\". It still matches the title's moment." + who + "\n")


def reel_headline(text, title="", n=7):
    """The reel header: at most n words (cut cleanly, see short_headline) + its emoji kept at the end."""
    words = str(text or "").split() or str(title or "").split()
    emo = [w for w in words if not any(ch.isalnum() for ch in w)]
    plain = short_headline(" ".join(w for w in words if any(ch.isalnum() for ch in w)), n - (1 if emo else 0))
    return (plain + (" " + emo[0] if emo else "")).strip()


def learned_note():
    """What today's report decided for the cutter (insights.decide: what got views here), as a prompt block."""
    from . import insights
    t = ((insights.decisions() or {}).get("cutter") or "").strip()
    return ("\n\nWHAT WORKS ON THIS CHANNEL (today's report of our posted clips; lean on it, but the moment itself "
            "decides):\n" + t + "\n") if t else ""


REPOST_SYSTEM = """You polish the title of a short vertical video (a Reel / Short / TikTok) that a US channel posts
again. THE VIDEO'S OWN TITLE IS YOUR MAIN REFERENCE: it was written by the creator and it's what the video is about.
- Keep its subject, its meaning and its key words (names, game, the thing that happens). Don't change the story,
  don't swap in a different angle, don't make up a hook it doesn't have. If it's already a good title, keep it almost
  as it is.
- Just clean it up: drop hashtags, emoji, @handles, links, "| Facebook" style tails and spammy bits; fix the
  capitalization (Title Case or normal case, one CAPS word for emphasis is fine); make it read smoothly in English;
  40-80 characters is ideal but never cut its meaning to fit.
- Use the caption only to fill in what the title leaves out (e.g. the game or the person's name), never to replace it.
- Only when the title is missing or useless (e.g. just "Facebook", "Instagram", "Video by someone") write it from the
  caption, same rules: only what it says, no invented events, names or results.
- Name a creator only when the title / caption does and it's a big individual creator (e.g. IShowSpeed, Kai Cenat).
Also: up to 5 hashtags taken from the title / caption's own topic (no #, no spaces) and a one-line description (a
plain sentence that goes with the title, no hashtags).
Answer JSON only: {"title": "...", "description": "...", "hashtags": ["..."]}"""

# Titles Facebook / Instagram give a video that has none of its own: useless as a title.
_GENERIC = re.compile(r"^\s*((facebook|instagram|youtube|reels?|videos?|watch|untitled)(\s+(video|reel|watch))?"
                      r"|(video|reel|post)\s+by\s+.+|.+\s+on\s+instagram.*)\s*$", re.I)


# Facebook's page title for a reel: "66K views · 1.6K reactions | <the caption> | <the page's name>"
_FB_STATS = re.compile(r"^\s*[\d.,]+\s*[KMB]?\s+(views?|plays?|reactions?|likes?|comments?|shares?)"
                       r"(\s*·\s*[\d.,]+\s*[KMB]?\s+(views?|plays?|reactions?|likes?|comments?|shares?))*\s*\|\s*", re.I)


def clean_title(title, uploader=""):
    """The video's own title, tidied: no "66K views · 1.6K reactions |" in front, no "| <page name>" / "| Facebook"
    at the end, no hashtags, @handles, links or emoji; the first line only. '' when it's a generic one ("Facebook",
    "Video by ...")."""
    t = str(title or "").strip().split("\n")[0]
    t = _FB_STATS.sub("", t)
    if uploader:
        t = re.sub(r"\s*\|\s*" + re.escape(uploader.strip()) + r"\s*$", "", t, flags=re.I)
    t = re.sub(r"https?://\S+|[#@]\w+|[\U0001F000-\U0001FAFF☀-➿️‍]", " ", t)
    t = re.sub(r"\s*[|\-–]\s*(facebook|instagram|youtube)\s*$", "", " ".join(t.split()), flags=re.I).strip(" -|·")
    return "" if not t or _GENERIC.match(t) else t[:100]


def repost_title(title, caption, creator, seconds):
    """Cheat page: the AI's title / description / hashtags for a reposted reel, built on its OWN title (the caption
    only fills gaps). -> dict (empty when every AI fails: the caller keeps the cleaned original title)."""
    from . import ai
    own = clean_title(title)
    task = (f"The video's own title (MAIN REFERENCE): {(title or '').strip()[:300] or '(none)'}"
            + ("" if own else "  <- missing / generic: write it from the caption") + "\n"
            f"Caption (only to fill gaps): {(caption or '').strip()[:1200] or '(none)'}\n"
            f"Creator: {creator or '(unknown)'}\nLength: {seconds:.0f} seconds")
    try:
        a = ai.ask(task, REPOST_SYSTEM, "repost", full=True)
    except Exception:
        return {}
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]", "", str(a.get("title") or ""))  # no emoji
    t = " ".join(t.replace("#", "").split())[:100]
    if len(t) < 8:
        return {}
    tags = [re.sub(r"\W", "", str(h)) for h in a.get("hashtags") or []]
    return {"title": t, "description": " ".join(str(a.get("description") or "").split())[:500],
            "hashtags": [h for h in tags if h][:5]}


def headline_for(headline, title):
    """The yellow headline: the AI's (3-6 words asked, hard cap 6), or, when it gave none, the title's first words."""
    words = str(headline or "").split() or re.sub(r"[^\w\s'!?&-]", " ", str(title or "")).split()
    return " ".join(words[:6])


WEAK_END = {"oh", "my", "the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "this", "that", "so", "i",
            "you", "we", "with", "for", "at", "but", "be", "are", "was", "just"}


def short_headline(text, n=5):
    """At most n words that still read as a title: when it has to be cut, stop at the last ':' / ',' / '-' / '!' /
    '?' inside those words, else drop dangling filler words ("GTA 6 FIRST LOOK: OH MY" -> "GTA 6 FIRST LOOK")."""
    words = str(text or "").split()
    if len(words) <= n:
        return " ".join(words)
    head = words[:n]
    stops = [i for i, w in enumerate(head) if w[-1] in ":,;-!?" or w in ("-", "|")]
    if stops and stops[-1] >= 1:
        head = head[:stops[-1] + 1]
    while len(head) > 2 and head[-1].lower().strip(":,;-!?|") in WEAK_END:
        head.pop()
    return " ".join(head).rstrip(":,;-| ")


def _num(v, default):  # LLM may return "12.5", null, or garbage
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


SCORE_WEIGHTS = {"hook": .40, "flow": .20, "emotion": .15, "share": .15, "trend": .10}


def virality(c):
    """0-100 from the AI's five 0-10 scores (the hook counts most); its own "virality" number when it gave none."""
    sc = c.get("scores") if isinstance(c.get("scores"), dict) else {}
    vals = {k: _num(sc.get(k), None) for k in SCORE_WEIGHTS}
    if all(v is None for v in vals.values()):
        return min(max(_num(c.get("virality"), 0), 0), 100)
    return round(10 * sum(w * min(max(vals[k] or 0, 0), 10) for k, w in SCORE_WEIGHTS.items()))


CONFIRM = ("heatmap", "chat", "comments")  # audience signals: loud audio and the streamer's own words don't count


def confirm(clips, parts, peak=0.6):
    """+5 virality (max 99) for each clip where 2+ audience signals peak (>= peak on their 0..1 scale) inside it;
    +3 more when it OPENS on a scream: very loud audio together with the streamer's surprise / shout words in its first
    4 seconds (the owner's favourite hook)."""
    for c in clips:
        s, e = int(c["start"]), max(int(c["start"]) + 1, int(c["end"]))
        hits = [k for k in CONFIRM if len((parts or {}).get(k, ())) > s and float(parts[k][s:e].max()) >= peak]
        if len(hits) >= 2:
            c["score"] = min(99, c["score"] + 5)
            c["confirmed"] = hits
        loud, react = (parts or {}).get("audio"), (parts or {}).get("reaction")
        if loud is not None and react is not None and len(loud) > s and len(react) > s \
                and float(loud[s:s + 4].max()) >= 0.9 and float(react[s:s + 4].max()) >= 0.6:
            c["score"] = min(99, c["score"] + 3)
            c["scream_hook"] = True
    return clips


def category_note(category):
    """The category's cutting rules (categories.py) as a prompt block; every category's when it isn't known."""
    return "\n\nCATEGORY RULES:\n" + categories.module(category, "cut") + "\n"


def _overlap(a, b):
    """Share of the shorter of two (start, end) spans that the other covers."""
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0])) / max(1e-9, min(a[1] - a[0], b[1] - b[0]))


def validate(clips, candidates, counts):
    """Never trust LLM numbers: clamp to window and duration limits, drop junk and same-kind clips overlapping a
    better one by more than 30%, cap counts."""
    by_id = {c["id"]: c for c in candidates}
    out, want = [], dict(counts)  # want = how many of each kind we still need
    # Best first, so if the LLM returns too many, we keep the highest-virality ones.
    for c in sorted((c for c in clips if isinstance(c, dict)), key=lambda c: -virality(c)):
        cand = by_id.get(c.get("candidate"))
        kind = c.get("kind")
        if not cand or kind not in want or want[kind] <= 0:  # unknown window, bad kind, or quota full
            continue
        lo = limits()[kind][0]
        # Clamp start/end inside the candidate window.
        s = max(cand["start"], _num(c.get("start"), cand["start"]))
        e = min(cand["end"], _num(c.get("end"), cand["end"]))
        if e - s < lo:  # too short: grow it to the minimum length, staying inside the window
            e = min(cand["end"], s + lo)
            s = max(cand["start"], e - lo)
        e = min(e, s + hard_max()[kind])  # too long: cut to max length (+ the allowed stretch)
        if e - s < lo * 0.8:  # window itself too small to fit a real clip
            continue
        if any(o["kind"] == kind and _overlap((s, e), (o["start"], o["end"])) > 0.3 for o in out):
            continue  # the same moment twice: the better one is already in
        want[kind] -= 1
        sc = c.get("scores") if isinstance(c.get("scores"), dict) else {}
        out.append({"kind": kind, "start": s, "end": e, "score": virality(c),
                    "title": str(c.get("title", ""))[:100],
                    "headline": headline_for(c.get("headline"), c.get("title")),
                    "description": str(c.get("description", "")),
                    "tags": [str(t).lstrip("#") for t in c.get("hashtags", [])][:15],
                    "reason": str(c.get("reason", "")),
                    "meta": {"moment": str(c.get("moment_type") or "")[:20], "framing": str(c.get("framing") or "")[:20],
                             "risk": str(c.get("copyright_risk") or "")[:10],
                             "scores": {k: _num(sc.get(k), None) for k in SCORE_WEIGHTS if _num(sc.get(k), None) is not None}}})
    return out


def fallback(candidates, counts, title):
    """No LLM? Use the raw hype windows, strongest signal first."""
    out, want = [], dict(counts)
    for c in sorted(candidates, key=lambda c: -c["signal"]):
        if want[c["kind"]] > 0:
            want[c["kind"]] -= 1
            out.append({"kind": c["kind"], "start": c["start"], "end": min(c["end"], c["start"] + limits()[c["kind"]][1]),
                        "score": round(c["signal"] * 10, 1), "title": f"{title} highlight"[:100], "headline": headline_for("", title),
                        "description": "", "tags": [], "reason": "hype signal only (LLM unavailable)"})
    return out


if __name__ == "__main__":
    limits = lambda: {"short": (15, 59), "long": (60, 600)}  # noqa: E731 (fixed numbers for the checks below)
    hard_max = lambda: {"short": 74, "long": 660}  # noqa: E731
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
    assert virality({"scores": {"hook": 10, "flow": 10, "emotion": 10, "share": 10, "trend": 10}}) == 100
    assert virality({"scores": {"hook": 8, "flow": 6, "emotion": 7, "share": 5, "trend": "x"}}) == 62  # 10*(3.2+1.2+1.05+.75)
    assert virality({"virality": 77}) == 77 and virality({"virality": "junk"}) == 0
    got = validate([{"candidate": 0, "kind": "short", "start": 0, "end": 40, "scores": {"hook": 9}, "moment_type": "fail"},
                    {"candidate": 0, "kind": "short", "start": 10, "end": 50, "scores": {"hook": 5}},   # 75% overlap: out
                    {"candidate": 0, "kind": "short", "start": 35, "end": 75, "scores": {"hook": 4}}],  # 12.5%: kept
                   [{"id": 0, "start": 0, "end": 100}], C(3, 0))
    assert [(g["start"], g["score"]) for g in got] == [(0, 36), (35, 16)], got
    assert got[0]["meta"] == {"moment": "fail", "framing": "", "risk": "", "scores": {"hook": 9.0}}, got[0]["meta"]
    P = {"heatmap": np.zeros(100), "chat": np.zeros(100), "audio": np.ones(100)}
    P["heatmap"][20], P["chat"][25] = 0.9, 0.7
    got = confirm([{"start": 10, "end": 30, "score": 97}, {"start": 50, "end": 70, "score": 50}], P)
    assert [g["score"] for g in got] == [99, 50] and got[0]["confirmed"] == ["heatmap", "chat"], got
    P2 = {"audio": np.zeros(100), "reaction": np.zeros(100)}
    P2["audio"][31], P2["reaction"][32] = 1.0, 0.8
    got = confirm([{"start": 30, "end": 60, "score": 50}, {"start": 20, "end": 60, "score": 50}], P2)
    assert [g["score"] for g in got] == [53, 50] and got[0]["scream_hook"], got  # a scream 11s in is no hook
    assert "CATEGORY RULES:\nPODCAST" in category_note("podcast") and "GAMING" in category_note("")
    print("pick ok")
