# Clipper

Turn YouTube videos and live streams into short clips, make photo and text posts, and publish both to YouTube,
Instagram, Facebook and TikTok. Each **brand** (page) has its own connected accounts and **categories**, and each
category sends its work to the right accounts automatically. Everything runs on your own computer, on macOS or Windows.

## Setup

```bash
# macOS (ffmpeg-full is needed for burned-in captions: libass)
brew install ffmpeg-full deno uv        # deno: yt-dlp needs a JavaScript runtime for YouTube
# Windows: install ffmpeg (a "full" build with libass), deno and uv, then the same commands below

git clone <this repo> && cd clipper
uv venv -p 3.12 && uv pip install -r requirements.lock -e .   # the exact tested versions
.venv/bin/uvicorn clipper.app:app --port 8000                  # Windows: .venv\Scripts\uvicorn ...
```

Open **http://127.0.0.1:8000**. On macOS, Homebrew's `ffmpeg-full` isn't on PATH; Clipper finds it by itself.
With an NVIDIA GPU (Windows), `uv pip install -e .[gpu]` makes transcription use it; clips use the GPU encoder
(NVENC, or VideoToolbox on a Mac) automatically.

When downloads break (YouTube changes often), update yt-dlp first: `uv pip install -U "yt-dlp[default]"`.

## First steps in the app

1. **Settings → General → AI**: choose your AI provider (DeepSeek, OpenAI, OpenRouter, Groq or any
   OpenAI-compatible address), paste the **API key**, and set the model names: one for picking clip moments (a thorough
   model), one for writing posts (a fast one). Or **ollama** for your own Ollama server (no key; the saved key isn't
   sent to it): e.g. a GPU machine reached through an SSH tunnel
   (`ssh -N -L 127.0.0.1:11435:127.0.0.1:11434 user@server`, address `http://127.0.0.1:11435/v1`). Give Ollama a model
   variant with a long context (`num_ctx` 32768), or long transcripts get cut short. Set the **content language** (e.g. Nepali) if titles and posts shouldn't
   be in English.
2. **Brands**: a fresh install has a **Demo channel**. Add your own brand or page in Settings → Brands (then you can
   delete the demo; its things move to the brand you pick). With more than one brand, switch at the top of the sidebar.
3. **Accounts**: connect the brand's social accounts (how to get each key: [SOCIALS.md](SOCIALS.md)).
   - YouTube: paste your Google OAuth client in Settings → YouTube, then **Connect a channel** (repeat per channel).
   - Facebook & Instagram: paste a long-lived Meta token; every Page and linked Instagram account is listed to add.
   - TikTok: client key and secret in Settings → TikTok, then paste a refresh token per account.
4. **Categories**: create one per kind of content (e.g. "Football", "Recipes"). Give it a **description** and
   **keywords**: the AI reads them when it picks moments and writes titles, captions and posts. Tick its accounts per
   platform and pick a **default** for each: that's where its clips and posts go unless you change it for one item.
5. **Settings → Branding**: upload your logo and type a watermark text. Nothing is built in.

An existing install that used `.env` is copied into the database on its first start (keys, Facebook / Instagram
account, YouTube and TikTok logins); after that `.env` isn't read.

## Using it

The sidebar has six places: **Create, Library, Scheduled, Categories, Accounts, Settings**.

**Create → Clip a video** is one guided flow:

1. **Source**: drop a video or paste a YouTube link (also live streams). A progress bar shows the upload and
   download, then each step: listening for big moments, transcribing, picking clips, rendering.
2. **Clip**: the AI's suggested clips with their length. Pick one, or make your own cut on the timeline (in / out
   handles).
3. **Format**: choose where it goes: IG Reel, IG Post, FB Reel, FB Post, YouTube Shorts, YouTube, TikTok. Each gets its
   shape automatically (9:16, 4:5 or 16:9, rendered when needed) and a warning if the clip is longer than that platform
   allows.
4. **Caption**: one title, description and hashtags, with an optional version per platform and a character count for
   each.
5. **Publish**: pick the category; its default accounts fill in as `[platform] account — format`. Untick or add an
   account for this clip only, without changing the category. Missing accounts and expired logins are flagged with a
   link to fix them. **Publish now** or **Schedule** for a time. Each destination then shows its own result; **Retry
   failed** retries only those.

**Create → Make a post**: **Let the AI write it** (pick a category, type a keyword or leave it empty for the
category's topic, and a template or let the learning choose): a finished post opens as a draft. Or your own photos (one photo, or a carousel of up to 10, drag to reorder) cropped to 4:5 or
1:1, a caption with hashtags, a live feed preview, and the category's Facebook / Instagram accounts. Facebook can also
get text-only posts.

**Create → Automate** has two tabs:
- **Clips**: one or more **autopilots** (Add an autopilot), each with its own topic, category, platforms and
  posting hours. Each finds what's trending every day, clips it and posts across its hours; **Make one clip now** finds
  one trending video and makes a single clip. Several run side by side (Settings → Finding videos → Automations at
  once, 2 by default). **Topics** queue a subject to clip several videos about it.
- **Posts**: **post automations** (Add a post automation), as many as you like, several per category. Each has
  its own topic, keywords, templates, posts per day, hours and approval, and posts to its category's default Facebook
  and Instagram accounts (the category also sets the handle and logo on the cards). Each card says what's in the way
  (no template, no account, a login to renew). With **Ask me before posting** on, drafts wait in **Drafts to
  approve** (one by one or all at once). **Make one now** writes one post and opens it in Library.
  News posts come from every source in Settings → Finding videos → **News sources** (Google News and Bing News by
  default; add any RSS feed, with `{q}` where the topic goes). Each source is asked at most every 20 minutes (an hour
  after "too many requests"). The AI picks a story, Clipper reads the article itself, and the post is written from its
  facts; when there's more to tell than the headline, it adds 2-4 detail slides and goes up as a carousel.

**Library** shows every clip and post with its status (Draft, Processing, Scheduled, Published, Failed), platforms and
category. Filter by category, platform and status, or search by title. Select one to see each destination, publish,
retry, schedule or delete. **Jobs** has each video's processing log and a Stop button that works within seconds.

**Scheduled** lists what goes out next, by day, with the target accounts. **Post now** sends one right away (an autopilot clip waiting for a slot goes to its autopilot's platforms); **Unschedule** / **Cancel** take it off.

## Getting follows, and learning what works

- **Follow prompts that say what a follower gets.** Clips end on an animated card ("WANT MORE GAMING? / NEW CLIPS
  EVERY DAY / FOLLOW @YOURPAGE"); Settings → Clips → End card holds several versions (one per line), and each clip
  gets the one whose clips have done best so far, sometimes another to keep testing. Made posts end with a follow line
  in the caption and a follow slide on carousels and Reels (Settings → Branding → Follow line). `{HANDLE}` / `{TOPIC}`
  come from the category.
- **Reels with music, for posts only.** Meta's API can't add Instagram's own music, so add tracks you have the rights
  to in Settings → Branding → Music (name files by mood). A template set to "Card or Reel" posts the slides as a short
  Reel with the track the AI picks for the story; the Reel's audio is named after your handle. Clips keep their own
  sound.
- **Series numbering**: `{N}` in a template's label ("GTA DAILY #{N}") numbers the posts, a reason to follow.
- **Learning from engagement.** Every hour Clipper reads each post's likes, comments, shares, saves and follows (follows
  count most). A day after a post goes up, its result trains a bandit that picks the template, card or Reel and
  the follow prompt: mostly what's done best, now and then another option so it keeps learning. Your best and worst
  posts are shown to the AI when it writes. Settings → Reports → "What posts taught it" shows the results.
  It never uses engagement bait (follow-for-follow, giveaways, "comment YES"), which Meta demotes.

## How it finds the moments

1. **Signals, one value per second:**

   | Signal | What it measures | Works on |
   |---|---|---|
   | YouTube most-replayed | where viewers rewatched | popular YouTube videos |
   | Live chat bursts | lots of messages at once | streams and replays |
   | Laughter, cheering, applause | tags in YouTube's captions | videos with captions |
   | Timestamped comments | times viewers wrote in comments | YouTube videos |
   | Loudness jumps | sudden rises, not constant noise | everything |
   | Shot changes and motion | how much the picture changes | everything |

   They're weighted and added into one hype curve.
2. **Learning from your views:** once your clips have views, signals that went along with more views get more weight
   (Settings → Reports shows what it learned). Your best and worst clips are also shown to the AI as examples.
3. **Candidate windows** are centred on the hottest peaks (setup before, payoff after) and their edges snap to shot
   changes.
4. **Two passes:** a fast model screens many candidates, then the thorough model picks the final clips. It uses the
   transcript, the signals, the category's description and keywords, and, with a vision model set in Settings → AI,
   two frames of each candidate. It also writes the titles, headlines and hashtags.
5. Clips are trimmed to whole sentences and rendered.

## Transcription

1. **YouTube's own captions first** (Settings → Clips): when a YouTube video has captions in its original language
   (uploaded, or YouTube's automatic ones such as `hi-orig`), they're used directly: instant, with word timing, and
   usually better than Whisper for Hindi and other non-English speech.
2. Otherwise Whisper transcribes, on the **Transcription engine** you choose:
   - **auto**: the GPU of an Apple Silicon Mac (MLX), else an NVIDIA GPU, else the CPU;
   - **cloud**: any OpenAI-compatible speech-to-text API (Groq is fast and cheap). The audio is sent to that service.

   The model **auto** is `large-v3-turbo` on a GPU and `small` on a CPU.
3. **Audio language** (e.g. `hi`) forces the language. Leave it empty for mixed Hindi/English speech: forcing `hi` on
   Hinglish makes it worse.

On this Mac (MLX, large-v3-turbo) speech transcribes at about 2-3× real time. The CPU is far slower, and a cloud API is
much faster.

## Data and security

- Everything lives in `data/`: the database, downloads, clips, logs. It's ignored by git.
- Tokens and API keys are **encrypted** in the database with `data/secret.key`. Keep that file with the database when
  you back up or move `data/`: without it, accounts have to be reconnected and keys re-entered.
- The server listens on `127.0.0.1` only. It has no login, so don't expose it on a network (`--host 0.0.0.0`).
- `/media` serves only pictures and videos from `data/`, never the database.

## Troubleshooting

| Problem | What to do |
|---|---|
| "Sign in to confirm you're not a bot" | Settings → Downloads: add a `cookies.txt` from a signed-in youtube.com tab, or read cookies from a browser (Settings → YouTube). |
| Download fails with "connect timeout" | Settings → YouTube → **Download over IPv4** (on by default): some providers' YouTube caches fail over IPv6. |
| An account shows **Expired** | Its token no longer works: Accounts → **Reconnect**. Tokens are checked every 6 hours and on Check. |
| Upload limit reached | The banner shows a 24 h timer; the autopilot skips that platform until then. Manual uploads always try. |
| Instagram rejects long Reels | Its API refuses Reels over about 45 s ("error 2207076" / "ProcessingFailedError"). Longer clips go to Instagram as their last 45 s automatically; change it in Settings → Facebook & Instagram → Instagram: longest Reel. |
| "The read operation timed out" | Meta was slow to answer. Instagram calls are now retried, and checked first so a retry can't post twice; press Retry if one still fails. |
| Meta errors after a while | Meta retires Graph API versions: raise Settings → Graph API version (currently v25.0). |
| YouTube login expires after ~7 days | Apps in "Testing" get short logins: reconnect, or publish the app on Google's consent screen. |

## Layout

```
clipper/
  app.py         FastAPI server + API
  config.py      every setting (stored in the database; the Settings page is built from FIELDS)
  crypto.py      encryption for tokens and keys
  brands.py      brands / pages
  accounts.py    social accounts (all platforms)
  categories.py  categories, their account mapping and AI context
  worker.py      the clip pipeline: download → signals → transcribe → AI pick → render → publish
  download.py    yt-dlp: info, download, live recording, chat, comments
  signals.py     hype curve + window picking
  transcribe.py  faster-whisper
  pick.py        AI prompts: clip picking, titles, voice-over, search
  render.py      ffmpeg: cut, frames (9:16 / 4:5 / 1:1 / 16:9), captions, watermark
  social.py      publishing to each platform (videos, photos, carousels, text)
  upload.py      YouTube API
  posts.py       photo / text posts, post templates, cards
  autopilot.py   daily trending rounds + posting slots
  topics.py      topic queue
  stats.py       view counts + WhatsApp report
  static/index.html  the whole UI (no build step)
data/            database, downloads, clips, logs, secret.key (git-ignored)
```

Self-checks: `python -m clipper.<module>` for crypto, signals, pick, render, posts, topics, stats, vision, transcribe.

## Legal

Only clip and publish content you own or have permission to use. Content ID claims and copyright strikes are on you.
