"""YouTube Data API v3 upload. One-time browser OAuth, token cached in data/."""
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import YT_CLIENT_SECRETS, YT_TOKEN

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def connected():
    return YT_TOKEN.exists()


def connect():
    """Opens a browser for Google sign-in. Needs client_secret.json (see README)."""
    if not YT_CLIENT_SECRETS.exists():
        raise RuntimeError(f"Missing {YT_CLIENT_SECRETS.name} — see README 'YouTube upload setup'")
    creds = InstalledAppFlow.from_client_secrets_file(str(YT_CLIENT_SECRETS), SCOPES).run_local_server(port=0)
    YT_TOKEN.write_text(creds.to_json())


def _creds():
    creds = Credentials.from_authorized_user_file(str(YT_TOKEN), SCOPES)
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
    req = yt.videos().insert(
        part="snippet,status",
        body={"snippet": {"title": title[:100], "description": description[:5000], "tags": tags,
                          "categoryId": "20"},  # 20 = Gaming
              "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}},
        media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]
