"""Upload one rendered clip to YouTube, Facebook, Instagram or TikTok. Every uploader -> {"id", "url"}.
Credentials live in .env (see README "Facebook / Instagram / TikTok setup"); YouTube uses its own OAuth (upload.py)."""
import json
import re
import time

import httpx

from . import upload as youtube
from .config import (FB_PAGE_ID, FB_PAGE_TOKEN, GRAPH_VERSION, IG_TOKEN, IG_USER_ID, TIKTOK_CLIENT_KEY,
                     TIKTOK_CLIENT_SECRET, TIKTOK_PRIVACY, TIKTOK_REFRESH_TOKEN, TIKTOK_TOKEN)

PLATFORMS = ("youtube", "facebook", "instagram", "tiktok")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
RUPLOAD = "https://rupload.facebook.com"
TIKTOK = "https://open.tiktokapis.com/v2"
HASHTAGS = {"youtube": 3, "facebook": 3, "instagram": 5, "tiktok": 5}
FB_REEL_MAX = 90  # seconds; longer clips go up as a normal Page video
TIMEOUT = httpx.Timeout(30, read=600, write=600)


# ---- upload counter + last error (information only: the app never holds uploads back) ----
DAY = 24 * 3600


def upload_times(platform, clips=None):
    """When each of our successful uploads to `platform` happened (seconds since 1970), oldest first."""
    from . import db
    out = []
    for c in clips if clips is not None else db.all("clips"):
        u = json.loads(c.get("uploads") or "{}").get(platform, {})
        if u.get("id") and u.get("at"):
            out.append(u["at"])
    return sorted(out)


def backfill_times():
    """Uploads from before the counter existed have no time. Recover it from the job log line
    "[HH:MM:SS] uploaded #<clip> (to <platform>) ..." + the job's date, so today's uploads count. Runs at startup."""
    from . import db
    for c in db.all("clips"):
        ups = json.loads(c.get("uploads") or "{}")
        if c.get("youtube_id") and not ups.get("youtube"):  # uploaded before multi-platform: YouTube only
            ups["youtube"] = {"id": c["youtube_id"], "url": f"https://youtu.be/{c['youtube_id']}"}
        missing = [p for p, u in ups.items() if u.get("id") and not u.get("at")]
        if not missing:
            continue
        job = db.get("jobs", c["job_id"]) or {}
        day = time.strftime("%Y-%m-%d", time.localtime(job.get("created") or time.time()))
        for p in missing:
            m = re.search(rf"\[(\d\d:\d\d:\d\d)\] uploaded #{c['id']} (?:to {p} )?(?:in \d+s )?->", job.get("log") or "")
            ups[p]["at"] = time.mktime(time.strptime(f"{day} {m.group(1)}", "%Y-%m-%d %H:%M:%S")) if m else \
                (job.get("created") or time.time())
        db.update("clips", c["id"], uploads=json.dumps(ups))


def error_text(err):
    """The platform's own message, readable: YouTube's HttpError -> 'The user has exceeded ... (uploadLimitExceeded)'."""
    reason = getattr(err, "reason", None)  # googleapiclient HttpError
    details = getattr(err, "error_details", None)
    if reason:
        why = ", ".join(d.get("reason", "") for d in details if isinstance(d, dict)) if isinstance(details, list) else ""
        status = getattr(getattr(err, "resp", None), "status", "")
        return f"HTTP {status}: {reason}" + (f" ({why})" if why else "")
    from .db import scrub
    return scrub(err)


# When a platform's API says the upload limit is exceeded, remember when (table limit_hits) so the UI can count down
# 24 hours from that moment. Information only: uploads are never held back.
LIMIT_RE = re.compile(r"uploadLimitExceeded|exceeded the number of videos|quotaExceeded|dailyLimitExceeded|"
                      r"spam_risk_too_many_posts|too many posts|request limit reached|publishing limit|"
                      r"reached the (?:maximum|limit)|rate.?limit", re.I)


def note_limit(platform, message):
    """If `message` is a limit error, store/refresh the time it happened. Returns True if it was one."""
    from . import db
    if not LIMIT_RE.search(message):
        return False
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO limit_hits (platform, hit_at, message) VALUES (?, ?, ?)",
                  (platform, time.time(), message[:500]))
    return True


def backfill_limit_hits():
    """Limit errors logged before limit_hits existed: take the latest one per platform from the job logs
    ("[HH:MM:SS] upload #8 to youtube failed: ...uploadLimitExceeded...") so its 24h timer starts at the real time."""
    from . import db
    known = limit_timers()
    for job in db.all("jobs"):
        day = time.strftime("%Y-%m-%d", time.localtime(job.get("created") or time.time()))
        for hh, p, msg in re.findall(r"\[(\d\d:\d\d:\d\d)\] upload #\d+ to (\w+) failed[^:]*: (.*)", job.get("log") or ""):
            at = time.mktime(time.strptime(f"{day} {hh}", "%Y-%m-%d %H:%M:%S"))
            if LIMIT_RE.search(msg) and at > time.time() - DAY and at > known.get(p, {}).get("hit_at", 0):
                with db.conn() as c:
                    c.execute("INSERT OR REPLACE INTO limit_hits (platform, hit_at, message) VALUES (?, ?, ?)",
                              (p, at, msg[:500]))
                known[p] = {"hit_at": at}


def limit_timers():
    """{platform: {"hit_at", "until", "message"}} for limit hits less than 24h old."""
    from . import db
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM limit_hits WHERE hit_at > ?", (time.time() - DAY,))]
    return {r["platform"]: {**r, "until": r["hit_at"] + DAY} for r in rows}


def upload_status():
    """For the UI, per platform: uploads in the last 24h, and the last error if the most recent attempt failed."""
    from . import db
    clips, now = db.all("clips"), time.time()
    out = {}
    for p in PLATFORMS:
        last_ok, last_err = 0, None
        for c in clips:
            u = json.loads(c.get("uploads") or "{}").get(p, {})
            if u.get("id"):
                last_ok = max(last_ok, u.get("at") or 0)
            elif u.get("error") and u.get("at") and (not last_err or u["at"] > last_err["at"]):
                last_err = {"error": u["error"], "at": u["at"], "clip": c["id"]}
        out[p] = {"used": len([t for t in upload_times(p, clips) if t > now - DAY]),
                  "error": last_err if last_err and last_err["at"] > last_ok else None}
    return out


def connected():
    return {"youtube": youtube.connected(), "facebook": bool(FB_PAGE_ID and FB_PAGE_TOKEN),
            "instagram": bool(IG_USER_ID and IG_TOKEN),
            "tiktok": bool(TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET and (TIKTOK_TOKEN.exists() or TIKTOK_REFRESH_TOKEN))}


def upload(platform, path, title, description, tags, seconds, caption=None):
    """caption: the exact caption to post (campaign clips: built to the brand's rules), as is, no hashtags added."""
    if not connected().get(platform):
        raise RuntimeError(f"{platform} not connected (see README)")
    if caption:
        if platform == "youtube":
            vid = youtube.upload(path, (title or caption.splitlines()[0])[:100], caption, [], short=True)
            return {"id": vid, "url": f"https://youtu.be/{vid}"}
        return {"facebook": facebook, "instagram": instagram, "tiktok": tiktok}[platform](path, title, caption, seconds)
    # Hashtags per platform: few and specific works best. YouTube: 3 + #Shorts (upload.py adds it; title and
    # description carry the search). Instagram/TikTok: 5. Facebook: 3.
    tags = tags[:HASHTAGS[platform]]
    if platform == "youtube":
        vid = youtube.upload(path, title, description, tags, short=seconds <= 180)  # over 3 min: a normal video, not a Short
        return {"id": vid, "url": f"https://youtu.be/{vid}"}
    caption = "\n\n".join(x for x in (title, description, " ".join(f"#{t}" for t in tags)) if x)
    if is_photo(path):
        return {"facebook": facebook_photo, "instagram": instagram_photo}[platform](path, caption)
    return {"facebook": facebook, "instagram": instagram, "tiktok": tiktok}[platform](path, title, caption, seconds)


PHOTO_PLATFORMS = ("facebook", "instagram")  # YouTube's and TikTok's APIs can't post a photo


def is_photo(path):
    return str(path).lower().endswith((".jpg", ".jpeg", ".png"))


def _fb_photo(path, caption, published):
    with open(path, "rb") as f:
        return _ok(httpx.post(f"{GRAPH}/{FB_PAGE_ID}/photos", timeout=TIMEOUT,
                              data={"caption": caption, "published": "true" if published else "false",
                                    "access_token": FB_PAGE_TOKEN}, files={"source": (path.name, f, "image/jpeg")}))


def facebook_photo(path, caption):
    r = _fb_photo(path, caption, True)
    pid = r.get("post_id") or r["id"]
    return {"id": pid, "url": f"https://www.facebook.com/{pid}"}


def instagram_photo(path, caption):
    """Instagram's API only takes images from a public URL: put the photo on the Page unpublished (invisible) and give
    Instagram that Facebook-hosted URL."""
    hosted = _fb_photo(path, "", False)["id"]
    imgs = _ok(httpx.get(f"{GRAPH}/{hosted}", params={"fields": "images", "access_token": FB_PAGE_TOKEN}))["images"]
    url = max(imgs, key=lambda i: i.get("width", 0))["source"]
    c = _ok(httpx.post(f"{GRAPH}/{IG_USER_ID}/media", timeout=TIMEOUT,
                       data={"image_url": url, "caption": caption[:2200], "access_token": IG_TOKEN}))["id"]
    for _ in range(60):
        st = _ok(httpx.get(f"{GRAPH}/{c}", params={"fields": "status_code,status", "access_token": IG_TOKEN}))
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"instagram photo processing failed: {st.get('status')}")
        time.sleep(3)
    mid = _ok(httpx.post(f"{GRAPH}/{IG_USER_ID}/media_publish", data={"creation_id": c, "access_token": IG_TOKEN},
                         timeout=TIMEOUT))["id"]
    link = _ok(httpx.get(f"{GRAPH}/{mid}", params={"fields": "permalink", "access_token": IG_TOKEN})).get("permalink")
    return {"id": mid, "url": link or "https://www.instagram.com/"}


def _ok(r):
    """Raise with the platform's own error message (much more useful than a bare HTTP status)."""
    if r.is_error:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _rupload(url, token, path):
    """Meta's resumable upload host: whole file in one POST."""
    data = path.read_bytes()
    return _ok(httpx.post(url, content=data, timeout=TIMEOUT,
                          headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))}))


def _fb_chunked(path, title, caption):
    """A Page video in Facebook's chunked upload (start -> transfer the pieces Facebook asks for -> finish).
    One POST with the whole file gets HTTP 413 for big 4K clips (a 160s long is ~360 MB)."""
    from pathlib import Path
    path = Path(path)
    url, size = f"https://graph-video.facebook.com/{GRAPH_VERSION}/{FB_PAGE_ID}/videos", path.stat().st_size
    s = _ok(httpx.post(url, timeout=TIMEOUT, data={"upload_phase": "start", "file_size": str(size),
                                                   "access_token": FB_PAGE_TOKEN}))
    sid, vid, start, end = s["upload_session_id"], s["video_id"], int(s["start_offset"]), int(s["end_offset"])
    with open(path, "rb") as f:
        while start < end:
            f.seek(start)
            chunk, tries = f.read(end - start), 0
            while True:
                try:
                    r = _ok(httpx.post(url, timeout=TIMEOUT, data={"upload_phase": "transfer", "upload_session_id": sid,
                                                                   "start_offset": str(start), "access_token": FB_PAGE_TOKEN},
                                       files={"video_file_chunk": (path.name, chunk, "application/octet-stream")}))
                    break
                except Exception:
                    tries += 1
                    if tries >= 3:  # a dropped piece is retried, the whole upload isn't restarted
                        raise
                    time.sleep(5 * tries)
            start, end = int(r["start_offset"]), int(r["end_offset"])
    _ok(httpx.post(url, timeout=TIMEOUT, data={"upload_phase": "finish", "upload_session_id": sid, "title": title[:255],
                                               "description": caption, "access_token": FB_PAGE_TOKEN}))
    return vid


def facebook(path, title, caption, seconds):
    if seconds > FB_REEL_MAX:  # Reels are 3-90s: a longer clip goes up as a normal Page video
        vid = _fb_chunked(path, title, caption)
        return {"id": vid, "url": f"https://www.facebook.com/watch/?v={vid}"}
    reels = f"{GRAPH}/{FB_PAGE_ID}/video_reels"
    vid = _ok(httpx.post(reels, data={"upload_phase": "start", "access_token": FB_PAGE_TOKEN}, timeout=TIMEOUT))["video_id"]
    _rupload(f"{RUPLOAD}/video-upload/{GRAPH_VERSION}/{vid}", FB_PAGE_TOKEN, path)
    _ok(httpx.post(reels, timeout=TIMEOUT, data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                                                 "description": caption, "access_token": FB_PAGE_TOKEN}))
    return {"id": vid, "url": f"https://www.facebook.com/reel/{vid}"}


def instagram(path, title, caption, seconds):
    """Instagram's ProcessingFailedError is often random (the same file goes through on a second try): retry once."""
    try:
        return _instagram(path, title, caption, seconds)
    except Exception as e:
        if "ProcessingFailedError" not in str(e) and "processing failed" not in str(e):
            raise
        time.sleep(20)
        return _instagram(path, title, caption, seconds)


def _instagram(path, title, caption, seconds):
    from pathlib import Path
    from . import render
    from .config import IG_BITRATE, IG_MAX_MBPS
    # Instagram rejects Reels over 25 Mbps or wider than 1920px: the 4K clip goes as a 1080x1920 copy
    if render.bitrate_mbps(path) > IG_MAX_MBPS or render.width_of(path) > 1080:
        path = render.reencode(path, Path(path).with_name(Path(path).stem + "_ig.mp4"), IG_BITRATE, 1080)
    c = _ok(httpx.post(f"{GRAPH}/{IG_USER_ID}/media", timeout=TIMEOUT,
                       data={"media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
                             "share_to_feed": "true", "access_token": IG_TOKEN}))["id"]
    _rupload(f"{RUPLOAD}/ig-api-upload/{GRAPH_VERSION}/{c}", IG_TOKEN, path)
    for _ in range(120):  # Instagram processes the video before it can be published (usually < 1 min)
        st = _ok(httpx.get(f"{GRAPH}/{c}", params={"fields": "status_code,status", "access_token": IG_TOKEN}))
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"instagram processing failed: {st.get('status')}")
        time.sleep(5)
    else:
        raise RuntimeError("instagram still processing after 10 min")
    mid = _ok(httpx.post(f"{GRAPH}/{IG_USER_ID}/media_publish", data={"creation_id": c, "access_token": IG_TOKEN},
                         timeout=TIMEOUT))["id"]
    link = _ok(httpx.get(f"{GRAPH}/{mid}", params={"fields": "permalink", "access_token": IG_TOKEN})).get("permalink")
    return {"id": mid, "url": link or "https://www.instagram.com/"}


def _tiktok_token():
    """Access tokens last 24h; refresh with the (rotating) refresh token and keep both in data/tiktok_token.json."""
    t = json.loads(TIKTOK_TOKEN.read_text()) if TIKTOK_TOKEN.exists() else {"refresh_token": TIKTOK_REFRESH_TOKEN}
    if t.get("access_token") and t.get("expires_at", 0) > time.time() + 300:
        return t["access_token"]
    r = _ok(httpx.post(f"{TIKTOK}/oauth/token/", timeout=TIMEOUT,
                       data={"client_key": TIKTOK_CLIENT_KEY, "client_secret": TIKTOK_CLIENT_SECRET,
                             "grant_type": "refresh_token", "refresh_token": t["refresh_token"]}))
    if "access_token" not in r:
        raise RuntimeError(f"tiktok token refresh failed: {r}")
    TIKTOK_TOKEN.write_text(json.dumps({"access_token": r["access_token"], "refresh_token": r["refresh_token"],
                                        "expires_at": time.time() + r["expires_in"]}))
    return r["access_token"]


def _tiktok_api(endpoint, token, body):
    r = _ok(httpx.post(f"{TIKTOK}{endpoint}", json=body, timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}))
    if r.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"tiktok: {r['error']}")
    return r.get("data", {})


def tiktok(path, title, caption, seconds):
    token = _tiktok_token()
    # Use the privacy from .env only if this account allows it (unaudited apps: SELF_ONLY only).
    options = _tiktok_api("/post/publish/creator_info/query/", token, {}).get("privacy_level_options", [])
    privacy = TIKTOK_PRIVACY if TIKTOK_PRIVACY in options else ("SELF_ONLY" if "SELF_ONLY" in options else options[0])
    size = path.stat().st_size
    # One chunk up to 64MB; bigger files in 10MB chunks, the last one taking the remainder (TikTok's rule).
    chunk = size if size <= 64_000_000 else 10_000_000
    count = max(1, size // chunk)
    d = _tiktok_api("/post/publish/video/init/", token, {
        "post_info": {"title": caption[:2200], "privacy_level": privacy},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}})
    with open(path, "rb") as f:
        for i in range(count):
            a = i * chunk
            b = size - 1 if i == count - 1 else a + chunk - 1
            f.seek(a)
            r = httpx.put(d["upload_url"], content=f.read(b - a + 1), timeout=TIMEOUT,
                          headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {a}-{b}/{size}"})
            if r.is_error:
                raise RuntimeError(f"tiktok upload chunk {i}: HTTP {r.status_code} {r.text[:200]}")
    # TikTok has no post URL until it finishes processing; the publish_id is what its API tracks.
    return {"id": d["publish_id"], "url": "https://www.tiktok.com/tiktokstudio/content"}
