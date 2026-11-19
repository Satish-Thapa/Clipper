"""The page: ONE plan for everything the brand posts, and categories as content recipes.

  plan        clips a day + posts a day, posting hours, on/off, approval. Every clip goes to the page's
              YouTube / Instagram / Facebook; every photo post to its Facebook / Instagram (YouTube takes no photos).
  recipe      per category (what YOU decide it's about, in its description): its share of the clips and their style
              (hype highlights / nostalgia), its share of the posts and their templates, which platforms, on / paused.
  producers   each category's recipe runs one hidden clip autopilot and one hidden post automation that only MAKE
              content into the queue (topics, clipping, writing: the machinery that already works). They never post.
  queue       every clip / post waiting for a slot. planner.py gives each a time from the plan and posts it then.
              Pausing a category stops making new content for it; what's already queued still goes out. Turning the
              whole plan off holds the queue (nothing posts, nothing is cancelled).

maker_tick() (every 5 min) keeps about a day of content queued per category, in proportion to its share."""
import json
import math
import threading
import time

from . import autopilot, brands, categories, db

PLAN_DEFAULTS = {"on": False, "clips_per_day": 15, "posts_per_day": 5, "from": 8, "to": 22, "approve": False}
RECIPE_DEFAULTS = {"on": True, "clip_share": 0, "clip_mode": "hype", "post_share": 0, "templates": [],
                   "platforms": ["youtube", "instagram", "facebook"],
                   "light": "any",  # nostalgia clips: "day" = bright daytime moments only
                   "look": "",  # nostalgia clips: "retro" = warm, faded, film grain
                   # hype clips: % of source videos searched as a streamer playing live (their facecam is cut out and
                   # stacked above the game); the rest clean gameplay. Moves itself toward the style that gets more
                   # views (learn_styles), between STYLE_MIN and 100 - STYLE_MIN.
                   "streamer_share": 50}
STYLE_MIN = 20       # each style keeps at least this % of the sources, so the test never stops
STYLE_MIN_CLIPS = 5  # posted clips with views needed per style before the share moves
CLIP_MODES = ("hype", "nostalgia")
POST_PLATFORMS = ("instagram", "facebook")


# ---- the plan ----

def plan(bid=None):
    bid = bid or brands.current()
    return {**PLAN_DEFAULTS, **json.loads(db.get_setting(f"page_plan_{bid}") or "{}")}


def save_plan(new, bid=None):
    bid = bid or brands.current()
    p = {**plan(bid), **{k: v for k, v in new.items() if k in PLAN_DEFAULTS}}
    p["clips_per_day"] = min(max(int(p["clips_per_day"]), 0), 48)
    p["posts_per_day"] = min(max(int(p["posts_per_day"]), 0), 24)
    p["from"] = min(max(int(p["from"]), 0), 23)
    p["to"] = min(max(int(p["to"]), p["from"] + 1), 24)
    p["on"], p["approve"] = bool(p["on"]), bool(p["approve"])
    db.set_setting(f"page_plan_{bid}", json.dumps(p))
    sync()
    return p


# ---- recipes ----

def recipe(cid):
    return {**RECIPE_DEFAULTS, **json.loads(db.get_setting(f"recipe_{cid}") or "{}")}


def save_recipe(cid, new):
    from . import posts as P
    r = {**recipe(cid), **{k: v for k, v in new.items() if k in RECIPE_DEFAULTS}}
    r["clip_share"] = min(max(int(r["clip_share"]), 0), 100)
    r["post_share"] = min(max(int(r["post_share"]), 0), 100)
    r["clip_mode"] = r["clip_mode"] if r["clip_mode"] in CLIP_MODES else "hype"
    have = {t["id"] for t in P.templates()}
    r["templates"] = [int(t) for t in r["templates"] if int(t) in have]
    r["platforms"] = [p for p in r["platforms"] if p in RECIPE_DEFAULTS["platforms"]] or list(RECIPE_DEFAULTS["platforms"])
    r["on"] = bool(r["on"])
    r["light"] = r["light"] if r["light"] in ("any", "day") else "any"
    r["look"] = r["look"] if r["look"] in ("", "retro") else ""
    r["streamer_share"] = min(max(int(r["streamer_share"]), 0), 100)
    db.set_setting(f"recipe_{cid}", json.dumps(r))
    sync()
    return r


def next_style(cid):
    """The source style for the category's next topic: "streamer" (a streamer playing live, facecam layout) or
    "clean" (gameplay without a facecam), whichever is further behind the recipe's streamer_share over its last 20
    topics. "" for nostalgia categories (their own kind of source)."""
    r = recipe(cid)
    if r["clip_mode"] != "hype":
        return ""
    share = r["streamer_share"] / 100
    recent = [t.get("style") for t in db.all("topics", "category_id=? AND source='auto'", (cid,))[:20]]
    s = sum(1 for x in recent if x == "streamer")
    c = sum(1 for x in recent if x == "clean")
    if not s + c:
        return "streamer" if share >= 0.5 else "clean"
    return "streamer" if s / (s + c) < share else "clean"


def clip_style(c):
    """What a made clip ended up as: "facecam" (a streamer's cam stacked above the game) or "clean"."""
    return "facecam" if json.loads(c.get("features") or "{}").get("cam") else "clean"


def learn_styles():
    """For each hype category: median views of its posted facecam clips vs its clean ones; once both have
    STYLE_MIN_CLIPS, streamer_share moves to the facecam side's share of the two medians (kept within
    STYLE_MIN..100-STYLE_MIN). -> {category id: {"facecam": median, "clean": median, "share": new %}}"""
    import statistics
    from . import learn
    out = {}
    for c in categories.all():
        r = recipe(c["id"])
        if not r["on"] or r["clip_mode"] != "hype" or not r["clip_share"]:
            continue
        views = {"facecam": [], "clean": []}
        for cl, v in learn._scored(db.all("clips", "category_id=?", (c["id"],))):
            views[clip_style(cl)].append(v)
        if min(len(views["facecam"]), len(views["clean"])) < STYLE_MIN_CLIPS:
            continue
        mf, mc = statistics.median(views["facecam"]), statistics.median(views["clean"])
        share = round(100 * mf / (mf + mc)) if mf + mc else 50
        share = min(max(share, STYLE_MIN), 100 - STYLE_MIN)
        if share != r["streamer_share"]:
            db.set_setting(f"recipe_{c['id']}", json.dumps({**r, "streamer_share": share}))
            db.syslog(f"{c['name']}: facecam clips median {mf:,.0f} views vs clean {mc:,.0f} -> streamer sources "
                      f"{r['streamer_share']}% -> {share}%")
        out[c["id"]] = {"facecam": mf, "clean": mc, "share": share}
    return out


def cats(bid=None):
    """The brand's content categories (Uncategorized left out) with their recipes."""
    return [{**c, "recipe": recipe(c["id"])} for c in categories.all(brand_id=bid or brands.current())
            if c["name"] != categories.UNCATEGORIZED]


def shares(kind, bid=None):
    """{category id: fraction} of the clips ("clip") or posts ("post") for the switched-on categories."""
    key = "clip_share" if kind == "clip" else "post_share"
    on = {c["id"]: c["recipe"][key] for c in cats(bid) if c["recipe"]["on"] and c["recipe"][key] > 0
          and (kind == "clip" or c["recipe"]["templates"])}
    total = sum(on.values())
    return {k: v / total for k, v in on.items()} if total else {}


def page_accounts(bid=None, platforms=None):
    """The page's connected accounts (one per platform: the first connected one)."""
    from . import accounts
    out, seen = [], set()
    for a in accounts.all(brand_id=bid or brands.current()):
        if a["status"] == "connected" and a["platform"] not in seen and (not platforms or a["platform"] in platforms):
            out.append(a)
            seen.add(a["platform"])
    return out


# ---- producers: the hidden makers behind each recipe ----

def _owners():
    """{category id (str): {"clip": autopilot id, "post": post automation id}}: which hidden makers a recipe runs.
    Kept on its own (the autopilot / automation settings are cleaned to their known fields on every save)."""
    return json.loads(db.get_setting("page_producers") or "{}")


def _own(cid, kind, iid):
    o = _owners()
    o.setdefault(str(cid), {})[kind] = iid
    db.set_setting("page_producers", json.dumps(o))


def _producer_clip(cid):
    aid = _owners().get(str(cid), {}).get("clip")
    return next((s for s in autopilot.profiles() if s["id"] == aid), None) if aid else None


def _producer_post(cid):
    from . import posts as P
    aid = _owners().get(str(cid), {}).get("post")
    return next((a for a in P.autos() if a["id"] == aid), None) if aid else None


def sync(bid=None):
    """Make each category's producers match its recipe and the plan (and switch off makers no recipe owns: the old
    hand-made autopilots / post automations; their already-made clips stay queued)."""
    from . import posts as P
    p = plan(bid)
    mine = cats(bid)
    owned_ap, owned_pa = set(), set()
    cs, ps = shares("clip", bid), shares("post", bid)
    for c in mine:
        r = c["recipe"]
        per_day = max(1, math.ceil(p["clips_per_day"] * cs.get(c["id"], 0)))
        want_clips = p["on"] and r["on"] and c["id"] in cs
        s = _producer_clip(c["id"])
        fields = {"name": c["name"], "category_id": c["id"], "mode": r["clip_mode"], "platforms": r["platforms"],
                  "posts_per_day": per_day, "post_from": p["from"], "post_to": p["to"], "hour": p["from"],
                  "shorts": 2, "longs": 0,  # 2 per video: more different videos, less of the same drive
                  "choosy": 3, "subtitles": r["clip_mode"] != "nostalgia",
                  "hq": False,  # 1080p sources (your choice: good enough, much faster than 4K)
                  "face_check": True, "voiceover": False, "keep_sources": False, "aspect": 1.0, "enabled": want_clips}
        if not s:
            s = autopilot.create({k: v for k, v in fields.items() if k != "enabled"})
            _own(c["id"], "clip", s["id"])
        _set_profile(s["id"], fields)
        owned_ap.add(s["id"])
        a = _producer_post(c["id"])
        pfields = {"name": c["name"], "category_id": c["id"], "templates": r["templates"] or [1],
                   "posts_per_day": max(1, math.ceil(p["posts_per_day"] * ps.get(c["id"], 0))),
                   "post_from": p["from"], "post_to": p["to"], "approve": p["approve"], "accounts": [],
                   "enabled": bool(p["on"] and r["on"] and c["id"] in ps)}
        if not a:
            a = P.auto_create({**pfields, "enabled": False})
            _own(c["id"], "post", a["id"])
        _set_auto(a["id"], pfields)
        owned_pa.add(a["id"])
    mine_ids = {c["id"] for c in mine}
    for s in autopilot.profiles():  # old makers of this brand's categories that no recipe owns: off
        if s["id"] not in owned_ap and s.get("category_id") in mine_ids and s.get("enabled"):
            _set_profile(s["id"], {"enabled": False})
    for a in P.autos():
        if a["id"] not in owned_pa and a.get("category_id") in mine_ids and a.get("enabled"):
            _set_auto(a["id"], {"enabled": False})


def _set_profile(aid, fields):
    lst = autopilot.profiles()
    for s in lst:
        if s["id"] == aid:
            s.update(fields)
    db.set_setting("autopilots", json.dumps(lst))


def _set_auto(aid, fields):
    from . import posts as P
    lst = P.autos()
    for a in lst:
        if a["id"] == aid:
            a.update(fields)
    db.set_setting("post_autos", json.dumps(lst))


# ---- keeping the queue full ----

MAKING = {}  # category id -> thread making a post right now


def queued(cid):
    """(clips waiting for a slot, posts ready / being made) of one category."""
    clips = len(db.all("clips", "status='scheduled' AND publish_at IS NULL AND category_id=?", (cid,)))
    posts = len(db.all("posts", "status IN ('ready','making','draft') AND publish_at IS NULL AND category_id=?", (cid,)))
    return clips, posts


def all_brands():
    return [b["id"] for b in db.all("brands")]


def maker_tick(bid=None):
    """Every few minutes: each switched-on category gets content made until about a day of its share is queued.
    Clips: a round of its clip producer (finds videos, clips them). Posts: one post at a time from its post producer."""
    from . import posts as P
    p = plan(bid)
    if not p["on"]:
        return
    sync(bid)
    cs, ps = shares("clip", bid), shares("post", bid)
    for c in cats(bid):
        if not c["recipe"]["on"]:
            continue
        have_clips, have_posts = queued(c["id"])
        s = _producer_clip(c["id"])
        if s and c["id"] in cs and not autopilot.bot_paused():
            want = math.ceil(p["clips_per_day"] * cs[c["id"]])
            coming = autopilot.pipeline(s["id"])[1]
            if have_clips + coming < want and not autopilot._lock(s["id"]).locked() and not autopilot._active_topics(s["id"]):
                threading.Thread(target=autopilot.run_round, args=("top-up", s["id"]), daemon=True).start()
        a = _producer_post(c["id"])
        if a and c["id"] in ps and have_posts < math.ceil(p["posts_per_day"] * ps[c["id"]]):
            t = MAKING.get(c["id"])
            if not (t and t.is_alive()) and not P.LOCK.locked():
                status = "draft" if p["approve"] else "ready"
                th = threading.Thread(target=P.locked, args=(P.make, c["id"], None, status, "", a), daemon=True)
                MAKING[c["id"]] = th
                th.start()


# ---- what went out today (the planner's daily caps and the category balance) ----

def posted_today(day_start):
    """{"clip": {category id: n}, "post": {category id: n}} of items first published since day_start."""
    out = {"clip": {}, "post": {}}
    for kind, table in (("clip", "clips"), ("post", "posts")):
        for r in db.all(table, "uploads != '{}' AND uploads != ''"):
            ats = [u.get("at") or 0 for u in json.loads(r["uploads"] or "{}").values() if isinstance(u, dict) and u.get("id")]
            if ats and min(ats) >= day_start:
                out[kind][r["category_id"]] = out[kind].get(r["category_id"], 0) + 1
    return out
