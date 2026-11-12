"""View counts for uploaded clips + an hourly WhatsApp report (Meta WhatsApp Cloud API, free: see SOCIALS.md).

refresh() asks each platform for the views of every clip we uploaded and stores them in clips.views (JSON
{platform: views}). report() turns that into a short message: totals per platform, the change since the last
report, the best platform and the top clips. loop() does both every REPORT_MINUTES."""
import json
import re
import threading
import time

import httpx

from . import db, upload as youtube
from .config import (FB_PAGE_TOKEN, GRAPH_VERSION, IG_TOKEN, REPORT_MINUTES, WA_PHONE_NUMBER_ID, WA_TEMPLATE,
                     WA_TEMPLATE_LANG, WA_TO, WA_TOKEN)

GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
NAMES = {"youtube": "YouTube", "facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok"}
LAST = {"totals": None, "at": 0, "errors": {}}  # totals at the previous report, for "+N since last time"


def whatsapp_ready():
    return bool(WA_PHONE_NUMBER_ID and WA_TOKEN and WA_TO)


def _wa(payload):
    r = httpx.post(f"{GRAPH}/{WA_PHONE_NUMBER_ID}/messages", timeout=30, headers={"Authorization": f"Bearer {WA_TOKEN}"},
                   json={"messaging_product": "whatsapp", "to": WA_TO, **payload})
    return r, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})


def send_whatsapp(text):
    """WhatsApp only delivers free text within 24h of YOU messaging the sender number, and when that window is closed
    the API still answers 200 (the failure only shows up later, in a webhook we don't have). So:
    - WA_TEMPLATE set (recommended): always send that approved template; its one {{1}} variable gets the report on
      one line, " | " between lines (template variables can't contain line breaks). Always delivered.
    - no template: plain text with line breaks, only delivered inside the 24h window."""
    if WA_TEMPLATE:
        one_line = re.sub(r" {4,}", "   ", re.sub(r"\s*\n+\s*", " | ", text).replace("\t", " "))[:1000]
        r, j = _wa({"type": "template", "template": {"name": WA_TEMPLATE, "language": {"code": WA_TEMPLATE_LANG},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": one_line}]}]}})
        how = "template"
    else:
        r, j = _wa({"type": "text", "text": {"body": text, "preview_url": False}})
        how = "text (only delivered within 24h of you messaging the number; set WA_TEMPLATE)"
    if r.is_error:
        raise RuntimeError(f"WhatsApp: HTTP {r.status_code} {(j.get('error') or {}).get('message') or r.text[:300]}")
    return how


def start_loop():
    if whatsapp_ready():
        threading.Thread(target=_loop, daemon=True).start()


def _loop():
    while True:
        time.sleep(REPORT_MINUTES * 60)
        try:
            send_report()
        except Exception as e:
            db.syslog(f"whatsapp report failed: {e!r}", exc=True)


def _insight(media, metric, token):
    """First value of one Graph API insights metric (Facebook video / Instagram media)."""
    r = httpx.get(f"{GRAPH}/{media}/{'video_insights' if metric != 'views' else 'insights'}",
                  params={"metric": metric, "access_token": token}, timeout=20)
    r.raise_for_status()
    data = r.json().get("data") or [{}]
    return int((data[0].get("values") or [{}])[0].get("value") or 0)


def _facebook(vid):
    for metric in ("blue_reels_play_count", "total_video_views"):  # Reel, else a normal Page video
        try:
            return _insight(vid, metric, FB_PAGE_TOKEN)
        except httpx.HTTPError:
            continue
    raise RuntimeError("facebook: no view metric for this video")


def refresh():
    """Fetch views for every uploaded clip -> {platform: error} for platforms that failed (shown in the report)."""
    clips = [c for c in db.all("clips") if c.get("uploads") and c["uploads"] != "{}"]
    ups = {c["id"]: json.loads(c["uploads"]) for c in clips}
    views = {c["id"]: json.loads(c.get("views") or "{}") for c in clips}
    errors = {}
    yt = {u["youtube"]["id"]: cid for cid, u in ups.items() if u.get("youtube", {}).get("id")}
    if yt and not youtube.connected():
        errors["youtube"] = "not connected: click Connect YouTube on the Socials page"
    elif yt:
        try:
            for vid, n in youtube.views(list(yt)).items():
                views[yt[vid]]["youtube"] = n
        except Exception as e:
            errors["youtube"] = str(e)[:150]
    for cid, u in ups.items():
        for p, fetch in (("facebook", _facebook), ("instagram", lambda m: _insight(m, "views", IG_TOKEN))):
            if u.get(p, {}).get("id"):
                try:
                    views[cid][p] = fetch(u[p]["id"])
                except Exception as e:
                    errors[p] = str(e)[:150]
        if u.get("tiktok", {}).get("id"):
            # TikTok's API only gives view counts for public posts listed with the video.list scope; app posts stay
            # private until TikTok audits the app, so there is nothing to read yet.
            errors["tiktok"] = "TikTok doesn't report views for private app posts yet"
    for cid, v in views.items():
        db.update("clips", cid, views=json.dumps(v))
    LAST["errors"] = errors
    db.syslog(f"views refreshed for {len(views)} clips: {totals()}" + (f", errors: {errors}" if errors else ""))
    return errors


def totals(clips=None):
    clips = clips if clips is not None else db.all("clips")
    out = {}
    for c in clips:
        for p, n in json.loads(c.get("views") or "{}").items():
            out[p] = out.get(p, 0) + n
    return out


def report_text(clips, now_totals, before, errors, top=5):
    """The WhatsApp message (plain text, *bold* works in WhatsApp)."""
    lines = [f"*Clipper report* {time.strftime('%d %b %H:%M')}"]
    if not now_totals:
        lines.append("No view counts yet.")
    for p, n in sorted(now_totals.items(), key=lambda x: -x[1]):
        gain = f" (+{n - before.get(p, 0):,})" if before is not None else ""
        lines.append(f"{NAMES.get(p, p)}: {n:,} views{gain}")
    if now_totals:
        best = max(now_totals, key=now_totals.get)
        lines.append(f"Best platform: *{NAMES.get(best, best)}*")
    ranked = sorted(clips, key=lambda c: -sum(json.loads(c.get("views") or "{}").values()))
    ranked = [c for c in ranked if json.loads(c.get("views") or "{}")][:top]
    if ranked:
        lines.append("")
        lines.append("*Top clips*")
    for i, c in enumerate(ranked, 1):
        v = json.loads(c["views"])
        best = max(v, key=v.get)
        per = ", ".join(f"{NAMES.get(p, p)} {n:,}" for p, n in sorted(v.items(), key=lambda x: -x[1]))
        lines.append(f"{i}. {c['title'][:60]}: {sum(v.values()):,} ({per}; best on {NAMES.get(best, best)})")
    for p, e in errors.items():
        lines.append(f"! {NAMES.get(p, p)}: {e}")
    return "\n".join(lines)[:1500]


def send_report():
    """Refresh views, build the message, send it on WhatsApp. Returns the text that was sent."""
    if not whatsapp_ready():
        raise RuntimeError("set WA_PHONE_NUMBER_ID, WA_TOKEN and WA_TO in .env (SOCIALS.md: WhatsApp report)")
    errors = refresh()
    clips = db.all("clips")
    now = totals(clips)
    text = report_text(clips, now, LAST["totals"], errors)
    LAST.update(totals=now, at=time.time())
    how = send_whatsapp(text)
    db.syslog(f"WhatsApp report sent as {how} ({len(text)} chars)")
    return text


if __name__ == "__main__":
    C = lambda t, v: {"title": t, "views": json.dumps(v)}
    clips = [C("Heist fail", {"youtube": 900, "instagram": 100}), C("Cop chase", {"youtube": 50}), C("No views", {})]
    t = totals(clips)
    assert t == {"youtube": 950, "instagram": 100}
    msg = report_text(clips, t, {"youtube": 900}, {"tiktok": "x"})
    assert "YouTube: 950 views (+50)" in msg and "Best platform: *YouTube*" in msg and "1. Heist fail: 1,000" in msg, msg
    assert "No views" not in msg and "! TikTok: x" in msg
    print("stats ok")
