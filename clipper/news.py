"""News & facts posts: topic -> latest news from news portals (or facts from Wikipedia) -> DeepSeek writes a short,
sourced post -> rendered as a 7-15s vertical video (picture + headline + key lines + "Source: ...") -> posted now,
at a set time, or on the autopilot's posting slots.

Why a video and not an image: YouTube only takes videos, and Instagram's API only takes images from a public URL
(this app has none), while video goes through the same upload path as the clips on every platform.

Where news comes from (no API keys):
  - Google News RSS search: headlines from thousands of portals, with the portal's name and the time.
  - Bing News RSS search: the same, plus the article's real URL, which is then read (one plain GET, like a browser):
    its picture (og:image), its description and the first paragraphs. DeepSeek only uses facts from that text.
  - Facts: Wikipedia's search + article text; DeepSeek picks one surprising fact from it, nothing made up.

Each post is a job (kind 'news'/'fact', url = the article) with one clip, so posts show on the Clips page, count in
the insights and use the same upload code. The `news` table remembers every story used, so none is posted twice."""
import html
import json
import re
import threading
import time
import urllib.parse
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree import ElementTree

import httpx

from . import db, pick, render
from .config import DATA, SEARCH_REGION, WIKI_CONTACT

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
MAX_AGE_H = 72  # news older than this is skipped
# Not news portals: posts from these are someone's post, not a sourced article, so they're left out.
SOCIAL = {"x", "twitter", "reddit", "youtube", "facebook", "instagram", "tiktok", "threads", "x.com", "twitter.com",
          "reddit.com", "youtube.com", "facebook.com", "instagram.com", "tiktok.com", "msn"}
LOCK = threading.Lock()  # one batch renders at a time (GPU encoder)


# ---------- runs: one per Find / autopilot news part, with its own live log ----------

def new_run(kind, topic, source="manual"):
    return db.insert("news_runs", at=time.time(), kind=kind, topic=topic, source=source, status="searching")


def run_logger(rid):
    """-> log(msg) that appends to the run's log (shown live on the News page)."""
    return (lambda m: db.log(rid, m, "news_runs")) if rid else (lambda m: None)


def note_upload(cid, uploads):
    """After a news/fact post was uploaded: its result per platform into the run's log."""
    row = next(iter(db.all("news", "clip_id=?", (cid,))), None)
    if row and row.get("run_id"):
        res = "; ".join(f"✓ {p} {u.get('url', '')}" if u.get("id") else f"✕ {p}: {str(u.get('error'))[:160]}"
                        for p, u in uploads.items())
        db.log(row["run_id"], f"post #{cid} \"{row['headline']}\" posted: {res}", "news_runs")


# ---------- finding news ----------

def _age_h(pub):
    try:
        return round((time.time() - parsedate_to_datetime(pub).timestamp()) / 3600, 1)
    except Exception:
        return None


def _rss(url):
    r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return ElementTree.fromstring(r.content).iter("item")


def google_news(q, n=25):
    out = []
    url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(q + ' when:3d')}"
           f"&hl=en-{SEARCH_REGION}&gl={SEARCH_REGION}&ceid={SEARCH_REGION}:en")
    for it in list(_rss(url))[:n]:
        title, src = it.findtext("title") or "", it.find("source")
        source = src.text if src is not None else ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        out.append({"title": html.unescape(title).strip(), "source": source or "Google News", "link": it.findtext("link"),
                    "age_h": _age_h(it.findtext("pubDate")), "snippet": "", "via": "google"})
    return out


def bing_news(q, n=25):
    out = []
    url = f"https://www.bing.com/news/search?q={urllib.parse.quote(q)}&format=rss&setlang=en-{SEARCH_REGION}&cc={SEARCH_REGION}"
    for it in list(_rss(url))[:n]:
        link = it.findtext("link") or ""
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]  # bing wraps the URL
        source = next((el.text for el in it if el.tag.endswith("Source")), "") or urllib.parse.urlparse(real).netloc
        image = next((el.text for el in it if el.tag.endswith("Image")), "")
        out.append({"title": html.unescape(it.findtext("title") or "").strip(), "source": source, "link": real,
                    "age_h": _age_h(it.findtext("pubDate")), "via": "bing", "image": image,
                    "snippet": re.sub(r"<[^>]+>", "", html.unescape(it.findtext("description") or ""))[:400]})
    return out


def _key(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:60]


def find_news(queries, log=None):
    """Search every query on Google News + Bing News -> fresh, de-duplicated stories not posted before, newest first."""
    say = log or (lambda m: None)
    seen, out, raw = set(), [], 0
    skip = {"duplicate": 0, "posted before": 0, f"older than {MAX_AGE_H} h": 0, "social-media post": 0}
    used = {r["link"] for r in _used()} | {_key(r["title"]) for r in _used()}
    for q in queries:
        for name, fn in (("Google News", google_news), ("Bing News", bing_news)):
            try:
                found = fn(q)
            except Exception as e:
                say(f"🔎 '{q}' on {name}: FAILED ({e!r})"[:300])
                continue
            raw += len(found)
            new = 0
            for s in found:
                k = _key(s["title"])
                why = ("duplicate" if not s["title"] or k in seen else "posted before" if k in used or s["link"] in used
                       else f"older than {MAX_AGE_H} h" if s["age_h"] is not None and s["age_h"] > MAX_AGE_H
                       else "social-media post" if (s["source"] or "").strip().lower() in SOCIAL else None)
                if why:
                    skip[why] += 1
                    continue
                seen.add(k)
                out.append({**s, "query": q})
                new += 1
            newest = min((s["age_h"] for s in found if s["age_h"] is not None), default=None)
            say(f"🔎 '{q}' on {name}: {len(found)} stories, {new} new" + (f", newest {newest} h old" if newest is not None else ""))
    out.sort(key=lambda s: s["age_h"] if s["age_h"] is not None else 999)
    say(f"📥 {raw} stories in total → {len(out)} kept"
        + (" (skipped: " + ", ".join(f"{n} {w}" for w, n in skip.items() if n) + ")" if any(skip.values()) else ""))
    for s in out[:12]:
        say(f"   · {s['title'][:110]} | {s['source']}, {_ago(s['age_h']) if s['age_h'] is not None else 'date unknown'}")
    if len(out) > 12:
        say(f"   · … and {len(out) - 12} more")
    return out


class _Page(HTMLParser):
    """og:image / og:description / <p> text of an article page."""

    def __init__(self):
        super().__init__()
        self.meta, self.paras, self._p, self._buf = {}, [], 0, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and (a.get("property") or a.get("name")) in ("og:image", "og:description", "og:title",
                                                                       "twitter:image", "description"):
            self.meta.setdefault(a.get("property") or a.get("name"), a.get("content") or "")
        if tag == "p":
            self._p, self._buf = 1, []

    def handle_endtag(self, tag):
        if tag == "p" and self._p:
            t = " ".join("".join(self._buf).split())
            if len(t) > 60:
                self.paras.append(t)
            self._p = 0

    def handle_data(self, data):
        if self._p:
            self._buf.append(data)


def real_link(story):
    """Google News links are redirects nobody can read: find the same headline on Bing, which gives the real URL."""
    if "news.google.com" not in (story.get("link") or ""):
        return story
    try:
        words = set(_key(story["title"]).split())
        for b in bing_news(story["title"], n=5):
            if len(words & set(_key(b["title"]).split())) >= max(3, len(words) * 0.6):
                return {**story, "link": b["link"], "image": story.get("image") or b.get("image"),
                        "snippet": story.get("snippet") or b["snippet"]}
    except Exception:
        pass
    return story


def read_article(url):
    """-> {text, image} from the article page ({} if it can't be read: paywall, blocked, Google redirect...)."""
    if not url or "news.google.com" in url:
        return {}
    try:
        r = httpx.get(url, headers=UA, timeout=12, follow_redirects=True)
        r.raise_for_status()
        p = _Page()
        p.feed(r.text[:600_000])
    except Exception:
        return {}
    text = " ".join([p.meta.get("og:description") or p.meta.get("description") or ""] + p.paras)[:2500]
    return {"text": html.unescape(text), "image": p.meta.get("og:image") or p.meta.get("twitter:image") or ""}


def wiki_facts(topic):
    """-> {title, link, text} of the best Wikipedia article for the topic (None if nothing)."""
    api = "https://en.wikipedia.org/w/api.php"
    ua = {"User-Agent": f"ClipperNews/1.0 ({WIKI_CONTACT})"}
    r = httpx.get(api, headers=ua, timeout=15, params={"action": "query", "list": "search", "srsearch": topic,
                                                         "format": "json", "srlimit": 1}).json()
    hits = r.get("query", {}).get("search") or []
    if not hits:
        return None
    title = hits[0]["title"]
    r = httpx.get(api, headers=ua, timeout=15, params={"action": "query", "prop": "extracts|pageimages", "explaintext": 1,
                                                         "titles": title, "format": "json", "pithumbsize": 1080}).json()
    page = next(iter(r["query"]["pages"].values()))
    return {"title": title, "link": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            "text": (page.get("extract") or "")[:9000], "image": (page.get("thumbnail") or {}).get("source", "")}


# ---------- DeepSeek ----------

PICK_SYSTEM = """You run the news feed of a viral social channel. From the candidate stories pick the ones worth a
short post: newest first, big or surprising for fans of the topic, clearly about it. One story per event (skip
duplicates of the same news from other sites), skip ones already posted (list given), skip pure opinion pieces,
listicles, deals/ads. Prefer real news/gaming sites, and stories with full_article=true (their text can be read, so
the post has real facts). Reply as JSON: {"picks": [{"id": 0, "why": "one line"}]}"""

WRITE_SYSTEM = """You write a short vertical news post for a viral social channel. Use ONLY facts from the given
headline and article text; never invent numbers, dates, quotes or details. If it's a rumor/leak/report, say so.
Reply as JSON: {"post": {
 "badge": one of "BREAKING", "NEWS", "UPDATE", "LEAK", "RUMOR", "REPORT", "DID YOU KNOW?",
 "headline": "max 9 words, punchy, the core news",
 "lines": ["2-3 short lines, max 12 words each, the key facts in order"],
 "title": "post title for YouTube/Facebook, max 70 chars, no clickbait lies",
 "caption": "1-2 sentences for the post text (no hashtags, no source: it's added)",
 "hashtags": ["4-5 relevant tags without #"],
 "narration": "what a voice-over would say, max 40 words, same facts"}}"""

FACT_SYSTEM = WRITE_SYSTEM.replace("short vertical news post", "short vertical 'did you know' fact post").replace(
    "If it's a rumor/leak/report, say so.", "Pick ONE surprising, little-known fact from the article text (not one "
    "already posted: list given), use badge DID YOU KNOW?.")


def pick_stories(topic, stories, n, posted):
    rows = [{"id": i, "title": s["title"], "source": s["source"], "hours_ago": s["age_h"], "snippet": s["snippet"][:160],
             "full_article": s.get("via") == "bing"}
            for i, s in enumerate(stories[:40])]
    picks = pick._ask(f"Topic: {topic}\nPick at most {n}.\nAlready posted: {json.dumps(posted[-30:])}\n"
                      f"Candidates: {json.dumps(rows, indent=1)}", PICK_SYSTEM, "picks")
    out = []
    for p in picks if isinstance(picks, list) else []:
        i = p.get("id") if isinstance(p, dict) else None
        if isinstance(i, int) and 0 <= i < len(rows) and stories[i] not in out:
            out.append({**stories[i], "why": str(p.get("why", ""))[:200]})
    return out[:n] or stories[:n]


def write_post(story, kind="news", posted=()):
    text = story.get("text") or story.get("snippet") or ""
    task = (f"Source: {story['source']}\nHeadline: {story['title']}\nPublished: {story.get('age_h')} hours ago\n"
            f"Article text: {text or '(not available: use only the headline)'}"
            + (f"\nFacts already posted: {json.dumps(list(posted)[-20:])}" if kind == "fact" else ""))
    p = pick._ask(task, FACT_SYSTEM if kind == "fact" else WRITE_SYSTEM, "post")
    if not isinstance(p, dict) or not p.get("headline"):
        raise RuntimeError(f"AI returned no post: {p!r}"[:200])
    lines = [str(x)[:90] for x in (p.get("lines") or []) if str(x).strip()][:3]
    return {"badge": str(p.get("badge") or "NEWS")[:16].upper(), "headline": str(p["headline"])[:80], "lines": lines,
            "title": str(p.get("title") or p["headline"])[:90], "caption": str(p.get("caption") or "")[:600],
            "hashtags": [re.sub(r"[^\w]", "", str(t)) for t in p.get("hashtags") or []][:5],
            "narration": str(p.get("narration") or "")[:400]}


# ---------- making posts ----------

def _used():
    return db.all("news")


def _download(url, out):
    try:
        r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
        if r.is_success and r.headers.get("content-type", "").startswith("image") and len(r.content) > 5000:
            out.write_bytes(r.content)
            return out
    except Exception:
        pass
    return None


def make_post(story, kind="news", platforms=(), voiceover=False, picture=True, log=None, status="rendered",
              post_at=None, auto=False, run_id=None, fmt="video", follow=True):
    """One story -> job + rendered clip. Returns the clip id. status: 'rendered' (wait), 'scheduled' (autopilot
    slots), 'timed' (post at post_at). Every step goes to the job's log and, via `log`, to the run's log."""
    say = log or (lambda m: None)
    jid = db.insert("jobs", url=story["link"], title=story["title"], kind=kind, shorts=1, longs=0, mode="news",
                    auto_upload=int(bool(platforms)), platforms=json.dumps(list(platforms)), voiceover=int(voiceover),
                    status="writing", created=time.time())
    d = DATA / "jobs" / str(jid) / "clips"
    d.mkdir(parents=True, exist_ok=True)
    try:
        db.log(jid, f"{kind} post from {story['source']}: {story['title']} ({story['link']})")
        say(f"✍️ job #{jid}: {kind} post from {story['source']}: {story['title'][:100]}")
        if kind == "news" and not story.get("text"):
            if "news.google.com" in story["link"]:
                resolved = real_link(story)
                say(f"   Google News link → {'real article: ' + resolved['link'][:110] if resolved['link'] != story['link'] else 'no real URL found (headline only)'}")
                story = resolved
            story = {**story, **{k: v for k, v in read_article(story["link"]).items() if v}}
            db.log(jid, f"article read: {len(story.get('text') or '')} chars of text, picture: {'yes' if story.get('image') else 'no'}")
            say(f"   article read: {len(story.get('text') or '')} chars of text, picture: {'yes' if story.get('image') else 'no'}"
                + ("" if story.get("text") else " → written from the headline only"))
        posted = [r["headline"] for r in _used() if r.get("kind") == "fact"]
        with db.step(jid, "AI writing the post"):
            p = write_post(story, kind, posted)
        db.log(jid, f"post: [{p['badge']}] {p['headline']} | " + " | ".join(p["lines"]))
        say(f"   AI wrote: [{p['badge']}] {p['headline']} | " + " | ".join(p["lines"]))
        img = _download(story["image"], d.parent / "picture.jpg") if picture and story.get("image") else None
        voice = None
        if voiceover and p["narration"] and render.can_speak():
            voice = d.parent / "voice.wav"
            render.tts(p["narration"], voice)
        cid = db.insert("clips", job_id=jid, kind=kind, start=0, end=0, score=0, title=p["title"],
                        headline=p["headline"], reason=story.get("why") or f"{kind} from {story['source']}",
                        description="", tags=json.dumps(p["hashtags"]), status="rendering")
        out = d / f"{cid}.{'jpg' if fmt == 'photo' else 'mp4'}"
        db.update("jobs", jid, status="rendering")
        with db.step(jid, f"rendering post #{cid} as a {fmt}"):
            if fmt == "photo":
                secs = render.news_image(out, p, story["source"], story.get("age_h"), img, follow=follow)
            else:
                secs = render.news_video(out, p, story["source"], story.get("age_h"), img, voice, follow=follow)
        when = "" if story.get("age_h") is None else f" ({_ago(story['age_h'])})"
        desc = (f"{p['caption']}\n\nSource: {story['source']}{when}\n{story['link']}").strip()
        db.update("clips", cid, path=str(out.relative_to(DATA)), end=secs, description=desc, status=status,
                  post_at=post_at)
        db.insert("news", link=story["link"], title=story["title"], source=story["source"], kind=kind,
                  headline=p["headline"], clip_id=cid, auto=int(auto), at=time.time(), run_id=run_id)
        db.update("jobs", jid, status="done")
        db.log(jid, f"post #{cid} ready ({secs:.0f}s), " + {"rendered": "waiting for you to post it",
                                                          "scheduled": "the autopilot posts it on its next slot",
                                                          "timed": f"posts at {time.strftime('%d %b %H:%M', time.localtime(post_at or 0))}"}[status])
        say(f"   🎬 rendered post #{cid} as a {'photo (1080x1350)' if fmt == 'photo' else f'{secs:.0f}s video'} ({'with' if img else 'no'} picture{', voice-over' if voice else ''}) → " + {
            "rendered": "waiting for you to post it", "scheduled": "the autopilot posts it on its next slot",
            "timed": f"posts at {time.strftime('%d %b %H:%M', time.localtime(post_at or 0))} to {', '.join(platforms)}"}[status])
        return cid
    except Exception as e:
        db.log(jid, f"ERROR: {e!r}", exc=True)
        db.update("jobs", jid, status="failed")
        say(f"   ❌ {kind} post from {story['source']} failed: {e!r}"[:300])
        return None


def _ago(h):
    return f"{h * 60:.0f} min ago" if h < 1 else f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} days ago"


def queries_for(topic):
    return pick.search_queries(f"latest news about {topic}")[:4] if topic else []


def facts(topic, n, log=None):
    """n 'did you know' stories about the topic (each becomes one fact post): from Wikipedia when WIKI_CONTACT is set,
    else from the longest news articles about it that can be read (the fact is credited to that article)."""
    say = log or (lambda m: None)
    w = None
    if WIKI_CONTACT:
        try:
            w = wiki_facts(topic)
        except Exception as e:
            say(f"Wikipedia failed ({e!r}), using news articles")
    if not w or len(w["text"]) < 500:
        arts = []
        for s in sorted(find_news([topic], say), key=lambda x: x["via"] != "bing")[:12]:  # bing = real article URLs
            a = read_article(s["link"])
            if len(a.get("text", "")) > 800:
                arts.append({**s, **a})
            if len(arts) >= n:
                break
        say(f"facts from {len(arts)} news article(s) about '{topic}'" if arts else f"no readable articles about '{topic}'")
        return [{**a, "age_h": None} for a in arts][:n]
    if log:
        log(f"facts from Wikipedia: {w['title']} ({len(w['text'])} chars)")
    return [{"title": w["title"], "source": "Wikipedia", "link": w["link"], "age_h": None, "text": w["text"],
             "image": w["image"], "snippet": ""} for _ in range(n)]


def timed_tick():
    """Post every clip whose time has come (status 'timed', post_at <= now). Runs every minute (autopilot loop),
    whether the autopilot is on or off: these were scheduled by hand."""
    from . import worker
    for c in db.all("clips", f"status='timed' AND post_at <= {time.time():.0f}"):
        job = db.get("jobs", c["job_id"]) or {}
        plats = json.loads(c.get("post_to") or "null") or json.loads(job.get("platforms") or "[]")  # the clip's own choice first
        if not plats:
            db.update("clips", c["id"], status="rendered")
            continue
        db.log(c["job_id"], f"scheduled time reached: posting #{c['id']} to {', '.join(plats)}")
        worker.upload_clip(c["id"], plats, force=True)
