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

## Facebook / Instagram / TikTok setup (one time)

Each platform is optional. One that isn't set up shows "(not set up)" in the UI, and uploads to it fail with that message.
Put the keys in `.env`, then restart the server.

**Facebook Page + Instagram** (one Meta app covers both; Instagram must be a Business/Creator account linked to the Page):

1. At [developers.facebook.com](https://developers.facebook.com/), create an app (type **Business**) and add **Facebook Login for Business** and **Instagram Graph API**.
2. In **Graph API Explorer**, pick the app, then **Get User Access Token** with: `pages_show_list`, `pages_read_engagement`,
   `pages_manage_posts`, `publish_video`, `instagram_basic`, `instagram_content_publish`, `business_management`.
3. Make it long-lived: **Access Token Debugger → Extend Access Token**.
4. With that token, call `GET /me/accounts`. Copy your Page's `id` → `FB_PAGE_ID` and its `access_token` → `FB_PAGE_TOKEN`
   (a Page token from a long-lived user token doesn't expire).
5. Call `GET /<FB_PAGE_ID>?fields=instagram_business_account`. Copy that id → `IG_USER_ID`.

Facebook clips up to 90s go up as Reels, and longer ones as normal Page videos. Instagram posts every clip as a Reel.
While the Meta app is in Development mode, only you (the app admin) can post, which is fine for your own Page.

**TikTok** (Content Posting API):

1. At [developers.tiktok.com](https://developers.tiktok.com/), create an app, add **Login Kit** and **Content Posting API**
   (enable **Direct Post**), with scopes `video.publish` and `video.upload`.
2. Copy `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`.
3. Authorize your account once with TikTok's OAuth flow and put the `refresh_token` in `TIKTOK_REFRESH_TOKEN`.
   It rotates on every refresh, and the current one is kept in `data/tiktok_token.json`.
4. Until TikTok audits the app, posts can only be **private** (`SELF_ONLY`). Make them public in the TikTok app.
   After the audit, set `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`.

## Upload limits

Platforms cap how many videos you can upload per 24 hours. YouTube allows about **10 per channel** for new or
unverified channels (that's where `uploadLimitExceeded` comes from). Clipper counts its uploads per platform and shows
them on the **Socials** page and in the header (e.g. "YouTube 10/10 today"). When a platform's limit is reached, its
uploads pause, a red banner shows when they resume, and waiting clips upload automatically once a slot frees up
(checked every 10 minutes). If you know your real limit, set it in `.env`: `UPLOAD_LIMIT_YOUTUBE=15`,
`UPLOAD_LIMIT_INSTAGRAM=50`, `UPLOAD_LIMIT_TIKTOK=15`, `UPLOAD_LIMIT_FACEBOOK=0` (0 = no limit).

## WhatsApp report (hourly views)

Every `REPORT_MINUTES` (default 60), Clipper checks the views of every uploaded clip on each platform and sends you a
WhatsApp message with views per platform (and the change since the last report), the best platform, and the top clips.
It's free and sent through [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/), a third-party service that sees the message text.

1. Add **+34 644 66 32 62** to your phone contacts (check CallMeBot's page for their current number).
2. Send it the WhatsApp message `I allow callmebot to send me messages`. It replies with your API key.
3. In `.env`, set `WHATSAPP_PHONE=+<country code><number>` and `WHATSAPP_APIKEY=<key>`, then restart the server.
4. On the **Socials** page, **Send report now** sends one straight away.

YouTube views need read access: click **Reconnect YouTube** once (older logins could only upload).
TikTok doesn't give view counts for private app posts, so it's listed as unavailable until your TikTok app is audited.

## Topics (auto-find videos)

In the UI, **+ Add topic** takes a topic, how many videos to clip, shorts and long clips per video, and where to upload.
DeepSeek writes 4-6 search queries, and the most-viewed videos from this week are clipped one after another (widening to this
month or any time if there aren't enough). Only videos between `TOPIC_MIN_MINUTES` and `TOPIC_MAX_MINUTES` are used,
live streams are skipped, and a video that was clipped before is never picked again. Topics run in queue order, and
queued ones survive a server restart.

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
