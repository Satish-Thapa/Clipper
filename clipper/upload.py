"""YouTube Data API v3 upload. One-time browser OAuth, token cached in data/."""
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from pathlib import Path

from . import db, render
from .config import YT_CLIENT_SECRETS, YT_TOKEN

# upload = post videos; readonly = read their numbers; yt-analytics.readonly = watch time, % watched, subscribers
# gained (Reports page). Tokens from before a scope was added lack it: Reconnect YouTube on the Socials page.
READ = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", READ, ANALYTICS]


def connected():
    return YT_TOKEN.exists()


def connect():
    """Opens a browser for Google sign-in. Needs client_secret.json (see README)."""
    if not YT_CLIENT_SECRETS.exists():
        raise RuntimeError(f"Missing {YT_CLIENT_SECRETS.name} — see README 'YouTube upload setup'")
    creds = InstalledAppFlow.from_client_secrets_file(str(YT_CLIENT_SECRETS), SCOPES).run_local_server(port=0)
    YT_TOKEN.write_text(creds.to_json())


def _creds():
    creds = Credentials.from_authorized_user_file(str(YT_TOKEN))  # the scopes actually granted (saved in the token)
    if not creds.valid:
        creds.refresh(Request())
        YT_TOKEN.write_text(creds.to_json())
    return creds


def upload(path, title, description, tags, short, privacy="public"):
    if short and "#shorts" not in (title + description).lower():
        description = f"{description}\n\n#Shorts".strip()
    if tags:
        description += "\n" + " ".join(f"#{t}" for t in tags[:5])
    yt = build("youtube", "v3", credentials=_creds(), cache_discovery=False)
    try:  # a 1440p copy: YouTube gives it the better encode (sharper for viewers); the 1080p clip if that fails
        send = render.youtube_copy(path)
    except Exception as e:
        db.syslog(f"YouTube 1440p copy failed ({e!r}), uploading the 1080p clip")
        send = path
    try:
        req = yt.videos().insert(
            part="snippet,status",
            body={"snippet": {"title": title[:100], "description": description[:5000], "tags": tags,
                              "categoryId": "20"},  # 20 = Gaming
                  "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}},
            media_body=MediaFileUpload(str(send), chunksize=-1, resumable=True))
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"]
    finally:
        if str(send) != str(path):
            Path(send).unlink(missing_ok=True)


def granted():
    """Which read scopes the saved login has (the Socials page asks to reconnect when one is missing)."""
    try:
        scopes = Credentials.from_authorized_user_file(str(YT_TOKEN)).scopes or []
    except Exception:
        return {"read": False, "analytics": False}
    return {"read": READ in scopes, "analytics": ANALYTICS in scopes}


def statistics(ids):
    """{video_id: {viewCount, likeCount, commentCount}} for our uploads (50 per API call). Needs the readonly scope."""
    creds = _creds()
    if READ not in (creds.scopes or []):
        raise RuntimeError("YouTube login can't read numbers yet: click Reconnect YouTube on the Socials page")
    yt, out = build("youtube", "v3", credentials=creds, cache_discovery=False), {}
    for i in range(0, len(ids), 50):
        for v in yt.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute().get("items", []):
            out[v["id"]] = v["statistics"]
    return out


def _analytics():
    creds = _creds()
    if ANALYTICS not in (creds.scopes or []):
        raise RuntimeError("YouTube analytics not granted yet: click Reconnect YouTube on the Socials page")
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def _rows(resp):
    cols = [h["name"] for h in resp.get("columnHeaders") or []]
    return [dict(zip(cols, r)) for r in resp.get("rows") or []]


def analytics(ids, metrics):
    """{video_id: {metric: value}} over each video's whole life (YouTube Analytics, 50 videos per call)."""
    from datetime import date
    api, out = _analytics(), {}
    for i in range(0, len(ids), 50):
        resp = api.reports().query(ids="channel==MINE", startDate="2015-01-01", endDate=date.today().isoformat(),
                                   metrics=metrics, dimensions="video", filters="video==" + ",".join(ids[i:i + 50]),
                                   maxResults=50).execute()
        out.update({r.pop("video"): r for r in _rows(resp)})
    return out


def channel_report(days=28):
    """The whole channel, last `days` days: views / minutes watched / subscribers per day, and where views come from."""
    from datetime import date, timedelta
    api, end = _analytics(), date.today()
    q = dict(ids="channel==MINE", startDate=(end - timedelta(days=days)).isoformat(), endDate=end.isoformat())
    per_day = _rows(api.reports().query(**q, metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost",
                                        dimensions="day", sort="day").execute())
    sources = _rows(api.reports().query(**q, metrics="views", dimensions="insightTrafficSourceType", sort="-views").execute())
    return {"days": per_day, "sources": sources, "at": time.time()}
