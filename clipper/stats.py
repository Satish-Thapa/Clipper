"""Numbers for everything we posted, from each platform's API. Refreshed every day at the research time
(autopilot.daily_report) and when you press Refresh on the Reports page.

refresh() -> per clip, per platform, into clips.stats ({platform: {views, likes, comments, shares, saves, ...}}) and
clips.views ({platform: views}):
  YouTube    Data API: views, likes, comments. Analytics API (needs YouTube reconnected once for read-only
             analytics): minutes watched, average view duration and % watched, subscribers gained, shares; plus the
             channel's views per day and where the views come from (traffic sources), last 28 days.
  Facebook   Reel plays (or video views), reactions, comments, shares, average watch time.
  Instagram  views, reach, likes, comments, shares, saves, average Reel watch time.
  TikTok     views, likes, comments, shares (only for posts TikTok lists: app posts stay private until the app is
             audited).
daily() = refresh() + today's snapshot of every number (table stats_daily), so the Reports page can show growth.
A platform that fails doesn't stop the others: its error is kept (errors()) and shown on the Reports page."""
import json
import time
from datetime import date, timedelta

import httpx

from . import db, upload as youtube
from .config import FB_PAGE_TOKEN, GRAPH_VERSION, IG_TOKEN, TIKTOK_TOKEN

GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
NAMES = {"youtube": "YouTube", "facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok"}
IG_METRICS = ("views", "reach", "likes", "comments", "shares", "saved", "ig_reels_avg_watch_time")
YT_ANALYTICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,shares"


def _num(v):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _yt(ids, out, errors):
    if not youtube.connected():
        errors["youtube"] = "not connected: Connect YouTube on the Socials page"
        return
    try:
        for vid, st in youtube.statistics(ids).items():
            out.setdefault(vid, {}).update(views=_num(st.get("viewCount")), likes=_num(st.get("likeCount")),
                                           comments=_num(st.get("commentCount")))
    except Exception as e:
        errors["youtube"] = db.scrub(e)[:200]
        return
    try:
        for vid, row in youtube.analytics(ids, YT_ANALYTICS).items():
            out.setdefault(vid, {}).update(watch_min=round(float(row.get("estimatedMinutesWatched") or 0), 1),
                                           avg_view_s=round(float(row.get("averageViewDuration") or 0), 1),
                                           avg_view_pct=round(float(row.get("averageViewPercentage") or 0), 1),
                                           subs=_num(row.get("subscribersGained")), shares=_num(row.get("shares")))
    except Exception as e:
        errors["youtube_analytics"] = db.scrub(e)[:200]


def _graph(path, **params):
    r = httpx.get(f"{GRAPH}/{path}", params=params, timeout=20)
    if r.is_error:
        raise RuntimeError(((r.json() if "json" in r.headers.get("content-type", "") else {}).get("error") or {})
                           .get("message") or f"HTTP {r.status_code}")
    return r.json()


def _values(resp):
    return {d["name"]: (d.get("values") or [{}])[0].get("value", d.get("total_value", {}).get("value")) for d in resp.get("data") or []}


def _facebook(vid):
    if "_" in str(vid):  # a photo post ("<page>_<post>"): how many people saw it
        v = _values(_graph(f"{vid}/insights", metric="post_impressions_unique", access_token=FB_PAGE_TOKEN))
        return {"views": _num(v.get("post_impressions_unique"))}
    out = {}
    for metric in ("blue_reels_play_count", "total_video_views"):  # a Reel, else a normal Page video
        try:
            out["views"] = _num(_values(_graph(f"{vid}/video_insights", metric=metric, access_token=FB_PAGE_TOKEN)).get(metric))
            break
        except RuntimeError:
            continue
    try:
        v = _values(_graph(f"{vid}/video_insights", metric="post_video_avg_time_watched", access_token=FB_PAGE_TOKEN))
        out["avg_view_s"] = round(_num(v.get("post_video_avg_time_watched")) / 1000, 1)
    except RuntimeError:
        pass
    try:
        p = _graph(vid, fields="likes.summary(true).limit(0),comments.summary(true).limit(0)", access_token=FB_PAGE_TOKEN)
        out.update(likes=_num(((p.get("likes") or {}).get("summary") or {}).get("total_count")),
                   comments=_num(((p.get("comments") or {}).get("summary") or {}).get("total_count")))
    except RuntimeError:
        pass
    if "views" not in out:
        raise RuntimeError("no view metric for this Facebook video")
    return out


def _instagram(mid):
    try:
        v = _values(_graph(f"{mid}/insights", metric=",".join(IG_METRICS), access_token=IG_TOKEN))
    except RuntimeError:  # an older post / a photo: not every metric exists
        v = _values(_graph(f"{mid}/insights", metric="views", access_token=IG_TOKEN))
    out = {"views": _num(v.get("views"))}
    for k, name in (("reach", "reach"), ("likes", "likes"), ("comments", "comments"), ("shares", "shares"), ("saved", "saves")):
        if v.get(k) is not None:
            out[name] = _num(v[k])
    if v.get("ig_reels_avg_watch_time") is not None:
        out["avg_view_s"] = round(_num(v["ig_reels_avg_watch_time"]) / 1000, 1)
    return out


def _tiktok(ids):
    """{video_id: {...}} via the Display API's video/query (needs the video.list scope)."""
    token = json.loads(TIKTOK_TOKEN.read_text()).get("access_token") if TIKTOK_TOKEN.exists() else ""
    if not token:
        raise RuntimeError("TikTok not connected")
    r = httpx.post("https://open.tiktokapis.com/v2/video/query/?fields=id,view_count,like_count,comment_count,share_count",
                   headers={"Authorization": f"Bearer {token}"}, json={"filters": {"video_ids": ids[:20]}}, timeout=20)
    j = r.json() if "json" in r.headers.get("content-type", "") else {}
    if r.is_error or (j.get("error") or {}).get("code") not in (None, "ok"):
        raise RuntimeError(((j.get("error") or {}).get("message")) or f"HTTP {r.status_code}")
    return {str(v["id"]): {"views": _num(v.get("view_count")), "likes": _num(v.get("like_count")),
                           "comments": _num(v.get("comment_count")), "shares": _num(v.get("share_count"))}
            for v in (j.get("data") or {}).get("videos") or []}


def refresh():
    """Fetch every number for every posted clip. -> {platform: error} for the platforms that failed."""
    clips = [c for c in db.all("clips") if c.get("uploads") and c["uploads"] != "{}"]
    ups = {c["id"]: json.loads(c["uploads"]) for c in clips}
    st = {c["id"]: json.loads(c.get("stats") or "{}") for c in clips}
    errors = {}
    ids = lambda p: {u[p]["id"]: cid for cid, u in ups.items() if (u.get(p) or {}).get("id") and u[p]["id"] != "manual"}
    yt = ids("youtube")
    if yt:
        got = {}
        _yt(list(yt), got, errors)
        for vid, v in got.items():
            st[yt[vid]]["youtube"] = v
        try:
            db.set_setting("yt_channel", json.dumps(youtube.channel_report()))
        except Exception as e:
            errors.setdefault("youtube_analytics", db.scrub(e)[:200])
    for p, fetch in (("facebook", _facebook), ("instagram", _instagram)):
        for vid, cid in ids(p).items():
            try:
                st[cid][p] = fetch(vid)
            except Exception as e:
                errors[p] = db.scrub(e)[:200]
    tt = ids("tiktok")
    if tt:
        try:
            for vid, v in _tiktok(list(tt)).items():
                if vid in tt:
                    st[tt[vid]]["tiktok"] = v
        except Exception as e:
            errors["tiktok"] = "TikTok doesn't report numbers for private app posts yet" if "scope" in str(e).lower() \
                else db.scrub(e)[:200]
    for cid, v in st.items():
        db.update("clips", cid, stats=json.dumps(v), views=json.dumps({p: x.get("views", 0) for p, x in v.items()}))
    db.set_setting("stats_errors", json.dumps(errors))
    db.set_setting("views_at", str(time.time()))
    db.syslog(f"stats refreshed for {len(st)} posted clip(s): {totals()}" + (f", errors: {errors}" if errors else ""))
    return errors


def daily():
    """refresh() + today's snapshot of every clip's numbers (stats_daily). -> errors."""
    errors = refresh()
    day = date.today().isoformat()
    with db.conn() as c:
        for clip in db.all("clips", "stats != '{}' AND stats IS NOT NULL"):
            for p, v in json.loads(clip["stats"] or "{}").items():
                c.execute("INSERT OR REPLACE INTO stats_daily (day, clip_id, platform, views, data) VALUES (?, ?, ?, ?, ?)",
                          (day, clip["id"], p, v.get("views", 0), json.dumps(v)))
    return errors


def errors():
    return json.loads(db.get_setting("stats_errors") or "{}")


def totals(clips=None):
    """{platform: views} over every clip."""
    out = {}
    for c in clips if clips is not None else db.all("clips"):
        for p, n in json.loads(c.get("views") or "{}").items():
            out[p] = out.get(p, 0) + (n or 0)
    return out


def series(days=30):
    """Views gained per day per platform from the daily snapshots: [{"day", platform: gained...}], oldest first."""
    since = (date.today() - timedelta(days=days)).isoformat()
    with db.conn() as c:
        rows = c.execute("SELECT day, clip_id, platform, views FROM stats_daily WHERE day >= ? ORDER BY day", (since,)).fetchall()
    last, out = {}, {}
    for day, cid, p, views in rows:
        gained = max(0, views - last.get((cid, p), 0))
        last[(cid, p)] = views
        out.setdefault(day, {"day": day})
        out[day][p] = out[day].get(p, 0) + gained
    return list(out.values())


if __name__ == "__main__":
    import os
    if "CLIPPER_DATA" not in os.environ:
        raise SystemExit("run as: CLIPPER_DATA=<empty temp dir> python -m clipper.stats")
    db.init()
    assert totals([{"views": json.dumps({"youtube": 900, "instagram": 100})}, {"views": json.dumps({"youtube": 50})}]) == \
        {"youtube": 950, "instagram": 100}
    assert _values({"data": [{"name": "views", "values": [{"value": 12}]}, {"name": "reach", "total_value": {"value": 7}}]}) == \
        {"views": 12, "reach": 7}
    with db.conn() as c:
        for day, v in (("2026-11-18", 100), ("2026-11-19", 160), ("2026-11-20", 150)):  # a dip never counts as negative
            c.execute("INSERT INTO stats_daily (day, clip_id, platform, views) VALUES (?, 1, 'youtube', ?)", (day, v))
    s = {r["day"]: r["youtube"] for r in series(10_000)}
    assert s == {"2026-11-18": 100, "2026-11-19": 60, "2026-11-20": 0}, s
    print("stats ok")
