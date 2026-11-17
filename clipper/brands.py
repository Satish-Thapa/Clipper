"""Brands (pages): each brand has its own connected social accounts and its own categories, so one Clipper can run
several channels / pages without mixing them up. Clips and posts belong to a brand through their category.

There is always at least one brand: "Demo channel" on a fresh install (rename it, or add yours and delete it).
The brand you're working in is remembered (setting "current_brand"); new accounts and categories go to it, and the
app's lists show only its things. The background loops (autopilot, auto posts, scheduled publishing) run for every
brand regardless."""
import time

from . import db

DEMO = "Demo channel"


def all():
    """Every brand, oldest first, with how many accounts and categories it has."""
    with db.conn() as c:
        acc = dict(c.execute("SELECT brand_id, COUNT(*) FROM accounts GROUP BY brand_id").fetchall())
        cat = dict(c.execute("SELECT brand_id, COUNT(*) FROM post_categories GROUP BY brand_id").fetchall())
    return [{**b, "accounts": acc.get(b["id"], 0), "categories": cat.get(b["id"], 0)} for b in reversed(db.all("brands"))]


def get(bid):
    return db.get("brands", int(bid)) if bid else None


def current():
    """The brand you're working in (made if there is none yet)."""
    bid = int(db.get_setting("current_brand") or 0)
    if bid and get(bid):
        return bid
    rows = list(reversed(db.all("brands")))
    bid = rows[0]["id"] if rows else db.insert("brands", name=DEMO, created=time.time())
    db.set_setting("current_brand", str(bid))
    return bid


def set_current(bid):
    if not get(bid):
        raise ValueError("brand not found")
    db.set_setting("current_brand", str(int(bid)))
    return int(bid)


def create(name):
    """A new brand; it becomes the one you're working in."""
    name = str(name or "").strip()[:60]
    if not name:
        raise ValueError("give the brand a name")
    bid = db.insert("brands", name=name, created=time.time())
    set_current(bid)
    db.syslog(f"brands: created '{name}'")
    return bid


def rename(bid, name):
    name = str(name or "").strip()[:60]
    if not name or not get(bid):
        raise ValueError("give the brand a name")
    db.update("brands", int(bid), name=name)


def delete(bid, move_to=None):
    """Delete a brand. Only when another brand exists. Its accounts and categories move to `move_to` (default: the
    oldest other brand); its Uncategorized items join that brand's Uncategorized. Nothing published is touched."""
    from . import categories
    bid = int(bid)
    others = [b for b in db.all("brands") if b["id"] != bid]
    if not get(bid):
        raise ValueError("brand not found")
    if not others:
        raise ValueError("add another brand first: there must always be one")
    dest = int(move_to) if move_to and get(move_to) and int(move_to) != bid else others[-1]["id"]
    with db.conn() as c:
        c.execute("UPDATE accounts SET brand_id=? WHERE brand_id=?", (dest, bid))
    for cat in categories.all(brand_id=bid):
        if cat["name"] == categories.UNCATEGORIZED:  # merge into the destination's Uncategorized
            target = categories.uncategorized(dest)
            with db.conn() as c:
                for table in ("clips", "posts", "jobs", "topics"):
                    c.execute(f"UPDATE {table} SET category_id=? WHERE category_id=?", (target, cat["id"]))
                c.execute("DELETE FROM category_accounts WHERE category_id=?", (cat["id"],))
            db.delete("post_categories", cat["id"])
        else:
            db.update("post_categories", cat["id"], brand_id=dest)
    db.delete("brands", bid)
    if int(db.get_setting("current_brand") or 0) == bid:
        set_current(dest)
    db.syslog(f"brands: deleted brand {bid}, its accounts and categories moved to brand {dest}")
    return dest


def migrate():
    """Make sure a brand exists; everything made before brands belongs to the first one."""
    first = current()
    with db.conn() as c:
        c.execute("UPDATE accounts SET brand_id=? WHERE brand_id IS NULL", (first,))
        c.execute("UPDATE post_categories SET brand_id=? WHERE brand_id IS NULL", (first,))
