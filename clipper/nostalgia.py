"""Nostalgia clips (clip mode "nostalgia"): short, calm gameplay moments from old games with a feelings caption.

The look (9:16):   black band   "POV: It's 2006, school just ended, and you have zero responsibilities ❤️"
                   square gameplay (driving at sunset, flying, a boat on the water: nothing dramatic)
                   black band   the game's name
with a nostalgic track from the music library (Settings → Music) over the game sound, no subtitles, no end card
(they loop). 10-15 s.

  moments(n, cuts, motion, want)   calm, unbroken stretches of the video (no shot change inside, some motion: not a
                                   paused menu, not chaos), spread over the video, skipping intros and outros
  write(title, cands, want, ...)   the vision model picks the most scenic / nostalgic ones and writes their captions
  render(src, out, start, end, caption, game, track)

Captions are drawn with Pillow (libass can't draw colour emoji: they came out as boxes)."""
import json
import random
import re
import subprocess
import threading
import time
import sys
import unicodedata
from pathlib import Path

from . import config, db

LENGTH = 12  # seconds per clip (10-15 reads and loops best)
W, H, SQ = 1080, 1920, 1080  # canvas and the square picture in the middle
TOP = (H - SQ) // 2  # 420: picture from y 420 to 1500

# fonts: a bold condensed face for the caption, the system's colour emoji font
TEXT_FONTS = ["/System/Library/Fonts/Supplemental/Impact.ttf", "C:/Windows/Fonts/impact.ttf",
              "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf", "/usr/share/fonts/truetype/msttcorefonts/impact.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]  # Linux: apt install ttf-mscorefonts-installer
EMOJI_FONTS = [("/System/Library/Fonts/Apple Color Emoji.ttc", 160), ("C:/Windows/Fonts/seguiemj.ttf", 109),
               ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109)]

SYSTEM = """You write the one caption on top of a nostalgia Reel: a short, calm moment from a classic game that
makes people who played it as kids stop scrolling and feel something. The caption carries the whole post.

Caption patterns (fill them with what is ACTUALLY on screen in this moment; vary them):
- POV + a year and a feeling:      "POV: It's <year the game came out>, <a childhood situation>, and you're <doing exactly
                                    what's on screen> <emoji>"
- How long ago it was:             "<game> is <N> years old and <a feeling about it> <emoji>"
- A feeling tied to the scene:     "Nothing beats <the exact activity on screen> in <the place on screen> <emoji>"
- A list of three, the last one a memory:  "Just a <vehicle on screen>, <something visible>, and <a memory> <emoji>"
- Then vs now:                     "<kids today / before GTA 6> ... <what we had> <emoji>"
- A tiny shared memory of playing it: the panic, the cheat codes, the radio station, saving the game, your parents
- Funny (about 1 in 3 captions): a relatable joke about playing it as a kid, still warm, never mean. E.g. the shape
  "Me cruising <place on screen> while my mom thinks I'm doing homework <emoji>", "<a cheat code> was the only
  password I ever remembered <emoji>", "Cops: 5 stars. Me: <doing exactly what's on screen> like nothing happened <emoji>",
  "Mom calling me for dinner in the middle of <the mission / the drive> <emoji>" (fill it with this scene, new words)
Rules: 8-22 words, plain everyday English. It MUST fit what's on screen: on foot / the exact vehicle (car, bike,
motorbike, plane, helicopter, boat), day / sunset / night, rain, the city / beach / countryside. Never name a vehicle,
time of day or place that isn't there. Ends with 1-2 emoji that fit. No hashtags, no quotes around it, never "clip",
"video" or "Reel". Every caption is new: never reuse a line you've seen before."""

# lines that must never come back word for word (they were examples once, and the model loved copying them)
BANNED = ["just a plane, an endless sky, and a thousand childhood memories", "pov: it's 2006, school just ended, and you have zero responsibilities",
          "nothing beats the feeling of returning to ocean beach", "if happiness had a soundtrack, it would be vice city",
          "low quality game but best quality times", "can't believe this is 25 years old game", "nothing could beat this feeling",
          "today's kids won't understand the heat of this scene", "before gta 6, we ruled these skies"]
VEHICLES = {"plane": ("plane", "jet", "dodo", "aircraft", "flying", "sky"), "helicopter": ("helicopter", "heli", "chopper"),
            "boat": ("boat", "speedboat", "jetski", "jet ski", "yacht", "sailing"), "motorbike": ("motorbike", "motorcycle", "pcj", "scooter", "chopper bike"),
            "bike": ("bike", "bmx", "bicycle", "cycling"), "car": ("car", "driving", "drive", "cruising", "cruise", "convertible"),
            "jetpack": ("jetpack", "jet pack"), "foot": ("on foot", "walking", "running")}

DESCRIBE = """You look at two frames (start and end) of one moment of classic game footage and say exactly what's on
screen.
- scene: one plain line: how the player moves (on foot, or the vehicle: car / motorbike / bicycle / plane / helicopter /
  boat / jetpack), the place (city street, beach, countryside, desert, bridge, rooftops, airport...), time of day (day /
  sunset / night), weather, anything notable (palm trees, neon, the sea).
- vehicle: one word: car, motorbike, bike, plane, helicopter, boat, jetpack, foot.
- clean: the game's own HUD (minimap / radar, money, clock, health, weapon icon, area name like "Verona Beach") is
  NORMAL and counts as clean. Set false ONLY when something was added on top of the game: a person's face / webcam box,
  subtitles or captions, a channel watermark or logo, or the frame is a menu, pause screen, map screen or loading screen.
- graphics: "original" for the blocky 2000s look with the game's own made-up cars; "remaster" for the Definitive Edition,
  ray tracing or HD texture packs; "mod" when anything is clearly modded in, above all modern REAL cars (a Range Rover,
  BMW, Lamborghini, Tesla... with real badges and shapes from after 2005), modern weapons or characters.
Reply as JSON: {"frame": {"scene": "...", "vehicle": "...", "clean": true, "graphics": "original" | "remaster" | "mod"}}"""


def describe(cands, images):
    """The vision model's account of each candidate moment (one request per moment, both its frames) ->
    {id: {"scene", "vehicle", "clean", "graphics"}}. {} without a vision model."""
    from concurrent.futures import ThreadPoolExecutor
    from . import pick
    if not images or not config.VISION_MODEL.strip():
        return {}

    def one(c):
        mine = [(lab, b) for lab, b in images if lab.startswith(f"candidate {c['id']} ")]
        if not mine:
            return None
        try:
            x = pick._ask("What's on screen in this moment? (its frames follow)", DESCRIBE, "frame",
                          config.VISION_MODEL.strip(), images=[(lab.split("(")[-1].rstrip("):") + " frame:", b)
                                                               for lab, b in mine])
        except Exception:
            return None
        if not isinstance(x, dict):
            return None
        return {"scene": str(x.get("scene") or "")[:200], "vehicle": str(x.get("vehicle") or "").lower().strip(),
                "clean": x.get("clean") is not False, "graphics": str(x.get("graphics") or "original").lower()}
    with ThreadPoolExecutor(3) as ex:
        got = list(ex.map(one, cands))
    return {c["id"]: g for c, g in zip(cands, got) if g}


def fits(caption, scene):
    """False when the caption copies an old example, or names a vehicle that isn't the one on screen."""
    import difflib
    low = caption.lower()
    if any(difflib.SequenceMatcher(None, low[:len(b) + 10], b).ratio() > 0.8 for b in BANNED):
        return False
    raw = scene.get("vehicle", "") or ""
    on = next((v for v in VEHICLES if v in raw), raw)  # "on foot" -> foot, "a red car" -> car
    for v, words in VEHICLES.items():
        if v == on or (v == "car" and on in ("", "none")):
            continue
        if v == "car" and on in ("motorbike", "bike"):  # "driving" / "cruising" fit a bike too
            continue
        hit = [w for w in words if re.search(rf"\b{re.escape(w)}\b", low)]
        if hit and not (v == "plane" and "sky" in hit and len(hit) == 1):  # "sky" alone is just scenery
            return False
    return True


# Choosing source videos (topics.py, mode "nostalgia"): the footage must be calm and voiceless.
VIDEO_RULE = ("These videos are for calm nostalgia clips with music over them (the video's own sound is replaced): score "
              "high for plain recordings of someone just playing: long streams / stream VODs, longplays, free roam, "
              "walkthroughs, \"no commentary\" videos, PS2 / PCSX2 / PC recordings of the original game, where the game "
              "fills the screen. Score 0: edited montages and compilations, challenge videos (\"Can I...\", \"I tried...\"), "
              "reactions, a facecam box, subtitled / lyrics / music videos, remasters, mods, \"realistic graphics\", and other "
              "games (GTA V, GTA Online, GTA 6).")
BAD_TITLE = re.compile(r"😱|🤯|\bcan i\b|\bi (tried|became|played|spent|survived)\b|reaction|react\b|legendado|lyrics?\b|"
                       r"letra|subtitl|sub espa|facecam|\bgta ?(5|v|vi|6|online)\b|"
                       r"definitive|remaster|\bmods?\b|modded|realistic|rtx|ray ?trac|trailer|compilation|funny|"
                       r"moments|challenge|speedrun", re.I)


def bad_title(title):
    """A title that gives away commentary, a stream, subtitles, a remaster / mod or another GTA."""
    return bool(BAD_TITLE.search(str(title or "")))


def facecam(src, start, end):
    """True when a streamer's facecam is in the moment: a face in a corner of the picture in BOTH checked frames
    (the player's character's face shows up near the middle and moves; a facecam sits in a corner and stays)."""
    try:
        import cv2
        import numpy as np
        from . import render
        from .vision import MODEL
        for t in (start + 2, end - 2):
            raw = subprocess.run([config.FFMPEG, "-loglevel", "error", "-nostdin", "-ss", f"{t:.2f}", "-i", str(src),
                                  "-frames:v", "1", "-vf", "scale=1280:-2", "-f", "image2pipe", "-c:v", "png", "-"],
                                 capture_output=True, timeout=30, stdin=subprocess.DEVNULL).stdout
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
            if img is None:
                continue
            det = cv2.FaceDetectorYN.create(str(MODEL), "", (img.shape[1], img.shape[0]), 0.75)
            _, faces = det.detect(img)
            h, w = img.shape[:2]
            corner = faces is not None and any(
                (fx + fw / 2 < w * 0.3 or fx + fw / 2 > w * 0.7) and (fy + fh / 2 < h * 0.4 or fy + fh / 2 > h * 0.6)
                for fx, fy, fw, fh in faces[:, :4])
            if not corner:
                return False
        return True
    except Exception:
        return False


def moments(n, cuts, motion, want):
    """-> up to `want` [{"id", "start", "end"}]: LENGTH-second windows with no shot change inside and some movement,
    the calmest first, at least a window apart, between 8% and 92% of the video (skips intros, menus, outros)."""
    import numpy as np
    if n < LENGTH + 4:
        return []
    m = np.asarray(motion if motion is not None and len(motion) else np.zeros(n), dtype=float)
    m = np.pad(m, (0, max(0, n - len(m))))[:n]
    lo, hi = int(n * 0.08), int(n * 0.92) - LENGTH
    if hi <= lo:
        lo, hi = 0, n - LENGTH
    scored = []
    for s in range(lo, hi + 1, 2):
        e = s + LENGTH
        if any(s - 0.5 <= c <= e + 0.5 for c in cuts):  # a shot change inside: not one continuous moment
            continue
        seg = m[s:e]
        mean = float(seg.mean()) if len(seg) else 0.0
        if mean < 0.004:  # frozen picture: a pause screen, a menu, a loading screen
            continue
        # calm but moving: driving / flying changes the picture a little every second; chaos changes it a lot
        scored.append((abs(mean - 0.03) + float(seg.std()), s))
    out = []
    for _, s in sorted(scored):
        if all(abs(s - o["start"]) >= LENGTH * 1.5 for o in out):
            out.append({"id": len(out), "start": float(s), "end": float(s + LENGTH)})
        if len(out) >= want:
            break
    return out


NIGHT = re.compile(r"\b(night|nighttime|dusk|evening|dark|midnight|rain|rainy|storm|sunset)\b", re.I)


def write(title, cands, want, scope="", examples="", images=None, tracks=(), day_only=False):
    """Two steps: the vision model says what's on screen in each candidate (describe), then the caption is written
    FROM that description, so it fits the picture. Unclean / remastered moments are dropped; a caption that names the
    wrong vehicle or copies an old example is written again. Also picks the track that suits the scene.
    -> [{"id", "caption", "game", "title", "description", "tags", "music", "scene"}]"""
    from . import pick
    seen = describe(cands, images)
    if seen:
        keep = [c for c in cands if seen.get(c["id"], {}).get("clean", True)
                and not any(w in seen.get(c["id"], {}).get("graphics", "") for w in ("remaster", "mod"))
                and not (day_only and NIGHT.search(seen.get(c["id"], {}).get("scene", "")))]  # the page posts bright days
        cands = keep or []
        if not cands:
            return []
    music = ("\nMusic library (pick the track whose mood fits each scene: sunset / night drives -> chill, lofi, "
             "nostalgic; flying, boats, open road -> retro, synthwave; rain -> lofi, chill):\n"
             + "\n".join(f"- {t}" for t in tracks)) if tracks else ""
    lines = "\n".join(f"candidate {c['id']}: " + (seen[c["id"]]["scene"] if c["id"] in seen else f"{c['start']:.0f}s")
                      for c in cands)
    recent = [r["headline"] for r in db.all("clips", "features LIKE '%nostalgia%' AND headline != ''")[:25]]
    scenes = [json.loads(r["features"] or "{}").get("scene") for r in db.all("clips", "features LIKE '%\"scene\"%'")[:15]]
    scenes = [x for x in scenes if x]
    if scenes:  # variety: what the page posted lately, so the next ones look different
        examples += ("\nScenes the page posted lately (prefer moments that look DIFFERENT: another vehicle, place or "
                     "activity):\n" + "\n".join(f"- {x}" for x in scenes))
    if recent:  # variety: the page's last captions, so the next ones don't all open the same way
        examples += ("\nThe page's latest captions (don't repeat their wording or opening; if most start with \"POV: "
                     "It's 20xx\", use a different pattern this time):\n" + "\n".join(f"- {r}" for r in recent))
    task = (f"The gameplay video: {title}\n{scope}\n{examples}\nCandidate moments ({LENGTH}s each) and what's on screen in "
            f"each:\n{lines}\n\nPick the {want} most scenic, nostalgic one(s) "
            + ("(this page posts bright, sunny DAYTIME scenes only: the beach, palm-lined streets, the sea, open roads "
               "and flying in the sun beat plain streets; captions about sunny days, summer, school holidays)." if day_only else
               "(sunsets, the sea, open roads, the city at night, flying beat plain streets).") + music
            + ("\nYour picks must differ from each other (different vehicle, place or activity), not two of the same drive."
               if want > 1 else "")
            + "\nFor each pick write: caption (the line on top: it must describe THIS scene, the right vehicle / on foot, "
            "place and time of day), game (the game in THIS video, from its title, by its short name, e.g. \"GTA San "
            "Andreas\", \"GTA Vice City\", \"GTA III\", \"Counter-Strike 1.6\"), music (one track name from the library, "
            "exactly as written), title (for YouTube, under 70 characters, no emoji), description (1-2 short lines for the "
            "post: a nostalgic question that makes people comment), tags (4-6, no #).\n"
            'Reply as JSON: {"picks": [{"id": <number>, "caption": "...", "game": "...", "title": "...", '
            '"description": "...", "tags": ["..."], "music": "..."}]}')
    # without descriptions the writer has to look itself
    model, imgs = (None, None) if seen else ((config.VISION_MODEL.strip() or None), images)
    ids, out = {c["id"] for c in cands}, []
    for attempt in range(3):
        extra = "" if attempt == 0 else ("\nYour last captions didn't match the scene or reused an old line: write new "
                                         "ones that name exactly what's on screen.")
        picks = pick._ask(task + extra, SYSTEM, "picks", model or config.POST_MODEL or None, images=imgs)
        for p in picks if isinstance(picks, list) else []:
            try:
                k = int(p.get("id"))
            except (TypeError, ValueError):
                continue
            cap = " ".join(str(p.get("caption") or "").replace('"', "").split())
            if k not in ids or not cap or k in [o["id"] for o in out]:
                continue
            if seen and not fits(cap, seen.get(k, {})):
                db.syslog(f"nostalgia: caption didn't fit the scene ({seen.get(k, {}).get('scene', '')}): {cap}")
                continue
            if not seen and not fits(cap, {}):
                continue
            out.append({"id": k, "caption": cap[:160], "game": str(p.get("game") or "").strip()[:40],
                        "title": str(p.get("title") or cap).strip()[:95], "description": str(p.get("description") or "").strip(),
                        "tags": [str(t).lstrip("#").replace(" ", "") for t in (p.get("tags") or [])][:6],
                        "music": str(p.get("music") or "").strip(), "scene": seen.get(k, {}).get("scene", "")})
        if len(out) >= want:
            break
    return out[:want]


def track(category_id=None, wanted=""):
    """The track the AI chose for the scene (`wanted`, its name), else one that suits nostalgia (retro / synthwave /
    lofi / chill / nostalgic in its name), else any. None when the library is empty (the game's own sound is kept)."""
    from . import engage
    ts = engage.tracks()
    hit = next((t for t in ts if wanted and t["name"].lower() == wanted.lower().strip()), None) or \
        next((t for t in ts if wanted and wanted.lower().strip() in t["name"].lower()), None)
    if hit:
        return hit
    good = [t for t in ts if any(k in t["name"].lower() for k in ("nostalg", "retro", "synth", "lofi", "lo-fi", "chill",
                                                                   "80s", "vapor", "dream"))]
    return random.choice(good or ts) if ts else None


# ---- drawing ----

def _font(paths, size):
    from PIL import ImageFont
    for p in paths:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def _emoji_font():
    from PIL import ImageFont
    for p, native in EMOJI_FONTS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, native), native
            except OSError:
                continue
    return None, 0


def _is_emoji(ch):
    return unicodedata.category(ch) in ("So", "Cs") or 0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF


def _runs(text):
    """'Vice City 🔥❤️' -> [("text", "Vice City "), ("emoji", "🔥"), ("emoji", "❤️")]. Variation selectors / ZWJ stay
    with their emoji."""
    out = []
    for ch in text:
        if out and out[-1][0] == "emoji" and (0xFE00 <= ord(ch) <= 0xFE0F or ord(ch) == 0x200D or 0x1F3FB <= ord(ch) <= 0x1F3FF
                                             or (out[-1][1].endswith("\u200d"))):
            out[-1] = ("emoji", out[-1][1] + ch)
        elif _is_emoji(ch):
            out.append(("emoji", ch))
        elif out and out[-1][0] == "text":
            out[-1] = ("text", out[-1][1] + ch)
        else:
            out.append(("text", ch))
    return out


class _Writer:
    """Measures and draws one line of mixed text + colour emoji (emoji scaled to the text size)."""

    def __init__(self, size, fonts=None, stroke=None, stroke_fill=(0, 0, 0)):
        self.size = size
        self.font = _font(fonts or TEXT_FONTS, size)
        self.stroke = max(1, size // 30) if stroke is None else stroke
        self.stroke_fill = stroke_fill
        self.efont, self.native = _emoji_font()
        self.cache = {}

    def _emoji(self, e):
        from PIL import Image, ImageDraw
        if e not in self.cache:
            n = self.native
            im = Image.new("RGBA", (int(n * 1.3), int(n * 1.3)), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((0, 0), e, font=self.efont, embedded_color=True)
            box = im.getbbox()
            im = im.crop(box) if box else im
            k = self.size * 0.95 / max(im.height, 1)
            self.cache[e] = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
        return self.cache[e]

    def width(self, text):
        w = 0
        for kind, s in _runs(text):
            if kind == "text":
                w += self.font.getlength(s)
            elif self.efont:
                w += self._emoji(s).width + self.size * 0.08
        return w

    def draw(self, img, x, y, text, fill=(255, 255, 255)):
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        for kind, s in _runs(text):
            if kind == "text":
                d.text((x, y), s, font=self.font, fill=fill, stroke_width=self.stroke, stroke_fill=self.stroke_fill)
                x += self.font.getlength(s)
            elif self.efont:
                e = self._emoji(s)
                img.alpha_composite(e, (int(x + self.size * 0.04), int(y + self.size * 0.12)))
                x += e.width + self.size * 0.08


def _wrap(wr, text, max_w):
    words = []
    for word in text.split():  # an emoji-only word sticks to the word before it (never alone on the last line)
        if words and all(k == "emoji" for k, _ in _runs(word)):
            words[-1] += " " + word
        else:
            words.append(word)
    lines, cur = [], ""
    for word in words:
        t = f"{cur} {word}".strip()
        if cur and wr.width(t) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = t
    return lines + ([cur] if cur else [])


def frame_png(caption, game, out):
    """The still layer: black canvas, caption block centred in the top band (just above the picture, below the
    platforms' top icons), the game's name under the picture, your watermark. The picture area stays black (the
    video goes over it)."""
    from PIL import Image
    from . import render
    img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    band_top, band_bottom = 250, TOP - 22  # Instagram's "Reels | Friends" tabs cover the top ~240 px
    for size in (62, 56, 50, 46, 42):  # largest size whose wrap fits the band (below the tabs) in at most 3 lines
        wr = _Writer(size)
        lines = _wrap(wr, caption, W - 120)
        lh = int(size * 1.22)
        if len(lines) <= 3 and lh * len(lines) <= band_bottom - band_top:
            break
    y = band_bottom - lh * len(lines)
    for ln in lines:
        wr.draw(img, (W - wr.width(ln)) / 2, y, ln)
        y += lh
    if game:  # the game's name under the picture, in the same face (not the official logo: that's a trademark)
        g = _Writer(84)
        name = render.plain(game).upper()
        g.draw(img, (W - g.width(name)) / 2, TOP + SQ + 40, name)
    mark = render.plain(config.WATERMARK)
    if mark:
        m = _Writer(34)
        m.draw(img, (W - m.width(mark)) / 2, TOP + SQ + 150, mark, fill=(170, 170, 170))
    img.save(out)
    return out


RETRO = ("eq=contrast=0.95:saturation=1.12:brightness=0.015:gamma=1.03,"  # a touch faded and warm, like an old capture
         "colorbalance=rs=0.05:gs=0.01:bs=-0.05:rm=0.03:bm=-0.03,vignette=angle=PI/5,noise=alls=7:allf=t")


def _fps(src):
    """60 when the source is 50-60 fps (smoother pans and drives), else 30."""
    from . import render as rd
    try:
        r = subprocess.run([rd.ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True, timeout=20).stdout.strip()
        num, den = (int(x) for x in r.split("/"))
        return 60 if num / den >= 49 else 30
    except Exception:
        return 30


def brightness(src, t):
    """Mean brightness 0-255 of the frame at t (the centre square): sunny daytime is well above 100, night under 70."""
    try:
        import cv2
        import numpy as np
        raw = subprocess.run([config.FFMPEG, "-loglevel", "error", "-nostdin", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                              "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=160:160", "-f", "image2pipe", "-c:v", "png", "-"],
                             capture_output=True, timeout=30, stdin=subprocess.DEVNULL).stdout
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE) if raw else None
        return float(img.mean()) if img is not None else 0.0
    except Exception:
        return 0.0


def daylight(src, start, end, min_level=100):
    """Bright, daytime moment: both its frames at least `min_level` bright."""
    return all(brightness(src, t) >= min_level for t in (start + 2.5, end - 2.5))


def render(src, out, start, end, caption, game, music=None, progress=None, grade=""):
    """The finished 9:16 clip: the frame layer, the centre square of the gameplay over its middle, and the music alone
    (faded in and out: the video's own sound can carry a voice); the game sound only when there's no track.
    grade="retro": warm, slightly faded colours, a soft vignette and fine grain (an old-video feel). 60 fps sources
    stay 60 fps (smoother)."""
    from . import render as rd
    out = Path(out)
    png = out.with_name(out.stem + "_frame.png")
    frame_png(caption, game, png)
    dur = end - start
    fps = _fps(src)
    look = f",{RETRO}" if grade == "retro" else ""
    pre = min(float(start), rd.PRE_ROLL)  # two-stage seek (see render.cut): no frozen first frame
    cmd = [config.FFMPEG, "-y", "-loglevel", "error", *rd.hwdec(), "-ss", f"{start - pre:.3f}", "-t", f"{dur + pre:.3f}", "-i", str(Path(src).resolve()),
           "-loop", "1", "-t", f"{dur + pre:.3f}", "-i", str(png.resolve())]
    graph = (f"[0:v]trim=start={pre:.3f},setpts=PTS-STARTPTS,crop='min(iw,ih)':'min(iw,ih)',scale={SQ}:{SQ}:flags=lanczos,setsar=1,fps={fps}{look}[v];"
             f"[1:v]format=rgba,fps={fps}[bg];[bg][v]overlay=0:{TOP}:shortest=1,format=yuv420p,"
             f"fade=t=in:st=0:d=0.3[o]")
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
        # the track from a quarter in (past its intro), the game sound quiet underneath
        lead = max(0.0, rd.duration(music) * 0.25)
        graph += (f";[2:a]atrim=start={lead:.2f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,"
                  f"volume=0.9,afade=t=in:d=0.6,afade=t=out:st={max(dur - 1.5, 0):.2f}:d=1.5,atrim=0:{dur:.3f}[a]")
    else:
        graph += f";[0:a]atrim=start={pre:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,afade=t=out:st={max(dur - 1.0, 0):.2f}:d=1[a]"
    cmd += ["-filter_complex", graph, "-map", "[o]", "-map", "[a]", "-t", f"{dur:.3f}", *rd.encoder(), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
            str(out.resolve())]
    for attempt in ([cmd, rd.cpu_cmd(cmd)] if rd.on_gpu(cmd) else [cmd]):  # GPU encode failed -> again on the CPU
        p = subprocess.Popen(attempt, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
        errs = []
        t = threading.Thread(target=lambda: errs.append(p.stderr.read()), daemon=True)
        t.start()
        for line in p.stdout:
            if progress and line.startswith("out_time_us=") and line[12:].strip().isdigit():
                progress(min(int(line[12:]) / (dur * 1e6), 1.0))
        rc = p.wait()
        t.join(5)
        err = "".join(errs)
        if not rc:
            break
    if rc:
        raise RuntimeError(f"nostalgia render failed: {err[-500:]}")
    png.unlink(missing_ok=True)
    return out


if __name__ == "__main__":  # python -m clipper.nostalgia <video> <start> "<caption>" "<game>" [music]
    a = sys.argv[1:]
    print(render(a[0], Path(a[0]).with_name("nostalgia_test.mp4"), float(a[1]), float(a[1]) + LENGTH, a[2], a[3],
                 a[4] if len(a) > 4 else None))


def redo(cid):
    """A waiting nostalgia clip made with the old captions: look at its own frames (the gameplay square), write a new
    caption + game name from what's on screen, and draw them over the clip's top and bottom bands (video and music
    stay). Remastered / unclean footage is taken off the schedule. -> "redone" | "unscheduled" | "kept" """
    import base64
    from . import db as _db, engage, render as rd
    from .config import DATA
    c = _db.get("clips", cid)
    src = DATA / c["path"]
    dur = c["end"] - c["start"]
    imgs = []
    for k, t in enumerate((2.5, dur - 2.5)):
        raw = subprocess.run([config.FFMPEG, "-loglevel", "error", "-nostdin", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                              "-vf", f"crop={SQ}:{SQ}:0:{TOP},scale=512:-2", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "-"],
                             capture_output=True, timeout=30, stdin=subprocess.DEVNULL).stdout
        if raw:
            imgs.append((f"candidate 0 ({'start' if k == 0 else 'end'}):", base64.b64encode(raw).decode()))
    job = _db.get("jobs", c["job_id"]) or {}
    cand = [{"id": 0, "start": 0.0, "end": dur}]
    seen = describe(cand, imgs).get(0)
    if seen and (not seen["clean"] or any(w in seen["graphics"] for w in ("remaster", "mod"))):
        _db.update("clips", cid, status="not uploaded (skipped)")
        _db.log(c["job_id"], f"#{cid} taken off the schedule: {'remastered graphics' if seen['clean'] else 'facecam / text on screen'} ({seen['scene']})")
        return "unscheduled"
    picks = write(job.get("title") or "", cand, 1, "", "", imgs, [t["name"] for t in engage.tracks()])
    if not picks:
        return "kept"
    p = picks[0]
    png = src.with_name(f"{cid}_redo.png")
    frame_png(p["caption"], p["game"], png)
    out = src.with_name(f"{cid}_n{int(time.time())}.mp4")
    # the new bands over the old ones: top band (0..TOP) and bottom band (TOP+SQ..H) of the new frame layer
    graph = (f"[1:v]format=rgba,crop={W}:{TOP}:0:0[t];[1:v]format=rgba,crop={W}:{H - TOP - SQ}:0:{TOP + SQ}[b];"
             f"[0:v][t]overlay=0:0[v1];[v1][b]overlay=0:{TOP + SQ},format=yuv420p[o]")
    rd.run_safe([config.FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-i", str(src), "-i", str(png), "-filter_complex", graph,
                    "-map", "[o]", "-map", "0:a?", *rd.encoder(), "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
                    str(out)], capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
    png.unlink(missing_ok=True)
    feats = json.loads(c.get("features") or "{}")
    feats.update(game=p["game"], scene=p.get("scene", ""))
    music = feats.get("music", "")
    credit = next((t["credit"] for t in engage.tracks() if t["name"] == music and t["credit"]), "")
    _db.update("clips", cid, path=str(out.relative_to(DATA)), headline=p["caption"], title=p["title"],
               description=p["description"] + (f"\n\n{credit}" if credit else ""), tags=json.dumps(p["tags"]),
               features=json.dumps(feats), renders="{}")
    src.unlink(missing_ok=True)
    _db.log(c["job_id"], f"#{cid} new caption from what's on screen ({p.get('scene', '')}): \"{p['caption']}\" ({p['game']})")
    return "redone"
