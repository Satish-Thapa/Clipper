"""Every setting lives in the database and is edited on the Settings page; nothing has to be typed into .env.

Read a setting as an attribute at the moment you need it: `config.DEEPSEEK_API_KEY`, `config.SHORT_MAX`, ...
(module __getattr__, so a value saved in the UI applies right away). Never `from .config import X` for a
setting: that would freeze the value at import time. Only paths and fixed limits are real module constants.

First start: whatever an old .env / client_secret.json held is copied into the database once (migrate()), so an
existing setup keeps working. After that .env is never read again."""
import json
import os
import shutil
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Where the database, downloads and clips live. The only thing that can't be a database setting (the database is in
# it); CLIPPER_DATA in the environment moves it, which only tests need.
DATA = Path(os.environ.get("CLIPPER_DATA", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)

# Fixed facts, not preferences.
SHORT_HARD_MAX = 180  # YouTube Shorts can't be longer than 3 min; no clip of any kind ever exceeds it
IG_MAX_MBPS, IG_BITRATE = 20, "14M"  # Instagram rejects Reels over 25 Mbps: heavier files get an IG-only re-encode
YT_TOKEN = DATA / "youtube_token.json"  # written by the Google sign-in
TIKTOK_TOKEN = DATA / "tiktok_token.json"  # TikTok's refresh token rotates; the current one is kept here
YT_COOKIES = ROOT / "cookies.txt"  # uploaded on the Socials page
LOGO_FILE = DATA / "logo.png"  # uploaded on the Settings page (Branding); no logo is built in

ASPECTS = [("0.5625", "Full screen 9:16"), ("0.8", "4:5 (thin bars)"), ("1", "Square 1:1"), ("1.7778", "Whole frame 16:9")]

# The Settings page is built from this list: (key, group, label, type, default, help).
# Types: secret (write-only), text, str, int, float, bool, select:<a|b|c>, aspect, account:<platform>.
FIELDS = [
    ("LLM_PROVIDER", "AI", "AI provider", "select:deepseek|openai|openrouter|groq|ollama|custom", "deepseek",
     "Any OpenAI-compatible service. Picking one fills in its address below. ollama = your own Ollama server (no key "
     "needed; the saved key isn't sent to it), e.g. through an SSH tunnel on http://127.0.0.1:11435/v1."),
    ("LLM_BASE_URL", "AI", "API address (base URL)", "str", "https://api.deepseek.com",
     "e.g. https://api.openai.com/v1 · https://openrouter.ai/api/v1 · https://api.groq.com/openai/v1"),
    ("DEEPSEEK_API_KEY", "AI", "API key", "secret", "", "The key from your AI provider's dashboard."),
    ("DEEPSEEK_MODEL", "AI", "Model for picking clips", "str", "deepseek-v4-pro",
     "The provider's model name. A thorough model works best here."),
    ("POST_MODEL", "AI", "Model for writing posts", "str", "deepseek-flash", "Short posts: a fast model is fine."),
    ("VISION_MODEL", "AI", "Model that can see images (optional)", "str", "",
     "Same provider. It looks at 2 frames of each candidate when picking clips, e.g. gpt-4o-mini on OpenAI. "
     "Empty = text only (DeepSeek can't see images)."),
    ("TWO_PASS", "AI", "Two-pass clip picking", "bool", True,
     "The fast model screens many more candidates first, the thorough one picks from the best. Better clips, "
     "a little more AI cost."),
    ("CHANNEL_FOCUS", "AI", "What your channel is about", "text", "",
     "Optional. E.g. the game, sport or kind of content you post. Steers moment picking, titles and hashtags. "
     "Empty = it works out each video by itself."),
    ("CONTENT_LANGUAGE", "AI", "Language for titles, captions and posts", "str", "English",
     "E.g. Nepali (नेपाली) or Hindi. Clip text follows this; Devanagari renders on clips and cards."),
    ("ANY_NICHE", "AI", "Autopilot with no niche looks for", "text", "anything trending this week that makes great short clips",
     "What the autopilot searches when its niche is left empty."),

    ("SHORT_MIN", "Clips", "Short clip: min seconds", "int", 15, ""),
    ("SHORT_MAX", "Clips", "Short clip: max seconds", "int", 60, "At most 180 (the YouTube Shorts limit)."),
    ("LONG_MIN", "Clips", "Long clip: min seconds", "int", 60, ""),
    ("LONG_MAX", "Clips", "Long clip: max seconds", "int", 180, "At most 180."),
    ("CROP_ASPECT", "Clips", "Default framing", "aspect", "1", "How much of the frame a vertical clip keeps."),
    ("END_CARD", "Clips", "End card (follow prompt)", "text",
     "WANT MORE {TOPIC}?|NEW CLIPS EVERY DAY|FOLLOW {HANDLE}\nTHE BEST {TOPIC} MOMENTS|HAND-PICKED DAILY|FOLLOW {HANDLE}\n"
     "DON'T MISS TOMORROW'S|FOLLOW {HANDLE}",
     "Animated call to action at the end of each clip: say what a follower gets. One version per line (Clipper tries "
     "each and uses the one whose clips get more views more often); \"|\" splits a version into its lines, the last "
     "goes on the red box. {HANDLE} = the category's handle (else the watermark, else the brand name), {TOPIC} = its "
     "topic. Empty = no end card."),
    ("POST_CTA", "Branding", "Follow line on posts", "str", "Follow {handle} for daily {topic}",
     "Added as the last line of made posts' captions and as a final slide on carousels and Reels. {handle} / {topic} "
     "as for the end card. Empty = none."),
    ("MUSIC_ON", "Branding", "Music on posts", "bool", True,
     "Posts that go up as Reels get a track from your music library (Settings → Branding → Music), picked by the AI to "
     "fit the story. Never on clips. Instagram's own music can't be added through Meta's API."),
    ("LOGO_ON", "Branding", "Show my logo on clips", "bool", True, "Upload the logo below. No logo uploaded = none shown."),
    ("WATERMARK", "Branding", "Watermark text on clips", "str", "",
     "E.g. @yourchannel, shown faintly on every clip. Empty = none."),
    ("VOICE", "Clips", "Voice-over voice", "str", "", "A macOS 'say' / Windows voice name. Empty = system default."),
    ("CAPTIONS_FIRST", "Clips", "Use YouTube's own captions when a video has them", "bool", True,
     "Instant, and usually better for Hindi and other languages. Only captions in the language actually spoken are "
     "used (YouTube's automatic ones sometimes guess the language wrong). Off = always transcribe."),
    ("WHISPER_FOR", "Clips", "Always transcribe these languages", "str", "en",
     "Spoken-language codes (comma-separated) that Whisper transcribes even when YouTube has captions: it's more "
     "accurate than YouTube's for English. The language is detected from the audio first. Empty = captions for all."),
    ("DELETE_CLIPS_AFTER_UPLOAD", "Clips", "Delete clip files once published", "bool", True,
     "When a clip has gone up to every destination it was sent to, its video files are deleted to save space (the "
     "thumbnail stays in Library). A clip with a failed destination keeps its file so Retry still works."),
    ("SOURCE_KEEP_HOURS", "Clips", "Keep source videos for (hours)", "float", 6,
     "Downloaded source videos are deleted this long after their job finishes (you can still cut a video by hand "
     "until then). 0 = as soon as the job is done. Autopilot ones go right after clipping unless its \"Keep source "
     "videos\" is on."),
    ("TRANSCRIBE_ENGINE", "Clips", "Transcription engine", "select:auto|mlx|local|cloud", "auto",
     "auto = Mac GPU on Apple Silicon, else local (NVIDIA GPU if there is one, else CPU). cloud = the API below."),
    ("WHISPER_MODEL", "Clips", "Transcription model", "select:auto|tiny|base|small|medium|large-v3|large-v3-turbo", "auto",
     "auto = large-v3-turbo on a GPU (accurate, fast), small on a CPU."),
    ("WHISPER_LANGUAGE", "Clips", "Audio language", "str", "",
     "Language code of the speech, e.g. hi, ne, en. Empty = detect it. Setting it helps Hindi a lot."),
    ("TRANSCRIBE_API_URL", "Clips", "Cloud transcription address", "str", "https://api.groq.com/openai/v1",
     "OpenAI-compatible, e.g. Groq (fast, cheap) or https://api.openai.com/v1"),
    ("TRANSCRIBE_API_KEY", "Clips", "Cloud transcription key", "secret", "", "Empty = the AI API key above."),
    ("TRANSCRIBE_API_MODEL", "Clips", "Cloud transcription model", "str", "whisper-large-v3-turbo",
     "Groq: whisper-large-v3-turbo or whisper-large-v3. OpenAI: whisper-1."),
    ("TRANSCRIBE_BATCH", "Clips", "Transcription batch size", "int", 8,
     "Several pieces of audio transcribed together: much faster. 0 = off (one at a time)."),
    ("TRANSCRIBE_WORKERS", "Clips", "Transcriptions at the same time", "int", 2,
     "For several jobs or live chunks at once; the CPU (or GPU) is shared between them. 1-4."),
    ("LIVE_CHUNK_SECONDS", "Clips", "Live streams: chunk length (s)", "int", 600, "Clips are made from each chunk while it streams."),

    ("SEARCH_REGION", "Finding videos", "Country", "str", "US", "Two-letter code: whose YouTube results and Google News are used."),
    ("NEWS_FEEDS", "Finding videos", "News sources for posts", "text", "google\nbing",
     "One per line. google / bing = their news search for the category's topic. Or any RSS feed address: put {q} "
     "where the topic goes (a search feed), or leave it out (a site's feed: only stories matching the topic or "
     "keywords are used). Each source is asked at most every 20 minutes."),
    ("AUTOMATIONS_AT_ONCE", "Finding videos", "Automations at once", "int", 2,
     "How many topics / autopilot videos are worked on at the same time (1-6). More = faster with several autopilots, "
     "but they share the computer: 2 suits most Macs and PCs."),
    ("TOPIC_MIN_MINUTES", "Finding videos", "Shortest video (min)", "float", 3, ""),
    ("TOPIC_MAX_MINUTES", "Finding videos", "Longest video (min)", "float", 90, ""),

    ("YT_CLIENT_SECRET_JSON", "YouTube", "OAuth client (client_secret.json contents)", "secret", "",
     "Google Cloud → Credentials → OAuth client ID (Desktop app) → download JSON, paste it here. Then Connect "
     "YouTube on the Socials page."),
    ("YT_COOKIES_FROM_BROWSER", "YouTube", "Read YouTube cookies from", "select:|firefox|chrome|safari|edge|brave", "",
     "Fixes \"confirm you're not a bot\". Or upload a cookies.txt on the Socials page. Chrome/Edge don't work on Windows."),
    ("YT_FORCE_IPV4", "YouTube", "Download over IPv4", "bool", True,
     "Leave on: some providers' YouTube caches time out over IPv6."),

    ("META_APP_ID", "Facebook & Instagram", "Meta App ID", "str", "",
     "developers.facebook.com → your app → App settings → Basic → App ID (the number in the app's address, not an "
     "Instagram account id). With the App secret, Graph API Explorer tokens are made long-lived automatically."),
    ("META_APP_SECRET", "Facebook & Instagram", "Meta App secret", "secret", "",
     "Same page (App settings → Basic → App secret → Show). Not the \"Instagram app secret\" from the Instagram setup page."),
    ("CLIP_FB_ACCOUNT", "Facebook & Instagram", "Default Facebook Page", "account:facebook", "",
     "For Uncategorized work; categories choose their own accounts."),
    ("CLIP_IG_ACCOUNT", "Facebook & Instagram", "Default Instagram account", "account:instagram", "", ""),
    ("IG_MAX_SECONDS", "Facebook & Instagram", "Instagram: longest Reel (s)", "float", 45,
     "Instagram's API refuses longer Reels (45 s worked, 50 s was refused), so a longer clip goes to Instagram as its "
     "last this-many seconds (the payoff). Other platforms get the whole clip. 0 = send it whole."),
    ("GRAPH_VERSION", "Facebook & Instagram", "Graph API version", "str", "v25.0",
     "Meta retires old versions; bump it when they announce one."),

    ("TIKTOK_CLIENT_KEY", "TikTok", "Client key", "str", "", "developers.tiktok.com → your app."),
    ("TIKTOK_CLIENT_SECRET", "TikTok", "Client secret", "secret", "", ""),
    ("TIKTOK_REFRESH_TOKEN", "hidden", "Refresh token (old)", "secret", "", ""),
    ("TIKTOK_PRIVACY", "TikTok", "Who can see posts", "select:SELF_ONLY|PUBLIC_TO_EVERYONE|MUTUAL_FOLLOW_FRIENDS|FOLLOWER_OF_CREATOR",
     "SELF_ONLY", "Unaudited apps may only post private (SELF_ONLY)."),

    ("WA_PHONE_NUMBER_ID", "WhatsApp report", "Sender phone number ID", "str", "", "Meta app → WhatsApp → API setup."),
    ("WA_TOKEN", "WhatsApp report", "Access token", "secret", "", ""),
    ("WA_TO", "WhatsApp report", "Send to (your number)", "str", "", "Country code first, e.g. 9779800000000."),
    ("WA_TEMPLATE", "WhatsApp report", "Message template", "str", "", "Needed to always deliver (see SOCIALS.md)."),
    ("WA_TEMPLATE_LANG", "WhatsApp report", "Template language", "str", "en_US", ""),
    ("REPORT_MINUTES", "WhatsApp report", "Report every (minutes)", "float", 60, ""),

    ("FFMPEG", "Advanced", "ffmpeg path", "str", "", "Empty = found automatically."),
    ("STRETCH_SHORT", "Advanced", "Short may run over by (s)", "int", 10, "When a moment needs its setup or payoff."),
    ("STRETCH_LONG", "Advanced", "Long may run over by (s)", "int", 10, ""),
]
SPEC = {f[0]: f for f in FIELDS}
_lock = threading.Lock()
_cache = {}  # the saved values, loaded once; save() keeps it current


def _coerce(key, v):
    kind, default = SPEC[key][3], SPEC[key][4]
    if v is None:
        return default
    try:
        if kind == "int":
            return int(float(v))
        if kind == "float":
            return float(v)
        if kind == "bool":
            return v if isinstance(v, bool) else str(v).strip().lower() not in ("0", "false", "no", "off", "")
        if kind.startswith("account:"):
            return int(v) if str(v).strip() else ""
    except (TypeError, ValueError):
        return default
    return str(v)


def _values():
    if "v" not in _cache:
        import sqlite3
        from . import db
        with _lock:
            try:
                raw = db.get_setting("config")
            except sqlite3.OperationalError:  # a brand-new data folder before db.init(): defaults, not cached
                return {}
            _cache["v"] = json.loads(raw) if raw else {}
    return _cache["v"]


def get(key):
    v = _values().get(key)
    if SPEC[key][3] == "secret" and v:
        from . import crypto
        v = crypto.decrypt(v)  # secrets are stored encrypted (crypto.py)
    return _coerce(key, v)


def save(new):
    """Merge `new` into the saved settings. A secret sent as "" is left as it was; None removes a value (back to the
    default). Returns the keys that changed."""
    from . import db
    vals, changed = dict(_values()), []
    for k, v in new.items():
        if k not in SPEC or (SPEC[k][3] == "secret" and v == ""):
            continue
        if v is None:
            if k in vals:
                vals.pop(k)
                changed.append(k)
            continue
        v = _coerce(k, v)
        if SPEC[k][3] == "secret":
            from . import crypto
            if get(k) != v:
                vals[k] = crypto.encrypt(v)
                changed.append(k)
        elif vals.get(k, SPEC[k][4]) != v:
            vals[k] = v
            changed.append(k)
    with _lock:
        db.set_setting("config", json.dumps(vals))
        _cache["v"] = vals
    return changed


def public():
    """For the Settings page: every field with its value. Secrets are never sent back, only whether one is saved."""
    vals, out = _values(), []
    for key, group, label, kind, default, help_ in FIELDS:
        if group == "hidden":
            continue
        f = {"key": key, "group": group, "label": label, "type": kind, "help": help_}
        if kind == "secret":
            f["set"] = bool(vals.get(key))
        else:
            f["value"] = get(key)
        out.append(f)
    return out


def _account(aid):
    if not aid:
        return None
    from . import db
    return db.get("accounts", int(aid))


def _default_ffmpeg():
    """`ffmpeg` on PATH, else Homebrew's keg-only ffmpeg-full (macOS; it has libass for burned-in captions)."""
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    for p in ("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "/usr/local/opt/ffmpeg-full/bin/ffmpeg"):
        if Path(p).exists():
            return p
    return "ffmpeg"


def __getattr__(name):
    """config.<SETTING>, plus the values worked out from settings."""
    if name in SPEC:
        v = get(name)
        if name in ("SHORT_MAX", "LONG_MAX"):
            return min(max(v, 5), SHORT_HARD_MAX)
        if name == "CROP_ASPECT":
            return min(max(float(v), 9 / 16), 16 / 9)
        if name == "WA_TO":
            return v.lstrip("+").replace(" ", "")
        if name == "FFMPEG":
            return v.strip() or _default_ffmpeg()
        return v
    if name == "LOGO":  # your uploaded logo, or None (switched off / none uploaded): nothing is built in
        return LOGO_FILE if get("LOGO_ON") and LOGO_FILE.exists() else None
    # The Facebook Page / Instagram account clips upload to: chosen on the Settings page from the Accounts list.
    if name in ("FB_PAGE_ID", "FB_PAGE_TOKEN"):
        a = _account(get("CLIP_FB_ACCOUNT"))
        return (a["ext_id"] if name == "FB_PAGE_ID" else a["token"]) if a else ""
    if name in ("IG_USER_ID", "IG_TOKEN"):
        a = _account(get("CLIP_IG_ACCOUNT"))
        return (a["ext_id"] if name == "IG_USER_ID" else a["token"]) if a else ""
    raise AttributeError(f"module 'clipper.config' has no attribute {name!r}")


def encrypt_saved_secrets():
    """Secrets saved before encryption existed (e.g. copied from .env) -> encrypted. Runs at every start; cheap."""
    from . import crypto, db
    vals = dict(_values())
    todo = [k for k, v in vals.items() if k in SPEC and SPEC[k][3] == "secret" and v and not crypto.is_encrypted(v)]
    if todo:
        for k in todo:
            vals[k] = crypto.encrypt(vals[k])
        with _lock:
            db.set_setting("config", json.dumps(vals))
            _cache["v"] = vals


def migrate():
    """Once, on the first start with this version: copy an old .env (+ client_secret.json, + its Facebook Page /
    Instagram account) into the database. Never runs again, so .env is ignored from then on."""
    from . import db
    encrypt_saved_secrets()
    if db.get_setting("config_migrated"):
        return
    env = {}
    try:
        from dotenv import dotenv_values
        env = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v}
    except Exception:
        pass
    new = {k: v for k, v in env.items() if k in SPEC}
    if "LOGO" in env and not env["LOGO"]:
        new["LOGO_ON"] = False
    secrets_file = Path(env.get("YT_CLIENT_SECRETS") or ROOT / "client_secret.json")
    if secrets_file.exists():
        new["YT_CLIENT_SECRET_JSON"] = secrets_file.read_text(encoding="utf-8")
    if "END_CARD" not in env and db.all("jobs"):
        # an existing install had the old built-in end card on every clip: keep it until changed in Settings
        new["END_CARD"] = "IF YOU MADE IT TO|THE END OF THIS VIDEO,|YOU LEGALLY HAVE TO|FOLLOW ME"
    save(new)
    encrypt_saved_secrets()
    # the .env Page / Instagram account -> the Accounts list, and chosen for clips
    from . import accounts
    for platform, ext, page, token, key in (
            ("facebook", env.get("FB_PAGE_ID"), env.get("FB_PAGE_ID"), env.get("FB_PAGE_TOKEN"), "CLIP_FB_ACCOUNT"),
            ("instagram", env.get("IG_USER_ID"), env.get("FB_PAGE_ID"), env.get("IG_TOKEN") or env.get("FB_PAGE_TOKEN"),
             "CLIP_IG_ACCOUNT")):
        if ext and page and token:
            try:
                save({key: accounts.add_meta(platform, ext, token, page, verify=False)})
            except Exception as e:
                db.syslog(f"settings: couldn't bring the {platform} account over from .env: {e!r}")
    db.set_setting("config_migrated", "1")
    if new:
        db.syslog(f"settings: copied {len(new)} value(s) from .env into the database ({', '.join(sorted(new))})")
