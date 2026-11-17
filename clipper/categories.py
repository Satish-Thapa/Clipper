"""Categories: a theme you name (e.g. "Football", "Tech tips") that clips and posts belong to, with the social
accounts it publishes to.

  category_accounts  which accounts a category maps to; several per platform, one of them the default.
                     Choosing a category in Create fills in its DEFAULT account for each chosen platform; the others
                     are one tick away.
  Uncategorized      always exists; things made before categories (and things whose category was deleted without
                     a destination) live there.

Categories also hold the post-automation settings (topic, templates, schedule, approval, card handle and logo):
the table is still called post_categories."""
import json
import time

from . import accounts, db
from .config import DATA

UNCATEGORIZED = "Uncategorized"
DEFAULTS = {"name": "", "description": "", "keywords": "", "color": "#6b8f86", "niche": "", "templates": [], "posts_per_day": 4,
            "post_from": 9, "post_to": 21, "approve": True, "enabled": False, "handle": "", "logo": True,
            "logo_file": ""}


def _mapping(cid):
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT account_id, is_default FROM category_accounts WHERE category_id=?",
                                           (cid,))]


def _cat(row):
    c = {**DEFAULTS, **{k: v for k, v in row.items() if v is not None and k in DEFAULTS}, "id": row["id"],
         "brand_id": row.get("brand_id")}
    c["templates"] = json.loads(c["templates"]) if isinstance(c["templates"], str) else c["templates"]
    for k in ("approve", "enabled", "logo"):
        c[k] = bool(c[k])
    m = _mapping(row["id"])
    c["accounts"] = [x["account_id"] for x in m]  # every mapped account
    c["defaults"] = [x["account_id"] for x in m if x["is_default"]]  # one per platform: auto-selected in Create
    return c


def all(brand_id=None):
    """Every category (oldest first); only one brand's when brand_id is given."""
    rows = db.all("post_categories", "brand_id=?", (brand_id,)) if brand_id else db.all("post_categories")
    return [_cat(r) for r in reversed(rows)]


def get(cid):
    r = db.get("post_categories", int(cid)) if cid else None
    return _cat(r) if r else None


def uncategorized(brand_id=None):
    """A brand's Uncategorized category id (made on first use). Default: the brand you're working in."""
    from . import brands
    bid = brand_id or brands.current()
    rows = db.all("post_categories", "name=? AND brand_id=?", (UNCATEGORIZED, bid))
    if rows:
        return rows[-1]["id"]
    return db.insert("post_categories", name=UNCATEGORIZED, color="#8a8f98", templates="[]", brand_id=bid,
                     created=time.time())


def save(cid, data):
    """Create (cid None) or update a category. `accounts` (a list of account ids) replaces its mapping; each platform
    keeps its default if still mapped, else its first mapped account becomes the default."""
    c = {**(get(cid) if cid else DEFAULTS), **{k: v for k, v in data.items() if k in DEFAULTS}}
    from . import posts
    have = {x["id"] for x in posts.templates()}
    c["name"] = str(c["name"]).strip()[:60] or "Untitled"
    c["templates"] = [int(x) for x in c["templates"] if int(x) in have]
    c["post_from"] = min(max(int(c["post_from"]), 0), 23)
    c["post_to"] = min(max(int(c["post_to"]), c["post_from"] + 1), 24)
    color = str(c["color"] or "")
    c["color"] = color if len(color) == 7 and color.startswith("#") else DEFAULTS["color"]
    row = {k: c[k] for k in DEFAULTS}
    row.update(templates=json.dumps(c["templates"]), approve=int(c["approve"]), enabled=int(c["enabled"]),
               logo=int(c["logo"]), description=str(c["description"] or "")[:500], keywords=str(c["keywords"] or "")[:300])
    if cid:
        db.update("post_categories", cid, **row)
    else:
        from . import brands
        cid = db.insert("post_categories", **row, brand_id=data.get("brand_id") or brands.current(), created=time.time())
    if "accounts" in data:
        set_accounts(cid, [int(a) for a in data["accounts"]], data.get("defaults"))
    return get(cid)


def set_accounts(cid, ids, defaults=None):
    """Replace the mapping. defaults: account ids to make the default for their platform (else kept / first)."""
    known = {a["id"]: a for a in accounts.all()}
    old_defaults = set(get(cid)["defaults"]) if get(cid) else set()
    want = set(defaults) if defaults is not None else old_defaults
    ids = [i for i in dict.fromkeys(ids) if i in known]
    chosen = {}  # platform -> default account id
    for i in ids:
        p = known[i]["platform"]
        if i in want and p not in chosen:
            chosen[p] = i
    for i in ids:
        chosen.setdefault(known[i]["platform"], i)
    with db.conn() as c:
        c.execute("DELETE FROM category_accounts WHERE category_id=?", (cid,))
        c.executemany("INSERT INTO category_accounts (category_id, account_id, is_default) VALUES (?, ?, ?)",
                      [(cid, i, int(chosen[known[i]["platform"]] == i)) for i in ids])


def map_account(cid, aid, mapped=True, default=False):
    """Tick / untick one account for a category, or make it its platform's default."""
    c = get(cid)
    ids = [x for x in c["accounts"] if x != aid] + ([aid] if mapped else [])
    defaults = [x for x in c["defaults"] if x != aid] + ([aid] if mapped and default else [])
    if mapped and default:  # the old default of that platform steps down
        plat = accounts.get(aid)["platform"]
        defaults = [x for x in defaults if x == aid or (accounts.get(x) or {}).get("platform") != plat]
    set_accounts(cid, ids, defaults)
    return get(cid)


def destinations(cid, platforms=None):
    """The accounts a category publishes to by default: [account row] (one per platform, its default), limited to
    `platforms` if given. Platforms the category maps nothing for fall back to accounts.default_for()."""
    c = get(cid) if cid else None
    out = [a for a in (accounts.get(i) for i in (c["defaults"] if c else [])) if a]
    have = {a["platform"] for a in out}
    for p in platforms or []:
        if p not in have:
            a = accounts.default_for(p, c["brand_id"] if c else None) if not c or c["name"] == UNCATEGORIZED else None
            if a:
                out.append(a)
    return [a for a in out if not platforms or a["platform"] in platforms]


def counts():
    """{category id: {"clips": n, "posts": n}} for the Categories screen."""
    out = {}
    with db.conn() as c:
        for table in ("clips", "posts"):
            for cid, n in c.execute(f"SELECT category_id, COUNT(*) FROM {table} GROUP BY category_id"):
                out.setdefault(cid, {"clips": 0, "posts": 0})[table] = n
    return out


def delete(cid, move_to=None):
    """Delete a category. Its clips, posts and jobs move to `move_to` (default Uncategorized); nothing is lost."""
    c = get(cid)
    if not c:
        return
    if c["name"] == UNCATEGORIZED:
        raise ValueError("Uncategorized can't be deleted")
    dest = int(move_to) if move_to and get(move_to) and int(move_to) != cid else uncategorized(c["brand_id"])
    with db.conn() as x:
        for table in ("clips", "posts", "jobs"):
            x.execute(f"UPDATE {table} SET category_id=? WHERE category_id=?", (dest, cid))
        x.execute("DELETE FROM category_accounts WHERE category_id=?", (cid,))
    if c["logo_file"]:
        (DATA / c["logo_file"]).unlink(missing_ok=True)
    db.delete("post_categories", cid)
    db.set_setting(f"posts_times_{cid}", "[]")
    _move_automations(cid, dest)
    return dest


def _move_automations(cid, dest):
    """The deleted category's post automations and clip autopilots now work for `dest` (switched off when that's
    Uncategorized, which has no accounts of its own)."""
    import json
    from . import autopilot
    off = (get(dest) or {}).get("name") == UNCATEGORIZED
    raw = db.get_setting("post_autos")
    if raw:
        lst = json.loads(raw)
        for a in lst:
            if a.get("category_id") == cid:
                a["category_id"] = dest
                a["enabled"] = a.get("enabled") and not off
        db.set_setting("post_autos", json.dumps(lst))
    for s in autopilot.profiles():
        if s["category_id"] == cid:
            autopilot.save({"category_id": None if off else dest, **({"enabled": False} if off else {})}, s["id"])


def context(cid):
    """What the AI should know about a category: its name, description and keywords (empty for Uncategorized)."""
    c = get(cid)
    if not c or c["name"] == UNCATEGORIZED:
        return ""
    parts = [f"Category: {c['name']}"]
    if c["description"].strip():
        parts.append(f"About it: {c['description'].strip()}")
    if c["keywords"].strip():
        parts.append(f"Keywords: {c['keywords'].strip()}")
    return "\n".join(parts)


def scope(cid):
    """The category as a RULE for every AI step that finds or judges content (searching, choosing videos, picking
    moments): stay inside it. Empty for Uncategorized."""
    ctx = context(cid)
    if not ctx:
        return ""
    return ("This channel posts ONLY content for this category. Anything outside it is off-topic and must be "
            "rejected, however popular:\n" + ctx)


def search_topic(cid):
    """What to search for when an autopilot has no topic of its own: the category's topic, else its keywords and name."""
    c = get(cid)
    if not c or c["name"] == UNCATEGORIZED:
        return ""
    kws = [k.strip() for k in (c.get("keywords") or "").split(",") if k.strip()][:5]  # the gist: the full list goes in scope()
    return (c.get("niche") or "").strip() or ", ".join([c["name"]] + [k for k in kws if k.lower() not in c["name"].lower()])


def logo(c):
    """The logo on this category's cards: its own upload, else your Settings logo; None when switched off."""
    from . import config
    if not c.get("logo"):
        return None
    own = DATA / c["logo_file"] if c.get("logo_file") else None
    return own if own and own.exists() else config.LOGO


def migrate():
    """Once: the account lists categories had (a JSON column) -> the mapping table; everything without a category
    -> Uncategorized."""
    if not db.get_setting("categories_migrated"):
        for r in db.all("post_categories"):
            ids = [int(i) for i in json.loads(r.get("accounts") or "[]")]
            if ids:
                set_accounts(r["id"], ids)
        db.set_setting("categories_migrated", "1")
    unc = None
    with db.conn() as x:
        for table in ("clips", "posts", "jobs"):
            if x.execute(f"SELECT 1 FROM {table} WHERE category_id IS NULL LIMIT 1").fetchone():
                unc = unc or uncategorized()
                x.execute(f"UPDATE {table} SET category_id=? WHERE category_id IS NULL", (unc,))
