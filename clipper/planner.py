"""The page's posting plan, used both to SHOW the times (Scheduled tab) and to POST on them (dispatch, every
minute), so what you see is what happens.

The plan (page.py): N = clips a day + posts a day, spread evenly over the posting hours (one every window / N,
never closer than Settings → "Minutes between posts"). Each slot goes to whichever kind (clip / post) is further
behind its daily number, and within the kind to the category furthest behind its share; that category's oldest
waiting item takes it. Clips go to the page's YouTube / Instagram / Facebook (the category's platforms); posts to
its Facebook / Instagram. A paused category's queued items still go out; the plan switched off holds everything.
Items with a fixed time (Create → Schedule) keep it and are posted by worker.publish_due / posts.tick.

plan(now)      -> {("clip"|"post", id): time}
dispatch(now)  -> posts the first item whose time has come (one per call)"""
import json
import time

from . import autopilot, brands, config, db, page, social, worker


def _window(p, t):
    day = autopilot._day_start(t)
    return day + int(p["from"]) * 3600, day + int(p["to"]) * 3600


def _fit(p, t):
    """t moved into the posting hours (the next day's start when past the end)."""
    w0, w1 = _window(p, t)
    if t < w0:
        return w0
    return t if t < w1 else w0 + 86400


PRIORITY = "you"  # the queue line of clips you made by hand: always next, even past the day's number


def _queue(bid):
    """Waiting items of the brand's categories, oldest first: {"clip": {cat: [ids]}, "post": {cat: [ids]}}."""
    mine = {c["id"] for c in page.cats(bid)}
    q = {"clip": {}, "post": {}}
    for c in reversed(db.all("clips", "status='scheduled' AND publish_at IS NULL")):
        if c["category_id"] in mine:  # clips you made by hand (a link, a live stream) wait in their own line, first
            key = PRIORITY if json.loads(c.get("features") or "{}").get("priority") else c["category_id"]
            q["clip"].setdefault(key, []).append(c["id"])
    for p in reversed(db.all("posts", "status='ready' AND publish_at IS NULL")):
        if p["category_id"] in mine:
            q["post"].setdefault(p["category_id"], []).append(p["id"])
    return q


def plan(now=None, bid=None):
    now = now or time.time()
    bid = bid or brands.current()
    p = page.plan(bid)
    if not p["on"]:
        return {}
    caps = {"clip": int(p["clips_per_day"]), "post": int(p["posts_per_day"])}
    n = max(1, caps["clip"] + caps["post"])
    every = max((int(p["to"]) - int(p["from"])) * 3600 / n, int(config.POST_GAP_MIN or 0) * 60)
    weights = {k: page.shares(k, bid) for k in caps}
    q = _queue(bid)
    accts = [a["id"] for a in page.page_accounts(bid)]
    last = social.last_post_at(accts) if accts else 0
    day = autopilot._day_start(now)
    mine = {c["id"] for c in page.cats(bid)}
    done = {k: {c: n for c, n in v.items() if c in mine} for k, v in page.posted_today(day).items()}  # this page's only
    out, t = {}, now
    for _ in range(sum(len(v) for kind in q.values() for v in kind.values()) + 7):
        if not any(ids for kind in q.values() for ids in kind.values()):
            break
        t = _fit(p, max(t, last + every))
        if autopilot._day_start(t) != day:  # a new day: fresh daily numbers
            day = autopilot._day_start(t)
            done = {"clip": {}, "post": {}}
        counts = {k: sum(done[k].values()) for k in caps}
        if q["clip"].get(PRIORITY):  # your own clips: the next slot, whatever the day's numbers say
            iid = q["clip"][PRIORITY].pop(0)
            out[("clip", iid)] = t
            last = t
            continue
        kinds = [k for k in caps if counts[k] < caps[k] and any(q[k].values())]
        if not kinds:
            t = _window(p, t)[0] + 86400  # today's numbers reached (or only the other kind is waiting): tomorrow
            continue
        kind = min(kinds, key=lambda k: (counts[k] / max(caps[k], 1), k))
        w = weights[kind]
        cands = [c for c, ids in q[kind].items() if ids]
        # furthest behind its share first; categories with no share (paused / 0%) still empty their queue, last
        cat = min(cands, key=lambda c: (done[kind].get(c, 0) / w[c] if w.get(c) else 1e9 + done[kind].get(c, 0), c))
        iid = q[kind][cat].pop(0)
        out[(kind, iid)] = t
        done[kind][cat] = done[kind].get(cat, 0) + 1
        last = t
    return out


def _clip_dests(c, bid):
    """The page's accounts for this clip: its category's platforms, each in its usual format."""
    chosen = json.loads(c.get("targets") or "[]")
    if chosen:  # accounts you picked for it in Create
        return chosen
    r = page.recipe(c["category_id"])
    seconds, out = c["end"] - c["start"], []
    for a in page.page_accounts(bid, r["platforms"]):
        fmt = "facebook_post" if a["platform"] == "facebook" and seconds > social.FB_REEL_MAX else social.DEFAULT_FORMAT[a["platform"]]
        out.append({"account": a["id"], "format": fmt})
    return out


def dispatch(now=None, bid=None):
    """Post the earliest item whose planned time has come (one per call, so the plan is fresh for the next).
    -> ("clip"|"post", id) or None."""
    from . import posts as P
    now = now or time.time()
    bid = bid or brands.current()
    due = sorted((t, k) for k, t in plan(now, bid).items() if t <= now + 20)
    if not due:
        return None
    _, (kind, iid) = due[0]
    if kind == "clip":
        c = db.get("clips", iid)
        dests = _clip_dests(c, bid)
        db.log(c["job_id"], f"#{iid}: posting on its planned slot to {len(dests)} account(s)")
        if not dests:
            db.update("clips", iid, status="upload failed")
            db.log(c["job_id"], f"#{iid}: no connected page account to post to (Accounts)")
            return kind, iid
        until = social.paused_until([d["account"] for d in dests], now)
        if until:  # all its accounts are paused by a limit: timed for when it ends (publish_due), not lost
            db.update("clips", iid, publish_at=until + 60, targets=json.dumps(dests))
            db.log(c["job_id"], f"#{iid}: its accounts are paused by a limit; timed for "
                                f"{time.strftime('%d %b %H:%M', time.localtime(until + 60))}")
            return kind, iid
        aid = autopilot.clip_aid(c)
        if aid:  # its producer's count (how much it has made / posted today)
            times = [x for x in autopilot._posts(aid) if x > now - 3 * 86400] + [now]
            db.set_setting(f"auto_posts_{aid}", json.dumps(times))
        worker.publish_clip(iid, dests, skip_limited=True)
    else:
        p = db.get("posts", iid)
        live = [a["id"] for a in page.page_accounts(bid, page.POST_PLATFORMS)]
        chosen = json.loads(p.get("targets") or "[]")
        ids = [x for x in chosen if x in live] or live if chosen else live
        db.log(iid, "posting on its planned slot", "posts")
        if not ids:
            db.update("posts", iid, status="failed")
            db.log(iid, "no connected Facebook / Instagram account to post to (Accounts)", "posts")
            return kind, iid
        if p.get("auto_id"):
            db.set_setting(f"posts_times_a{p['auto_id']}",
                           json.dumps([x for x in P._atimes(p["auto_id"]) if x > now - 3 * 86400] + [now]))
        P.locked(P.publish, iid, ids, auto=True)
    return kind, iid
