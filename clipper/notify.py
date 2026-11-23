"""Messages to you in Messenger (replaces the old WhatsApp): the daily report and every upload, sent by your Facebook
Page to your own account (Settings: notify_reports, notify_uploads; the recipient is the Cheat page's Facebook sender,
`inbox_sender`). Uses the Page token's pages_messaging permission (the same one the inbox needs).

- Your Messenger id for the Page (PSID) is found once: from a message you sent the Page (inbox.py remembers it), else
  from the Page's conversations by your name. Send the Page any message once if it isn't found.
- Messenger's rule: a Page may message you freely for 24h after your last message to it; after that it sends with
  the ACCOUNT_UPDATE tag (updates about your own account). If Meta refuses, the reason is kept (Cheat page) and
  sending a message to the Page opens the window again.
- Sent in a thread: a slow Messenger never holds up an upload. State: DB setting "notify_state"."""
import json
import threading
import time
from datetime import date, timedelta

import httpx

from . import db, settings
from .config import FB_PAGE_ID, FB_PAGE_TOKEN, GRAPH_VERSION

GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
PLAT = {"youtube": "YouTube", "facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok"}
MAX = 1900  # Messenger takes 2000 characters per message


def state():
    raw = db.get_setting("notify_state")
    return json.loads(raw) if raw else {}


def _save(**kw):
    st = state()
    st.update(kw)
    db.set_setting("notify_state", json.dumps(st))


def _num(n):
    n = int(n or 0)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e4 else f"{n:,}"


def recipient():
    """Your PSID (Page-scoped id): remembered from your messages to the Page, else looked up by name."""
    from . import inbox
    st = inbox.state("facebook")
    if st.get("psid"):
        return st["psid"], st.get("psid_at") or 0
    name = " ".join(settings.get().inbox_sender.split()).casefold()
    if not name:
        raise RuntimeError("set your Facebook name on the Cheat page (Reels from your messages: Facebook sender)")
    r = httpx.get(f"{GRAPH}/{FB_PAGE_ID}/conversations", timeout=30,
                  params={"fields": "participants,updated_time", "limit": 50, "access_token": FB_PAGE_TOKEN}).json()
    if "error" in r:
        raise RuntimeError(_why(r["error"].get("message", "")))
    for c in r.get("data", []):
        for p in (c.get("participants") or {}).get("data", []):
            if p.get("id") != FB_PAGE_ID and " ".join(str(p.get("name") or "").split()).casefold() == name:
                st.update(psid=p["id"])
                inbox._save("facebook", st)
                return p["id"], 0
    raise RuntimeError(f"no conversation with {settings.get().inbox_sender} yet: send your Page any message once in Messenger")


def _why(msg):
    if "pages_messaging" in msg or "permission" in msg.lower():
        return "the Page token can't send messages: it needs the pages_messaging permission (README)"
    if "outside of allowed window" in msg or "24 hour" in msg or "(#10)" in msg:
        return "Messenger's 24h window is closed: send your Page any message to open it again"
    return msg[:300]


def _chunks(text):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(line) > MAX:  # a very long line: what came before goes first, then the line in pieces
            out.append(cur)
            cur = ""
            while len(line) > MAX:
                out.append(line[:MAX])
                line = line[MAX:]
        if len(cur) + len(line) + 1 > MAX:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    return [x for x in out + [cur] if x.strip()]


def send_now(text, post=None, to=None):
    """Send `text` to you now (blocking). -> True when Messenger took it. post: a stand-in for httpx.post (tests).
    to: reply to this sender id (Messenger PSID or Instagram IGSID) who just wrote (the 24h window is open);
    default: your Messenger (recipient())."""
    post = post or httpx.post
    if not FB_PAGE_ID or not FB_PAGE_TOKEN:
        _save(error="Facebook isn't connected (FB_PAGE_ID / FB_PAGE_TOKEN in .env)", error_at=time.time())
        return False
    try:
        psid, last_from = (to, time.time()) if to else recipient()
        open_window = time.time() - (last_from or 0) < 23.5 * 3600
        for part in _chunks(text):
            body = {"recipient": {"id": psid}, "message": {"text": part}, "access_token": FB_PAGE_TOKEN,
                    **({"messaging_type": "RESPONSE"} if open_window else {"messaging_type": "MESSAGE_TAG", "tag": "ACCOUNT_UPDATE"})}
            r = post(f"{GRAPH}/{FB_PAGE_ID}/messages", json=body, timeout=30)
            j = r.json() if hasattr(r, "json") else {}
            if getattr(r, "is_error", False) or "error" in j:
                raise RuntimeError(_why((j.get("error") or {}).get("message") or f"HTTP {getattr(r, 'status_code', '?')}"))
        _save(sent_at=time.time(), error=None, last=text[:200])
        return True
    except Exception as e:
        _save(error=str(e)[:300], error_at=time.time())
        db.syslog(f"Messenger message not sent: {e}")
        return False


def send(text, to=None):
    threading.Thread(target=send_now, args=(text,), kwargs={"to": to}, daemon=True).start()


def uploaded(c, done):
    """After an upload (worker.upload_clip): what went up where, with the links, and what failed."""
    if not settings.get().notify_uploads:
        return
    ok = [(p, u) for p, u in done.items() if u.get("id")]
    bad = [(p, u) for p, u in done.items() if u.get("error")]
    if not ok and not bad:
        return
    secs = int((c.get("end") or 0) - (c.get("start") or 0))
    lines = [("Posted" if ok else "Upload failed") + f": \"{c.get('title')}\" ({c.get('kind')}, {secs // 60}:{secs % 60:02d}, "
             f"virality {round(c.get('score') or 0)})"]
    lines += [f"{PLAT.get(p, p)}: {u.get('url')}" for p, u in ok]
    lines += [f"{PLAT.get(p, p)} failed: {str(u.get('error'))[:200]}" for p, u in bad]
    send("\n".join(lines))


def report(decisions=None):
    """The daily report (after autopilot.daily_report), when Settings notify_reports is on."""
    if settings.get().notify_reports:
        send(report_text(decisions))


def reply_report(to, platform="facebook"):
    """You wrote "report" in a DM: the report now, back in that chat (whatever the notify setting says)."""
    from . import insights
    db.syslog(f"report asked in a {platform} DM: sending it")
    send(report_text(insights.decisions(), asked=True), to=to)


def report_text(decisions=None, asked=False):
    """The report: views (yesterday + all-time), what was posted, the best clip, today's slots, the AI's summary.
    asked: on demand (a DM said "report"): also what went up today so far."""
    from . import quota, stats
    clips, today = db.all("clips"), date.today()
    yday = today - timedelta(days=1)
    lines = [f"Clipper report, {time.strftime('%a %d %b, %I:%M %p')}" if asked else f"Clipper daily report, {today.strftime('%a %d %b')}"]
    try:
        row = next((r for r in reversed(stats.series(3)) if r["day"] == yday.isoformat()), None) or {}
        got = {p: v for p, v in row.items() if p != "day" and v}
        if got:
            lines.append("Views yesterday: " + " · ".join(f"{PLAT.get(p, p)} {_num(v)}" for p, v in got.items()))
    except Exception:
        pass
    tot = stats.totals(clips)
    if tot:
        lines.append("All-time views: " + " · ".join(f"{PLAT.get(p, p)} {_num(v)}" for p, v in tot.items() if v))
    posted = [c for c in clips if any(u.get("id") and u.get("at") and date.fromtimestamp(u["at"]) == yday
                                      for u in json.loads(c.get("uploads") or "{}").values())]
    lines.append(f"Posted yesterday: {len(posted)} clip(s)")
    if asked:
        now_posted = [c for c in clips if any(u.get("id") and u.get("at") and date.fromtimestamp(u["at"]) == today
                                              for u in json.loads(c.get("uploads") or "{}").values())]
        lines.append(f"Posted today so far: {len(now_posted)} clip(s)"
                     + "".join(f"\n- {c.get('title')}" for c in now_posted[:8]))
    views = lambda c: sum((json.loads(c.get("views") or "{}") or {}).values() or [0])
    best = max(clips, key=views, default=None)
    if best and views(best):
        lines.append(f"Best clip so far: \"{best.get('title')}\" ({_num(views(best))} views)")
    plan, now = quota.plan(today), time.time()
    filled = sum(1 for c in clips if c.get("status") == "scheduled" and c.get("post_at") and date.fromtimestamp(c["post_at"]) == today)
    ahead = [x for x in plan if x["at"] > now]
    lines.append(f"Today: {len(plan)} slot(s), {len(ahead)} still ahead, {filled} with a clip waiting")
    d = decisions or {}
    if d.get("summary"):
        lines.append(f"AI: {d['summary']}")
    for s in d.get("suggestions") or []:
        lines.append(f"- {s}")
    return "\n".join(lines)


if __name__ == "__main__":  # self-test: CLIPPER_DATA=<tmp> python -m clipper.notify
    import clipper.notify as me
    db.init()
    me.FB_PAGE_ID, me.FB_PAGE_TOKEN = "PAGE", "tok"
    from clipper import inbox
    inbox._save("facebook", {"psid": "U1", "psid_at": time.time() - 3600})
    sent = []

    class R:
        is_error, status_code = False, 200

        def __init__(s, j): s.j = j
        def json(s): return s.j
    ok = me.send_now("hello\n" + "x" * 2500, post=lambda url, json=None, timeout=None: (sent.append(json), R({"message_id": "m"}))[1])
    assert ok and len(sent) == 3 and sent[0]["recipient"]["id"] == "U1" and sent[0]["messaging_type"] == "RESPONSE"
    inbox._save("facebook", {"psid": "U1", "psid_at": time.time() - 30 * 3600}); sent.clear()
    me.send_now("later", post=lambda url, json=None, timeout=None: (sent.append(json), R({}))[1])
    assert sent[0]["messaging_type"] == "MESSAGE_TAG" and sent[0]["tag"] == "ACCOUNT_UPDATE"
    assert not me.send_now("x", post=lambda url, json=None, timeout=None: R({"error": {"message": "(#10) This message is sent outside of allowed window"}}))
    assert "24h window" in me.state()["error"]
    settings.save({"notify_uploads": True})
    got = []
    me.send = got.append
    me.uploaded({"title": "T", "kind": "short", "start": 0, "end": 45, "score": 91},
                {"facebook": {"id": "1", "url": "https://fb/1"}, "instagram": {"error": "too long"}})
    assert "Posted: \"T\" (short, 0:45, virality 91)" in got[0] and "Facebook: https://fb/1" in got[0] and "Instagram failed: too long" in got[0]
    settings.save({"notify_reports": True})
    me.report({"summary": "fails win", "suggestions": ["more GTA"]})
    assert got[1].startswith("Clipper daily report") and "AI: fails win" in got[1] and "- more GTA" in got[1]
    print("notify ok")
