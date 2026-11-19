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
from contextlib import contextmanager
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
YT_COOKIES = ROOT / "cookies.txt"  # uploaded in Settings → Downloads
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
    ("OLLAMA_KEEP_WARM", "AI", "Keep the local model loaded while busy", "bool", True,
     "ollama only: Ollama unloads an idle model after 5 minutes and the next request waits ~15 s while it reloads. On = "
     "keep it loaded while Clipper is working (an AI call in the last 30 min, or an automation inside its hours); it "
     "unloads as usual when Clipper goes quiet."),
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
     "Posts that go up as Reels (Instagram only: Facebook always gets the still card) get a track from your music "
     "library (Settings → Branding → Music), picked by the AI to fit the story. Never on clips. Instagram's own music "
     "can't be added through Meta's API."),
    ("LOGO_ON", "Branding", "Show my logo on clips", "bool", True, "Upload the logo below. No logo uploaded = none shown."),
    ("WATERMARK", "Branding", "Watermark text on clips", "str", "",
     "E.g. @yourchannel, shown faintly on every clip. Empty = none."),
    ("MIN_CLIP_SCORE", "Clips", "Only keep clips the AI scores at least", "int", 80,
     "0-100. Higher = pickier: fewer, stronger clips (most moments score 40-75; 85+ is a moment people share)."),
    ("LIVE_CLIPS_PER_CHUNK", "Clips", "Live streams: most clips per chunk", "int", 3,
     "The best few moments of each chunk, whatever a job asks for (a 3-hour stream at 3 = about 54 clips at most)."),
    ("COLD_OPEN", "Clips", "Cold open: flash the payoff first", "bool", True,
     "The clip opens on 1.5 s of its best moment with \"WAIT FOR IT...\", then plays from the start: hooks scrollers."),
    ("CAPTION_HIGHLIGHT", "Clips", "Spoken word colour in captions", "str", "#FFE500",
     "The word being said lights up in this colour while the rest stay white (hex: #FFE500 yellow, #39FF14 green, "
     "#00E5FF cyan). Empty = plain white captions."),
    ("VOICE", "Clips", "Voice-over voice", "str", "", "A macOS 'say' / Windows voice name. Empty = system default."),
    ("CAPTIONS_FIRST", "Clips", "Use YouTube's own captions when a video has them", "bool", True,
     "Instant, and usually better for Hindi and other languages. Only captions in the language actually spoken are "
     "used (YouTube's automatic ones sometimes guess the language wrong). Off = always transcribe."),
    ("WHISPER_FOR", "Clips", "Always transcribe these languages", "str", "en",
     "Spoken-language codes (comma-separated) that Whisper transcribes even when YouTube has captions: it's more "
     "accurate than YouTube's for English. The language is detected from the audio first. Empty = captions for all."),
    ("POST_GAP_MIN", "Clips", "Minutes between posts on one account", "int", 30,
     "However many automations post to an account (clip autopilots and post automations), it gets at most one post "
     "per this many minutes: a post that's due waits for the gap instead of going up right after another. Platforms "
     "treat bursts as spam. Your own \"Post now\" and timed schedules aren't held back. 0 = no gap."),
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
     "YouTube in Settings → Downloads."),
    ("YT_COOKIES_FROM_BROWSER", "YouTube", "Read YouTube cookies from", "select:|firefox|chrome|safari|edge|brave", "",
     "Fixes \"confirm you're not a bot\". Or upload a cookies.txt in Settings → Downloads. Chrome/Edge don't work on Windows."),
    ("YT_MAX_PER_DAY", "YouTube", "Most uploads a day", "int", 6,
     "The autopilot sends at most this many clips to YouTube in any 24 hours. YouTube blocks a channel's uploads for a "
     "day after too many (\"uploadLimitExceeded\"), especially in a burst. Other platforms aren't affected. 0 = no cap."),
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

# Settings that belong to a brand (its look, voice and audience): each brand has its own, starting from the defaults.
# Everything else (AI keys and models, ffmpeg, downloads, the Meta / YouTube apps, cleanup) is shared by all brands.
BRAND_KEYS = {"CHANNEL_FOCUS", "CONTENT_LANGUAGE", "ANY_NICHE", "END_CARD", "POST_CTA", "MUSIC_ON", "LOGO_ON", "WATERMARK",
              "VOICE", "CROP_ASPECT", "SEARCH_REGION", "NEWS_FEEDS", "CLIP_FB_ACCOUNT", "CLIP_IG_ACCOUNT", "POST_GAP_MIN",
              "IG_MAX_SECONDS", "YT_MAX_PER_DAY", "WHISPER_LANGUAGE",
              "MIN_CLIP_SCORE", "COLD_OPEN"}
_tl = threading.local()


@contextmanager
def brand(bid):
    """Work as brand `bid`: its settings are the ones read (background work for a brand the app isn't showing)."""
    prev = getattr(_tl, "bid", None)
    _tl.bid = int(bid) if bid else None
    try:
        yield
    finally:
        _tl.bid = prev


def brand_of_category(cid):
    from . import db
    c = db.get("post_categories", int(cid)) if cid else None
    return c["brand_id"] if c else None


def active_brand():
    """The brand whose settings apply right now: the one set for this piece of work, else the one the app shows."""
    bid = getattr(_tl, "bid", None)
    if bid:
        return bid
    from . import db
    try:
        return int(db.get_setting("current_brand") or 0) or None
    except Exception:
        return None


def in_brand(resolve):
    """Decorator: run the function as the brand `resolve(*args)` returns (a job's / clip's / post's category's brand).
    Errors resolving it just leave the current brand."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                bid = resolve(*a, **k)
            except Exception:
                bid = None
            if not bid:
                return fn(*a, **k)
            with brand(bid):
                return fn(*a, **k)
        return wrapper
    return deco


def migrate_brands():
    """Once: the branding settings saved before brands had their own go to the brand that has accounts (the real
    page), with the uploaded logo; other brands start from the defaults."""
    from . import db
    if db.get_setting("brand_config_migrated"):
        return
    with db.conn() as c:
        row = c.execute("SELECT brand_id, COUNT(*) n FROM accounts GROUP BY brand_id ORDER BY n DESC").fetchone()
    bid = (row[0] if row else None) or active_brand()
    if bid:
        old = {k: v for k, v in _values().items() if k in BRAND_KEYS}
        if old:
            db.set_setting(f"config_b{bid}", json.dumps({**old, **_brand_values(bid)}))
            _cache.pop(f"b{bid}", None)
        if LOGO_FILE.exists() and not logo_file(bid).exists():
            logo_file(bid).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(LOGO_FILE, logo_file(bid))
    db.set_setting("brand_config_migrated", "1")


def _brand_values(bid):
    key = f"b{bid}"
    if key not in _cache:
        from . import db
        try:
            raw = db.get_setting(f"config_b{bid}")
        except Exception:
            return {}
        _cache[key] = json.loads(raw) if raw else {}
    return _cache[key]


def logo_file(bid=None):
    """The brand's uploaded logo (data/logos/<brand>.png)."""
    bid = bid or active_brand()
    return DATA / "logos" / f"{bid}.png" if bid else LOGO_FILE


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
    v = _brand_values(active_brand()).get(key) if key in BRAND_KEYS else _values().get(key)
    if SPEC[key][3] == "secret" and v:
        from . import crypto
        v = crypto.decrypt(v)  # secrets are stored encrypted (crypto.py)
    return _coerce(key, v)


def save(new):
    """Merge `new` into the saved settings. A secret sent as "" is left as it was; None removes a value (back to the
    default). Returns the keys that changed."""
    from . import db
    bid = active_brand()
    mine = {k: v for k, v in new.items() if k in BRAND_KEYS}
    new = {k: v for k, v in new.items() if k not in BRAND_KEYS}
    changed = []
    if mine and bid:  # this brand's own settings
        bvals = dict(_brand_values(bid))
        for k, v in mine.items():
            if v is None:
                if bvals.pop(k, None) is not None:
                    changed.append(k)
                continue
            v = _coerce(k, v)
            if bvals.get(k, SPEC[k][4]) != v:
                bvals[k] = v
                changed.append(k)
        with _lock:
            db.set_setting(f"config_b{bid}", json.dumps(bvals))
            _cache[f"b{bid}"] = bvals
    vals = dict(_values())
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
        f = {"key": key, "group": group, "label": label, "type": kind, "help": help_, "brand": key in BRAND_KEYS}
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
    if shutil.which("ffmpeg"):  # the full path: yt-dlp's ffmpeg_location takes a bare "ffmpeg" for a file name
        return shutil.which("ffmpeg")
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
        f = logo_file()
        return f if get("LOGO_ON") and f.exists() else None
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
