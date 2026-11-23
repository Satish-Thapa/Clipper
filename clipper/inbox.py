"""Reels you send in a DM -> the Cheat page, by themselves. Two inboxes, the same way:
  - Facebook: messages to your Page in Messenger, from Settings `inbox_sender` (the name Facebook shows).
    Needs the Page token's pages_messaging permission.
  - Instagram: DMs to your Instagram account, from Settings `inbox_ig_sender` (the username).
    Needs instagram_manage_messages (and "Allow access to messages" on in the Instagram app).
autopilot's minute loop calls tick(): it reads the latest conversations (Graph API, the Page token), and every NEW
message from that sender with a reel (a typed / shared Facebook, Instagram or YouTube link, or the video file a shared
reel arrives as) becomes a repost job (worker.add_reposts). Settings `inbox_enabled` turns both on / off.

- Only messages that arrive after you turned it on (`since`, per inbox); off forgets it. Each message once.
- What happened is kept per inbox in the DB settings "inbox_state" / "inbox_state_ig" for the Cheat page."""
import json
import re
import time
import urllib.parse
from datetime import datetime

import httpx

from . import db, settings, worker
from .config import FB_PAGE_ID, FB_PAGE_TOKEN, GRAPH_VERSION

GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
KEEP_SEEN = 500
GRACE = 300  # seconds before turning it on that still count (you send the reel, then flip the switch)
INBOXES = {"facebook": "inbox_state", "instagram": "inbox_state_ig"}
NEEDS = {"facebook": "pages_messaging", "instagram": "instagram_manage_messages"}


def state(platform="facebook"):
    raw = db.get_setting(INBOXES[platform])
    return json.loads(raw) if raw else {}


def _save(platform, st):
    db.set_setting(INBOXES[platform], json.dumps(st))


def _unwrap(u):
    """Facebook wraps outside links (l.facebook.com/l.php?u=...): the real link."""
    p = urllib.parse.urlparse(u)
    if p.netloc.lower() in ("l.facebook.com", "lm.facebook.com", "l.instagram.com") and p.path.startswith("/l.php"):
        return urllib.parse.parse_qs(p.query).get("u", [u])[0]
    return u.rstrip(").,!?]")


def links_of(m):
    """Every reel in one message -> [{"url", "title"}]: links typed in the text, shared posts / reels, and the video
    file a shared reel comes as (its title hint: the share's name or the message text)."""
    text = m.get("message") or ""
    found = [(u, "") for u in URL.findall(text)]
    for s in (m.get("shares") or {}).get("data", []):
        found.append((s.get("link") or "", s.get("name") or s.get("description") or ""))
    for a in (m.get("attachments") or {}).get("data", []):
        name = a.get("name") or ""
        for u in (a.get("file_url"), (a.get("video_data") or {}).get("url"), a.get("url"),
                  (a.get("target") or {}).get("url"), (a.get("payload") or {}).get("url")):
            found.append((u or "", (a.get("payload") or {}).get("title") or name))
    out, urls = [], set()
    for u, title in found:
        u = _unwrap(u.strip())
        if u and worker.REEL_LINK.match(u) and u not in urls:
            urls.add(u)
            hint = title if title and not URL.fullmatch(title.strip()) else URL.sub("", text).strip()
            out.append({"url": u, "title": hint[:300]})
    return out


def messages(platform="facebook"):
    """The latest messages of that inbox's latest conversations: [{id, created_time, from, message, shares, attachments}]."""
    fields = ("messages.limit(10){id,created_time,from,message,shares{link,name,description},"
              "attachments{file_url,video_data,name}},updated_time")
    params = {"fields": fields, "limit": 10, "access_token": FB_PAGE_TOKEN}
    if platform == "instagram":
        params["platform"] = "instagram"
    r = httpx.get(f"{GRAPH}/{FB_PAGE_ID}/conversations", timeout=30, params=params)
    j = r.json()
    if r.is_error or "error" in j:
        msg = (j.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        if NEEDS[platform] in msg or "permission" in msg.lower():
            msg = (f"the Page token can't read {platform} messages: make a new one with the {NEEDS[platform]} "
                   f"permission (README: Reels from your messages)")
        raise RuntimeError(msg)
    return [m for c in j.get("data", []) for m in (c.get("messages") or {}).get("data", [])]


def _at(m):
    try:
        return datetime.strptime(m["created_time"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except Exception:
        return 0


def _who(platform, m):
    """The sender as the setting is written: Facebook = the name (spaces tidied, any case); Instagram = the username."""
    f = m.get("from") or {}
    if platform == "instagram":
        return str(f.get("username") or f.get("name") or "").strip().lstrip("@").lower()
    return " ".join(str(f.get("name") or "").split()).casefold()


def _own(platform, m):
    """A message the account itself sent (its replies): never a reel to fetch."""
    f = m.get("from") or {}
    return f.get("id") == FB_PAGE_ID or (platform == "instagram" and f.get("id") and f.get("id") == _ig_id())


def _ig_id():
    from .config import IG_USER_ID
    return IG_USER_ID


def tick_one(platform, sender, now=None, fetch=None):
    """One check of one inbox. -> started job ids."""
    s, st, now = settings.get(), state(platform), now or time.time()
    if not s.inbox_enabled or not sender:
        if st.get("since"):  # turned off: the next "on" starts from then, never from the old messages
            st.pop("since", None)
            _save(platform, st)
        return []
    if not FB_PAGE_ID or not FB_PAGE_TOKEN:
        st.update(checked_at=now, error="Facebook isn't connected (FB_PAGE_ID / FB_PAGE_TOKEN in .env)")
        _save(platform, st)
        return []
    if not st.get("since"):  # a reel sent just before you turned it on (up to GRACE ago) still counts
        st.update(since=now - GRACE, seen=[], error=None)
        _save(platform, st)
        db.syslog(f"reels from {platform} messages: on, watching for messages from {sender}")
        return []
    try:
        msgs = (fetch or (lambda: messages(platform)))()
    except Exception as e:
        st.update(checked_at=now, error=str(e)[:300])
        _save(platform, st)
        return []
    want = sender.casefold() if platform == "facebook" else sender.lower()
    seen, started = set(st.get("seen") or []), []
    for m in sorted(msgs, key=_at):
        mid = m.get("id")
        if not mid or mid in seen or _at(m) < st["since"]:
            continue
        seen.add(mid)
        if _own(platform, m) or _who(platform, m) != want:
            continue
        if platform == "facebook":  # your Messenger id for the Page + when you last wrote (notify.py's 24h window)
            st.update(psid=(m.get("from") or {}).get("id") or st.get("psid"), psid_at=max(_at(m), st.get("psid_at") or 0))
        links = links_of(m)
        if not links:
            continue
        name = (m.get("from") or {}).get("username") or (m.get("from") or {}).get("name") or sender
        got, skipped = worker.add_reposts(links, s.inbox_fill, via=f"a {platform.title()} message from {name}",
                                          source=platform)
        started += got
        db.syslog(f"reels from {platform} messages: {name} sent {len(links)} reel(s): {len(got)} added"
                  + (f", {len(skipped)} skipped ({'; '.join(x['why'] for x in skipped)})" if skipped else ""))
        st["last"] = {"at": _at(m), "links": [x["url"] for x in links], "jobs": got, "skipped": skipped}
    st.update(checked_at=now, error=None, seen=list(seen)[-KEEP_SEEN:])
    _save(platform, st)
    return started


def tick(now=None):
    """Every minute: both inboxes (each only when its sender is set)."""
    s = settings.get()
    return tick_one("facebook", s.inbox_sender, now) + tick_one("instagram", s.inbox_ig_sender, now)


def summary():
    """For /api/state: the switch, the senders and each inbox's last check / error / last reels."""
    s = settings.get()
    keep = ("since", "checked_at", "error", "last")
    from . import notify
    return {"enabled": s.inbox_enabled, "sender": s.inbox_sender, "ig_sender": s.inbox_ig_sender, "fill": s.inbox_fill,
            "notify_reports": s.notify_reports, "notify_uploads": s.notify_uploads,
            "notify": {k: v for k, v in notify.state().items() if k in ("sent_at", "error", "error_at")},
            "facebook": {k: v for k, v in state("facebook").items() if k in keep},
            "instagram": {k: v for k, v in state("instagram").items() if k in keep}}


if __name__ == "__main__":  # self-test: CLIPPER_DATA=<tmp> python -m clipper.inbox
    db.init()
    calls = []
    worker.add_reposts = lambda links, fill, via="", source="": (calls.append((links, fill, source)), ([len(calls)], []))[1]
    settings.save({"inbox_enabled": True, "inbox_sender": "  Main   Account ", "inbox_ig_sender": "@Main.Acct"})
    import clipper.inbox as me
    me.FB_PAGE_ID, me.FB_PAGE_TOKEN = "PAGE", "tok"
    t0 = 1_800_000_000

    def iso(t):
        return datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S+0000")
    fb = [{"id": "old", "created_time": iso(t0 - GRACE - 60), "from": {"name": "Main Account"}, "message": "https://www.facebook.com/reel/1"},
          {"id": "a", "created_time": iso(t0 + 10), "from": {"name": "main account"}, "message": "look https://www.instagram.com/reel/XYZ/ lol"},
          {"id": "b", "created_time": iso(t0 + 20), "from": {"name": "Someone Else"}, "message": "https://youtu.be/abcdefg"},
          {"id": "c", "created_time": iso(t0 + 30), "from": {"name": "Main Account"},
           "shares": {"data": [{"link": "https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com%2Freel%2F99&h=x", "name": "Speed vs cops"}]}},
          {"id": "d", "created_time": iso(t0 + 40), "from": {"id": "PAGE", "name": "Main Account"}, "message": "https://youtu.be/zzzzzzz"},
          {"id": "e", "created_time": iso(t0 + 50), "from": {"name": "Main Account"}, "message": "no link here https://evil.com/x"},
          {"id": "f", "created_time": iso(t0 - 100), "from": {"name": "Main Account"}, "message": "https://www.facebook.com/reel/77"}]
    ig = [{"id": "i1", "created_time": iso(t0 + 15), "from": {"username": "main.acct", "id": "77"}, "message": "Speed crashes the car",
           "attachments": {"data": [{"video_data": {"url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=5&sig=y"}}]}},
          {"id": "i2", "created_time": iso(t0 + 25), "from": {"username": "stranger", "id": "88"}, "message": "https://www.instagram.com/reel/NO/"}]
    assert me.tick_one("facebook", settings.get().inbox_sender, t0, lambda: fb) == []  # first check: remembers the time
    assert me.tick_one("instagram", settings.get().inbox_ig_sender, t0, lambda: ig) == []
    assert me.tick_one("facebook", settings.get().inbox_sender, t0 + 60, lambda: fb) == [1, 2, 3]
    assert calls[0][0] == [{"url": "https://www.facebook.com/reel/77", "title": ""}]  # sent just before "on": still counts
    assert calls[1][0] == [{"url": "https://www.instagram.com/reel/XYZ/", "title": "look  lol"}] and calls[1][2] == "facebook"
    assert calls[2][0] == [{"url": "https://www.facebook.com/reel/99", "title": "Speed vs cops"}]
    assert me.tick_one("instagram", settings.get().inbox_ig_sender, t0 + 60, lambda: ig) == [4]
    assert calls[3][0][0]["url"].startswith("https://lookaside.fbsbx.com/") and calls[3][0][0]["title"] == "Speed crashes the car"
    assert calls[3][2] == "instagram"
    assert me.tick_one("facebook", settings.get().inbox_sender, t0 + 120, lambda: fb) == []  # each message once
    assert me.tick_one("facebook", "Main Account", t0 + 180, lambda: (_ for _ in ()).throw(RuntimeError("boom"))) == []
    assert me.state("facebook")["error"] == "boom" and me.summary()["instagram"]["last"]["jobs"] == [4]
    settings.save({"inbox_enabled": False}); me.tick(t0 + 240)
    assert "since" not in me.state("facebook") and "since" not in me.state("instagram")  # off forgets the start time
    print("inbox ok")
