"""YouTube Data API v3: sign in a channel, upload Shorts or normal videos, read view counts.

Each YouTube channel is an account (accounts.py) whose token is that channel's Google OAuth credentials JSON
(stored encrypted). Refreshed credentials are written back to the account."""
import json

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from . import config

# upload = post videos; readonly = channel name + view counts for the report.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def connected():
    from . import accounts
    return bool(accounts.all(platform="youtube"))


def connect():
    """Google sign-in in a browser tab on this computer, with the OAuth client pasted in Settings → YouTube.
    -> (credentials JSON, {"id", "title", "thumb"} of the channel that signed in)."""
    raw = config.YT_CLIENT_SECRET_JSON.strip()
    if not raw:
        raise RuntimeError("Paste your OAuth client (client_secret.json) in Settings → YouTube first")
    try:
        client = json.loads(raw)
    except ValueError:
        raise RuntimeError("The YouTube OAuth client in Settings isn't valid JSON: paste the whole file") from None
    # A "Desktop app" client accepts any http://localhost port. A "Web application" client only accepts addresses
    # registered on it, so it signs in on a fixed one: WEB_REDIRECT (add it under Authorized redirect URIs).
    hint = client_hint(raw)
    if hint:  # it would only end on Google's "redirect_uri_mismatch" page
        raise RuntimeError(hint)
    web = "web" in client
    try:
        creds = InstalledAppFlow.from_client_config(client, SCOPES).run_local_server(
            port=WEB_PORT if web else 0, open_browser=True, timeout_seconds=300)
    except OSError as e:
        raise RuntimeError(f"couldn't open the sign-in page on port {WEB_PORT}: is another sign-in still waiting? ({e})") from e
    return creds.to_json(), _channel(creds)


WEB_PORT = 8765
WEB_REDIRECT = f"http://localhost:{WEB_PORT}/"


def client_hint(raw):
    """A warning for Settings when the pasted OAuth client won't sign in as it is ("" when it's fine)."""
    try:
        client = json.loads(raw)
    except ValueError:
        return "That isn't valid JSON: paste the whole client_secret.json file."
    if "installed" in client:
        return ""
    if "web" in client:
        uris = client["web"].get("redirect_uris") or []
        if WEB_REDIRECT not in uris and WEB_REDIRECT.rstrip("/") not in uris:
            return ("This is a \"Web application\" OAuth client. Either create a \"Desktop app\" client instead "
                    "(Google Cloud → APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app) "
                    f"and paste that, or add {WEB_REDIRECT} under this client's Authorized redirect URIs, then download "
                    "and paste its JSON again.")
        return ""
    return "That doesn't look like an OAuth client file (expected \"installed\" or \"web\" at the top)."


def _creds(account):
    """The account's credentials, refreshed if needed (and the refreshed ones saved back)."""
    creds = Credentials.from_authorized_user_info(json.loads(account["token"]))
    if not creds.valid:
        creds.refresh(Request())
        if account.get("id"):
            from . import accounts
            accounts.set_token(account["id"], creds.to_json())
    return creds


def _channel(creds):
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items") or []
    if not items:
        raise RuntimeError("this Google account has no YouTube channel")
    s = items[0]["snippet"]
    return {"id": items[0]["id"], "title": s.get("title", "YouTube channel"),
            "thumb": ((s.get("thumbnails") or {}).get("default") or {}).get("url", "")}


def channel(account):
    """{"id", "title", "thumb"} of an account's channel (also checks its token still works)."""
    return _channel(_creds(account))


def upload(account, path, title, description, tags, short, privacy="public"):
    """short=True: a Short (#Shorts added; the video must be vertical and <= 3 min). False: a normal video."""
    if short and "#shorts" not in (title + description).lower():
        description = f"{description}\n\n#Shorts".strip()
    if tags:
        description += "\n" + " ".join(f"#{t}" for t in tags[:5])
    yt = build("youtube", "v3", credentials=_creds(account), cache_discovery=False)
    req = yt.videos().insert(
        part="snippet,status",
        body={"snippet": {"title": title[:100], "description": description[:5000], "tags": tags},
              "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}},
        media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def views(account, ids):
    """{video_id: view count} for uploads of this account (50 per API call)."""
    creds = _creds(account)
    if "https://www.googleapis.com/auth/youtube.readonly" not in (creds.scopes or SCOPES):
        raise RuntimeError("this YouTube login can't read views: reconnect it on the Accounts page")
    yt, out = build("youtube", "v3", credentials=creds, cache_discovery=False), {}
    for i in range(0, len(ids), 50):
        for v in yt.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute().get("items", []):
            out[v["id"]] = int(v["statistics"].get("viewCount", 0))
    return out
