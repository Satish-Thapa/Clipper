"""Getting follows and learning what earns engagement.

  music      your own track library (data/music): Meta's API can't add Instagram's licensed music, so posts that go up
             as Reels carry a track from here, mixed into the video by us. Use tracks you have the rights to.
  slideshow  a post's images as a 9:16 Reel (cover + detail slides + follow slide) with a music track.
  cta        the follow prompt: end-card variants for clips, a follow slide + caption line for posts. {handle} and
             {topic} are filled in per category, so it says what the viewer gets ("FOLLOW @X FOR DAILY GTA VI").
  stats      likes, comments, shares, saves, follows... of every published post (posts.stats), read hourly.
  bandit     each choice (template, card vs Reel, track, follow prompt) is an arm; its reward is how the posts / clips
             made with it did. UCB1: mostly the arm that's done best, sometimes one with few tries, so it keeps learning
             (and an untried arm is tried first). Rewards are relative to the category's own posts.
Nothing here asks for follow-for-follow, giveaways or "comment YES": Meta demotes engagement bait."""
import json
import math
import random
import re
import subprocess
import time
from pathlib import Path

import httpx

from . import config, db
from .config import DATA

MUSIC = DATA / "music"
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".ogg")
MATURE = 24 * 3600  # a post's numbers count once it's been up this long (most engagement comes in the first day)
EXPLORE = 0.8  # UCB exploration strength: higher = tries less-proven options more often


# ---- music library ----

def tracks():
    """[{name, file, seconds, credit}] of the tracks in data/music. The file name is the description the AI reads
    (e.g. "upbeat electronic gaming.mp3"), so name them by mood and genre. credit: the attribution a Creative Commons
    track needs in the caption (from its .json next to it), "" for your own."""
    from . import render
    MUSIC.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(MUSIC.iterdir()):
        if f.suffix.lower() in AUDIO_EXT:
            meta = f.with_suffix(".json")
            credit = json.loads(meta.read_text()).get("credit", "") if meta.exists() else ""
            out.append({"name": f.stem, "file": f.name, "seconds": round(render.duration(f), 1), "credit": credit})
    return out


OPENVERSE = "https://api.openverse.org/v1/audio/"
FREE_LICENSES = "by,cc0"  # commercial use + changes allowed, no share-alike: CC BY needs a credit in the caption


def fetch_music(moods, per=2):
    """Fill the library from Openverse (open Creative Commons music: Jamendo, Freesound, Wikimedia...): `per` tracks
    of 30 s - 4 min for each mood, CC BY / CC0 only. Each gets a .json with its credit line. -> how many were added."""
    import re
    MUSIC.mkdir(parents=True, exist_ok=True)
    have = {json.loads(m.read_text()).get("id") for m in MUSIC.glob("*.json")}
    added = 0
    for mood in [m.strip() for m in moods if m.strip()][:12]:
        try:
            r = httpx.get(OPENVERSE, timeout=30, params={"q": mood, "license": FREE_LICENSES, "category": "music",
                                                         "page_size": 20})
            r.raise_for_status()
        except httpx.HTTPError as e:
            db.syslog(f"music: Openverse search '{mood}' failed: {e!r}")
            continue
        got = 0
        for x in r.json().get("results", []):
            if got >= per:
                break
            if x.get("id") in have or not (30000 <= (x.get("duration") or 0) <= 240000) or not x.get("url"):
                continue
            title = re.sub(r"[^\w\- ]+", "", x.get("title") or "track").strip()[:50] or "track"
            creator = (x.get("creator") or "unknown").strip()
            name = re.sub(r"\s+", " ", f"{mood} - {title} - {re.sub(r'[^\w\- ]+', '', creator)[:30]}").strip()
            try:
                a = httpx.get(x["url"], timeout=60, follow_redirects=True)
                if not a.is_success or len(a.content) < 50_000:
                    continue
            except httpx.HTTPError:
                continue
            ext = ".mp3" if "mp3" in (x.get("filetype") or "mp3") or "mpeg" in a.headers.get("content-type", "") else ".ogg"
            (MUSIC / f"{name}{ext}").write_bytes(a.content)
            lic = (x.get("license") or "").upper()
            credit = "" if lic == "CC0" else f"🎵 {x.get('title')} by {creator} (CC {lic} {x.get('license_version') or ''})".strip()
            (MUSIC / f"{name}.json").write_text(json.dumps({"id": x["id"], "title": x.get("title"), "creator": creator,
                                                            "license": x.get("license"), "page": x.get("foreign_landing_url"),
                                                            "credit": credit.replace(" )", ")")}))
            have.add(x["id"])
            got += 1
            added += 1
    db.syslog(f"music: {added} free tracks added from Openverse ({', '.join(moods)})")
    return added


def save_track(name, data):
    safe = re.sub(r"[^\w\- ]+", "", Path(name).stem).strip()[:80] or f"track {int(time.time())}"
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXT:
        raise ValueError("choose an MP3, M4A, AAC, WAV or OGG file")
    if len(data) > 25_000_000:
        raise ValueError("tracks up to 25 MB")
    MUSIC.mkdir(parents=True, exist_ok=True)
    (MUSIC / f"{safe}{ext}").write_bytes(data)
    return tracks()


def delete_track(file):
    f = MUSIC / Path(file).name
    if f.parent == MUSIC:
        f.unlink(missing_ok=True)
        f.with_suffix(".json").unlink(missing_ok=True)
    return tracks()


# ---- the follow prompt ----

def fill(text, cat=None, account_name=""):
    """{handle} / {topic} in a follow prompt -> this category's handle (or the watermark / account name) and topic."""
    cat = cat or {}
    if not account_name and cat.get("brand_id"):
        from . import brands
        account_name = (brands.get(cat["brand_id"]) or {}).get("name", "")
    handle = (cat.get("handle") or config.WATERMARK or account_name or "").strip()
    if handle and not handle.startswith("@") and " " not in handle:
        handle = "@" + handle
    topic = (cat.get("niche") or "").strip()
    if not topic or "," in topic or len(topic) > 18:  # a list of topics / a long one reads badly: the category's name
        topic = (cat.get("name") or "").strip()
    if topic.lower() in ("", "uncategorized"):
        topic = ""
    if not topic:  # no topic: drop the end-card lines that need it, or say "updates" in a one-liner
        parts = text.split("|")
        text = "|".join(x for x in parts if "{topic}" not in x.lower()) if len(parts) > 1 else text
        topic = "updates"
    out = text.replace("{handle}", handle).replace("{HANDLE}", handle.upper()).replace("{topic}", topic) \
              .replace("{TOPIC}", topic.upper())
    return re.sub(r"\s+([?!.,])", r"\1", re.sub(r"[ \t]+", " ", out)).strip()


def cta_variants():
    """Settings → Clips → End card: one variant per line ("|" splits a variant into its lines)."""
    return [v.strip() for v in (config.END_CARD or "").splitlines() if v.strip()]


# ---- engagement numbers ----

def _get(url, params):
    r = httpx.get(url, params=params, timeout=20)
    return r.json() if r.is_success else {}


def _ig_stats(media, token, fb_token=None):
    """Instagram: like / comment counts (instagram_basic) + insights where the token allows (reach, shares, saves,
    follows, profile visits, views; which exist depends on the media type, so they're asked one by one)."""
    from . import social
    base = social.igb(token)
    out = {}
    d = _get(f"{base}/{media}", {"fields": "like_count,comments_count,media_type,media_product_type", "access_token": token})
    out["likes"], out["comments"] = int(d.get("like_count") or 0), int(d.get("comments_count") or 0)
    for tok in [t for t in (token, fb_token) if t]:
        b = social.igb(tok)
        got = False
        for m in ("reach", "shares", "saved", "follows", "profile_visits", "views", "total_interactions"):
            x = _get(f"{b}/{media}/insights", {"metric": m, "access_token": tok}).get("data") or []
            if x:
                v = x[0].get("total_value", {}).get("value") if x[0].get("total_value") else (x[0].get("values") or [{}])[0].get("value")
                out[m] = int(v or 0)
                got = True
        if got:
            break
    return out


def _fb_stats(post_id, token, reel=False):
    """Facebook: reactions, comments, shares (pages_read_engagement); a Reel also its plays."""
    out = {}
    d = _get(f"https://graph.facebook.com/{config.GRAPH_VERSION}/{post_id}",
             {"fields": "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0),shares"
                        if not reel else "likes.summary(total_count).limit(0),comments.summary(total_count).limit(0)",
              "access_token": token})
    out["likes"] = int(((d.get("reactions") or d.get("likes") or {}).get("summary") or {}).get("total_count") or 0)
    out["comments"] = int(((d.get("comments") or {}).get("summary") or {}).get("total_count") or 0)
    out["shares"] = int((d.get("shares") or {}).get("count") or 0)
    if reel:
        x = _get(f"https://graph.facebook.com/{config.GRAPH_VERSION}/{post_id}/video_insights",
                 {"metric": "blue_reels_play_count", "access_token": token}).get("data") or []
        if x:
            out["views"] = int(((x[0].get("values") or [{}])[0]).get("value") or 0)
    return out


def score(stats):
    """One engagement number for a post across its platforms: actions weighted by how much they say "I want more of
    this" (a follow most, then saves and shares, comments, likes; views and profile visits a little)."""
    total = 0.0
    for s in (stats or {}).values():
        if isinstance(s, dict):
            total += (s.get("likes", 0) + 2 * s.get("comments", 0) + 3 * s.get("shares", 0) + 3 * s.get("saved", 0)
                      + 8 * s.get("follows", 0) + 0.5 * s.get("profile_visits", 0) + 0.02 * s.get("views", 0))
    return round(total, 2)


def refresh_posts():
    """Read the numbers of every published post younger than 30 days (older ones don't change much)."""
    from . import accounts
    toks = {a["id"]: a for a in accounts.all(secret=True)}
    n = 0
    for p in db.all("posts", "status='posted' AND created > ?", (time.time() - 30 * 86400,)):
        ups, stats = json.loads(p.get("uploads") or "{}"), json.loads(p.get("stats") or "{}")
        reel = json.loads(p.get("features") or "{}").get("format") == "reel"
        for aid, u in ups.items():
            a = toks.get(int(aid)) if str(aid).isdigit() else None
            if not a or not u.get("id") or u.get("manual"):
                continue
            try:
                if a["platform"] == "instagram":
                    fb = next((x for x in toks.values() if x["platform"] == "facebook" and x["ext_id"] == a.get("page_id")), None)
                    stats[aid] = _ig_stats(u["id"], a["token"], fb["token"] if fb else None)
                else:
                    stats[aid] = _fb_stats(u["id"], a["token"], reel)
                n += 1
            except Exception as e:
                db.syslog(f"post {p['id']} stats ({a['platform']}): {e!r}")
        stats["_score"], stats["_at"] = score({k: v for k, v in stats.items() if not k.startswith("_")}), time.time()
        db.update("posts", p["id"], stats=json.dumps(stats))
    db.syslog(f"engagement refreshed for {n} post uploads")
    return n


# ---- the bandit ----

def _mature_posts(cid=None):
    rows = db.all("posts", "status='posted'" + (" AND category_id=?" if cid else ""), (cid,) if cid else ())
    return [p for p in rows if json.loads(p.get("stats") or "{}").get("_at")
            and time.time() - (min((u.get("at") or 0) for u in json.loads(p["uploads"] or "{}").values() if isinstance(u, dict)) or 0) > MATURE]


def _rewards(cid, dim):
    """{arm: [reward 0..1]} for one choice dimension, from this category's mature posts (else all categories')."""
    rows = _mature_posts(cid)
    if len(rows) < 3:
        rows = _mature_posts()
    scores = [(json.loads(p.get("features") or "{}"), json.loads(p["stats"]).get("_score", 0)) for p in rows]
    top = max([s for _, s in scores] + [1])
    out = {}
    for f, s in scores:
        if dim in f and f[dim] not in (None, ""):
            out.setdefault(str(f[dim]), []).append(math.log1p(s) / math.log1p(top))
    return out


def choose(cid, dim, options, rewards=None):
    """UCB1 over `options` (strings) for dimension `dim`: an untried option first, else the best mean + a bonus for
    rarely tried ones. -> (option, why)."""
    options = [str(o) for o in options]
    if len(options) <= 1:
        return (options[0] if options else None), "only option"
    r = rewards if rewards is not None else _rewards(cid, dim)
    untried = [o for o in options if not r.get(o)]
    if untried:
        return random.choice(untried), "trying it (no results yet)"
    n = sum(len(r[o]) for o in options)
    best = max(options, key=lambda o: sum(r[o]) / len(r[o]) + EXPLORE * math.sqrt(math.log(n) / len(r[o])))
    return best, f"best so far ({sum(r[best]) / len(r[best]):.2f} from {len(r[best])} posts)"


def clip_cta(cid=None):
    """Which end-card variant a new clip gets: the bandit over clips' views (index into cta_variants)."""
    variants = cta_variants()
    if len(variants) <= 1:
        return 0
    from . import learn
    rows = [(json.loads(c.get("features") or "{}").get("cta"), learn._views(c)) for c in db.all("clips")
            if c.get("uploads") not in (None, "", "{}")]
    rows = [(k, v) for k, v in rows if k is not None]
    top = max([v for _, v in rows] + [1])
    r = {}
    for k, v in rows:
        r.setdefault(str(k), []).append(math.log1p(v) / math.log1p(top))
    pick, _ = choose(cid, "cta", list(range(len(variants))), r)
    return int(pick)


def examples(cid, k=3):
    """Best and worst posts of this category, for the AI writing the next one ("" until there are enough)."""
    rows = sorted(_mature_posts(cid), key=lambda p: -json.loads(p["stats"]).get("_score", 0))
    if len(rows) < 4:
        return ""
    line = lambda p: f'- [{p["kind"]}] "{p["card"]}" ({json.loads(p["stats"]).get("_score", 0):g} engagement)'
    return ("Posts on this page that got the most engagement (write more like these):\n" + "\n".join(map(line, rows[:k]))
            + "\nThe least (avoid this style):\n" + "\n".join(map(line, rows[-k:])))


def status():
    """For the UI: per dimension, each option's posts and average reward; the best posting hours."""
    rows = _mature_posts()
    out = {"posts": len(rows), "dims": {}}
    for dim in ("template", "format", "music", "hour"):
        r = _rewards(None, dim)
        out["dims"][dim] = sorted(({"option": k, "posts": len(v), "avg": round(sum(v) / len(v), 2)} for k, v in r.items()),
                                  key=lambda x: -x["avg"])
    return out


# ---- the Reel slideshow ----

def slideshow(images, out, track=None, per=None):
    """Images (the post's 4:5 cards) -> a 9:16 MP4 Reel: each image over a blurred copy of itself, crossfading, with
    `track` (a file in data/music) faded in/out and levelled, or silent. per: seconds per image (default by count)."""
    from . import render
    n = len(images)
    per = per or (8.0 if n == 1 else 5.5 if n == 2 else 4.2 if n == 3 else 3.6)  # short posts: each card stays longer
    fade = 0.45
    total = per * n - fade * (n - 1)
    ins, chains = [], []
    for k, img in enumerate(images):
        ins += ["-loop", "1", "-t", f"{per:.2f}", "-i", str(img)]
        chains.append(f"[{k}:v]split[a{k}][b{k}];[a{k}]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                      f"gblur=sigma=40,eq=brightness=-0.25[bg{k}];[b{k}]scale=1080:-2,zoompan=z='min(zoom+0.0006,1.06)':"
                      f"d={int(per * 30)}:s=1080x1350:fps=30[fg{k}];[bg{k}][fg{k}]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p[v{k}]")
    last = "v0"
    for k in range(1, n):
        chains.append(f"[{last}][v{k}]xfade=transition=fade:duration={fade}:offset={per * k - fade * k:.2f}[x{k}]")
        last = f"x{k}"
    audio = []
    if track and (MUSIC / track).exists():
        song = render.duration(MUSIC / track)
        start = max(0.0, min(song * 0.25, song - total - 1))  # past a quiet intro, still long enough to cover the Reel
        ins += ["-ss", f"{start:.2f}", "-i", str(MUSIC / track)]
        chains.append(f"[{n}:a]atrim=0:{total:.2f},asetpts=PTS-STARTPTS,afade=t=in:d=0.6,"
                      f"afade=t=out:st={max(total - 1.2, 0):.2f}:d=1.2,loudnorm=I=-16:TP=-1.5[au]")
        audio = ["-map", "[au]", "-c:a", "aac", "-b:a", "192k"]
    out.parent.mkdir(parents=True, exist_ok=True)
    render.run_safe([config.FFMPEG, "-y", "-loglevel", "error", *ins, "-filter_complex", ";".join(chains),
                    "-map", f"[{last}]", *audio, *render.encoder()[:2], "-b:v", "8M", "-pix_fmt", "yuv420p",
                    "-r", "30", "-t", f"{total:.2f}", "-movflags", "+faststart", str(out)], capture_output=True)
    return out


if __name__ == "__main__":
    assert fill("FOLLOW {HANDLE} FOR DAILY {TOPIC}", {"handle": "laggy.world", "niche": "GTA VI"}) == "FOLLOW @LAGGY.WORLD FOR DAILY GTA VI"
    assert score({"1": {"likes": 10, "comments": 2, "follows": 1}}) == 22
    r = {"a": [0.9, 0.8], "b": [0.1]}
    assert choose(None, "x", ["a", "b", "c"], r)[0] == "c"  # untried first
    assert choose(None, "x", ["a", "b"], {"a": [0.9] * 20, "b": [0.1] * 20})[0] == "a"
    print("engage ok")
