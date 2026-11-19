"""Clipping campaigns (Whop / Clipping Culture style): a brand pays per view for shorts cut from ITS official footage,
under strict rules (tag the brand, #Ad first, a required line, no music, no AI voice, minimum length...).

Flow: paste the brief -> the AI turns it into rules (read_brief; a regex pass fills anything the AI missed, so the
must-haves never depend on the AI) -> add the official videos (links, files, or a folder on this PC) -> each video is
a normal clipping job (worker.py) with the campaign's rules in the AI's prompt, a clean render (no follow message,
end card, logo, voice-over or background video) -> every clip gets a caption built to the rules (caption_for) and a
checklist (check) -> nothing is posted by itself: you post from the Campaigns page (API) or by hand (copy the
caption, download the clip, paste the post link back to track the keep-live days)."""
import json
import queue
import re
import shutil
import threading
import time
from pathlib import Path

from . import db, pick

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".ts", ".avi", ".m4v")
DISCLOSURES = ("#Ad", "#Advertisement", "#Sponsored")
ASPECTS = {"9:16": 0.5625, "4:5": 0.8, "1:1": 1.0, "16:9": 1.7778}
QUEUE = queue.Queue()  # campaign job ids, clipped one at a time (the GPU is never shared between two videos)
_LOOP = threading.Lock()
_started = False

RULES_SYSTEM = """You read the brief of a paid clipping campaign (brands pay clippers per view for shorts cut from their
official footage) and turn it into rules a clipping tool will follow exactly. Copy text EXACTLY as written in the
brief for tags, hashtags, required lines and examples. Never invent a rule, tag, line or number that isn't in the
brief: use "" / [] / null when the brief doesn't say.

Return JSON only:
{"name": "campaign name", "brand": "brand or game", "platforms": ["tiktok"|"instagram"|"youtube"|"facebook"],
 "what_to_clip": "what the clips must show, 1-2 sentences",
 "focus": "the specific new content to highlight (names of modes, maps, features), comma separated",
 "tags": ["@handle", ...]  (accounts every caption must tag),
 "disclosures": ["#Ad", ...]  (allowed FTC disclosure hashtags, first one = preferred),
 "required_lines": ["..."]  (lines every post must contain, e.g. a free trial mention; exact text),
 "required_where": "caption"|"onscreen"|"either",
 "extra_hashtags_max": <int, hashtags allowed after the disclosure>,
 "min_seconds": <int or null>, "max_seconds": <int or null>, "keep_live_days": <int or null>,
 "caption_examples": ["..."]  (without the disclosure / hashtags), "onscreen_examples": ["..."],
 "language": "English", "no_music": true|false, "no_ai_voice": true|false, "official_footage_only": true|false,
 "content_folder": "url or empty",
 "dont": ["short list of what is NOT allowed in the content and captions"],
 "tone": "how captions must sound, 1 sentence",
 "clips_per_video": <int or null: ONLY if the brief says how many clips/posts to make from each video>,
 "aspect": "9:16"|"4:5"|"1:1"|"16:9"|null  (ONLY if the brief states the video format/aspect ratio, e.g. "vertical 9:16"),
 "subtitles": true|false|null  (ONLY if the brief requires or forbids subtitles/captions burned into the video),
 "onscreen_text": "required"|"optional"|"forbidden"|null  (what the brief says about on-screen text)}"""

CAPTION_SYSTEM = """You write the caption of one short for a paid clipping campaign. The caption must follow the brand's
rules and sound like a real fan wrote it, about what THIS clip actually shows.
- Keep it true: only facts from the clip's transcript and the brief. No made-up facts, stats, dates or features.
- Don't mislabel: a trailer or briefing is not "gameplay".
- No exaggerated hooks, rage bait, clickbait or ALL CAPS shouting. One short sentence (under 90 characters) is best.
- Write your OWN caption, close in style to the examples but not a copy, and different from the captions already used.
- No hashtags and no @tags in "caption": the tool adds the tags, the disclosure and the required line itself.
- "hashtags": 0-3 hashtags related to the content (the game, the mode), without the disclosure.
Return JSON only: {"caption": "...", "hashtags": ["..."], "onscreen": "optional short on-screen text, true to the clip"}"""


# ---- the brief -> rules ----

def _lines_after(text, marker, n=3):
    """The non-empty lines right after the line containing `marker` (e.g. 'Use one of these lines:')."""
    out, hit = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if hit:
            if not s:
                if out:
                    break
                continue
            if s.isupper() or s.endswith(":") or len(s) > 140:
                break
            out.append(s)
            if len(out) >= n:
                break
        elif marker.lower() in s.lower():
            hit = True
    return out


def regex_rules(brief):
    """What can be read from the brief without the AI. Fills gaps in the AI's rules, and is the whole answer when
    every AI is down."""
    t = brief or ""
    low = t.lower()
    tags = list(dict.fromkeys(m for m in re.findall(r"(?<![\w.])@[A-Za-z0-9_.]{2,30}", t) if not m.endswith(".")))
    disc = [d for d in DISCLOSURES if re.search(re.escape(d) + r"\b", t)]
    req = _lines_after(t, "use one of these lines", 3) or _lines_after(t, "must mention", 2)
    num = lambda pat: (int(m.group(1)) if (m := re.search(pat, t, re.I)) else None)
    first = next((ln.strip() for ln in t.splitlines() if ln.strip()), "")
    url = re.search(r"https?://\S+", t)
    return {
        "name": first[:120], "brand": "",
        "platforms": [p for p, w in (("tiktok", "tiktok"), ("instagram", "instagram"), ("youtube", "youtube"),
                                     ("facebook", "facebook")) if w in low] or ["tiktok", "instagram"],
        "what_to_clip": "", "focus": "", "tags": tags, "disclosures": disc or ["#Ad"], "required_lines": req,
        "required_where": "either" if re.search(r"caption or (the )?on-screen", low) else "caption",
        "extra_hashtags_max": num(r"up to (\d+) more hashtags"),
        "min_seconds": num(r"minimum (?:video )?length[^0-9]{0,20}(\d+)\s*s"),
        "max_seconds": num(r"maximum (?:video )?length[^0-9]{0,20}(\d+)\s*s"),
        "keep_live_days": num(r"(?:stay|remain) (?:live|up) for (\d+) days") or num(r"before (\d+) days"),
        "caption_examples": [], "onscreen_examples": [], "language": "English",
        "no_music": bool(re.search(r"music[^\n]{0,30}not allowed|no (?:added |background )?music|any added music", low)),
        "no_ai_voice": bool(re.search(r"no ai voice|ai voiceover|ai-generated voice", low)),
        "official_footage_only": "official" in low and ("only footage" in low or "outside footage" in low),
        "content_folder": url.group(0).rstrip(").,") if url else "", "dont": [], "tone": "",
        "clips_per_video": num(r"(\d+)\s+(?:clips|shorts|posts|edits)\s+(?:per|from each|for each|from every)\b"),
        "aspect": next((a for pat, a in ((r"9\s*:\s*16|vertical", "9:16"), (r"4\s*:\s*5", "4:5"), (r"1\s*:\s*1|square", "1:1"),
                                          (r"16\s*:\s*9|horizontal|landscape", "16:9")) if re.search(pat, low)), None),
        "subtitles": (False if re.search(r"no (?:burned[- ]in )?(?:subtitles|captions on (?:the )?video)", low) else
                      True if re.search(r"(?:subtitles|burned[- ]in captions) (?:are )?required|must (?:have|include) subtitles", low) else None),
        "onscreen_text": next((v for pat, v in ((r"on-?screen text[^\n]{0,15}\(?optional|on-?screen text is optional", "optional"),
                                                (r"no on-?screen text", "forbidden"),
                                                (r"on-?screen text[^\n]{0,15}\(?required", "required")) if re.search(pat, low)), None)}


def _clean_rules(r, base):
    """AI answer + regex base -> rules with the right types; the regex wins for the must-haves it found."""
    r = r if isinstance(r, dict) else {}
    out = dict(base)
    for k, v in r.items():
        if k in base and v not in (None, "", []):
            out[k] = v
    strs = lambda v: [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
    out["tags"] = list(dict.fromkeys(("@" + x.lstrip("@")) for x in strs(base["tags"]) + strs(out["tags"])))[:5]
    out["disclosures"] = [d for d in dict.fromkeys(strs(base["disclosures"]) + strs(out["disclosures"]))
                          if d.startswith("#")][:4] or ["#Ad"]
    out["required_lines"] = list(dict.fromkeys(strs(base["required_lines"]) or strs(out["required_lines"])))[:4]
    for k in ("caption_examples", "onscreen_examples", "dont"):
        out[k] = strs(out.get(k))[:10]
    out["platforms"] = [p for p in dict.fromkeys(strs(out["platforms"])) if p in ("tiktok", "instagram", "youtube", "facebook")] \
        or base["platforms"]
    if out.get("aspect") not in ASPECTS:
        out["aspect"] = base.get("aspect") if base.get("aspect") in ASPECTS else None
    if out.get("onscreen_text") not in ("required", "optional", "forbidden"):
        out["onscreen_text"] = base.get("onscreen_text")
    out["subtitles"] = out["subtitles"] if isinstance(out.get("subtitles"), bool) else base.get("subtitles")
    for k in ("extra_hashtags_max", "min_seconds", "max_seconds", "keep_live_days", "clips_per_video"):
        b = base.get(k)
        try:
            out[k] = int(b if b is not None else out[k]) if (b is not None or out.get(k) is not None) else None
        except (TypeError, ValueError):
            out[k] = None
    if out["required_where"] not in ("caption", "onscreen", "either"):
        out["required_where"] = base["required_where"]
    for k in ("no_music", "no_ai_voice", "official_footage_only"):
        out[k] = bool(out.get(k)) or bool(base.get(k))
    return out


def edited_rules(r):
    """Rules as you edited them on the page: cleaned up, nothing from the brief added back (so you can remove a tag)."""
    empty = {**regex_rules(""), "tags": [], "disclosures": [], "required_lines": [], "no_music": False,
             "no_ai_voice": False, "official_footage_only": False, "name": ""}
    return _clean_rules(r, empty)


def read_brief(brief):
    """The brief -> rules (AI + regex). Never raises: no AI = the regex rules (you can edit them on the page)."""
    base = regex_rules(brief)
    try:
        return _clean_rules(pick._ask("CAMPAIGN BRIEF:\n" + brief[:20000], RULES_SYSTEM, "rules", full=True), base), True
    except Exception as e:
        db.here(f"the AI couldn't read the brief ({e}); using what could be read from it directly")
        return _clean_rules({}, base), False


# ---- the campaign pipeline: its own flow, not the viral-clip one in worker._process ----
#   C1 WATCH: Whisper transcript of the whole video + every shot cut (ffmpeg scene detection). No hype curves:
#      official footage is edited, so the picture's own cuts and the narration's sentences are the clip edges.
#   C2 PLAN: one AI call with the campaign editor's prompt (PLAN_SYSTEM): list the features the video shows, give
#      each clip ONE feature, write its caption / hashtags / on-screen text in the same answer.
#   C3 TRIM: each clip's edges moved to the nearest sentence edge or shot cut, inside the campaign's length.
#   C4 RENDER: clean (no follow message, end card, logo, voice-over or background video: only the official picture
#      and sound), the on-screen text on top, the required line on screen too if you asked for it.
#   C5 CAPTION + CHECK: the caption built to the rules (build_caption), every rule ticked off (check). Nothing posts.

PLAN_SYSTEM = """You are the editor for a brand's PAID CLIPPING CAMPAIGN. You get the brand's OFFICIAL video (its
transcript with [seconds] markers and the times where the picture cuts to a new shot) and the campaign's rules.
This is not about finding viral chaos: cut clean, true, high-quality shorts that each clearly SHOW the campaign's
content, so the brand approves them and real fans watch them to the end.

How to plan:
1. Work out what the video is (trailer, intel briefing, gameplay reveal, developer talk, ...) and list the distinct
   things it shows: each new mode, map, location, feature, character, weapon, offer.
2. Give each clip ONE of those things, the strongest first. Clips must not overlap and should not repeat a feature.
3. Each clip is a COMPLETE piece the viewer watches to the end, in three parts, all required:
   - HOOK (first 1-3s): a reveal, a strong line or a striking shot that stops the scroll and says what this is.
     Never a logo, a black screen, a slow fade-in, or a line pointing back ("and/but/so/this").
   - CONTENT: the feature shown properly: enough of it that a fan actually sees what's new, every shot earning its
     place, no repeated or empty stretch.
   - ENDING: the thought or shot sequence closes (the sentence finishes, the reveal completes, the scene lands), so
     it feels whole, not cut off. Never end mid-sentence or mid-reveal.
   It starts at the start of a sentence or on a shot cut, and it still makes sense to someone who finds it weeks
   later with no context. A slightly longer complete clip beats a shorter cut-off one (within the length).
4. Prefer parts where the narration names the feature (so the caption can be true) and the picture shows it.
   Skip logos, legal text, rating screens, end slates and "available now" cards unless they carry the offer.
5. No speech (a music-only trailer)? Plan from the shot cuts: one sequence of shots per clip.

For every clip write:
- "caption": one genuine fan sentence about what THIS clip shows (under 90 characters), true to the transcript and
  the brief, close in style to the brief's examples but in your own words, different from the other clips. No
  hashtags, no @tags, no disclosure: the tool adds the tags, the disclosure and the required line itself.
- "hashtags": 0-3 hashtags related to the content (game, mode), never the disclosure.
- "onscreen": short on-screen text (2-7 words) that matches what is on screen, like the brief's examples.
- "shows": the feature it shows. "quality": 0-100, how clearly and attractively it shows the campaign content.
- "reason": one line: hook -> content -> ending, and why people will watch it to the end.
Never: made-up facts, stats or dates; calling a trailer or briefing "gameplay"; exaggerated hooks, rage bait,
clickbait words; NSFW, gore or anything controversial.

Return JSON only:
{"video_type": "...", "features": ["..."],
 "clips": [{"start": <sec>, "end": <sec>, "shows": "...", "caption": "...", "hashtags": ["..."], "onscreen": "...",
            "quality": <0-100>, "reason": "..."}]}"""


def lengths(camp):
    """(min, target max) seconds for this campaign's clips."""
    from .config import SHORT_HARD_MAX
    r = rules(camp)
    lo = max(int(r.get("min_seconds") or 10), 10) + 2  # +2: still over the minimum after trimming to the edges
    hi = int(camp.get("max_seconds") or r.get("max_seconds") or 60)
    return lo, max(lo + 5, min(hi, SHORT_HARD_MAX))


def shots(src, threshold=0.3):
    """Seconds where the picture cuts to a new shot (ffmpeg scene score, on small frames: fast)."""
    import subprocess
    from .config import FFMPEG
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(src), "-an", "-sn", "-vf",
                            f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        return sorted({round(float(m), 2) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)})
    except Exception as e:
        db.here(f"shot detection failed ({e}): sentences only")
        return []


def _num(v, d):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def plan(camp, title, n, transcript, cuts, count):
    """The AI plans up to `count` clips of the official video. -> (clips, video_type, features)."""
    r, (lo, hi) = rules(camp), lengths(camp)
    keep = {k: r.get(k) for k in ("brand", "name", "what_to_clip", "focus", "tone", "caption_examples",
                                  "onscreen_examples", "dont", "required_lines")}
    task = (f"CAMPAIGN RULES:\n{json.dumps(keep, ensure_ascii=False, indent=1)}\n\n"
            f"Official video: \"{title}\" ({n} seconds)\nPlan up to {count} clips, each {lo}-{hi}s "
            f"(fewer if the video doesn't hold {count} different good parts).\n"
            f"Shot cuts (seconds): {', '.join(f'{c:g}' for c in cuts[:400]) or 'none found'}\n\n"
            f"Transcript:\n{transcript or '(no speech: plan from the shot cuts)'}"
            + (pick.user_note(camp.get("notes")) if (camp.get("notes") or "").strip() else ""))
    a = pick._ask(task, PLAN_SYSTEM, "plan", full=True)
    out = []
    for c in sorted((c for c in a.get("clips") or [] if isinstance(c, dict)), key=lambda c: -_num(c.get("quality"), 0)):
        s, e = max(0.0, _num(c.get("start"), -1)), min(float(n), _num(c.get("end"), -1))
        if e - s < lo * 0.6 or any(s < o["end"] - 1 and e > o["start"] + 1 for o in out):
            continue  # junk times, or it overlaps a better clip
        out.append({"start": s, "end": min(e, s + hi + 8), "shows": str(c.get("shows") or "")[:120],
                    "caption": str(c.get("caption") or "")[:200],
                    "hashtags": [str(h) for h in (c.get("hashtags") or []) if str(h).strip()][:6],
                    "onscreen": str(c.get("onscreen") or "")[:60], "score": _num(c.get("quality"), 0),
                    "reason": str(c.get("reason") or "")[:300]})
        if len(out) >= count:
            break
    return out, str(a.get("video_type") or ""), [str(f) for f in (a.get("features") or [])][:12]


def fallback_plan(n, cuts, count, lo, hi):
    """No AI: whole sequences of shots, each lo..hi seconds, from the start of the video."""
    out, s = [], 0.0
    for e in [c for c in cuts if 0 < c < n] + [float(n)]:
        if e - s >= lo:
            out.append({"start": s, "end": min(e, s + hi), "shows": "", "caption": "", "hashtags": [], "onscreen": "",
                        "score": 0, "reason": "a sequence of shots (the AI was unavailable)"})
            s = e
            if len(out) >= count:
                break
    return out


def trim(c, words, cuts, n, lo, hi, reach=2.5):
    """Edges to the nearest sentence edge or shot cut (within `reach` s), never through a word, lo..hi+8 seconds."""
    starts = [w["s"] for i, w in enumerate(words) if i == 0 or words[i - 1]["w"].endswith((".", "?", "!"))]
    ends = [w["e"] for w in words if w["w"].endswith((".", "?", "!"))]
    near = lambda t, opts: min((o for o in opts if abs(o - t) <= reach), key=lambda o: abs(o - t), default=t)
    s, e = near(c["start"], starts + cuts), near(c["end"], ends + cuts)
    inside = [w for w in words if s < w["e"] and w["s"] < e]
    if inside:  # a word cut in half at either edge: take all of it
        s, e = min(s, inside[0]["s"] - 0.15), max(e, inside[-1]["e"] + 0.3)
    s = max(0.0, s)
    if e - s < lo:
        e = s + lo
    e = min(e, s + hi + 8, float(n))
    return s, e


def process(job, src, title, d):
    """One official video -> campaign clips (the flow above). Runs in the job's thread (worker._process sends it here)."""
    from . import render, transcribe, worker
    from .config import DATA
    jid, camp = job["id"], db.get("campaigns", job["campaign_id"])
    say = lambda m: db.log(jid, m)
    if not camp:
        return say("its campaign was deleted: nothing to do")
    r, (lo, hi) = rules(camp), lengths(camp)
    n = int(render._duration(src))
    say(f"CAMPAIGN \"{camp['name']}\": {n}s of official footage, up to {job['shorts']} clips of {lo}-{hi}s")
    worker._check(jid)
    with db.step(jid, "C1 watching the footage: finding the shot cuts"):
        cuts = shots(src)
    say(f"{len(cuts)} shot cuts")
    worker._check(jid)
    with db.step(jid, f"C1 watching the footage: transcribing {n}s ({transcribe.device()})"):
        words = transcribe.words(src, 0, n)
    say(f"transcript: {len(words)} words")
    worker._check(jid)
    pick.LAST.model, pick.LAST.link = "", ""
    try:
        with db.step(jid, f"C2 {pick.ai_name()} plans the clips as the campaign editor"):
            clips, vtype, feats = plan(camp, title, n, transcribe.text_with_times(words, 0, n), cuts, job["shorts"])
        say(f"the AI sees: {vtype or 'a video'}; it shows: {', '.join(feats) or '?'}")
    except Exception as e:
        say(f"the AI couldn't plan ({e}): cutting along the shot sequences")
        clips, vtype = fallback_plan(n, cuts, job["shorts"], lo, hi), ""
    if not clips:
        return say("no part of this video makes a good campaign clip")
    by = pick.LAST.model or "", pick.LAST.link or ""
    used = [x["caption"].splitlines()[0] for x in db.all(
        "clips", "caption != '' AND job_id IN (SELECT id FROM jobs WHERE campaign_id=?)", (camp["id"],))]
    trial = r["required_lines"][0] if camp.get("trial_on_screen") and r.get("required_lines") else ""
    taken = []  # (start, end) of the clips already made: trimming must not make two clips overlap
    for c in clips:
        worker._check(jid)
        s, e = trim(c, words, cuts, n, lo, hi)
        for a, b in taken:
            if s < b and e > a:
                s, e = (b, max(e, b + lo)) if c["start"] >= a else (s, min(e, a))
        e = min(e, float(n))
        if e - s < lo - 2 or any(s < b and e > a for a, b in taken):
            say(f"C3 plan {worker._mmss(c['start'])}-{worker._mmss(c['end'])} skipped: overlaps a better clip")
            continue
        taken.append((s, e))
        say(f"C3 plan {worker._mmss(c['start'])}-{worker._mmss(c['end'])} -> {worker._mmss(s)}-{worker._mmss(e)} "
            f"({e - s:.0f}s), shows {c['shows']!r} | {c['reason']}")
        onscreen = c["onscreen"] if camp.get("onscreen", 1) else ""
        cap = build_caption(r, c["caption"] or c["shows"] or title, c["hashtags"] or [r.get("brand", "")], f"{onscreen} {trial}")
        if cap.splitlines()[0] in used:
            say("the caption repeats an earlier clip's: asking for a new one")
            cap = caption_for(camp, {"title": c["caption"], "headline": onscreen, "start": s, "end": e},
                              transcribe.text_with_times(words, s, e), used)[0]
        used.append(cap.splitlines()[0])
        lines = worker._sentences([w for w in words if s <= w["s"] < e])
        why = {"by": by[0], "prompt": by[1], "mode": "campaign editor: whole transcript + shot cuts", "score": c["score"],
               "signals": {}, "opening": (lines[0] if lines else "")[:300],
               "closing": (lines[-1] if len(lines) > 1 else "")[:300], "source": title,
               "campaign": {"id": camp["id"], "name": camp["name"], "shows": c["shows"], "onscreen_line": trial,
                            "keep_live_days": r.get("keep_live_days"), "video_type": vtype}}
        cid = db.insert("clips", job_id=jid, kind="short", start=s, end=e, score=c["score"],
                        title=(c["caption"] or c["shows"] or title)[:100], description=cap,
                        tags=json.dumps([h.lstrip("#") for h in re.findall(r"#\w+", cap)]), reason=c["reason"],
                        headline=onscreen, caption=cap, why=json.dumps(why, ensure_ascii=False), status="rendering")
        out = d / "clips" / f"{cid}.mp4"
        try:
            with db.step(jid, f"C4 rendering #{cid} ({e - s:.0f}s, clean: only the official picture and sound)"):
                render.cut(src, out, s, e, True, words if job.get("subtitles", 1) else (), onscreen, (),
                           job.get("aspect"), job.get("layout") or "", follow=False, hook_text=trial, clean=True)
        except Exception:
            db.update("clips", cid, status="render failed")
            raise
        worker.keep_master(jid, src, out, s, e, words)
        db.update("clips", cid, path=str(out.relative_to(DATA)), status="rendered")
        bad = [x["rule"] for x in recheck(cid) if not x["ok"]]
        say(f"C5 clip #{cid} ready to post. Caption: {cap!r}. Rules: {'all met' if not bad else 'NOT met: ' + '; '.join(bad)}")


# ---- captions ----

def _tag(t):
    t = re.sub(r"[^\w]", "", str(t).lstrip("#"))
    return f"#{t}" if t else ""


def build_caption(r, text, hashtags=(), onscreen=""):
    """Caption to the rules: the text + the @tags, the required line (unless it's on screen and the rules allow that),
    then on its own line the disclosure FIRST and at most `extra_hashtags_max` more hashtags."""
    text = re.sub(r"(?<![\w.])[#@][\w.]+", "", str(text or "")).strip(" -|·\n")  # the tool adds tags/hashtags itself
    text = " ".join(text.split())
    for t in r.get("tags", []):
        if t.lower() not in text.lower():
            text = f"{text} {t}".strip()
    lines = [text]
    for req in r.get("required_lines", [])[:1]:  # "use one of these lines": the first one. Always in the caption, even
        if req.lower() not in text.lower():     # when it's on screen too: a reviewer reads the caption first
            lines.append(req)
    disc = (r.get("disclosures") or ["#Ad"])[0]
    n = r.get("extra_hashtags_max")
    n = 3 if n is None else max(0, int(n))
    extra = [h for h in dict.fromkeys(_tag(h) for h in hashtags)
             if h and h.lower() not in (d.lower() for d in DISCLOSURES)][:n]
    lines.append(" ".join([disc, *extra]))
    return "\n".join(lines)


def caption_for(camp, clip, transcript, used=()):
    """AI caption for one clip (true to its transcript, close to the examples, not a repeat), built to the rules.
    No AI -> the clip's own title. -> (caption, onscreen text the AI suggested)."""
    r = rules(camp)
    task = (f"Brand: {r.get('brand') or '?'}\nCampaign: {r.get('name')}\nWhat the clips show: {r.get('what_to_clip')}\n"
            f"Focus: {r.get('focus')}\nTone: {r.get('tone') or 'a genuine fan'}\n"
            f"Caption examples from the brief: {json.dumps(r.get('caption_examples', []), ensure_ascii=False)}\n"
            f"Captions already used in this campaign (don't repeat them): {json.dumps(list(used)[-12:], ensure_ascii=False)}\n"
            f"Not allowed: {json.dumps(r.get('dont', [])[:8], ensure_ascii=False)}\n\n"
            f"This clip: title \"{clip.get('title')}\", on-screen text \"{clip.get('headline')}\", "
            f"{clip['end'] - clip['start']:.0f}s\nTranscript of the clip:\n{transcript[:4000] or '(no speech)'}"
            + (pick.user_note(camp.get("notes")) if (camp.get("notes") or "").strip() else ""))
    try:
        a = pick._ask(task, CAPTION_SYSTEM, "caption", full=True)
        text, tags, onscreen = str(a.get("caption") or "").strip(), a.get("hashtags") or [], str(a.get("onscreen") or "")
    except Exception as e:
        db.here(f"caption: the AI failed ({e}), using the clip title")
        text, tags, onscreen = "", [], ""
    text = text or clip.get("title") or r.get("name") or ""
    tags = tags if isinstance(tags, list) else []
    return build_caption(r, text, tags or [r.get("brand", "")], clip.get("headline") or ""), onscreen


def check(camp, clip, job=None):
    """The campaign's rules ticked off for one clip: [{"rule", "ok", "note"}] (shown on the clip, before you post)."""
    r, cap = rules(camp), str(clip.get("caption") or "")
    lines = [ln.strip() for ln in cap.splitlines() if ln.strip()]
    tags_in = re.findall(r"(?<![\w.])#\w+", cap)
    disc = [d.lower() for d in (r.get("disclosures") or DISCLOSURES)]
    last = lines[-1] if lines else ""
    shown = " ".join([str(clip.get("headline") or ""), str((json.loads(clip.get("why") or "{}").get("campaign") or {}).get("onscreen_line") or "")])
    secs = clip["end"] - clip["start"]
    job = job or {}
    out = [{"rule": f"Tags {', '.join(r.get('tags', []))}", "ok": all(t.lower() in cap.lower() for t in r.get("tags", []))}] if r.get("tags") else []
    out.append({"rule": f"Disclosure {r['disclosures'][0] if r.get('disclosures') else '#Ad'} is the first hashtag, on its own line after the text",
                "ok": bool(tags_in) and tags_in[0].lower() in disc and last.lower().startswith(tags_in[0].lower()) and len(lines) > 1})
    if r.get("extra_hashtags_max") is not None:
        out.append({"rule": f"At most {r['extra_hashtags_max']} hashtags after the disclosure",
                    "ok": len(tags_in) - 1 <= r["extra_hashtags_max"], "note": f"{max(0, len(tags_in) - 1)} used"})
    for req in r.get("required_lines", [])[:1]:
        where = r.get("required_where")
        in_cap, on_scr = req.lower() in cap.lower(), req.lower() in shown.lower()
        out.append({"rule": f"Mentions \"{req}\"", "ok": in_cap or (where in ("either", "onscreen") and on_scr),
                    "note": " + ".join(x for x, y in (("in the caption", in_cap), ("on screen", on_scr)) if y)})
    if r.get("min_seconds"):
        out.append({"rule": f"At least {r['min_seconds']}s long", "ok": secs >= r["min_seconds"], "note": f"{secs:.0f}s"})
    if r.get("max_seconds"):
        out.append({"rule": f"At most {r['max_seconds']}s long", "ok": secs <= r["max_seconds"], "note": f"{secs:.0f}s"})
    if r.get("no_ai_voice"):
        out.append({"rule": "No AI voice-over", "ok": not job.get("voiceover")})
    if r.get("no_music") or r.get("official_footage_only"):
        out.append({"rule": "Only the official footage and its audio (no music, no background video)",
                    "ok": not str(job.get("layout") or "").startswith("split:")})
    return out


# ---- campaigns + their videos ----

def rules(camp):
    try:
        return json.loads(camp.get("rules") or "{}") or regex_rules(camp.get("brief", ""))
    except ValueError:
        return regex_rules(camp.get("brief", ""))


def suggest(brief):
    """Analyze the brief for the New campaign form: the rules, plus every setting filled from the brief where it says
    so ("found": true, with where it came from) or a question with a sensible default where it doesn't ("found": false).
    -> {"rules", "by_ai", "fields": {field: {"value", "found", "why" | "ask"}}}"""
    r, by_ai = read_brief(brief)
    low = brief.lower()
    f = {}

    def put(k, value, found, why="", ask=""):
        f[k] = {"value": value, "found": bool(found), **({"why": why} if found else {"ask": ask})}
    named = by_ai and r.get("name") and r["name"] != regex_rules(brief)["name"]
    put("name", r.get("name") or brief.strip().splitlines()[0][:120], True,
        "the campaign's name" if named else "the brief's first line")
    plats = [p for p in r.get("platforms", []) if p in low or (p == "instagram" and "reels" in low)]
    put("platforms", plats or ["tiktok", "instagram"], bool(plats), "the platforms the brief names",
        "Where do you post? The brief doesn't name a platform.")
    put("shorts", r.get("clips_per_video") or 3, r.get("clips_per_video"), "the brief says how many per video",
        "How many clips from each official video? The brief doesn't say (3 is a good start; each shows a different feature).")
    lo = max(int(r.get("min_seconds") or 10), 10)
    put("max_seconds", r.get("max_seconds") or max(60, lo + 15), r.get("max_seconds"), "the brief's maximum length",
        f"Longest clip in seconds? The brief only sets a minimum ({lo}s)." if r.get("min_seconds") else
        "Longest clip in seconds? The brief sets no length.")
    put("aspect", ASPECTS.get(r.get("aspect") or "", 0.5625), r.get("aspect"), f"the brief asks for {r.get('aspect')}",
        "Framing? The brief doesn't set one: 9:16 fills the phone (crops the sides), 16:9 keeps the whole picture with bars.")
    put("subtitles", r.get("subtitles") if r.get("subtitles") is not None else True, r.get("subtitles") is not None,
        "the brief " + ("requires" if r.get("subtitles") else "forbids") + " subtitles",
        "Subtitles of what's said on the video? The brief doesn't say (they help people watching muted).")
    ost = r.get("onscreen_text")
    put("onscreen", ost != "forbidden" if ost in ("required", "forbidden") else bool(r.get("onscreen_examples")) or True,
        ost in ("required", "forbidden"), f"the brief: on-screen text is {ost}",
        "Short on-screen text at the top (true to the picture)? The brief makes it optional." if ost == "optional" else
        "Short on-screen text at the top? The brief doesn't say.")
    where, req = r.get("required_where"), (r.get("required_lines") or [""])[0]
    put("trial_on_screen", where == "onscreen", where in ("onscreen", "caption") or not req,
        "no required line" if not req else f"the brief puts \"{req}\" " + ("on screen" if where == "onscreen" else "in the caption"),
        f"Show \"{req}\" on screen too? The brief allows caption OR on-screen; it's always in the caption.")
    return {"rules": r, "by_ai": by_ai, "fields": f}


def create(brief, name="", rules=None, **opts):
    """New campaign. rules: from suggest() (the form's analysis) -> ready now; else the brief is read in the
    background (status 'reading the brief' -> 'ready'). name: yours (else the AI's); opts: campaigns columns."""
    if rules:
        r = edited_rules(rules)
        cid = db.insert("campaigns", brief=brief, name=(name or r.get("name") or brief.strip().splitlines()[0])[:120],
                        rules=json.dumps(r, ensure_ascii=False), status="ready", created=time.time(), **opts)
        db.log(cid, f"created from the analysed brief: tags {r['tags']}, disclosure {r['disclosures'][:1]}, required line "
                    f"{r['required_lines'][:1]}, +{r['extra_hashtags_max']} hashtags, min {r['min_seconds']}s. Add the official videos.",
               table="campaigns")
        return cid
    cid = db.insert("campaigns", brief=brief, name=(name or brief.strip().splitlines()[0])[:120],
                    status="reading the brief", created=time.time(), **opts)

    def run():
        db.log_to(lambda m: db.log(cid, m, table="campaigns"))
        try:
            with db.step(cid, "reading the brief", table="campaigns"):
                r, by_ai = read_brief(brief)
            c = db.get("campaigns", cid)
            db.update("campaigns", cid, rules=json.dumps(r, ensure_ascii=False), status="ready",
                      name=c["name"] if name else (r.get("name") or c["name"])[:120],
                      platforms=c["platforms"] if c.get("platforms") not in (None, "", "[]") else json.dumps(r["platforms"]))
            db.log(cid, f"rules {'read by the AI' if by_ai else 'read without the AI'}: tags {r['tags']}, disclosure "
                        f"{r['disclosures'][:1]}, required line {r['required_lines'][:1]}, +{r['extra_hashtags_max']} hashtags, "
                        f"min {r['min_seconds']}s, platforms {r['platforms']}. Check them, then add the official videos.",
                   table="campaigns")
        except Exception as e:
            db.update("campaigns", cid, status="failed")
            db.log(cid, f"couldn't read the brief: {e!r}", table="campaigns", exc=True)
        finally:
            db.log_to(None)
    threading.Thread(target=run, daemon=True).start()
    return cid


def _job(camp, url, title=None, status="queued"):
    """One official video -> a clipping job with the campaign's settings (never auto-uploaded)."""
    return db.insert("jobs", url=url, title=title, shorts=int(camp.get("shorts") or 3), longs=0, auto_upload=0,
                     platforms=camp.get("platforms") or '["tiktok","instagram"]', aspect=camp.get("aspect"),
                     layout=camp.get("layout") or "", mode="campaign", subtitles=int(camp.get("subtitles", 1)),
                     follow=0, notes="", voiceover=0, campaign_id=camp["id"], status=status, created=time.time())


def add_urls(camp, urls):
    ids = [_job(camp, u) for u in urls]
    for j in ids:
        enqueue(j)
    db.log(camp["id"], f"added {len(ids)} video link(s): jobs {', '.join(f'#{j}' for j in ids)}", table="campaigns")
    return ids


def add_file_job(camp, filename):
    """A job waiting for its uploaded bytes (status 'uploading'); call enqueue() when the file is written."""
    return _job(camp, f"file:{Path(filename).name}", Path(filename).stem, status="uploading")


def add_folder(camp, folder):
    """Every video in a folder on this PC (the downloaded content folder) -> one job each (copied into the job)."""
    p = Path(folder.strip().strip('"'))
    if not p.is_dir():
        raise ValueError(f"not a folder on this PC: {p}")
    files = sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXT)
    if not files:
        raise ValueError(f"no videos ({', '.join(VIDEO_EXT)}) in {p}")
    from .config import DATA
    ids = []
    for f in files[:50]:
        j = _job(camp, f"file:{f.name}", f.stem, status="uploading")
        d = DATA / "jobs" / str(j)
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, d / f"source{f.suffix.lower()}")
        db.update("jobs", j, status="queued")
        enqueue(j)
        ids.append(j)
    db.log(camp["id"], f"added {len(ids)} video(s) from {p}: jobs {', '.join(f'#{j}' for j in ids)}", table="campaigns")
    return ids


def enqueue(job_id):
    global _started
    with _LOOP:
        if not _started:
            threading.Thread(target=_loop, daemon=True).start()
            _started = True
    QUEUE.put(job_id)


def _loop():
    from . import worker
    while True:
        jid = QUEUE.get()
        j = db.get("jobs", jid)
        if j and j["status"] == "queued":
            try:
                worker.run(jid)
            except Exception as e:
                db.log(jid, f"campaign job crashed: {e!r}", exc=True)


def recheck(cid):
    """After you edit a caption: tick the rules off again."""
    c = db.get("clips", cid)
    job = db.get("jobs", c["job_id"]) or {}
    camp = db.get("campaigns", job.get("campaign_id")) if job.get("campaign_id") else None
    if not camp:
        return []
    why = json.loads(c.get("why") or "{}")
    why.setdefault("campaign", {})["checks"] = ok = check(camp, c, job)
    db.update("clips", cid, why=json.dumps(why, ensure_ascii=False))
    return ok
