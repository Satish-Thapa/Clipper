# Clipper

Paste a YouTube link (normal video **or** live stream) → get viral-ready Shorts and long-form clips → auto-upload to YouTube.

## How it finds the moments

Each signal becomes a per-second "hype" curve. The curves are weighted, summed, and the hottest windows become candidates:

| Signal | Source | Weight | Works on |
|---|---|---|---|
| **Most replayed heatmap** | YouTube's own retention graph (via yt-dlp) | 3.0 | Normal videos |
| **Chat velocity** | messages/sec, shifted 8s back for reaction lag | 2.0 | Live + stream replays |
| **Timestamped comments** | "12:34 lmaooo" → 754s | 1.5 | Normal videos |
| **Audio loudness** | ffmpeg EBU R128 momentary loudness | 1.0 | Everything |

Next, the candidate windows are transcribed locally with **faster-whisper**. **DeepSeek** reads the transcripts and picks the final clips (hook, payoff, makes sense without context), snaps them to sentence boundaries, and writes the title, description and hashtags. Shorts are center-cropped to 9:16 with burned-in captions.

**Live streams:** the stream is recorded in rolling chunks (`LIVE_CHUNK_SECONDS`, default 10 min). Each chunk is clipped in the background while the next one records, so clips come out during the stream. The clip counts you enter apply per chunk.

## Setup

```bash
# macOS (ffmpeg-full is needed for burned-in captions: libass)
brew install ffmpeg-full deno uv   # deno: yt-dlp needs a JS runtime for YouTube

git clone <this repo> && cd clipper
uv venv -p 3.12 && uv pip install -e .
cp .env.example .env   # add your DEEPSEEK_API_KEY
```

Run it:

```bash
.venv/bin/uvicorn clipper.app:app --port 8000
```

Open http://127.0.0.1:8000.

## YouTube upload setup (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **YouTube Data API v3**.
2. Under **OAuth consent screen**, choose External and add yourself as a test user.
3. Under **Credentials**, create an **OAuth client ID** of type **Desktop app** and download the JSON.
4. Save it as `client_secret.json` in the repo root (it's gitignored).
5. In the UI, click **Connect YouTube**. Sign in with the account that owns your channel; on "Google hasn't verified this app" click **Advanced → Go to (your app name) (unsafe)**. This saves your login to `data/youtube_token.json`.

**`Error 403: access_denied` / "has not completed the Google verification process"?** The account you signed in with isn't an approved tester:

1. In Google Cloud Console, select the project your `client_secret.json` came from.
2. Go to **APIs & Services → OAuth consent screen** (newer UI: **Google Auth Platform → Audience**).
3. Under **Test users**, click **+ Add users**, add the Gmail of the account that owns your channel, and save. For a brand-account channel, add the Google account you log in with.
4. Check **APIs & Services → Library → YouTube Data API v3** says **Enabled**.
5. Click **Connect YouTube** again with that same account.

**Login stops working after ~7 days?** Apps in "Testing" mode get short-lived logins. Delete `data/youtube_token.json` and click **Connect YouTube** again, or publish the app on the OAuth consent screen. Clearing `data/` also logs you out.

**Quota:** the default quota is 10,000 units/day and each upload costs 1,600, so that's about **6 uploads/day**. You can request more quota from Google. Also, uploads from unverified API projects may be locked to private until Google audits your app.

## Layout

```
clipper/
  app.py         FastAPI server + API
  worker.py      background pipeline (VOD + live loop)
  download.py    yt-dlp: info, download, live recording, chat, comments
  signals.py     hype curve + window picking
  transcribe.py  faster-whisper
  pick.py        DeepSeek clip selection + validation
  render.py      ffmpeg cut, 9:16 crop, captions
  upload.py      YouTube Data API upload
  static/index.html  the whole UI
data/            downloads, clips, sqlite db (gitignored)
```

Self-checks: `python -m clipper.signals`, `python -m clipper.pick`, `python -m clipper.render`.

## Roadmap

- AI voice-over
- Facecam/subject tracking for vertical crops
- Overlapping live chunks so moments aren't split at boundaries

## Legal

Only clip and upload content you own or have permission to use. Content ID claims and copyright strikes are on you.
