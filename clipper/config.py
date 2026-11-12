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
VOICE = os.getenv("VOICE", "")  # voice-over voice (macOS 'say' / Windows SAPI name); empty = system default
CROP_ASPECT = min(max(float(os.getenv("CROP_ASPECT", "1")), 9 / 16), 16 / 9)  # kept part of the frame, w/h
STRETCH_SHORT, STRETCH_LONG = int(os.getenv("STRETCH_SHORT", "10")), int(os.getenv("STRETCH_LONG", "10"))

YT_CLIENT_SECRETS = Path(os.getenv("YT_CLIENT_SECRETS", ROOT / "client_secret.json"))
YT_TOKEN = DATA / "youtube_token.json"

DATA.mkdir(parents=True, exist_ok=True)
