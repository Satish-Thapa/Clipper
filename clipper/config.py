import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = Path(os.getenv("CLIPPER_DATA", ROOT / "data"))
FFMPEG = os.getenv("FFMPEG", "ffmpeg")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
# Any OpenAI-compatible provider instead of DeepSeek: cloud with an API key (OpenAI, OpenRouter, Groq, Gemini...)
# or local (Ollama/LM Studio, also on another machine). Empty LLM_BASE_URL = DeepSeek with the keys above.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")  # empty is fine for local servers
LLM_MODEL = os.getenv("LLM_MODEL", "")
# A small model on THIS computer (Ollama installed here), the last fallback when everything else fails
PC_LLM_BASE_URL = os.getenv("PC_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
PC_LLM_MODEL = os.getenv("PC_LLM_MODEL", "qwen3.5:2b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
# Everything you tune (clip lengths, slots, live pieces, keep sources, research...) is in Settings (settings.py).
_logo = os.getenv("LOGO", str(ROOT / "clipper" / "assets" / "logo.png"))
LOGO = Path(_logo) if _logo else None  # news posts only (switched off): their watermark
# News posts only (switched off): a message asking viewers to follow. FOLLOW_LINE set = always
# that one; empty = FOLLOW_LINES in turn (clip 1 gets the 1st, clip 2 the 2nd... then from the top again).
FOLLOW_LINE = os.getenv("FOLLOW_LINE", "").strip()
FOLLOW_LINES = ("I'm working hard to make these videos. One follow from you would mean the world ❤️",
                "Please break your scrolling streak for 2 seconds and hit follow. It costs you nothing 🥹",
                "I'm not asking for much, just one little follow. Help a small creator grow ❤️",
                "Every follow motivates me to create something even crazier. Please support!",
                "You have no idea how happy one follow from you would make me.")
VOICE = os.getenv("VOICE", "")  # news posts only (switched off): the voice-over voice

# "Sign in to confirm you're not a bot": YouTube wants a signed-in browser's cookies. Export cookies.txt from a
# logged-in youtube.com tab (extension "Get cookies.txt LOCALLY") to YT_COOKIES, or upload it on the Socials page.
# Or YT_COOKIES_FROM_BROWSER=firefox (Chrome/Edge on Windows lock + encrypt their cookies, so yt-dlp can't read them).
YT_COOKIES = Path(os.getenv("YT_COOKIES", ROOT / "cookies.txt"))
YT_COOKIES_FROM_BROWSER = os.getenv("YT_COOKIES_FROM_BROWSER", "")
# Wikipedia only answers tools that say who runs them (its robot policy): your email or a website. Set it to use
# Wikipedia for fact posts; without it, facts come from news articles about the topic (also sourced).
WIKI_CONTACT = os.getenv("WIKI_CONTACT", "")
YT_CLIENT_SECRETS = Path(os.getenv("YT_CLIENT_SECRETS", ROOT / "client_secret.json"))
YT_TOKEN = DATA / "youtube_token.json"

# Facebook Page + Instagram (business/creator account linked to that Page): long-lived Page access token.
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")
FB_PAGE_ID, FB_PAGE_TOKEN = os.getenv("FB_PAGE_ID", ""), os.getenv("FB_PAGE_TOKEN", "")
IG_USER_ID, IG_TOKEN = os.getenv("IG_USER_ID", ""), os.getenv("IG_TOKEN", "") or FB_PAGE_TOKEN
# Instagram's Reels spec caps video at 25 Mbps (a 20s clip at 25.3 Mbps failed): files above this are re-encoded
# to IG_BITRATE for Instagram only.
IG_MAX_MBPS, IG_BITRATE = 20, "14M"
# TikTok Content Posting API. The refresh token rotates, so the current one lives in data/tiktok_token.json.
TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_KEY", ""), os.getenv("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REFRESH_TOKEN = os.getenv("TIKTOK_REFRESH_TOKEN", "")
TIKTOK_PRIVACY = os.getenv("TIKTOK_PRIVACY", "SELF_ONLY")  # unaudited TikTok apps may only post SELF_ONLY
TIKTOK_TOKEN = DATA / "tiktok_token.json"

DATA.mkdir(parents=True, exist_ok=True)
