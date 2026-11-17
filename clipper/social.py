"""Publishing to YouTube, Facebook, Instagram and TikTok. Every publisher -> {"id", "url"}.

A destination = an account (accounts.py, token decrypted) + a format (FORMATS: IG Reel, FB Post, YouTube Shorts...).
publish() sends one clip to one destination; the photo / carousel / text functions serve posts (posts.py)."""
import json
import re
import time

import httpx
from pathlib import Path

from . import upload as youtube
from . import config

PLATFORMS = ("youtube", "facebook", "instagram", "tiktok")
def graph():
    """Graph API base URL for the version saved in Settings."""
    return f"https://graph.facebook.com/{config.GRAPH_VERSION}"
RUPLOAD = "https://rupload.facebook.com"


def is_ig_token(token):
    """Instagram Login tokens ("IGAA...", from the Instagram API setup page) work on graph.instagram.com;
    Facebook Login tokens ("EAA...") on graph.facebook.com."""
    return str(token or "").startswith("IG")


def igb(token):
    """The API base for Instagram calls made with this token."""
    return f"https://graph.instagram.com/{config.GRAPH_VERSION}" if is_ig_token(token) else graph()
TIKTOK = "https://open.tiktokapis.com/v2"
HASHTAGS = {"youtube": 3, "facebook": 3, "instagram": 5, "tiktok": 5}
FB_REEL_MAX = 90  # seconds; longer "FB Reel" clips go up as a normal Page video

# Where a clip can go. aspect = the rendered frame; max = the platform's length limit in seconds (a warning in the
# UI, never a blocker). IG Reels use Settings' Instagram limit.
FORMATS = {
    "youtube_short": dict(platform="youtube", label="YouTube Shorts", aspect="9:16", max=180),
    "youtube": dict(platform="youtube", label="YouTube", aspect="16:9", max=None),
    "instagram_reel": dict(platform="instagram", label="IG Reel", aspect="9:16", max=None),
    "instagram_post": dict(platform="instagram", label="IG Post", aspect="4:5", max=60),
    "facebook_reel": dict(platform="facebook", label="FB Reel", aspect="9:16", max=FB_REEL_MAX),
    "facebook_post": dict(platform="facebook", label="FB Post", aspect="4:5", max=None),
    "tiktok": dict(platform="tiktok", label="TikTok", aspect="9:16", max=600),
}
DEFAULT_FORMAT = {"youtube": "youtube_short", "instagram": "instagram_reel", "facebook": "facebook_reel", "tiktok": "tiktok"}


def format_max(fmt):
    m = FORMATS[fmt]["max"]
    return (config.IG_MAX_SECONDS or None) if fmt == "instagram_reel" else m


def entries(uploads):
    """(platform, entry) for each upload in a clip's uploads JSON. Keys are "<format>@<account id>" now, and the
    platform name in uploads made before destinations existed."""
    return [(u.get("platform") or k, u) for k, u in uploads.items()]
TIMEOUT = httpx.Timeout(30, read=600, write=600)  # file uploads
API_TIMEOUT = httpx.Timeout(30, read=90)  # small JSON calls: a stalled one fails fast and is retried instead of hanging 10 min
SLOW = (httpx.TimeoutException, httpx.NetworkError)


def _retry(fn, tries=3, wait=5):
    """Run a request again after a timeout / dropped connection (not after an error answer from the platform)."""
    for i in range(tries):
        try:
            return fn()
        except SLOW:
            if i == tries - 1:
                raise
            time.sleep(wait * (i + 1))


# ---- upload counter + last error (information only: the app never holds uploads back) ----
DAY = 24 * 3600


def upload_times(platform, clips=None):
    """When each of our successful uploads to `platform` happened (seconds since 1970), oldest first."""
    from . import db
    out = []
    for c in clips if clips is not None else db.all("clips"):
        for p, u in entries(json.loads(c.get("uploads") or "{}")):
            if p == platform and u.get("id") and u.get("at"):
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
    return explain(str(err))


# Meta's permission errors, in plain words (the raw message stays after it, for reference).
HINTS = [
    ("pages_manage_posts", "This token can't post to the Facebook Page. In your Meta app's configuration add the Page "
                           "as an asset with pages_show_list, pages_read_engagement and pages_manage_posts, make a new "
                           "token and paste it on Accounts (the account is updated in place)."),
    ("instagram_content_publish", "This token can't post to Instagram: add instagram_content_publish to your Meta "
                                  "app's configuration, make a new token and paste it on Accounts."),
    ("publish_video", "This token can't upload videos to the Page: add the publish_video permission."),
]


def explain(msg):
    for key, hint in HINTS:
        if key in msg and "(#200)" in msg or key in msg and "permission" in msg.lower():
            return f"{hint} [Meta said: {msg[:200]}]"
    return msg


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
            for pl, u in entries(json.loads(c.get("uploads") or "{}")):
                if pl != p:
                    continue
                if u.get("id"):
                    last_ok = max(last_ok, u.get("at") or 0)
                elif u.get("error") and u.get("at") and (not last_err or u["at"] > last_err["at"]):
                    last_err = {"error": u["error"], "at": u["at"], "clip": c["id"]}
        out[p] = {"used": len([t for t in upload_times(p, clips) if t > now - DAY]),
                  "error": last_err if last_err and last_err["at"] > last_ok else None}
    return out


def connected():
    """{platform: has at least one account}."""
    from . import accounts
    have = {a["platform"] for a in accounts.all()}
    return {p: p in have for p in PLATFORMS}


def publish(account, fmt, path, title, description, tags, seconds):
    """One clip to one destination. account: an accounts row with its token decrypted (accounts.get(id, True))."""
    platform = FORMATS[fmt]["platform"]
    # Hashtags per platform: few and specific works best. YouTube: 3 (+ #Shorts). Instagram/TikTok: 5. Facebook: 3.
    tags = tags[:HASHTAGS[platform]]
    if platform == "youtube":
        vid = youtube.upload(account, path, title, description, tags, short=fmt == "youtube_short")
        return {"id": vid, "url": f"https://youtu.be/{vid}" if fmt == "youtube_short" else f"https://www.youtube.com/watch?v={vid}"}
    caption = "\n\n".join(x for x in (title, description, " ".join(f"#{t}" for t in tags)) if x)
    if platform == "facebook":
        return facebook(account, path, title, caption, reel=fmt == "facebook_reel" and seconds <= FB_REEL_MAX)
    if platform == "instagram":
        return instagram(account, path, caption)
    return tiktok(account, path, caption)


def upload(platform, path, title, description, tags, seconds):
    """Older callers: the platform's default account (accounts.default_for) and its usual format."""
    from . import accounts
    a = accounts.default_for(platform)
    if not a:
        raise RuntimeError(f"no {platform} account: add one on the Accounts page")
    fmt = "facebook_post" if platform == "facebook" and seconds > FB_REEL_MAX else DEFAULT_FORMAT[platform]
    return publish(accounts.get(a["id"], secret=True), fmt, path, title, description, tags, seconds)


def _ok(r):
    """Raise with the platform's own error message (much more useful than a bare HTTP status)."""
    if r.is_error:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


CHUNK = 16 * 1024 * 1024  # bytes per upload request
CHUNK_TIMEOUT = httpx.Timeout(30, read=300, write=120)  # a stalled chunk fails within minutes (one big POST could hang)


def _rupload(url, token, path):
    """Meta's resumable upload host (rupload.facebook.com), in chunks: each POST says where it starts (offset) and the
    whole size (file_size); Meta answers 206 until the last one. A chunk that times out is sent again (up to 3 times)
    instead of the whole upload hanging on one slow connection."""
    data = Path(path).read_bytes()
    size, off, r = len(data), 0, None
    while off < size:
        piece = data[off:off + CHUNK]
        for attempt in range(3):
            try:
                r = httpx.post(url, content=piece, timeout=CHUNK_TIMEOUT,
                               headers={"Authorization": f"OAuth {token}", "offset": str(off), "file_size": str(size)})
                break
            except SLOW:
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        if r.status_code != 206:  # 206 = "got it, send the rest"; anything else is the final answer (or an error)
            return _ok(r)
        off += len(piece)
    return _ok(r)


def facebook(account, path, title, caption, reel=True):
    """reel=True: a Reel (3-90 s, vertical). False: a normal Page video (any length / shape, e.g. a 4:5 feed post)."""
    page, token = account["ext_id"], account["token"]
    if not reel:
        with open(path, "rb") as f:
            r = _ok(httpx.post(f"https://graph-video.facebook.com/{config.GRAPH_VERSION}/{page}/videos", timeout=TIMEOUT,
                               data={"title": title[:255], "description": caption, "access_token": token},
                               files={"source": (path.name, f, "video/mp4")}))
        return {"id": r["id"], "url": f"https://www.facebook.com/watch/?v={r['id']}"}
    reels = f"{graph()}/{page}/video_reels"
    vid = _ok(httpx.post(reels, data={"upload_phase": "start", "access_token": token}, timeout=TIMEOUT))["video_id"]
    _rupload(f"{RUPLOAD}/video-upload/{config.GRAPH_VERSION}/{vid}", token, path)
    _ok(httpx.post(reels, timeout=TIMEOUT, data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                                                 "description": caption, "access_token": token}))
    return {"id": vid, "url": f"https://www.facebook.com/reel/{vid}"}


def instagram(account, path, caption):
    """Instagram's ProcessingFailedError is often random (the same file goes through on a second try): retry once."""
    try:
        return _instagram(account, path, caption)
    except Exception as e:
        if "ProcessingFailedError" not in str(e) and "processing failed" not in str(e):
            raise
        time.sleep(20)
        return _instagram(account, path, caption)


def _wait_ig(container, token, tries=120):
    """Instagram processes a container before it can be published (videos ~1 min, photos at once)."""
    for _ in range(tries):
        try:
            st = _ok(httpx.get(f"{igb(token)}/{container}", params={"fields": "status_code,status", "access_token": token},
                               timeout=API_TIMEOUT))
        except SLOW:  # one slow status check isn't a failure: ask again
            time.sleep(5)
            continue
        if st.get("status_code") in (None, "FINISHED"):
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"instagram processing failed: {st.get('status')}")
        time.sleep(5)
    raise RuntimeError("instagram still processing after 10 min")


def _ig_publish(ig_id, container, token, tries=3):
    """Publish a finished container. A timeout doesn't mean it failed: Meta may have published it anyway, so check
    the container before trying again (otherwise a retry would post it twice)."""
    mid = None
    for i in range(tries):
        try:
            mid = _ok(httpx.post(f"{igb(token)}/{ig_id}/media_publish", timeout=API_TIMEOUT,
                                 data={"creation_id": container, "access_token": token}))["id"]
            break
        except SLOW:
            time.sleep(10)
            mid = _published_from(ig_id, container, token)
            if mid or i == tries - 1:
                if not mid:
                    raise
                break
    try:
        link = _retry(lambda: _ok(httpx.get(f"{igb(token)}/{mid}", timeout=API_TIMEOUT,
                                            params={"fields": "permalink", "access_token": token}))).get("permalink")
    except (RuntimeError, *SLOW):
        link = None  # it's posted: a missing link isn't worth failing over
    return {"id": mid, "url": link or "https://www.instagram.com/"}


def _published_from(ig_id, container, token):
    """After a timed-out media_publish: the new media id if the container did get published, else None."""
    try:
        st = _ok(httpx.get(f"{igb(token)}/{container}", params={"fields": "status_code", "access_token": token},
                           timeout=API_TIMEOUT))
        if st.get("status_code") != "PUBLISHED":
            return None
        recent = _ok(httpx.get(f"{igb(token)}/{ig_id}/media", timeout=API_TIMEOUT,
                               params={"fields": "id,timestamp", "limit": 1, "access_token": token})).get("data") or []
        return recent[0]["id"] if recent else None
    except (RuntimeError, *SLOW):
        return None


NEEDS_PAGE_VIDEO = ("Instagram accounts connected with an Instagram token (IGAA…) can't upload a Reel file: Meta only "
                    "lets them post a video from a public link. Clipper makes one by hosting the Reel on a Facebook Page "
                    "of this brand for a moment, so add your Page on Accounts with a Facebook token (EAA…). Or connect "
                    "the Instagram account through Facebook Login (linked to your Page) instead.")


def _hidden_video_url(path, page_id, token, tries=60):
    """Upload the video to the Page unpublished and wait until Facebook serves it: -> (public mp4 URL, video id).
    Instagram then fetches the Reel from that URL; the hidden video is deleted afterwards."""
    with open(path, "rb") as f:
        vid = _ok(httpx.post(f"https://graph-video.facebook.com/{config.GRAPH_VERSION}/{page_id}/videos", timeout=TIMEOUT,
                             data={"published": "false", "no_story": "true", "access_token": token},
                             files={"source": (Path(path).name, f, "video/mp4")}))["id"]
    for _ in range(tries):  # Facebook processes it first (usually under a minute)
        d = _ok(httpx.get(f"{graph()}/{vid}", timeout=TIMEOUT, params={"fields": "source,status", "access_token": token}))
        if d.get("source"):
            return d["source"], vid
        if ((d.get("status") or {}).get("video_status") or "") == "error":
            raise RuntimeError("Facebook couldn't process the video it was hosting for Instagram")
        time.sleep(5)
    raise RuntimeError("Facebook didn't make the hosted video available within 5 minutes")


def _reel_via_url(account, path, caption):
    """Reel from a public link: the video is hosted on a Facebook Page of the brand for a moment (unpublished),
    Instagram fetches it, the hosted copy is deleted. The only way for Instagram-token accounts; the fallback for
    the others when Meta's direct upload refuses the file."""
    ig, token, host = account["ext_id"], account["token"], account.get("host_page")
    if not host:
        raise RuntimeError(NEEDS_PAGE_VIDEO)
    url, hidden = _hidden_video_url(path, host[0], host[1])
    try:
        c = _ok(httpx.post(f"{igb(token)}/{ig}/media", timeout=TIMEOUT,
                           data={"media_type": "REELS", "video_url": url, "caption": caption[:2200],
                                 "share_to_feed": "true", "access_token": token,
                                 **({"audio_name": account["audio_name"][:60]} if account.get("audio_name") else {})}))["id"]
        _wait_ig(c, token)
        return _ig_publish(ig, c, token)
    finally:
        _delete_hidden([hidden], host[1])


def _container_status(container, token):
    try:
        st = httpx.get(f"{igb(token)}/{container}", params={"fields": "status_code,status", "access_token": token},
                       timeout=TIMEOUT).json()
        return f"{st.get('status_code', '')} {st.get('status', '')}".strip()
    except Exception:
        return ""


def _instagram(account, path, caption):
    from . import render
    from .config import IG_BITRATE, IG_MAX_MBPS
    ig, token = account["ext_id"], account["token"]
    limit = config.IG_MAX_SECONDS
    if limit and render.duration(path) > limit + 0.5:  # its API refuses longer Reels (tested: 45 s ok, 50 s refused)
        path = render.keep_last(path, Path(path).with_name(Path(path).stem + f"_ig{int(limit)}.mp4"), limit, IG_BITRATE)
    elif render.bitrate_mbps(path) > IG_MAX_MBPS:  # Instagram rejects Reels over 25 Mbps: send a lighter copy
        path = render.reencode(path, Path(path).with_name(Path(path).stem + "_ig.mp4"), IG_BITRATE)
    if is_ig_token(token):  # Instagram Login: no file upload at all, a public video_url only (Meta's docs)
        return _reel_via_url(account, path, caption)
    resp = _ok(httpx.post(f"{igb(token)}/{ig}/media", timeout=TIMEOUT,
                          data={"media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
                                "share_to_feed": "true", "access_token": token,
                                **({"audio_name": account["audio_name"][:60]} if account.get("audio_name") else {})}))
    c = resp["id"]
    upload_to = resp.get("uri") or f"{RUPLOAD}/ig-api-upload/{config.GRAPH_VERSION}/{c}"  # Meta's own address first
    try:
        _rupload(upload_to, token, path)
    except RuntimeError as e:
        if account.get("host_page"):  # direct upload refused: let Instagram fetch it from a hosted link instead
            from . import db
            db.syslog(f"instagram: direct upload refused ({str(e)[:150]}), trying the hosted-link route")
            return _reel_via_url(account, path, caption)
        st = _container_status(c, token)
        raise RuntimeError(f"Meta refused the video upload{f' (Instagram says: {st})' if st else ''}. Add your Facebook "
                           f"Page to this brand on Accounts: Clipper can then send the Reel as a hosted link instead. "
                           f"[{str(e)[:200]}]") from e
    _wait_ig(c, token)
    return _ig_publish(ig, c, token)


# ---- photo / text posts (posts.py) ----

# Each takes the account's own ids + token (posts.py keeps several Pages / Instagram accounts in the db).

def fb_photo(path, caption, page_id, token, published=True):
    """Photo post on the Page. published=False: a hidden photo, only used to get a public image URL for Instagram."""
    with open(path, "rb") as f:
        r = _ok(httpx.post(f"{graph()}/{page_id}/photos", timeout=TIMEOUT, files={"source": (path.name, f, "image/jpeg")},
                           data={"message": caption, "published": "true" if published else "false", "access_token": token}))
    return {"id": r.get("post_id") or r["id"], "photo_id": r["id"],
            "url": f"https://www.facebook.com/{r.get('post_id') or r['id']}"}


def fb_text(message, page_id, token):
    r = _ok(httpx.post(f"{graph()}/{page_id}/feed", timeout=TIMEOUT, data={"message": message, "access_token": token}))
    return {"id": r["id"], "url": f"https://www.facebook.com/{r['id']}"}


def _ig_image_url(path, page_id, token):
    """Instagram only takes a public image_url (no file upload for photos). So the JPEG goes to the linked Facebook
    Page as a hidden photo and Instagram fetches it from Facebook's CDN. -> (url, hidden photo id to delete after)."""
    tmp = fb_photo(path, "", page_id, token, published=False)["photo_id"]
    imgs = _retry(lambda: _ok(httpx.get(f"{graph()}/{tmp}", params={"fields": "images", "access_token": token},
                                        timeout=API_TIMEOUT))).get("images") or []
    if not imgs:
        raise RuntimeError("facebook gave no image URL for the hidden photo")
    return max(imgs, key=lambda i: i.get("width", 0))["source"], tmp


def _delete_hidden(ids, token):
    for x in ids:
        try:
            httpx.delete(f"{graph()}/{x}", params={"access_token": token}, timeout=TIMEOUT)
        except httpx.HTTPError:
            pass


NEEDS_PAGE = ("Instagram photo posts need a Facebook Page in this brand (Instagram only takes photos from a public link, "
              "which Clipper hosts on a Page for a moment). Add any Page on the Accounts page. Reels work without one.")


def ig_photo(path, caption, ig_id, page_id, token, host_token=None):
    """page_id / host_token: the Facebook Page that briefly hosts the photo (its own token if it isn't the IG's)."""
    if not page_id:
        raise RuntimeError(NEEDS_PAGE)
    tmps, host = [], host_token or token
    try:
        url, tmp = _ig_image_url(path, page_id, host)
        tmps.append(tmp)
        c = _retry(lambda: _ok(httpx.post(f"{igb(token)}/{ig_id}/media", timeout=API_TIMEOUT,
                                          data={"image_url": url, "caption": caption[:2200], "access_token": token})))["id"]
        _wait_ig(c, token, tries=24)
        return _ig_publish(ig_id, c, token)
    finally:
        _delete_hidden(tmps, host)


def ig_carousel(paths, caption, ig_id, page_id, token, host_token=None):
    """2-10 photos as one Instagram carousel: a container per photo, then a CAROUSEL container holding them."""
    if not page_id:
        raise RuntimeError(NEEDS_PAGE)
    tmps, children, host = [], [], host_token or token
    try:
        for p in paths[:10]:
            url, tmp = _ig_image_url(p, page_id, host)
            tmps.append(tmp)
            children.append(_retry(lambda: _ok(httpx.post(f"{igb(token)}/{ig_id}/media", timeout=API_TIMEOUT, data={
                "image_url": url, "is_carousel_item": "true", "access_token": token})))["id"])
        for ch in children:
            _wait_ig(ch, token, tries=24)
        c = _ok(httpx.post(f"{igb(token)}/{ig_id}/media", timeout=TIMEOUT, data={
            "media_type": "CAROUSEL", "children": ",".join(children), "caption": caption[:2200], "access_token": token}))["id"]
        _wait_ig(c, token, tries=24)
        return _ig_publish(ig_id, c, token)
    finally:
        _delete_hidden(tmps, host)


def fb_carousel(paths, caption, page_id, token):
    """Several photos in one Page post: each uploaded unpublished, then one feed post that attaches them."""
    ids = [fb_photo(p, "", page_id, token, published=False)["photo_id"] for p in paths[:10]]
    data = {"message": caption, "access_token": token}
    for i, x in enumerate(ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": x})
    r = _ok(httpx.post(f"{graph()}/{page_id}/feed", timeout=TIMEOUT, data=data))
    return {"id": r["id"], "url": f"https://www.facebook.com/{r['id']}"}


def meta_pages(token):
    """Pages (+ linked Instagram accounts) a Meta token can post to: [{page_id, name, token, ig_id, ig_username}].
    A user token lists all its Pages (each with its own Page token); a Page token gives just that Page."""
    ig = "instagram_business_account{id,username}"
    try:
        rows = _ok(httpx.get(f"{graph()}/me/accounts", timeout=TIMEOUT, params={
            "fields": f"id,name,access_token,{ig}", "limit": 100, "access_token": token})).get("data", [])
    except RuntimeError:  # a Page token: /me is the Page itself
        rows = [{**_ok(httpx.get(f"{graph()}/me", timeout=TIMEOUT, params={"fields": f"id,name,{ig}", "access_token": token})),
                 "access_token": token}]
    out = [{"page_id": r["id"], "name": r.get("name", r["id"]), "token": r.get("access_token") or token,
            "ig_id": (r.get("instagram_business_account") or {}).get("id"),
            "ig_username": (r.get("instagram_business_account") or {}).get("username")} for r in rows]
    return out + meta_ig_direct(token, {p["ig_id"] for p in out if p["ig_id"]})


def meta_long_lived(token):
    """A short-lived user token (Graph API Explorer: ~1 hour) -> a long-lived one (~60 days; the Page tokens it gives
    never expire), when Settings has the Meta App ID + secret. Otherwise, or if Meta refuses, the token as it was."""
    if is_ig_token(token) or not (config.META_APP_ID and config.META_APP_SECRET):
        return token  # Instagram Login tokens are long-lived already (and renewed by ig_refresh)
    r = httpx.get(f"{graph()}/oauth/access_token", timeout=TIMEOUT, params={
        "grant_type": "fb_exchange_token", "client_id": config.META_APP_ID, "client_secret": config.META_APP_SECRET,
        "fb_exchange_token": token})
    return r.json().get("access_token") or token if r.is_success else token


def token_check(token):
    """What a Meta token can do, for the Accounts page: {"valid", "type", "expires_at" (0 = never), "scopes",
    "instagram_ids", "error"}. Uses debug_token (works with your own app's tokens)."""
    if is_ig_token(token):  # Instagram Login token: no debug_token; asking who it is proves it works
        try:
            me = ig_me(token)
            return {"valid": True, "type": "INSTAGRAM", "expires_at": None, "scopes": [], "instagram_ids": [me["id"]],
                    "ig_login": True, "username": me["username"], "error": ""}
        except Exception as e:
            return {"valid": False, "type": "INSTAGRAM", "error": str(e)[:300], "scopes": [], "instagram_ids": [], "ig_login": True}
    try:
        d = _ok(httpx.get(f"{graph()}/debug_token", timeout=TIMEOUT,
                          params={"input_token": token, "access_token": token})).get("data") or {}
    except Exception as e:
        return {"valid": False, "error": str(e)[:300], "scopes": [], "instagram_ids": []}
    ig = sorted({i for s in d.get("granular_scopes", []) if s.get("scope", "").startswith("instagram")
                 for i in s.get("target_ids", [])})
    return {"valid": bool(d.get("is_valid")), "type": d.get("type", ""), "expires_at": d.get("expires_at", 0),
            "scopes": d.get("scopes", []), "instagram_ids": ig, "error": (d.get("error") or {}).get("message", "")}


def meta_ig_direct(token, known=()):
    """Instagram accounts a token was granted directly (e.g. a "Facebook Login for Business" configuration with
    only the Instagram account as an asset, or an Instagram account not linked to any of your Pages). They show up in
    the token's granular scopes. -> [{"page_id": None, "ig_id", "ig_username", "token"}]"""
    try:
        d = _ok(httpx.get(f"{graph()}/debug_token", timeout=TIMEOUT, params={"input_token": token, "access_token": token}))
    except Exception:
        return []
    ids = {i for s in (d.get("data") or {}).get("granular_scopes", []) if s.get("scope", "").startswith("instagram")
           for i in s.get("target_ids", [])} - set(known)
    out = []
    for i in sorted(ids):
        try:
            name = meta_name(i, token, "username")
        except Exception:
            continue  # not an Instagram account this token can read
        out.append({"page_id": None, "name": f"@{name}", "token": token, "ig_id": i, "ig_username": name})
    return out


def meta_name(object_id, token, field="name"):
    """Checks the token can read the object and returns its name (Page) / username (Instagram)."""
    return _ok(httpx.get(f"{igb(token)}/{object_id}", timeout=TIMEOUT,
                         params={"fields": field, "access_token": token})).get(field) or object_id


def ig_me(token):
    """The Instagram account an Instagram Login token belongs to: {"id", "username"}."""
    d = _ok(httpx.get(f"{igb(token)}/me", timeout=TIMEOUT, params={"fields": "user_id,username", "access_token": token}))
    return {"id": str(d.get("user_id") or d.get("id")), "username": d.get("username", "")}


def ig_refresh(token):
    """Renew an Instagram Login token for another 60 days (Meta allows it once the token is a day old).
    -> the new token, or None if Meta says not yet / no."""
    r = httpx.get("https://graph.instagram.com/refresh_access_token", timeout=TIMEOUT,
                  params={"grant_type": "ig_refresh_token", "access_token": token})
    return r.json().get("access_token") if r.is_success else None


def tiktok_refresh(refresh_token):
    """Refresh token -> {"access_token", "refresh_token" (TikTok rotates it), "expires_at", "open_id"}."""
    r = _ok(httpx.post(f"{TIKTOK}/oauth/token/", timeout=TIMEOUT,
                       data={"client_key": config.TIKTOK_CLIENT_KEY, "client_secret": config.TIKTOK_CLIENT_SECRET,
                             "grant_type": "refresh_token", "refresh_token": refresh_token}))
    if "access_token" not in r:
        raise RuntimeError(f"tiktok token refresh failed: {r}")
    return {"access_token": r["access_token"], "refresh_token": r["refresh_token"],
            "expires_at": time.time() + r["expires_in"], "open_id": r.get("open_id", "")}


def tiktok_access(account):
    """A working access token for a TikTok account (they last 24 h); the rotated tokens are saved back."""
    from . import accounts
    t = json.loads(account["token"])
    if t.get("access_token") and t.get("expires_at", 0) > time.time() + 300:
        return t["access_token"]
    new = tiktok_refresh(t["refresh_token"])
    if account.get("id"):
        accounts.set_token(account["id"], json.dumps(new))
    return new["access_token"]


def tiktok_user(access_token):
    """{"open_id", "display_name", "avatar_url"} (needs the user.info.basic scope; {} without it)."""
    r = httpx.get(f"{TIKTOK}/user/info/", params={"fields": "open_id,display_name,avatar_url"}, timeout=TIMEOUT,
                  headers={"Authorization": f"Bearer {access_token}"})
    return (r.json().get("data") or {}).get("user") or {} if r.is_success else {}


def _tiktok_api(endpoint, token, body):
    r = _ok(httpx.post(f"{TIKTOK}{endpoint}", json=body, timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}))
    if r.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"tiktok: {r['error']}")
    return r.get("data", {})


def tiktok(account, path, caption):
    token = tiktok_access(account)
    # Use the privacy from .env only if this account allows it (unaudited apps: SELF_ONLY only).
    options = _tiktok_api("/post/publish/creator_info/query/", token, {}).get("privacy_level_options", [])
    privacy = config.TIKTOK_PRIVACY if config.TIKTOK_PRIVACY in options else ("SELF_ONLY" if "SELF_ONLY" in options else options[0])
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
