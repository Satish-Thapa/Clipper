import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = Path(os.getenv("CLIPPER_DATA", ROOT / "data"))
FFMPEG = os.getenv("FFMPEG", "ffmpeg")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
LIVE_CHUNK_SECONDS = int(os.getenv("LIVE_CHUNK_SECONDS", "600"))
SHORT_HARD_MAX = 180  # YouTube Shorts can't be longer than 3 min; no clip of any kind ever exceeds it
SHORT_MIN, SHORT_MAX = int(os.getenv("SHORT_MIN", "15")), min(int(os.getenv("SHORT_MAX", "60")), SHORT_HARD_MAX)
LONG_MIN, LONG_MAX = int(os.getenv("LONG_MIN", "60")), min(int(os.getenv("LONG_MAX", "180")), SHORT_HARD_MAX)
_logo = os.getenv("LOGO", str(ROOT / "clipper" / "assets" / "logo.png"))
LOGO = Path(_logo) if _logo else None  # watermark on every clip; LOGO= (empty) in .env turns it off
END_CARD = os.getenv("END_CARD", "FOLLOW FOR MORE")  # animated call to action at the end of every clip; empty = off
VOICE = os.getenv("VOICE", "")  # voice-over voice (macOS 'say' / Windows SAPI name); empty = system default
CROP_ASPECT = min(max(float(os.getenv("CROP_ASPECT", "1")), 9 / 16), 16 / 9)  # kept part of the frame, w/h
STRETCH_SHORT, STRETCH_LONG = int(os.getenv("STRETCH_SHORT", "10")), int(os.getenv("STRETCH_LONG", "10"))

YT_CLIENT_SECRETS = Path(os.getenv("YT_CLIENT_SECRETS", ROOT / "client_secret.json"))
YT_TOKEN = DATA / "youtube_token.json"

# Facebook Page + Instagram (business/creator account linked to that Page): long-lived Page access token.
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")
FB_PAGE_ID, FB_PAGE_TOKEN = os.getenv("FB_PAGE_ID", ""), os.getenv("FB_PAGE_TOKEN", "")
IG_USER_ID, IG_TOKEN = os.getenv("IG_USER_ID", ""), os.getenv("IG_TOKEN", "") or FB_PAGE_TOKEN
# TikTok Content Posting API. The refresh token rotates, so the current one lives in data/tiktok_token.json.
TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_KEY", ""), os.getenv("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REFRESH_TOKEN = os.getenv("TIKTOK_REFRESH_TOKEN", "")
TIKTOK_PRIVACY = os.getenv("TIKTOK_PRIVACY", "SELF_ONLY")  # unaudited TikTok apps may only post SELF_ONLY
TIKTOK_TOKEN = DATA / "tiktok_token.json"

# Topic search: only videos this long (minutes) are clipped. Too short = nothing to clip, too long = slow.
TOPIC_MIN_MINUTES = float(os.getenv("TOPIC_MIN_MINUTES", "3"))
TOPIC_MAX_MINUTES = float(os.getenv("TOPIC_MAX_MINUTES", "90"))
# Hourly WhatsApp report of views per clip/platform, sent through CallMeBot (free; see README).
WHATSAPP_PHONE, WHATSAPP_APIKEY = os.getenv("WHATSAPP_PHONE", ""), os.getenv("WHATSAPP_APIKEY", "")
REPORT_MINUTES = float(os.getenv("REPORT_MINUTES", "60"))
SEARCH_REGION = os.getenv("SEARCH_REGION", "US")  # country whose YouTube results topics search (trending there)

DATA.mkdir(parents=True, exist_ok=True)
