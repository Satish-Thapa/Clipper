"""The daily quota: the slots every clip posts in, and the one guard every upload passes.

A day's plan (plan()) = one slot per short time + one per long time from Settings; on weekend days each kind you
post at all gets one extra slot (weekend_short_time / weekend_long_time). One slot = one clip, posted at that time to
every platform in Settings.

A clip sits in a slot when its post_at is that slot's time and it's scheduled / uploading / posted. A clip whose
upload failed everywhere gives its slot back. Nothing is queued past today by itself: clips that don't get a slot
wait in Clips ("ready") until you place them (assign) or post them (post_now).

can_post() is the hard limit, checked inside worker.upload_clip for every platform of every upload (slots, Post now,
retries): a platform never gets more shorts (or longs) in one day than that day's slots.

Automatic filling (fill): a Normal / Live job set to "fill today's free slots" and each Auto topic put their best clips
into today's free future slots. While Auto is on and a topic hasn't finished today, its share of the slots
(Settings → Auto: short_slots / long_slots) is kept free for it; you can still place any clip anywhere by hand."""
import json
import threading
import time
from datetime import date, datetime, timedelta

from . import db, settings

KINDS = ("short", "long")
LIVE = ("scheduled", "uploading")  # a clip with one of these statuses holds its slot
LEAD = 60  # seconds: a slot must be at least this far ahead to be given a clip automatically / by hand
_LOCK = threading.Lock()  # assign / fill / post_now: two renders finishing together never take the same slot


def kind_of(c):
    return "long" if c.get("kind") == "long" else "short"  # news / fact posts count as shorts


def at(day, hhmm):
    """The epoch time of HH:MM on `day` (this computer's clock)."""
    return time.mktime(datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time()).timetuple())


def day_of(ts):
    return date.fromtimestamp(ts)


def is_weekend(day, s=None):
    return day.weekday() in (s or settings.get()).weekend_days


def plan(day, s=None):
    """[{"kind", "time", "at", "extra"}] for `day`, in time order."""
    s = s or settings.get()
    out = [{"kind": "short", "time": t, "at": at(day, t), "extra": False} for t in s.short_times]
    out += [{"kind": "long", "time": t, "at": at(day, t), "extra": False} for t in s.long_times]
    if is_weekend(day, s):
        if s.shorts_per_day:
            out.append({"kind": "short", "time": s.weekend_short_time, "at": at(day, s.weekend_short_time), "extra": True})
        if s.longs_per_day:
            out.append({"kind": "long", "time": s.weekend_long_time, "at": at(day, s.weekend_long_time), "extra": True})
    return sorted(out, key=lambda x: x["at"])


def limit(day, kind, s=None):
    return len([x for x in plan(day, s) if x["kind"] == kind])


def _ups(c):
    try:
        return json.loads(c.get("uploads") or "{}") or {}
    except ValueError:
        return {}


def posted_ok(c):
    """True when the clip is up on at least one platform."""
    return any(u.get("id") for u in _ups(c).values())


def posted(day, kind, platform, clips=None):
    """How many clips of `kind` went up to `platform` on `day` (by the time of the successful upload)."""
    n = 0
    for c in clips if clips is not None else db.all("clips"):
        u = _ups(c).get(platform) or {}
        if kind_of(c) == kind and u.get("id") and u.get("at") and day_of(u["at"]) == day:
            n += 1
    return n


def holder(slot, clips):
    """The clip in this slot, or None."""
    for c in clips:
        if kind_of(c) == slot["kind"] and c.get("post_at") and abs(c["post_at"] - slot["at"]) < 1 \
                and (c.get("status") in LIVE or posted_ok(c)):
            return c
    return None


def can_post(c, platform, now=None, clips=None):
    """'' when `platform` may get this clip now, else why not (the daily quota)."""
    day, kind = day_of(now or time.time()), kind_of(c)
    lim, done = limit(day, kind), posted(day, kind, platform, clips)
    if done >= lim:
        return (f"today's {platform} quota is used: {done}/{lim} {kind}(s) posted"
                + (" (weekend +1 included)" if is_weekend(day) else ""))
    return ""


# ---------- who may fill which slot ----------

def _auto_state():
    raw = db.get_setting("auto_day")
    st = json.loads(raw) if raw else {}
    return st if st.get("day") == date.today().isoformat() else {"day": date.today().isoformat(), "rows": {}}


def set_row_state(row, status):
    """Auto: today's state of one topic ('running' / 'done'); a done topic's unused share is free for everyone."""
    st = _auto_state()
    st["rows"][str(row)] = status
    db.set_setting("auto_day", json.dumps(st))


def row_of(c, jobs):
    j = jobs.get(c.get("job_id")) or {}
    return j.get("auto_row") if j.get("mode") == "auto" else None


def reserved(day, kind, clips, jobs, s=None):
    """{row index: slots of `kind` still kept free for that Auto topic today}."""
    s = s or settings.get()
    if not s.auto_enabled or day != date.today():
        return {}
    st = _auto_state()
    left = {i: share_left(day, kind, i, clips, jobs, s) for i in range(len(s.auto_rows)) if st["rows"].get(str(i)) != "done"}
    return {i: n for i, n in left.items() if n > 0}


def share_left(day, kind, row, clips, jobs, s=None):
    """How many more `kind` slots Auto topic `row` may fill on `day`: its share minus the ones it already holds."""
    s = s or settings.get()
    if row >= len(s.auto_rows):
        return 0
    share = s.auto_rows[row].short_slots if kind == "short" else s.auto_rows[row].long_slots
    used = len([c for c in clips if row_of(c, jobs) == row and kind_of(c) == kind and c.get("post_at")
                and day_of(c["post_at"]) == day and (c.get("status") in LIVE or posted_ok(c))])
    return max(0, share - used)


def free_slots(day, kind, now=None, clips=None):
    """Today's / that day's slots of `kind` with no clip, still ahead (LEAD), in time order."""
    now = now or time.time()
    clips = clips if clips is not None else db.all("clips")
    return [x for x in plan(day) if x["kind"] == kind and x["at"] > now + LEAD and not holder(x, clips)]


def fill(cids, row=None, say=None, only=None):
    """Put the best of these clips (by score) into today's free future slots: an Auto topic (row) up to its share,
    anyone else only into slots no unfinished Auto topic is keeping; only = just these slot times (the weekend's
    extra slots). -> {clip id: slot time} given. The rest wait."""
    say = say or (lambda m: None)
    given = {}
    with _LOCK:
        clips, jobs = db.all("clips"), {j["id"]: j for j in db.all("jobs")}
        today, s = date.today(), settings.get()
        mine = sorted([c for c in clips if c["id"] in set(cids) and c.get("path") and c["status"] == "rendered"
                       and not posted_ok(c)], key=lambda c: -(c.get("score") or 0))
        for kind in KINDS:
            want = [c for c in mine if kind_of(c) == kind]
            if not want:
                continue
            free = [f for f in free_slots(today, kind, clips=clips) if only is None or any(abs(f["at"] - o) < 1 for o in only)]
            keep = reserved(today, kind, clips, jobs, s)
            if row is None:  # anyone else: never into a slot a topic keeps (the earliest free ones, as the Slots page shows)
                free = free[sum(keep.values()):]
                budget = len(free)
            else:  # a topic: never past its share
                budget = min(share_left(today, kind, row, clips, jobs, s), len(free))
            for c, slot in zip(want[:budget], free):
                db.update("clips", c["id"], status="scheduled", post_at=slot["at"])
                c.update(status="scheduled", post_at=slot["at"])
                given[c["id"]] = slot["at"]
                say(f"clip #{c['id']} (score {c.get('score') or 0:.0f}) gets today's {t12(slot['time'])} {kind} slot")
            if len(want) > budget:
                say(f"{len(want) - budget} {kind}(s) wait in Clips: "
                    + ("no free slot left today" if not free else f"today's free {kind} slots are kept for Auto topics"
                       if row is None and keep else "this topic's slots are full today"))
    return given


# ---------- by hand ----------

def t12(hhmm):
    """'18:30' -> '6:30 PM' (what the owner reads)."""
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def assign(cid, slot_at, now=None, swap=False):
    """Put a clip into the slot at `slot_at` (any day, never a passed one, same kind only). Raises ValueError saying
    why it can't. swap (drag and drop on the Slots page): a clip already in that slot changes places with this one
    (it goes to this clip's old slot, or back to Clips when this one wasn't in a slot)."""
    now = now or time.time()
    with _LOCK:
        c = db.get("clips", cid)
        if not c:
            raise ValueError("clip not found")
        if not c.get("path") or c["status"] in ("rendering", "uploading"):
            raise ValueError(f"the clip is {c['status']}: wait until it's ready")
        if posted_ok(c):
            raise ValueError("this clip is already posted")
        slot = next((x for x in plan(day_of(slot_at)) if abs(x["at"] - slot_at) < 1), None)
        if not slot:
            raise ValueError("there's no slot at that time (Settings → Slots)")
        if slot["kind"] != kind_of(c):
            raise ValueError(f"that's a {slot['kind']} slot and this clip is a {kind_of(c)}: shorts go in short slots, longs in long slots")
        if slot["at"] <= now + LEAD:
            raise ValueError("that slot's time has passed: pick a later one")
        h = holder(slot, db.all("clips"))
        if h and h["id"] != cid:
            if not swap:
                raise ValueError(f"clip #{h['id']} already has that slot")
            if posted_ok(h) or h["status"] == "uploading":
                raise ValueError("the clip in that slot is already posting")
            old = c["post_at"] if c["status"] == "scheduled" and c.get("post_at") and c["post_at"] > now + LEAD else None
            db.update("clips", h["id"], status="scheduled" if old else "rendered", post_at=old)
            db.log(h["job_id"], f"clip #{h['id']} swapped out of its slot by clip #{cid}"
                                + (f": it moves to {datetime.fromtimestamp(old):%a %d %b %I:%M %p}" if old else ": it waits in Clips"))
        db.update("clips", cid, status="scheduled", post_at=slot["at"])
    db.log(c["job_id"], f"clip #{cid} placed in the {t12(slot['time'])} {slot['kind']} slot of {day_of(slot['at']):%a %d %b}")
    return db.get("clips", cid)


def unassign(cid, why="taken off its slot"):
    c = db.get("clips", cid)
    if c and c["status"] == "scheduled":
        db.update("clips", cid, status="rendered", post_at=None)
        db.log(c["job_id"], f"clip #{cid} {why}: it waits in Clips")
    return db.get("clips", cid)


def claim_now(cid, now=None):
    """Post now: the clip takes one of TODAY's upcoming free slots of its kind (its own, if it already has one today;
    else the next free one; a slot whose time has passed is never used). -> the slot. Raises ValueError when there's
    none left today."""
    now = now or time.time()
    with _LOCK:
        c = db.get("clips", cid)
        if not c or not c.get("path") or c["status"] in ("rendering", "uploading"):
            raise ValueError("the clip isn't ready")
        if posted_ok(c):
            raise ValueError("already posted: use Retry for the platforms that failed")
        today, kind, clips = day_of(now), kind_of(c), db.all("clips")
        mine = next((x for x in plan(today) if x["kind"] == kind and c.get("post_at") and abs(x["at"] - c["post_at"]) < 1), None)
        free = [x for x in plan(today) if x["kind"] == kind and x["at"] > now and not holder(x, clips)]
        slot = mine or (free[0] if free else None)
        if not slot:
            raise ValueError(f"no free {kind} slot left today (today's {limit(today, kind)} are used or passed): put it in a later slot")
        db.update("clips", cid, status="scheduled", post_at=slot["at"])
        return slot


def tick(upload, now=None):
    """Every minute: post each clip whose slot time has come (upload(cid) does it). A clip whose slot was on an
    earlier day (the app was off) goes back to Clips instead."""
    now = now or time.time()
    today = day_of(now)
    for c in sorted(db.all("clips", f"status='scheduled' AND post_at <= {now:.0f}"), key=lambda c: c["post_at"]):
        if day_of(c["post_at"]) < today:
            unassign(c["id"], f"missed its slot ({datetime.fromtimestamp(c['post_at']):%a %d %b %I:%M %p}, the app was off)")
            continue
        db.log(c["job_id"], f"slot time {datetime.fromtimestamp(c['post_at']):%I:%M %p}: posting #{c['id']}")
        upload(c["id"])


def reconcile():
    """After Settings change: clips sitting in slots that no longer exist go back to Clips."""
    for c in db.all("clips", "status='scheduled'"):
        if not any(abs(x["at"] - c["post_at"]) < 1 and x["kind"] == kind_of(c) for x in plan(day_of(c["post_at"]))):
            unassign(c["id"], "lost its slot (the slot times changed)")


def calendar(days=7, now=None):
    """For the Slots page and the dashboard: every slot from today on, with its clip and state."""
    now = now or time.time()
    clips, jobs, s = db.all("clips"), {j["id"]: j for j in db.all("jobs")}, settings.get()
    out = []
    for k in range(days):
        day = day_of(now) + timedelta(days=k)
        keep = {kind: reserved(day, kind, clips, jobs, s) for kind in KINDS}
        left = {kind: sum(v.values()) for kind, v in keep.items()}
        slots = []
        for x in plan(day, s):
            c = holder(x, clips)
            state = ("posted" if c and posted_ok(c) else "uploading" if c and c["status"] == "uploading" else
                     "scheduled" if c else "missed" if x["at"] <= now else "free")
            res = None
            if state == "free" and left[x["kind"]] > 0:  # the earliest free slots are the ones Auto keeps
                left[x["kind"]] -= 1
                res = "Auto"
            slots.append({**x, "state": state, "reserved": res,
                          "clip": c and {k: c.get(k) for k in ("id", "title", "headline", "kind", "score", "path", "uploads", "status")}})
        out.append({"day": day.isoformat(), "weekday": day.strftime("%a"), "weekend": is_weekend(day, s), "slots": slots,
                    "used": {kind: max([posted(day, kind, p, clips) for p in settings.platforms_for(kind, s)] or [0])
                             for kind in KINDS},
                    "limit": {kind: limit(day, kind, s) for kind in KINDS}})
    return out


if __name__ == "__main__":
    import os
    if "CLIPPER_DATA" not in os.environ:
        raise SystemExit("run as: CLIPPER_DATA=<empty temp dir> python -m clipper.quota")
    db.init()
    settings.save({"shorts_per_day": 2, "short_times": ["10:00", "18:00"], "longs_per_day": 1, "long_times": ["20:00"],
                   "weekend_days": [4, 5], "platforms": ["youtube", "facebook"]})
    fri, mon = date(2026, 11, 20), date(2026, 11, 23)
    assert [(x["kind"], x["time"]) for x in plan(mon)] == [("short", "10:00"), ("short", "18:00"), ("long", "20:00")]
    assert [x["time"] for x in plan(fri) if x["extra"]] == ["21:00", "22:00"] and limit(fri, "short") == 3
    settings.save({"longs_per_day": 0, "long_times": []})
    assert limit(fri, "long") == 0, "no extra weekend long when you post no longs"
    settings.save({"longs_per_day": 1, "long_times": ["20:00"]})

    j = db.insert("jobs", url="u", shorts=1, longs=0, auto_upload=0, created=time.time(), mode="normal")
    C = lambda **kw: db.insert("clips", job_id=j, kind=kw.pop("kind", "short"), start=0, end=30, score=kw.pop("score", 50),
                               title="t", description="", tags="[]", reason="", path="x.mp4", status="rendered", **kw)
    now = at(mon, "09:00")
    a, b, c3 = C(score=90), C(score=80), C(score=70)
    up = lambda p, t: json.dumps({p: {"id": "v", "at": t}})
    # the hard limit, per platform, per day
    x = {"kind": "short"}
    assert can_post(x, "youtube", now, [{"kind": "short", "uploads": up("youtube", now)}]) == ""
    two = [{"kind": "short", "uploads": up("youtube", now - 60)}, {"kind": "short", "uploads": up("youtube", now - 30)}]
    assert "quota is used: 2/2" in can_post(x, "youtube", now, two) and can_post(x, "facebook", now, two) == ""
    assert can_post({"kind": "long"}, "youtube", now, two) == ""
    yesterday = [{"kind": "short", "uploads": up("youtube", now - 86400)}] * 5
    assert can_post(x, "youtube", now, yesterday) == ""
    # by hand
    slot10, slot18 = at(mon, "10:00"), at(mon, "18:00")
    assign(a, slot10, now)
    for bad, msg in ((lambda: assign(b, slot10, now), "already has that slot"), (lambda: assign(b, at(mon, "20:00"), now), "long slot"),
                     (lambda: assign(b, at(mon, "11:00"), now), "no slot at that time"),
                     (lambda: assign(b, slot10, at(mon, "12:00")), "has passed")):
        try:
            bad()
            raise AssertionError(msg)
        except ValueError as e:
            assert msg in str(e), (msg, e)
    assert [s_["time"] for s_ in free_slots(mon, "short", now)] == ["18:00"]
    unassign(a)
    assert db.get("clips", a)["status"] == "rendered"
    # post now takes the earliest free slot of today; when they're all held, it refuses
    assert claim_now(a, at(mon, "12:00"))["time"] == "18:00"   # 10:00 passed: never used, the next free one is
    try:
        claim_now(b, at(mon, "12:00"))
        raise AssertionError("no upcoming short slot left today")
    except ValueError as e:
        assert "no free short slot left today" in str(e)
    db.update("clips", a, post_at=at(mon, "10:00"))  # the rest of the test: a holds 10:00, b holds 18:00
    db.update("clips", b, status="scheduled", post_at=at(mon, "18:00"))
    # drag and drop: swap two slotted clips; a ready clip dropped on a taken slot sends that one back to Clips
    tue = mon + timedelta(days=1)
    assign(c3, at(tue, "10:00"), now)
    x = C(score=60)
    assign(x, at(tue, "18:00"), now)
    assign(c3, at(tue, "18:00"), now, swap=True)
    assert db.get("clips", c3)["post_at"] == at(tue, "18:00") and db.get("clips", x)["post_at"] == at(tue, "10:00")
    y = C(score=55)
    assign(y, at(tue, "10:00"), now, swap=True)
    assert db.get("clips", x)["status"] == "rendered" and db.get("clips", y)["post_at"] == at(tue, "10:00")
    for cid in (c3, x, y):
        db.update("clips", cid, status="rendered", post_at=None)
    assert t12("18:30") == "6:30 PM" and t12("09:05") == "9:05 AM"
    # tick: due today -> uploaded; an earlier day's slot -> back to Clips
    sent = []
    fake = lambda cid: (sent.append(cid), db.update("clips", cid, status="uploaded", uploads=up("youtube", at(mon, "12:00"))))
    tick(fake, at(mon, "12:00"))
    assert sent == [a], sent
    db.update("clips", b, post_at=at(mon - timedelta(days=1), "18:00"))
    tick(fake, at(mon, "12:00"))
    assert db.get("clips", b)["status"] == "rendered" and sent == [a]
    assert calendar(1, at(mon, "12:00"))[0]["slots"][0]["state"] == "posted"  # 10:00 holds the posted clip
    # reconcile: a slot time removed from Settings
    db.update("clips", c3, status="scheduled", post_at=at(mon, "18:00"))
    settings.save({"short_times": ["10:00", "19:00"]})
    reconcile()
    assert db.get("clips", c3)["status"] == "rendered"
    print("quota ok")
