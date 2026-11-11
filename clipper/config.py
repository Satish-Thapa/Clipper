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
YT_CLIENT_SECRETS = Path(os.getenv("YT_CLIENT_SECRETS", ROOT / "client_secret.json"))
YT_TOKEN = DATA / "youtube_token.json"

DATA.mkdir(parents=True, exist_ok=True)
