"""Photo / text posts for Facebook Pages and Instagram accounts, organised in categories.

  category  = a theme you name, with its own settings: topic, post types, which accounts it posts to, schedule,
              approval, the handle on its cards.
  account   = one Facebook Page or Instagram account (id + token), added on the Accounts tab (Settings). One account
              can serve several categories, and clips upload to the one chosen in Settings.

  make(cid)  ->  DeepSeek writes it (news: picked from Google News RSS)  ->  a 1080x1350 card image (ffmpeg + libass,
                 the same stack that burns clip captions)  ->  a 'draft' (or 'ready' when the category's approval is off)
  tick()     ->  every minute, per enabled category: on each posting slot the oldest 'ready' post goes up to the
                 category's accounts. Slots use the clip autopilot's logic (autopilot.schedule).

Facebook: questions go up as text posts, everything else as a photo. Instagram: always the photo (no text posts)."""
import json
import math
import random
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from . import accounts as accts, autopilot, categories as cats, config, db, engage, pick, social
from .config import DATA

PLATFORMS = ("facebook", "instagram")  # posts go to Facebook Pages and Instagram accounts
TPL_DEFAULTS = {"name": "", "label": "", "source": "ai", "instructions": "", "format": "photo", "bg": "#111111",
                "accent": "#ff2e88", "font": "sans", "picture": True}
SOURCES = ("ai", "news", "meme")  # the AI writes it / picks today's news / makes a meme (picture + joke)
FONTS = {"sans": ("Arial Black", 0), "serif": ("Georgia", 1)}  # card text font + italic
# photo = the card (a carousel when there are slides); text = a text post on Facebook (Instagram gets the card);
# reel = the slides as a short video with music; auto = card or Reel, whichever has done better (engage.choose)
FORMATS = ("photo", "text", "reel", "auto")
BUFFER = 3  # with approval on: drafts kept waiting per category
NEWS_STALE = 12 * 3600  # unposted news older than this is skipped (it's not news any more)
RETRY_AFTER = 10 * 60  # after a failed make(), that category waits this long before the loop tries again
LIMIT_PAUSE = 24 * 3600  # an account whose API says "limit reached" is skipped this long
# Meta's "this token no longer works" (OAuthException code 190: expired, password changed, app removed...).
# Rate limits are OAuthExceptions too (codes 4/17/32), so match on 190 / the wording, not on "OAuthException".
TOKEN_RE = re.compile(r'"code":\s*190\b|Error validating access token|session has (?:expired|been invalidated)|'
                      r"access token has expired", re.I)
CHECK_EVERY = 6 * 3600  # tokens are re-checked this often (and at startup), so you hear about it before a post fails
LOCK = threading.Lock()  # one tick / make / publish at a time
ERRORS = {}  # category id -> {"at", "error"} of its last failed automatic make/post

W, H = 1080, 1350

RULES = """Rules for every post:
- Plain, punchy wording. No emojis on the card text; 1-2 emojis in the caption are fine.
- "tags": 5 hashtags without the #, specific to the post (not #viral #fyp).
- Never invent facts, numbers, names or quotes. If you are not sure something is true, pick something else."""

NEWS_PICK_SYSTEM = """You run a social media page that posts trending news as image cards. You get today's headlines
(id, title, source, hours ago, a snippet), the page's topic and the post type's instructions. Pick the ONE story that
will get the most engagement from the page's audience: surprising, useful, exciting or widely talked about, and that
fits the topic. Skip tragedies and deaths used as engagement bait, unverified rumours, and paid/press-release items.
Give your best 3, best first (the first whose article can be read is used).
Reply as JSON: {"ids": [<id>, <id>, <id>]}"""

NEWS_SYSTEM = f"""You run a social media page that posts trending news as image cards. You get one story: its headline,
source, and the article text (or only a snippet when the article couldn't be read), plus the page's topic and the post
type's instructions. Use ONLY facts from the headline and that text: never invent names, numbers, dates or quotes.
- "card": the headline rewritten for the image (max 90 characters, no clickbait the story doesn't back up).
- "caption": the story in 2-4 short sentences with its key facts (who, what, when, where, the numbers), so a reader
  gets the news without opening the link; then follow the post type's instructions (e.g. a question for comments).
- "slides": when the card alone doesn't tell the story and the text has the details, 2-4 extra images for a carousel,
  each ONE fact or step in max 160 characters, in reading order (e.g. how the trick works, the release times, what's
  new). [] when the story is simple or there's only a snippet.
- "image": a short YouTube search for gameplay footage that would make a fitting background picture: the game +
  the place, character, vehicle or moment the post is about (e.g. "GTA 6 Vice City beach gameplay", "CS2 Mirage A site
  smoke"). Never a person's real face unless the post is about them.
{RULES}
Reply as JSON: {{"post": {{"card": "...", "caption": "...", "slides": ["..."], "image": "...", "tags": ["..."]}}}}"""

POST_SYSTEM = f"""You write one social media post for a page, following the post type's instructions exactly.
- "card": the main text, shown big on the image. Short: follow the instructions' limits, never over 200 characters.
- "footer": one short line under it (e.g. "— Author" for a quote), or "" if the post type doesn't call for one.
- "caption": the text posted with it.
- "image": a short YouTube search for gameplay footage that would make a fitting background picture: the game +
  the place, character, vehicle or moment the post is about (e.g. "GTA 6 Vice City beach gameplay", "CS2 Mirage A site
  smoke"). Never a person's real face unless the post is about them.
{RULES}
Reply as JSON: {{"post": {{"card": "...", "footer": "...", "caption": "...", "image": "...", "tags": ["..."]}}}}"""

MEME_SYSTEM = f"""You make one meme for a page, for its audience, following the post type's instructions.
Choose the layout that fits the joke:
- "caption": the classic one: a line of text above ONE picture (POV / Me when / When you... + the scene that lands it).
- "two_panel": two pictures, each with a short label: Expectation vs Reality, Nobody / Me, Them / Us, Before / After.
- "dialogue": a comic strip of 2-4 pictures of the game's characters talking, one speech bubble each (two in the last
  one is fine): a normal question, then the character's answer is the punchline in their voice. E.g. "What are your
  plans for today?" / "Me and Big Smoke are going to buy some beer." / "And after that?" / "Unity Station."
- "logic": ONE picture with a big header at the top ("VICE CITY LOGIC:", "BIG SMOKE:", "GTA PHYSICS:"), optionally one
  speech bubble a character says, and the punchline at the bottom ("FALL 10 STORIES... GET UP, SWIM, AND CONTINUE LIKE
  NOTHING HAPPENED", "ME: THAT'S THE PLAN").
For every picture write a short YouTube search for gameplay footage that shows exactly that scene (the game + what's
happening: "GTA 5 police chase crash", "CS2 player whiffing spray", "GTA San Andreas CJ Big Smoke cutscene"). The picture
carries the punchline: be specific; for dialogue pick cutscenes where those characters are on screen.
- "caption": the meme text (max 110 characters; a line break between setup and punchline is fine). For dialogue / logic:
  a short summary of the joke (it's how repeats are spotted; it isn't drawn).
- "panels": the pictures: [{{"label": "max 30 characters, or empty for the caption layout", "image": "search",
  "lines": [{{"who": "CJ", "text": "max 90 characters, emoji allowed"}}]}}] (1 for caption / logic, 2 for two_panel,
  2-4 for dialogue; "lines" only for dialogue).
- "header", "bubble", "bottom": logic only (header max 30 characters, bubble max 80, bottom max 90; emoji allowed).
- "post": the caption posted with it: one short line that adds to the joke, then a question inviting people to share.
Funny for people who play the game; nothing offensive, no real people mocked.
{RULES}
Reply as JSON: {{"meme": {{"layout": "caption", "caption": "...", "panels": [{{"label": "", "image": "..."}}], "post": "...",
"tags": ["..."]}}}}"""

ANY_NICHE = "general interest"  # a category with no topic

# Starting templates, written to the database once (seed_templates). After that they're yours: edit or delete them.
MEME_TEMPLATE = dict(name="Meme", label="", source="meme", bg="#111111", accent="#facc15", font="sans", format="photo",
                     instructions="A relatable meme for the category's audience, in a classic text format: \"POV: ...\", "
                     "\"Me when ...\", \"Nobody: / Me: ...\", \"Expectation vs reality\", or a one-line hot take. Put the "
                     "setup and punchline on separate lines (a line break between them). Max 120 characters on the card, "
                     "specific to things this audience actually experiences (the game's mechanics, moments, community "
                     "running jokes). Footer: empty. Caption: one short line that adds to the joke, then a question that "
                     "invites people to share their own version. Nothing offensive, no real people mocked.")
STARTER_TEMPLATES = [MEME_TEMPLATE, 
    dict(name="Trending news", label="TRENDING NOW", source="news", bg="#0f172a", accent="#ef4444", font="sans",
         instructions="Caption: 1-3 short sentences on what the headline says, then one question that invites comments."),
    dict(name="Quote", label="QUOTE", bg="#111111", accent="#facc15", font="serif",
         instructions="A real, well-known quote that fits the topic, whose wording and author you are certain of (if in "
                      "doubt choose another), or an original line. Card: the quote, max 160 characters, no quote marks. "
                      "Footer: \"— Author\" (\"\" for an original line). Caption: 1-2 sentences applying it to everyday "
                      "life, then a short call to action (save / share / tag someone)."),
    dict(name="Did you know", label="DID YOU KNOW?", bg="#1e1b4b", accent="#22d3ee", font="sans",
         instructions="One surprising but well-established fact about the topic (encyclopedia-level, not disputed "
                      "trivia; no prices or numbers that change). Card: the fact, max 150 characters. Caption: 1-2 "
                      "sentences of context that are just as certain, then a question."),
    dict(name="Question", label="YOUR TAKE?", format="text", bg="#2a0f1f", accent="#ff2e88", font="sans",
         instructions="An engagement question about the topic, easy to answer in a word or two (this-or-that, would you "
                      "rather, hot take, rate it 1-10). Never political, mean or financial advice. Card: the question, "
                      "max 110 characters. Caption: one line nudging people to answer in the comments."),
]


# ---- templates ----

def _tpl(row):
    return {**TPL_DEFAULTS, **{k: v for k, v in row.items() if v is not None}}


def templates():
    return [_tpl(r) for r in reversed(db.all("post_templates"))]


def template(tid):
    r = db.get("post_templates", tid) if tid else None
    return _tpl(r) if r else None


def _hex(v, default):
    v = str(v or "").strip()
    return v.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else default


def save_template(tid, data):
    """Create (tid None) or update a post template; fields are checked and cleaned."""
    t = {**(template(tid) if tid else TPL_DEFAULTS), **{k: v for k, v in data.items() if k in TPL_DEFAULTS}}
    row = {"name": _clean(t["name"], 60) or "Untitled", "label": _clean(t["label"], 30).upper(),
           "source": t["source"] if t["source"] in SOURCES else "ai", "instructions": str(t["instructions"] or "").strip()[:2000],
           "picture": int(bool(t.get("picture", True))),
           "format": t["format"] if t["format"] in FORMATS else "photo", "bg": _hex(t["bg"], "#111111"),
           "accent": _hex(t["accent"], "#ff2e88"), "font": t["font"] if t["font"] in FONTS else "sans"}
    if tid:
        db.update("post_templates", tid, **row)
    else:
        tid = db.insert("post_templates", **row, created=time.time())
    return template(tid)


def delete_template(tid):
    db.delete("post_templates", tid)
    for c in categories():  # taken out of every category's rotation
        if tid in c["templates"]:
            db.update("post_categories", c["id"], templates=json.dumps([x for x in c["templates"] if x != tid]))


def seed_templates():
    """Once: the starter templates, and categories made before templates existed get the matching ones."""
    if db.get_setting("templates_seeded"):
        return
    ids = {}
    for s in STARTER_TEMPLATES:
        ids[s["name"]] = save_template(None, s)["id"]
    old = {"news": "Trending news", "quote": "Quote", "fact": "Did you know", "question": "Question"}
    for r in db.all("post_categories"):
        kinds = json.loads(r.get("kinds") or "[]")
        if kinds and json.loads(r.get("templates") or "[]") == []:
            db.update("post_categories", r["id"], templates=json.dumps([ids[old[k]] for k in kinds if k in old]))
    db.set_setting("templates_seeded", "1")


def preview(tpl, out=None):
    """A sample card for a template (the Templates page shows it). tpl: a template dict (saved or not)."""
    out = out or DATA / "previews" / f"tpl_{tpl.get('id') or 'new'}_{int(time.time())}.jpg"
    sample = {"news": "A headline from today's news shows here",
              "ai": "Your post's main text shows here, as big as it fits"}["news" if tpl.get("source") == "news" else "ai"]
    render_card({**TPL_DEFAULTS, **tpl}, sample, "Source: example.com" if tpl.get("source") == "news" else "", out,
                "@yourpage", config.LOGO)
    return out


# ---- accounts and categories: they live in accounts.py / categories.py (shared with clips) ----

def accounts(secret=False):
    return accts.all(secret)


def add_account(platform, ext_id, token, page_id="", verify=True):
    return accts.add_meta(platform, ext_id, token, page_id, verify)


def discover(token):
    """What a Meta token can post to -> [{platform, ext_id, page_id, name, added}] (no tokens)."""
    have = {(a["platform"], a["ext_id"]) for a in accounts()}
    out = []
    token = social.meta_long_lived(token.strip())
    if social.is_ig_token(token):  # an Instagram Login token is exactly one Instagram account
        me = social.ig_me(token)
        return [{"platform": "instagram", "ext_id": me["id"], "page_id": "", "name": f"@{me['username']}",
                 "added": ("instagram", me["id"]) in have}]
    for p in social.meta_pages(token):
        if p["page_id"]:
            out.append({"platform": "facebook", "ext_id": p["page_id"], "page_id": p["page_id"], "name": p["name"]})
        if p["ig_id"]:
            out.append({"platform": "instagram", "ext_id": p["ig_id"], "page_id": p["page_id"] or "",
                        "name": f"@{p['ig_username'] or p['ig_id']}" + (f" (via {p['name']})" if p["page_id"]
                                                                        else " (not linked to a Page)")})
    return [{**x, "added": (x["platform"], x["ext_id"]) in have} for x in out]


def import_accounts(token, picks):
    """Save the accounts in `picks` ([[platform, ext_id], ...]) that the token can see, each with its own Page token."""
    picks, added = {tuple(p) for p in picks}, []
    if social.is_ig_token(token.strip()):
        me = social.ig_me(token.strip())
        return [add_account("instagram", me["id"], token.strip(), "")] if ("instagram", me["id"]) in picks else []
    for p in social.meta_pages(social.meta_long_lived(token.strip())):
        if p["page_id"] and ("facebook", p["page_id"]) in picks:
            added.append(add_account("facebook", p["page_id"], p["token"]))
        if p["ig_id"] and ("instagram", p["ig_id"]) in picks:
            added.append(add_account("instagram", p["ig_id"], p["token"], p["page_id"] or ""))
    return added


def check_accounts():
    accts.check()


def remove_account(aid):
    accts.remove(aid)


categories, category, save_category, delete_category, category_logo = cats.all, cats.get, cats.save, cats.delete, cats.logo
CAT_DEFAULTS = cats.DEFAULTS


def _times(cid):
    return json.loads(db.get_setting(f"posts_times_{cid}") or "[]")  # when each scheduled post of it went up


def overview():
    """Every category with its counts (the Posts tabs), the accounts and the post templates."""
    rows = db.all("posts")
    cats = []
    for c in categories():
        mine = [p for p in rows if p["category_id"] == c["id"]]
        sch = autopilot.schedule(c, posts=_times(c["id"]))
        cats.append({**c, "drafts": len([p for p in mine if p["status"] == "draft"]),
                     "ready": len([p for p in mine if p["status"] == "ready"]),
                     "posted_today": sch["posted_today"], "next_at": sch["next_at"]})
    return {"categories": cats, "accounts": accounts(), "templates": templates(), "busy": LOCK.locked()}


def problem(c, dest=None):
    """Why an automation's posts can't go out, in plain words ("" when nothing's in the way)."""
    dest = cats.destinations(c.get("category_id") or c["id"], PLATFORMS) if dest is None else dest
    if not c["templates"]:
        return "Pick at least one template."
    if not [a for a in dest if a["platform"] in PLATFORMS]:
        return "No account to post to: tick a Facebook Page or Instagram account in its settings (Post to)."
    if not [a for a in dest if a["platform"] in PLATFORMS and a["status"] == "connected"]:
        return "Its Facebook / Instagram accounts need reconnecting."
    return ""


# ---- post automations: several per category, each with its own topic, keywords, templates and schedule ----

AUTO_DEFAULTS = {"name": "", "category_id": None, "enabled": False, "niche": "", "keywords": "", "templates": [],
                 "posts_per_day": 4, "post_from": 9, "post_to": 21, "approve": True,
                 "accounts": []}  # the Facebook Pages / Instagram accounts it posts to ([] = the category's defaults)


def _auto_clean(a):
    a = {**AUTO_DEFAULTS, **{k: v for k, v in a.items() if k in AUTO_DEFAULTS}, "id": int(a["id"])}
    a["templates"] = [int(x) for x in a["templates"] if str(x).isdigit()]
    a["accounts"] = [int(x) for x in (a.get("accounts") or []) if str(x).isdigit()]
    a["post_from"] = min(max(int(a["post_from"]), 0), 23)
    a["post_to"] = min(max(int(a["post_to"]), a["post_from"] + 1), 24)
    a["posts_per_day"] = min(max(int(a["posts_per_day"]), 1), 48)
    a["enabled"], a["approve"] = bool(a["enabled"]), bool(a["approve"])
    return a


def autos(brand_id=None):
    """Every post automation (one brand's when brand_id is given). The first start turns each category's built-in
    auto-post settings into its first automation."""
    raw = db.get_setting("post_autos")
    if raw is None:
        lst, n = [], 0
        for c in categories():
            if c["name"] == cats.UNCATEGORIZED or not (c["enabled"] or c["templates"] or c["niche"]):
                continue
            n += 1
            lst.append({**{k: c[k] for k in ("enabled", "niche", "templates", "posts_per_day", "post_from", "post_to", "approve")},
                        "id": n, "category_id": c["id"], "name": "", "keywords": ""})
            if db.get_setting(f"posts_times_{c['id']}"):  # its posting history carries over
                db.set_setting(f"posts_times_a{n}", db.get_setting(f"posts_times_{c['id']}"))
            for x in db.all("posts", "category_id=? AND status IN ('draft','ready') AND auto_id IS NULL", (c["id"],)):
                db.update("posts", x["id"], auto_id=n)  # its waiting drafts / queued posts too
        db.set_setting("post_autos", json.dumps(lst))
        raw = json.dumps(lst)
    out = [_auto_clean(a) for a in json.loads(raw)]
    if brand_id:
        ids = {c["id"] for c in cats.all(brand_id=brand_id)}
        out = [a for a in out if a["category_id"] in ids]
    return out


def auto_get(aid):
    return next((a for a in autos() if a["id"] == int(aid)), None)


def _auto_store(lst):
    db.set_setting("post_autos", json.dumps([{k: a[k] for k in list(AUTO_DEFAULTS) + ["id"]} for a in lst]))


def auto_save(aid, data):
    lst = autos()
    cur = next((a for a in lst if a["id"] == int(aid)), None)
    if not cur:
        raise ValueError("no such automation")
    new = _auto_clean({**cur, **{k: v for k, v in data.items() if k in AUTO_DEFAULTS}})
    if not category(new["category_id"]):
        raise ValueError("pick a category")
    _auto_store([new if a["id"] == new["id"] else a for a in lst])
    return new


def auto_create(data):
    lst = autos()
    aid = max([a["id"] for a in lst] + [int(db.get_setting("post_autos_last_id") or 0), 0]) + 1  # never reuse an id
    db.set_setting("post_autos_last_id", str(aid))
    a = _auto_clean({**AUTO_DEFAULTS, "templates": [x["id"] for x in templates()], **data, "id": aid})
    if not category(a["category_id"]):
        raise ValueError("pick a category")
    _auto_store(lst + [a])
    return a


def auto_delete(aid):
    gone = auto_get(aid)
    heir = next((a for a in autos() if gone and a["id"] != int(aid) and a["category_id"] == gone["category_id"]), None)
    for p in db.all("posts", "auto_id=? AND status IN ('draft','ready')", (int(aid),)):
        # its waiting posts aren't stranded: another automation of the same category takes them (as it would post them)
        if heir:
            db.update("posts", p["id"], auto_id=heir["id"], status="draft" if heir["approve"] else "ready")
            db.log(p["id"], f"its automation was deleted: now with automation {heir['id']}", "posts")
    db.set_setting("post_autos_last_id", str(max(int(db.get_setting("post_autos_last_id") or 0), int(aid))))
    db.set_setting(f"posts_times_a{int(aid)}", "[]")  # its posting history isn't inherited by a later automation
    _auto_store([a for a in autos() if a["id"] != int(aid)])


WORKING = {}  # automation id -> what it's doing right now ("Writing a post", "Posting"), for the UI


def auto_dest(a):
    """The accounts an automation posts to: the ones ticked on it, else its category's defaults (Facebook /
    Instagram only). Accounts removed since are dropped."""
    if a.get("accounts"):
        rows = [accts.get(x) for x in a["accounts"]]
        return [r for r in rows if r and r["platform"] in PLATFORMS]
    return [x for x in cats.destinations(a["category_id"], PLATFORMS) if x["platform"] in PLATFORMS]


def _atimes(aid):
    return json.loads(db.get_setting(f"posts_times_a{aid}") or "[]")  # when each of its posts went up


def automation(brand_id=None):
    """Automate -> Posts: each post automation of the brand with its plan, what's in the way, and the drafts
    waiting for approval (newest first)."""
    from . import brands
    bid = brand_id or brands.current()
    mine = [c for c in cats.all(brand_id=bid) if c["name"] != cats.UNCATEGORIZED]
    ids = {c["id"] for c in mine}
    waiting = [p for p in db.all("posts", "status IN ('draft','ready')") if p["category_id"] in ids]
    out = []
    for a in autos(bid):
        c = category(a["category_id"]) or {}
        dest = auto_dest(a)
        err = ERRORS.get(f"a{a['id']}")
        mine_posts = db.all("posts", "auto_id=?", (a["id"],))[:3]  # newest first
        label = lambda p: p["card"] or " / ".join(x.get("label", "") for x in (json.loads(p.get("features") or "{}").get("meme") or {}).get("panels", []) if x.get("label")) or p["kind"]
        recent = [{"id": p["id"], "card": label(p), "image": p["image"], "status": p["status"], "created": p["created"],
                   "uploads": [{"platform": u.get("platform"), "url": u.get("url"), "error": (u.get("error") or "")[:160]}
                               for u in json.loads(p["uploads"] or "{}").values() if isinstance(u, dict)]} for p in mine_posts]
        busy = WORKING.get(a["id"]) or ("Posting" if any(p["status"] == "posting" for p in mine_posts) else "")
        logs = []  # its recent posts' steps, oldest first
        for q in reversed(db.all("posts", "auto_id=?", (a["id"],))[:4]):
            logs += [f"{ln}  · post {q['id']}" for ln in (q["log"] or "").splitlines()[-8:]]
        out.append({**a, "category": c.get("name", ""), "color": c.get("color", "#6b8f86"),
                    "schedule": autopilot.schedule(a, posts=_atimes(a["id"])), "problem": problem(a, dest),
                    "error": err["error"] if err else "", "recent": recent, "working": busy, "log": logs[-14:],
                    "targets": [{"id": x["id"], "platform": x["platform"], "name": x["name"], "status": x["status"]} for x in dest],
                    "drafts": len([p for p in waiting if p.get("auto_id") == a["id"] and p["status"] == "draft"]),
                    "ready": len([p for p in waiting if p.get("auto_id") == a["id"] and p["status"] == "ready"])})
    drafts = [{**{k: p[k] for k in ("id", "category_id", "card", "caption", "image", "created")}, "card": p["card"] or p["caption"]}
              for p in waiting if p["status"] == "draft"][:30]
    return {"gap_min": int(config.POST_GAP_MIN or 0), "autos": out, "categories": [{"id": c["id"], "name": c["name"]} for c in mine], "drafts": drafts,
            "templates": templates(), "busy": LOCK.locked()}


def approve_all(brand_id=None, category_id=None):
    """Every draft of the brand (or of one category) -> queued for its next slot. Returns how many."""
    from . import brands
    ids = {c["id"] for c in cats.all(brand_id=brand_id or brands.current())}
    n = 0
    for p in db.all("posts", "status='draft'"):
        if p["category_id"] in ids and (category_id is None or p["category_id"] == category_id):
            db.update("posts", p["id"], status="ready")
            n += 1
    return n


def status(cid):
    c = category(cid)
    if not c:
        return None
    rows = db.all("posts", "category_id=?", (cid,))
    count = lambda st: len([p for p in rows if p["status"] == st])
    return {"category": c, "schedule": autopilot.schedule(c, posts=_times(cid)), "drafts": count("draft"),
            "ready": count("ready"), "busy": LOCK.locked(), "error": ERRORS.get(cid), "posts": rows[:60]}


# ---- news ----

FEED_TTL = 20 * 60  # each news source is asked at most this often (the same answer is reused meanwhile)
_feeds = {}  # url -> (fetched at, items) ; also failures, so a broken source isn't hammered
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"}
MAX_HOURS = 36  # older stories aren't news any more


def _sources(niche):
    """Settings → News sources -> [(name, url, is_search)]."""
    from urllib.parse import quote_plus
    region, q = config.SEARCH_REGION.upper(), niche.strip()
    out = []
    for line in (config.NEWS_FEEDS or "google").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower() == "google":
            base = f"hl=en-{region}&gl={region}&ceid={region}:en"
            out.append(("Google News", f"https://news.google.com/rss/search?q={quote_plus(q + ' when:1d')}&{base}" if q
                        else f"https://news.google.com/rss?{base}", bool(q)))
        elif s.lower() == "bing":
            out.append(("Bing News", f"https://www.bing.com/news/search?format=rss&cc={region}&q={quote_plus(q)}" if q
                        else f"https://www.bing.com/news?format=rss&cc={region}", bool(q)))
        elif s.startswith("http"):
            out.append((s.split("/")[2], s.replace("{q}", quote_plus(q)), "{q}" in s))
    return out


def _text(html):
    import html as h
    return re.sub(r"\s+", " ", h.unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def _direct(link):
    """Bing wraps article links (apiclick.aspx?...&url=<the article>): -> the article's own address."""
    from urllib.parse import parse_qs, urlparse
    if "bing.com" in link and "url=" in link:
        return parse_qs(urlparse(link).query).get("url", [link])[0]
    return link


def _parse_feed(content, default_source):
    """RSS 2.0 or Atom -> [{title, source, link, hours, snippet}]. Junk after the feed's closing tag (Bing sometimes
    adds some) is cut off instead of failing the whole source."""
    for end in (b"</rss>", b"</feed>"):
        k = content.rfind(end)
        if k != -1:
            content = content[:k + len(end)]
            break
    root, out = ET.fromstring(content), []
    entries = list(root.iter("item")) or [e for e in root.iter() if e.tag.endswith("}entry")]
    for it in entries:
        get = lambda name: next((c for c in it if c.tag == name or c.tag.endswith("}" + name)), None)
        title = _text((get("title").text if get("title") is not None else "") or "")
        link_el = get("link")
        link = (link_el.text or link_el.get("href") or "").strip() if link_el is not None else ""
        first = lambda *names: next((e for e in map(get, names) if e is not None), None)  # (an Element with no
        src_el = first("source", "Source")                                                 #  children is falsy)
        source = _text(src_el.text if src_el is not None and src_el.text else "") or default_source
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        date_el = first("pubDate", "updated", "published")
        hours = None
        try:
            from datetime import datetime
            raw = (date_el.text or "").strip()
            ts = parsedate_to_datetime(raw).timestamp() if "," in raw else datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            hours = round((time.time() - ts) / 3600, 1)
        except Exception:
            pass
        desc = first("description", "summary", "content")
        if title:
            out.append({"title": title.strip(), "source": source, "link": _direct(link), "hours": hours,
                        "snippet": _text(desc.text if desc is not None else "")[:300]})
    return out


def _fetch(name, url):
    hit = _feeds.get(url)
    if hit and time.time() < hit[0]:
        if isinstance(hit[1], Exception):
            raise hit[1]
        return hit[1]
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers=UA)
        r.raise_for_status()
        items = _parse_feed(r.content, name)
    except Exception as e:  # don't ask a failing source again for a while; one that says "too many requests": an hour
        busy = isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429
        _feeds[url] = (time.time() + (3600 if busy else FEED_TTL), e)
        raise
    _feeds[url] = (time.time() + FEED_TTL, items)  # (valid until, answer)
    return items


def _words(s):
    return {w for w in _key(s).split() if len(w) > 2}


def news(niche="", n=30, terms=()):
    """Today's stories from every source in Settings → News sources, asked in parallel (a failing one is skipped),
    duplicates merged (the copy with a readable article link wins), newest first.
    -> [{title, source, link, hours, snippet}]. terms: words a site's own feed (no search) must match to count."""
    from concurrent.futures import ThreadPoolExecutor
    srcs = _sources(niche)
    want = {w for t in ([niche] + list(terms)) for w in _words(t)}
    got, errors = [], []
    with ThreadPoolExecutor(max(len(srcs), 1)) as ex:
        futs = [(s, ex.submit(_fetch, s[0], s[1])) for s in srcs]
        for (name, url, search), f in futs:
            try:
                items = f.result()
            except Exception as e:
                errors.append(f"{name}: {str(e)[:80]}")
                continue
            if not search and want:
                items = [i for i in items if want & _words(i["title"] + " " + i["snippet"])]
            got += items
    if not got and errors:
        raise RuntimeError("no news source answered: " + "; ".join(errors))
    if errors:
        db.syslog("news: skipped " + "; ".join(errors))
    merged = []
    for i in sorted(got, key=lambda x: x["hours"] if x["hours"] is not None else MAX_HOURS):
        if i["hours"] is not None and i["hours"] > MAX_HOURS:
            continue
        dup = next((m for m in merged if _same(m["title"], i["title"])), None)
        if not dup:
            merged.append({**i, "also": []})
        else:
            dup["also"].append(i)  # same story elsewhere: its link may be readable when this one isn't
            if not dup["snippet"] and i["snippet"]:
                dup["snippet"] = i["snippet"]
    return merged[:n]


def _same(a, b):
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def article(item, limit=5000):
    """The story's text from its article page (the first readable link among its copies), "" if none can be read.
    Google News links are encrypted redirects, so a copy from another source is used for those."""
    links = [x["link"] for x in [item] + item.get("also", []) if x.get("link") and "news.google.com" not in x["link"]]
    if not links:  # only Google's encrypted link: find the same story on Bing (one search) to get a readable one
        from urllib.parse import quote_plus
        try:
            found = _fetch("Bing News", f"https://www.bing.com/news/search?format=rss&cc={config.SEARCH_REGION.upper()}"
                                        f"&q={quote_plus(item['title'][:120])}")
            links = [x["link"] for x in found if _same(x["title"], item["title"]) and "bing.com" not in x["link"]]
        except Exception:
            links = []
    for link in links[:3]:
        try:
            r = httpx.get(link, timeout=15, follow_redirects=True, headers=UA)
            if not r.is_success or "html" not in r.headers.get("content-type", ""):
                continue
            html = re.sub(r"<(script|style|noscript|svg|nav|header|footer|form)\b.*?</\1>", " ", r.text, flags=re.S | re.I)
            meta = re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']+)', r.text, re.I)
            paras = [_text(x) for x in re.findall(r"<p(?:\s[^>]*)?>(.*?)</p>", html, re.S | re.I)]
            body = " ".join(x for x in paras if len(x) >= 60)
            text = ((_text(meta.group(1)) + " ") if meta else "") + body
            if len(text) > 200:
                return text[:limit], link
        except Exception:
            continue
    return "", ""


def _key(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:200]


def _seen(cid, tid):
    return {p["key"] for p in db.all("posts", "category_id=? AND template_id=?", (cid, tid)) if p["key"]}


# ---- writing a post ----

def _clean(x, n):
    return re.sub(r"\s+", " ", str(x or "")).strip()[:n]


PLACEHOLDER = re.compile(r"^(an?|one|the)\s+(\w+[\s,]+){0,4}(line|caption|sentence|text|summary|joke|question|punchline|header)"
                         r"\b.*\b(about|over|for|that|describing|of)\b", re.I)


def _placeholder(text):
    """The AI describing what it should write ("A calm, warm line about the atmosphere...") instead of writing it."""
    return bool(PLACEHOLDER.search(str(text or "").strip()))


def _clean_card(x, n):
    """Like _clean, but a line break stays (at most 3 lines): a meme's setup / punchline."""
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in str(x or "").replace("\r", "").split("\n")]
    return "\n".join(l for l in lines if l)[:n] if len([l for l in lines if l]) <= 3 else _clean(x, n)


def _tags(x):
    return [re.sub(r"[^\w]", "", str(t)) for t in (x if isinstance(x, list) else []) if re.sub(r"[^\w]", "", str(t))][:5]


def _language():
    lang = config.CONTENT_LANGUAGE.strip() or "English"
    return f"\nWrite card, footer, caption and hashtags in {lang} (natural, the way people write it online)."


MUSIC_ASK = ('\n- "music": the exact name of the track below that best fits the story\'s mood (it plays under the post '
             'as a Reel).')


def _music(d, names):
    m = str(d.get("music") or "") if isinstance(d, dict) else ""
    return m if m in names else (random.choice(names) if names else "")


def write(tpl, niche="", cid=None, music=(), keywords="", say=lambda s: None):
    """DeepSeek writes one post from template `tpl` -> {card, footer, caption, tags, link, source, key, slides, music}.
    Raises if it can't. Never repeats a headline / card this category already has from that template.
    music: track names to choose from (the post goes up as a Reel); the AI picks the one that fits."""
    topic, tid = niche or ANY_NICHE, tpl.get("id")
    how = f"Post type: {tpl['name']}\nInstructions: {tpl['instructions'] or '(none: use your judgement)'}"
    ctx = cats.scope(cid)  # the category (description + keywords) as a rule: posts stay inside it
    if ctx:
        how = f"{ctx}\n{how}"
    if keywords.strip():
        how += f"\nKeywords for these posts (use where they fit, in the text and hashtags): {keywords.strip()}"
    learned = engage.examples(cid)  # what got engagement on this page before
    if learned:
        how += "\n" + learned
    names = list(music)
    if names:
        how += "\nMusic tracks: " + json.dumps(names, ensure_ascii=False)
    if tpl["source"] == "news":
        seen = _seen(cid, tid)
        c = category(cid) if cid else None
        terms = [x for x in (((c or {}).get("keywords") or "") + "," + keywords).split(",") if x.strip()]
        say(f"reading the news sources for '{niche or 'top stories'}'")
        items = [i for i in news(niche, terms=terms) if _key(i["title"]) not in seen]
        say(f"{len(items)} new stories found; the AI is picking one")
        if not items:
            raise RuntimeError(f"no new headlines for '{niche or 'top stories'}' in the news sources (Settings)")
        rows = [{"id": k, "title": i["title"], "source": i["source"], "hours_ago": i["hours"], "snippet": i["snippet"][:160]}
                for k, i in enumerate(items)]
        d = pick._ask(f"Page topic: {niche or 'top stories, anything'}\n{how}\nHeadlines:\n{json.dumps(rows, indent=1)}",
                      NEWS_PICK_SYSTEM, "ids", config.POST_MODEL)
        picks = [items[int(x)] for x in (d if isinstance(d, list) else [d]) if str(x).isdigit() and int(x) < len(items)] or items[:1]
        for item in picks[:3]:  # the first pick whose article can be read; else the first, from its snippet
            say(f"reading the article: {item['title'][:70]} ({item['source']})")
            text, read_from = article(item)
            if text:
                break
        else:
            item, text, read_from = picks[0], "", ""
        story = {"headline": item["title"], "source": item["source"], "hours_ago": item["hours"],
                 "article_text" if text else "snippet": text or item["snippet"] or "(none)"}
        say("the AI is writing the post from " + ("the article" if text else "the headline (article unreadable)"))
        d = pick._ask(f"Page topic: {niche or 'top stories, anything'}\n{how}\nStory:\n{json.dumps(story, indent=1, ensure_ascii=False)}",
                      NEWS_SYSTEM + (MUSIC_ASK if names else "") + _language(), "post", config.POST_MODEL)
        if not isinstance(d, dict):
            raise RuntimeError("DeepSeek gave no post")
        card = _clean(d.get("card"), 120) or item["title"][:120]
        slides = [_clean(s, 200) for s in (d.get("slides") if isinstance(d.get("slides"), list) else []) if _clean(s, 200)][:4]
        return {"card": card, "footer": f"Source: {item['source']}" if item["source"] else "",
                "caption": _clean(d.get("caption"), 1500), "tags": _tags(d.get("tags")),
                "link": read_from or item["link"], "source": item["source"], "key": _key(item["title"]),
                "slides": slides if text else [], "music": _music(d, names), "image": _clean(d.get("image"), 100)}
    if tpl["source"] == "meme":
        recent = [p["card"] for p in db.all("posts", "category_id=? AND template_id=?", (cid, tid))[:30]]
        task = f"Page topic: {topic}\n{how}\n" + (f"Already posted, don't repeat these jokes:\n{json.dumps(recent, indent=1)}" if recent else "")
        for _ in range(3):
            d = pick._ask(task, MEME_SYSTEM + _language(), "meme", config.POST_MODEL)
            if not isinstance(d, dict):
                continue
            layout = d.get("layout") if d.get("layout") in ("two_panel", "dialogue", "logic") else "caption"
            most = {"two_panel": 2, "dialogue": 4}.get(layout, 1)
            panels = [{"label": _clean(x.get("label"), 30), "image": _clean(x.get("image"), 100),
                       "lines": [{"who": _clean(ln.get("who"), 30), "text": _clean(ln.get("text"), 120)}
                                 for ln in (x.get("lines") or []) if isinstance(ln, dict) and _clean(ln.get("text"), 120)][:2]}
                      for x in (d.get("panels") or []) if isinstance(x, dict) and _clean(x.get("image"), 100)][:most]
            meme = {"layout": layout, "panels": panels}
            if layout == "dialogue":
                panels = [x for x in panels if x["lines"]]
                meme["panels"] = panels
                if len(panels) < 2:
                    continue
            if layout == "logic":
                meme.update(header=_clean(d.get("header"), 40), bubble=_clean(d.get("bubble"), 100),
                            bottom=_clean(d.get("bottom"), 110))
                if not (meme["header"] or meme["bottom"]) or any(_placeholder(meme[k]) for k in ("header", "bubble", "bottom")):
                    continue
            if any(_placeholder(ln["text"]) for x in panels for ln in x["lines"]) or _placeholder(d.get("caption")) and layout == "caption":
                continue
            card = _clean_card(d.get("caption"), 160)
            if layout in ("dialogue", "logic") and not card:  # the joke's text, for spotting repeats
                card = _clean(" / ".join([meme.get("header", ""), meme.get("bottom", "")] if layout == "logic" else
                                         [ln["text"] for x in panels for ln in x["lines"]]).strip(" /"), 160)
            if panels and (card or layout == "two_panel") and _key(card or panels[0]["label"]) not in _seen(cid, tid):
                if layout == "two_panel" and len(panels) < 2:
                    meme["layout"] = "caption"
                # dialogue / logic draw their own text: the card text isn't printed on them
                return {"card": card, "footer": "", "caption": _clean(d.get("post"), 600), "tags": _tags(d.get("tags")),
                        "link": "", "source": "", "key": _key(card or " ".join(x["label"] for x in panels)),
                        "meme": meme, "music": _music(d, names)}
        raise RuntimeError("the AI gave no new meme")
    recent = [p["card"] for p in db.all("posts", "category_id=? AND template_id=?", (cid, tid))[:40]]
    task = f"Page topic: {topic}\n{how}\n" + (f"Already posted, don't repeat or reword these:\n{json.dumps(recent, indent=1)}"
                                               if recent else "")
    for _ in range(2):  # a repeat gets one more try
        d = pick._ask(task, POST_SYSTEM + (MUSIC_ASK if names else "") + _language(), "post", config.POST_MODEL)
        card = _clean_card(d.get("card") if isinstance(d, dict) else "", 220).strip('"“”')
        if card and _key(card) not in _seen(cid, tid) and not _placeholder(card):
            return {"card": card, "footer": _clean(d.get("footer"), 80), "caption": _clean(d.get("caption"), 1500),
                    "tags": _tags(d.get("tags")), "link": "", "source": "", "key": _key(card), "music": _music(d, names),
                    "image": _clean(d.get("image"), 100)}
    raise RuntimeError(f"DeepSeek gave no new '{tpl['name']}' post")


# ---- the card image ----

def _ass_color(hex_rgb, alpha="00"):
    h = hex_rgb.lstrip("#")
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_text(s):
    """Card text for libass: a line break the AI wrote (a meme's setup / punchline) stays a line break."""
    from .render import plain  # emoji out: the card fonts have none (empty boxes)
    s = str(s).replace("\\", "/").replace("{", "(").replace("}", ")").replace("\r", "")
    return "\\N".join(x for x in (plain(l) for l in s.split("\n")) if x)


def _ink(hex_rgb):
    """Black or white, whichever reads better on this colour (the tag text on the accent box)."""
    r, g, b = (int(hex_rgb.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return "#111111" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


def card_ass(tpl, card, footer="", handle="", picture=False):
    """The card as an ASS subtitle frame: accent stripe, tag box top-left, the text centred (size fits its length),
    footer (author / source) under it, page handle bottom-left. libass wraps the lines inside the margins."""
    font, italic = FONTS.get(tpl["font"], FONTS["sans"])
    fs = min(max(int(math.sqrt(700_000 / max(len(card), 1))), 44), 110)  # ~fills the middle 900x650 box
    white, dim, acc = _ass_color("#ffffff"), _ass_color("#9ca3af"), _ass_color(tpl["accent"])
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Tag,Arial Black,40,{_ass_color(_ink(tpl['accent']))},{acc},{acc},1,0,3,14,0,7,90,90,0
Style: Main,{font},{fs},{white},&H00000000,&H80000000,1,{italic},1,{3 if picture else 0},{2 if picture else 0},5,90,90,0
Style: Foot,Arial,40,{acc},&H00000000,&H00000000,1,0,1,0,0,8,90,90,0
Style: Handle,Arial,34,{dim},&H00000000,&H00000000,1,0,1,0,0,1,90,90,0
Style: Deco,Georgia,360,{acc},&H00000000,&H00000000,1,0,1,0,0,7,0,0,0

[Events]
Format: Layer, Start, End, Style, Text
"""
    ev = lambda style, tags, text, layer=1: f"Dialogue: {layer},0:00:00.00,0:00:05.00,{style},{{{tags}}}{text}"
    lines = [ev("Main", rf"\an7\pos(0,0)\bord0\shad0\1c{acc}\p1", f"m 0 0 l {W} 0 {W} 14 0 14{{\\p0}}", 0)]  # top stripe
    if tpl["label"]:
        lines.append(ev("Tag", r"\pos(104,104)", _ass_text(tpl["label"])))
    if footer.startswith("—"):  # a quote: big quote mark
        lines.append(ev("Deco", r"\pos(60,150)\alpha&H60&", "“", 0))
    lines.append(ev("Main", rf"\pos({W // 2},{620})\q0", _ass_text(card)))
    if footer:
        lines.append(ev("Foot", rf"\pos({W // 2},{1070})", _ass_text(footer)))
    if handle:
        lines.append(ev("Handle", rf"\pos(90,{H - 70})", _ass_text(handle)))
    return head + "\n".join(lines) + "\n"


def render_card(tpl, card, footer, out, handle="", logo=None, picture=None):
    """Write the card as a JPEG (Instagram only takes JPEG): the template's background (or `picture`, a gameplay frame,
    cropped to fill and darkened so the text reads) + a soft vignette, text via libass, `logo` bottom-right."""
    out.parent.mkdir(parents=True, exist_ok=True)
    ass = out.with_suffix(".ass")
    pic = Path(picture) if picture and Path(picture).exists() else None
    ass.write_text(card_ass(tpl, card, footer, handle, picture=bool(pic)), encoding="utf-8")
    bg = "0x" + _hex(tpl.get("bg"), "#111111").lstrip("#")
    if pic:
        inp = ["-i", str(pic.resolve())]
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},eq=brightness=-0.22:saturation=1.1,"
                f"gblur=sigma=1.2,vignette=PI/3.2")
    else:
        inp = ["-f", "lavfi", "-i", f"color=c={bg}:s={W}x{H}:d=1"]
        base = "[0:v]vignette=PI/5"
    graph, logo_in = f"{base},subtitles={ass.name}[o]", []
    if logo and Path(logo).exists():
        logo_in = ["-i", str(Path(logo).resolve())]
        graph = f"{base},subtitles={ass.name}[v];[1:v]scale=180:-1[l];[v][l]overlay=W-w-80:H-h-60[o]"
    subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", *inp, *logo_in,
                    "-filter_complex", graph, "-map", "[o]", "-frames:v", "1", "-q:v", "2", out.name],
                   check=True, capture_output=True, cwd=out.parent)
    ass.unlink(missing_ok=True)
    return out


def render_meme(meme, caption, out, handle="", logo=None):
    """A meme as a 1080x1350 JPEG.
    caption: the classic one: a white bar with the black meme text (sized to fit), the picture filling the rest.
    two_panel: two pictures, the top one fading softly into the bottom one (no hard seam), a light cinematic grade
    (contrast, vignette, a little grain) so they read as one image, labels as bold white text with a soft shadow;
    `caption` (if any) over the top on a dark fade."""
    out.parent.mkdir(parents=True, exist_ok=True)
    pics = [DATA / x["file"] for x in meme["panels"] if x.get("file") and (DATA / x["file"]).exists()]
    if not pics:
        raise RuntimeError("no picture found for the meme")
    two = meme["layout"] == "two_panel" and len(pics) >= 2
    text = _ass_text(caption or "")
    n = max(len(caption or ""), 1)
    lines, styles = [], []
    if two:
        fade, ph = 160, (H + 160) // 2  # each panel 755 tall, overlapping by `fade`: the seam is a gradient
        y2 = H - ph
        labels = [x.get("label", "") for x in meme["panels"][:2]]
        styles.append("Style: Label,Arial Black,64,&H00FFFFFF,&H00000000,&HA0000000,1,0,1,2,4,7,0,0,0")
        styles.append(f"Style: Cap,Arial Black,{56 if n < 60 else 48},&H00FFFFFF,&H00000000,&HA0000000,1,0,1,3,3,8,70,70,0")
        top_off = 0
        if caption:
            lines.append(f"Dialogue: 2,0:00:00.00,0:00:05.00,Cap,{{\\an8\\pos({W // 2},48)}}{text}")
            top_off = 150 if n < 60 else 200
        for k, lab in enumerate(labels):
            if lab:
                y = (top_off + 44) if k == 0 else (y2 + fade + 30)
                lines.append(f"Dialogue: 2,0:00:00.00,0:00:05.00,Label,{{\\an7\\pos(56,{y})}}{_ass_text(lab)}")
        grade = "eq=contrast=1.06:saturation=1.08,unsharp=5:5:0.6"
        chain = (f"[0:v]scale={W}:{ph}:force_original_aspect_ratio=increase,crop={W}:{ph},{grade}[a];"
                 f"[1:v]scale={W}:{ph}:force_original_aspect_ratio=increase,crop={W}:{ph},{grade},format=rgba,"
                 f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(Y,{fade}),255*pow(Y/{fade},1.4),255)'[b];"
                 f"color=c=black:s={W}x{H}:d=1[bg];[bg][a]overlay=0:0[t1];[t1][b]overlay=0:{y2}:format=auto,"
                 + (f"drawbox=x=0:y=0:w={W}:h={top_off + 40}:color=black@0.35:t=fill," if caption else "")
                 + "vignette=PI/4.5,noise=alls=5:allf=t,format=yuv420p")
    else:
        top = 300 if n < 50 else 400 if n < 90 else 470
        fs = 64 if n < 50 else 56 if n < 90 else 50
        styles.append(f"Style: Meme,Arial,{fs},&H00111111,&H00FFFFFF,&H00FFFFFF,1,0,1,0,0,5,60,60,0")
        lines.append(f"Dialogue: 1,0:00:00.00,0:00:05.00,Meme,{{\\an5\\pos({W // 2},{top // 2})}}{text}")
        chain = (f"[0:v]scale={W}:{H - top}:force_original_aspect_ratio=increase,crop={W}:{H - top},"
                 f"eq=contrast=1.04:saturation=1.06,unsharp=5:5:0.5[a];color=c=white:s={W}x{H}:d=1[bg];[bg][a]overlay=0:{top}")
    if handle:
        styles.append("Style: Handle,Arial,30,&H00FFFFFF,&H00000000,&H80000000,1,0,1,2,1,3,0,0,0")
        lines.append(f"Dialogue: 2,0:00:00.00,0:00:05.00,Handle,{{\\an3\\pos({W - 28},{H - 22})}}{_ass_text(handle)}")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
""" + "\n".join(styles) + """

[Events]
Format: Layer, Start, End, Style, Text
"""
    ass = out.with_suffix(".ass")
    ass.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    ins = []
    for pth in pics[:2 if two else 1]:
        ins += ["-i", str(pth.resolve())]
    k = len(ins) // 2
    logo_in, tail = [], f",subtitles={ass.name}[o]"
    if logo and Path(logo).exists():
        logo_in = ["-i", str(Path(logo).resolve())]
        tail = f",subtitles={ass.name}[v];[{k}:v]scale=120:-1[l];[v][l]overlay=W-w-28:H-h-70[o]"
    subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", *ins, *logo_in, "-filter_complex", chain + tail,
                    "-map", "[o]", "-frames:v", "1", "-q:v", "2", out.name], check=True, capture_output=True, cwd=out.parent)
    ass.unlink(missing_ok=True)
    return out


@config.in_brand(lambda pid: config.brand_of_category(db.get("posts", pid)["category_id"]))
def _render(pid):
    """The card image, plus one image per detail slide (then the post goes up as a carousel: cover first)."""
    p = db.get("posts", pid)
    c = category(p["category_id"]) or CAT_DEFAULTS
    tpl = template(p["template_id"]) or {**TPL_DEFAULTS, "label": ""}  # template deleted: plain card
    feats = json.loads(p.get("features") or "{}")
    reel = feats.get("format") == "reel"
    slides = json.loads(p.get("slides") or "[]")
    follow = engage.fill(config.POST_CTA, c) if config.POST_CTA.strip() and p.get("template_id") else ""
    if follow and (slides or reel):  # a carousel / Reel ends on the follow slide
        slides = slides + [follow]
    if "{N}" in (tpl.get("label") or ""):  # a numbered series ("GTA DAILY #{N}"): this post's number in it
        n = len([x for x in db.all("posts", "category_id=? AND template_id=?", (p["category_id"], p["template_id"]))
                 if x["id"] <= pid and x["status"] not in ("skipped", "failed")])
        tpl = {**tpl, "label": tpl["label"].replace("{N}", str(n))}
    old = [p.get("image"), p.get("video")] + (json.loads(p.get("media") or "[]") if slides or p.get("template_id") else [])
    stamp = int(time.time())  # new names each time, so the browser doesn't show a cached card
    rel = f"posts/{pid}_{stamp}.jpg"
    pic = DATA / feats["bg"] if feats.get("bg") else None
    if feats.get("meme") and feats["meme"]["layout"] in ("dialogue", "logic") and \
            any(x.get("file") for x in feats["meme"]["panels"]):
        from . import memes  # comic layouts: speech bubbles and big outlined text, drawn with Pillow
        memes.render(feats["meme"], DATA / rel, c["handle"], category_logo(c))
    elif feats.get("meme") and any(x.get("file") for x in feats["meme"]["panels"]):
        render_meme(feats["meme"], p["card"], DATA / rel, c["handle"], category_logo(c))
    else:
        render_card(tpl, p["card"], p["footer"], DATA / rel, c["handle"], category_logo(c), pic)
    media = [rel]
    for k, s in enumerate(slides):
        r = f"posts/{pid}_{stamp}_{k + 1}.jpg"
        is_follow = follow and k == len(slides) - 1
        render_card({**tpl, "label": "FOLLOW" if is_follow else f"{k + 2}/{len(slides) + 1}"}, s,
                    p["footer"] if k == len(slides) - 1 - bool(is_follow) else "", DATA / r, c["handle"], category_logo(c),
                    None if is_follow else pic)
        media.append(r)
    video = ""
    if reel:  # the slides as a Reel, with the track the AI picked (data/music); silent if it's gone
        video = f"posts/{pid}_{stamp}.mp4"
        track = next((x["file"] for x in engage.tracks() if x["name"] == feats.get("music")), None)
        engage.slideshow([DATA / m for m in media], DATA / video, track)
    feats["follow_slide"] = media[-1] if follow and len(media) > 1 else ""  # Facebook leaves it out (caption has the line)
    db.update("posts", pid, image=rel, media=json.dumps(media if len(media) > 1 else []), video=video, features=json.dumps(feats))
    for o in old:
        if o and o not in media and o != video:
            (DATA / o).unlink(missing_ok=True)


def start(cid, auto=None):
    """The post's row, saved the moment making starts (status 'making'), so its log shows every step live."""
    if not category(cid):
        raise ValueError("category not found")
    pid = db.insert("posts", category_id=cid, kind="", status="making", card="", created=time.time(),
                    auto_id=(auto or {}).get("id"))
    db.log(pid, "started", "posts")
    return pid


@config.in_brand(lambda cid, *a, **k: config.brand_of_category(cid))
def make(cid, template_id=None, status="draft", topic="", auto=None, pid=None):
    """Write + render one post for category `cid`. template_id None = the template the learning picks.
    topic: what this one post is about (a keyword); empty = the automation's / category's own topic.
    auto: the post automation making it (its templates, topic and keywords are used).
    pid: its row from start() (the API makes it first, so you can watch); None = made here."""
    pid = pid or start(cid, auto)
    REQUESTS[pid] = {"cid": cid, "template": template_id, "topic": topic, "auto": auto}

    def say(step):  # one line in the post's log, and the automation card's "Now:" line
        if pid in CANCELLED:  # you pressed Start over while it was being written
            raise Cancelled()
        db.log(pid, step, "posts")
        if auto:
            WORKING[auto["id"]] = f"Writing a post: {step}"
    try:
        out = _make(cid, template_id, status, topic, auto, pid, say)
        if pid in CANCELLED:  # finished in the same moment you started over: still thrown away
            raise Cancelled()
        return out
    except Cancelled:
        discard(pid)
        return None
    except Exception as e:
        db.update("posts", pid, status="failed")
        db.log(pid, f"failed: {social.error_text(e)[:300]}", "posts")
        raise
    finally:
        CANCELLED.discard(pid)
        REQUESTS.pop(pid, None)
        if auto:
            WORKING.pop(auto["id"], None)


class Cancelled(Exception):
    pass


REQUESTS = {}      # post id -> what it was asked for, while it's being written (Start over makes the same again)
CANCELLED = set()  # posts thrown away by Start over while being written: they stop at their next step


def discard(pid):
    """Delete a post that never went out, with its files."""
    p = db.get("posts", pid)
    if not p:
        return
    for f in [p.get("image"), p.get("video")] + json.loads(p.get("media") or "[]"):
        if f and str(f).startswith("posts/"):
            (DATA / f).unlink(missing_ok=True)
    db.delete("posts", pid)


def restart(pid):
    """Start over on a post that hasn't gone out: throw it away (a post still being written stops at its next step)
    and return what to make instead: {"cid", "template", "topic", "auto"} (the same request; a post made before a
    restart of Clipper, or by you from photos, gets its category and post type)."""
    p = db.get("posts", pid)
    if not p:
        raise ValueError("post not found")
    if p["status"] in ("posting", "posted") or any(u.get("id") for u in json.loads(p.get("uploads") or "{}").values()
                                                 if isinstance(u, dict)):
        raise ValueError("it's already posted: make a new one instead")
    if p.get("kind") == "Your post":
        raise ValueError("a post from your own photos: change it in Create instead")
    req = REQUESTS.get(pid) or {"cid": p["category_id"], "template": p.get("template_id"), "topic": "",
                                "auto": autopilot_post(p.get("auto_id"))}
    if p["status"] == "making" and pid in REQUESTS:
        CANCELLED.add(pid)  # its writer deletes it at its next step
        db.log(pid, "started over by you: stopping", "posts")
    else:
        discard(pid)
    return req


def autopilot_post(aid):
    """The post automation with this id (its templates / topic / keywords), or None."""
    return next((a for a in autos() if a["id"] == aid), None) if aid else None


def _make(cid, template_id, status, topic, auto, pid, say):
    c = category(cid)
    if not c:
        raise ValueError("category not found")
    rotation = [x for x in ((auto or {}).get("templates") or c["templates"] or [t["id"] for t in templates()]) if template(x)]
    if not rotation:
        raise ValueError("no post templates: make one in Settings → Post templates")
    why = "chosen"
    if template_id is None:  # the template whose posts got the most engagement, sometimes another one to keep learning
        pick_id, why = engage.choose(cid, "template", rotation)
        template_id = int(pick_id)
    tpl = template(template_id)
    if not tpl:
        raise ValueError("template not found")
    fmt, fwhy = tpl["format"], "the template's"
    songs = [x["name"] for x in engage.tracks()] if config.MUSIC_ON else []
    if fmt == "auto":  # card or Reel: whichever has done better (a Reel only with music to put under it)
        fmt, fwhy = engage.choose(cid, "format", ["photo", "reel"] if songs else ["photo"])
    db.update("posts", pid, template_id=tpl["id"], kind=tpl["name"])
    say(f"template '{tpl['name']}' ({why}), {fmt} ({fwhy})")
    if tpl["source"] != "news":
        say("the AI is writing it" + (" (a meme: the joke and the pictures to look for)" if tpl["source"] == "meme" else ""))
    subject = (topic or "").strip() or (auto or {}).get("niche") or c["niche"] or cats.search_topic(cid)  # never "anything"
    w = write(tpl, subject, cid, songs if fmt == "reel" else (),
              (auto or {}).get("keywords", ""), say)
    feats = {"template": tpl["id"], "format": fmt, "music": w.get("music", ""), "slides": len(w.get("slides") or []),
             "cta": bool(config.POST_CTA.strip())}
    db.update("posts", pid, card=w["card"], footer=w["footer"], caption=w["caption"], tags=json.dumps(w["tags"]),
              link=w["link"], source=w["source"], key=w["key"], slides=json.dumps(w.get("slides") or []),
              features=json.dumps(feats))
    say(f"written: {(w['card'] or w['caption'])[:80]}" + (f" · music: {feats['music']}" if feats["music"] else ""))
    if w.get("meme") or (tpl.get("picture", True) and w.get("image")):
        say("finding a picture: " + (", ".join(f"'{x['image']}'" for x in w["meme"]["panels"]) if w.get("meme") else f"'{w['image']}'"))
    _pictures(pid, tpl, w)
    say("rendering the card" + (" and the Reel" if fmt == "reel" else ""))
    try:
        _render(pid)
    except subprocess.CalledProcessError as e:
        db.update("posts", pid, status="failed")
        db.log(pid, f"card render failed: {(e.stderr or b'').decode(errors='replace')[-300:]}", "posts")
        raise RuntimeError("card render failed (ffmpeg needs libass: see README setup)") from e
    db.update("posts", pid, status=status)
    db.log(pid, f"made '{tpl['name']}' post for '{c['name']}' ({status}): {w['card']}", "posts")
    return db.get("posts", pid)


def _pictures(pid, tpl, w):
    """Gameplay frames for the post (images.py): one background for a card, one per panel for a meme. Stored in its
    features; a picture that can't be found just leaves the plain card."""
    from . import images
    post = db.get("posts", pid)
    feats = json.loads(post.get("features") or "{}")
    scope = cats.scope(post["category_id"])  # the right game / era: the vision pick checks the frame against it
    from . import page
    day = page.recipe(post["category_id"]).get("light") == "day"
    if day:  # a bright-daytime page: its pictures too
        scope += "\nOnly bright, sunny DAYTIME pictures of the original game (no night, dusk, sunset, rain or remastered graphics)."
    try:
        if w.get("meme"):
            def one(k, panel):
                said = " / ".join(f"{ln['who']}: {ln['text']}" for ln in panel.get("lines") or [])
                dest = images.picture(panel["image"] + (" daytime" if day else ""), f'{w["card"]} / {panel["label"] or said}',
                                      DATA / "posts" / "src" / f"{pid}_{k}.jpg", scope)
                return {**panel, "file": str(dest.relative_to(DATA)) if dest else ""}
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(4) as ex:  # a 4-panel strip: the searches at once, not one after another
                panels = list(ex.map(lambda kp: one(*kp), enumerate(w["meme"]["panels"])))
            feats["meme"] = {**w["meme"], "panels": panels}  # logic memes keep their header / bubble / bottom
            db.log(pid, f"meme ({w['meme']['layout']}): " + ", ".join(f"'{x['image']}' -> {'found' if x['file'] else 'none'}" for x in panels), "posts")
        elif tpl.get("picture", True) and w.get("image"):
            dest = images.picture(w["image"] + (" daytime" if day else ""), w["card"], DATA / "posts" / "src" / f"{pid}_bg.jpg", scope)
            if dest:
                feats["bg"] = str(dest.relative_to(DATA))
            db.log(pid, f"background picture '{w['image']}': {'found' if dest else 'none found, plain card'}", "posts")
    except Exception as e:
        db.log(pid, f"picture search failed ({e!r}): plain card", "posts")
    db.update("posts", pid, features=json.dumps(feats))


@config.in_brand(lambda pid, *a, **k: config.brand_of_category(db.get("posts", pid)["category_id"]))
def edit(pid, card=None, footer=None, caption=None):
    new = {k: v for k, v in (("card", card), ("footer", footer), ("caption", caption)) if v is not None}
    if not db.get("posts", pid) or not new:
        return db.get("posts", pid)
    db.update("posts", pid, **new)
    if "card" in new or "footer" in new:
        _render(pid)
    db.log(pid, f"edited: {', '.join(new)}", "posts")
    return db.get("posts", pid)


# ---- publishing ----

def _is_text(p):
    t = template(p.get("template_id"))
    return bool(t and t["format"] == "text")


def caption_for(p, platform, text_post=False):
    """A text post's message starts with the card text (there's no image to carry it); news adds its source."""
    tags = json.loads(p["tags"] or "[]")[: social.HASHTAGS[platform]]
    parts = [" ".join(x for x in (p["card"], p["footer"]) if x) if text_post else "", p["caption"]]
    if p.get("template_id") and config.POST_CTA.strip():  # made posts end with what a follower gets
        parts.append(engage.fill(config.POST_CTA, category(p["category_id"]) or {}))
    song = json.loads(p.get("features") or "{}").get("music") if p.get("video") and platform == "instagram" else ""
    if song:  # a Creative Commons track has to be credited where it's used
        credit = next((x["credit"] for x in engage.tracks() if x["name"] == song and x["credit"]), "")
        if credit:
            parts.append(credit)
    if p["source"]:
        parts.append(f"Source: {p['source']}" + (f"\n{p['link']}" if platform == "facebook" and p["link"] else ""))
    parts.append(" ".join(f"#{t}" for t in tags))
    return "\n\n".join(x for x in parts if x)


def _send(p, a):
    """Your own photos: one = a photo post, several = a carousel. A made post: its card (or, on Facebook, text only when
    the template says so). Instagram always needs a picture."""
    media = [DATA / m for m in json.loads(p.get("media") or "[]")]
    if p.get("video") and (DATA / p["video"]).exists() and a["platform"] == "instagram":
        # a Reel (the slides as a video with music): Instagram only. Facebook always gets the still card / carousel.
        text = caption_for(p, a["platform"])
        c = category(p["category_id"]) or {}
        a = {**a, "audio_name": (c.get("handle") or "").strip() or None}  # "original audio" credited to the page
        fb = next((x for x in accts.all(secret=True, platform="facebook", brand_id=a.get("brand_id")) if not x["error"]), None)
        return social.instagram({**a, "host_page": (fb["ext_id"], fb["token"])} if fb else a, DATA / p["video"], text)
    text_post = a["platform"] == "facebook" and (_is_text(p) or (not media and not p["image"]))
    text = caption_for(p, a["platform"], text_post)
    if a["platform"] == "facebook":
        if text_post:
            return social.fb_text(text, a["ext_id"], a["token"])
        follow = json.loads(p.get("features") or "{}").get("follow_slide")
        if follow:  # just the post on Facebook: no "Follow …" image (the line is in the caption already)
            media = [m for m in media if m != DATA / follow]
        if len(media) > 1:
            return social.fb_carousel(media, text, a["ext_id"], a["token"])
        return social.fb_photo(media[0] if media else DATA / p["image"], text, a["ext_id"], a["token"])
    if not media and not p["image"]:
        raise RuntimeError("Instagram needs a picture: add a photo to this post")
    # The photo is hosted on a Page for a moment, and only the Page's own token may do that ("Unpublished posts must
    # be posted to a page as the page itself"): use the brand's Facebook account for that Page (its linked one first,
    # else any), not the Instagram account's token, which is often a user / system-user token.
    pages = [x for x in accts.all(secret=True, platform="facebook", brand_id=a.get("brand_id")) if not x["error"]]
    fb = next((x for x in pages if x["ext_id"] == a["page_id"]), None) or (None if a["page_id"] else next(iter(pages), None))
    page, host = (fb["ext_id"], fb["token"]) if fb else (a["page_id"], None)
    if len(media) > 1:
        return social.ig_carousel(media, text, a["ext_id"], page, a["token"], host)
    return social.ig_photo(media[0] if media else DATA / p["image"], text, a["ext_id"], page, a["token"], host)


def targets_of(p):
    """The accounts a post goes to: the ones picked for it, else its category's defaults (Facebook / Instagram)."""
    chosen = json.loads(p.get("targets") or "[]")
    if chosen:
        return chosen
    auto = auto_get(p["auto_id"]) if p.get("auto_id") else None
    if auto:
        return [x["id"] for x in auto_dest(auto)]  # its automation's accounts
    return [a["id"] for a in cats.destinations(p["category_id"]) if a["platform"] in PLATFORMS]


@config.in_brand(lambda pid, *a, **k: config.brand_of_category(db.get("posts", pid)["category_id"]))
def publish(pid, account_ids=None, auto=False):
    """Post to each target account it isn't on yet. uploads = {account id: result}. Expired accounts are refused
    (reconnect them first); an account whose API says its limit is reached is paused 24 h: automatic posting (auto)
    skips it, and when every target is paused the post waits for the first pause to end. You posting by hand: tries all."""
    p = db.get("posts", pid)
    if not p or p["status"] in ("posting", "making"):
        return p
    by_id = {a["id"]: a for a in accounts(secret=True)}
    targets = [by_id[i] for i in (account_ids or targets_of(p)) if i in by_id]
    if not targets:
        raise ValueError("no accounts to post to: map a Facebook Page or Instagram account to this category")
    ups = json.loads(p["uploads"] or "{}")
    if auto:
        paused = [a for a in targets if accts.limited(a) and not ups.get(str(a["id"]), {}).get("id")]
        if paused and len(paused) == len([a for a in targets if not ups.get(str(a["id"]), {}).get("id")]):
            until = min(a["limited_until"] for a in paused) + 60
            db.update("posts", pid, publish_at=until)
            db.log(pid, f"its accounts are paused by a limit; timed for {time.strftime('%d %b %H:%M', time.localtime(until))}", "posts")
            return db.get("posts", pid)
        for a in paused:
            db.log(pid, f"not sent to {a['platform']} {a['name']}: paused by a limit until "
                        f"{time.strftime('%d %b %H:%M', time.localtime(a['limited_until']))}", "posts")
        targets = [a for a in targets if a not in paused]
    db.update("posts", pid, status="posting")
    for a in [a for a in targets if not ups.get(str(a["id"]), {}).get("id")]:
        try:
            if a["error"]:
                raise RuntimeError(f"{a['name']} needs reconnecting on the Accounts page")
            r = _send(p, a)
            ups[str(a["id"])] = {**r, "at": time.time(), "platform": a["platform"], "name": a["name"]}
            db.log(pid, f"posted to {a['platform']} {a['name']} -> {r['url']}", "posts")
        except Exception as e:
            msg = social.error_text(e)
            ups[str(a["id"])] = {"error": msg[:500], "at": time.time(), "platform": a["platform"], "name": a["name"]}
            social.note_limit(a["platform"], msg, a["id"])  # a limit error: that account paused 24 h
            accts.token_error(a["id"], msg)
            db.log(pid, f"post to {a['platform']} {a['name']} failed: {msg}", "posts", exc=True)
    done = all(ups.get(str(a["id"]), {}).get("id") for a in targets)
    feats = json.loads(p.get("features") or "{}")
    feats.setdefault("hour", time.localtime().tm_hour)  # when it went up: the learning also reports the best hours
    db.update("posts", pid, uploads=json.dumps(ups), status="posted" if done else "failed", features=json.dumps(feats))
    return db.get("posts", pid)


def create_custom(category_id, media, caption, tags=(), targets=(), publish_at=None, now=False):
    """A post from your own photos (none = a text post on Facebook). media: saved image paths (relative to data/).
    publish_at: a time to post at; now: post right away; neither: a draft."""
    media = [m for m in media if (DATA / m).exists()][:10]
    pid = db.insert("posts", category_id=category_id or cats.uncategorized(), template_id=None, kind="Your post",
                    status="draft", card="", footer="", caption=str(caption or "")[:2200],
                    tags=json.dumps([str(x).lstrip("#") for x in tags][:30]), media=json.dumps(media),
                    image=media[0] if media else "", targets=json.dumps([int(x) for x in targets]),
                    publish_at=publish_at, created=time.time())
    if publish_at:
        db.update("posts", pid, status="ready")
    if now:
        publish(pid)
    return db.get("posts", pid)


def save_upload(data, aspect):
    """An uploaded photo -> cropped to the chosen shape (1:1 or 4:5, from the centre), saved as a JPEG in data/uploads.
    -> its path relative to data/."""
    import uuid
    w, h = {"1:1": (1080, 1080), "4:5": (1080, 1350)}.get(aspect, (1080, 1350))
    src = DATA / "uploads" / f"in_{uuid.uuid4().hex}"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    out = src.with_name(f"{uuid.uuid4().hex}.jpg")
    try:
        subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-i", src.name, "-vf",
                        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}", "-frames:v", "1", "-q:v", "2",
                        out.name], check=True, capture_output=True, cwd=src.parent)
    finally:
        src.unlink(missing_ok=True)
    return str(out.relative_to(DATA))


# ---- the loop ----

def tick_auto(a, now):
    """One automation: keep drafts topped up (approval on), and post on its slot. Returns the post id posted, or None."""
    cid, key = a["category_id"], f"a{a['id']}"
    err = ERRORS.get(key)
    can_make = not err or now - err["at"] > RETRY_AFTER
    sch = autopilot.schedule(a, now, _atimes(a["id"]))
    if a["approve"] and can_make:
        pending = len(db.all("posts", "auto_id=? AND status IN ('draft','ready')", (a["id"],)))
        if pending < min(BUFFER, max(sch["posts_per_day"] - sch["posted_today"], 1)):
            make(cid, auto=a)
    if not a["approve"] and can_make:  # no approval: keep one post made ahead, so its slot never waits on the AI
        if not db.all("posts", "auto_id=? AND status IN ('ready','making') AND publish_at IS NULL", (a["id"],)):
            make(cid, status="ready", auto=a)
    ERRORS.pop(key, None)
    return None  # posting itself: planner.dispatch() on the planned slot (the Scheduled tab's time)


def tick(now=None):
    """Every minute: drop stale news, then each enabled category with post types and accounts gets its turn."""
    now = now or time.time()
    for p in db.all("posts", "source != '' AND status IN ('draft','ready') AND publish_at IS NULL AND created < ?",
                    (now - NEWS_STALE,)):
        db.update("posts", p["id"], status="skipped")
        db.log(p["id"], "skipped: news older than 12 h", "posts")
    for p in db.all("posts", "status='ready' AND publish_at IS NOT NULL AND publish_at <= ?", (now,)):
        try:  # scheduled for a time: goes up then, whatever the category's slots
            publish(p["id"], auto=True)
        except Exception as e:
            db.log(p["id"], f"scheduled post failed: {e!r}", "posts", exc=True)
            db.update("posts", p["id"], status="failed")
    # making posts: page.maker_tick (each category's recipe); posting them: planner.dispatch (the page plan)


def locked(fn, *a, **kw):
    with LOCK:
        return fn(*a, **kw)


def _loop():
    checked = 0
    while True:
        try:
            if time.time() - checked > CHECK_EVERY:  # at startup, then every 6 h
                checked = time.time()
                check_accounts()
            locked(tick)
        except Exception as e:
            db.syslog(f"posts loop error: {e!r}", exc=True)
        time.sleep(60)


def start_loop():
    threading.Thread(target=_loop, daemon=True).start()


if __name__ == "__main__":
    quote = {**TPL_DEFAULTS, "label": "QUOTE", "font": "serif", "accent": "#facc15"}
    a = card_ass(quote, "Stay {hungry}, stay foolish", "— Steve Jobs", "@page")
    assert "Stay (hungry), stay foolish" in a and "— Steve Jobs" in a and "@page" in a and "Georgia" in a and "“" in a, a
    assert "Tag," in a and "QUOTE" in a and "Tag" not in card_ass({**quote, "label": ""}, "x").split("[Events]")[1]
    assert _ass_color("#ef4444") == "&H004444EF" and _ink("#facc15") == "#111111" and _ink("#0f172a") == "#ffffff"
    assert _key("Hello, World!") == "hello world" and _hex("#ABCDEF", "x") == "#abcdef" and _hex("red", "x") == "x"
    p = {"card": "X", "caption": "Y", "footer": "", "source": "BBC", "link": "http://l",
         "tags": json.dumps(["a", "b", "c", "d", "e", "f"])}
    assert caption_for(p, "facebook") == "Y\n\nSource: BBC\nhttp://l\n\n#a #b #c"
    assert caption_for(p, "instagram") == "Y\n\nSource: BBC\n\n#a #b #c #d #e"
    q = {**p, "card": "Pizza or tacos?", "source": "", "link": ""}
    assert caption_for(q, "facebook", text_post=True).startswith("Pizza or tacos?\n\nY")
    print("posts ok")
