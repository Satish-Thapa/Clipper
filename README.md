# Clipper

Clipper turns YouTube videos and live streams into short vertical clips and posts them for you. It also posts
sourced news and fact posts, and it learns from your view counts what to make next.

- **Clips from any video or stream.** Paste a link or upload a file. The AI finds the best moments and renders
  9:16 clips with a follow message and a headline at the top, captions, your logo, a "FOLLOW" end card and an optional
  voice-over.
- **Topics.** Type "GTA 6". It finds this week's most-viewed videos on that topic, skips the ones that don't clip
  well, and clips the rest.
- **Autopilot.** Say "6 clips a day between 10:00 and 22:00". Every day it finds what's trending, clips it, and posts
  one every 2 hours.
- **News & facts.** It searches news portals for a topic, writes short posts from the articles' facts with the
  source credited, as videos or photos, and posts them now, at a set time, or on the autopilot's slots. Every search
  has a live log.
- **Posting.** Posts to YouTube Shorts, Facebook Reels, Instagram Reels and TikTok.
- **Learning.** It reads the views of everything it posted on each platform. When you give no niche, it decides
  from what got views.
- **Reports.** A views report every hour, on the Reports page and on WhatsApp.

Everything runs on your PC. It uses the NVIDIA GPU for transcription and encoding when there is one. The UI uses a
Vice City sunset look (night purple, pink-to-orange gradients, neon glow, Anton headings).

---

## Contents

1. [The full flow](#the-full-flow)
2. [Setup](#setup)
3. [The pages](#the-pages)
4. [Autopilot](#autopilot)
5. [News & facts posts](#news--facts-posts)
6. [Live streams](#live-streams)
7. [What works (analytics) and reports](#what-works-analytics-and-reports)
8. [Connecting your socials](#connecting-your-socials)
9. [Limits and known platform issues](#limits-and-known-platform-issues)
10. [Settings (.env)](#settings-env)
11. [How it works inside](#how-it-works-inside)
12. [Troubleshooting](#troubleshooting)
13. [Legal](#legal)

---

## The full flow

```
                       ┌───────────────── AUTOPILOT (once a day + top-ups) ─────────────────┐
                       │ views of everything posted ─▶ "what works" ─▶ niche (yours or learned)│
                       │ niche ─▶ DeepSeek search queries ─▶ YouTube search (US, this week)   │
                       │       ─▶ DeepSeek: 1-3 trending topics                               │
                       │ topics ─▶ the topic queue (clips)   topics ─▶ news portals (posts)   │
                       └──────────────┬───────────────────────────────────┬──────────────────┘
 Create page (link / file / live)     │ Topics page (your topic)          │ News page (your topic)
            │                         ▼                                   ▼
            │             topic ─▶ search queries ─▶ YouTube search   Google News + Bing News
            │             ─▶ most-viewed, right length, not used      ─▶ read article (text, picture)
            │             ─▶ DeepSeek: clips well? ─▶ face check      ─▶ DeepSeek picks + writes
            │             ─▶ one job per video                        ─▶ 7-15s news video
            ▼                         │                                   │
   JOB: download (8 parallel chunks, 1080p or 4K) ◀┘                      │
        ─▶ hype curve (YouTube heatmap + chat + comment timestamps + loudness)
        ─▶ candidate windows ─▶ Whisper transcript (GPU)
        ─▶ DeepSeek picks moments, writes titles, headlines, hashtags
        ─▶ render 1080x1920 (GPU): framing, headline, captions, logo, voice-over, end card
        ─▶ source video deleted
            │                                                             │
            ▼                                                             ▼
   CLIP ── post now / later by hand ── "timed" (set hour) ── "scheduled" (autopilot slots)
            ▼
   YouTube Shorts · Facebook Reels · Instagram Reels · TikTok      (errors + limits shown, never hidden)
            ▼
   views every hour ─▶ Reports page + WhatsApp ─▶ "What works" ─▶ back into the autopilot
```

Everything is logged: per job, per topic, per autopilot round (the **Activity** view) and in one file
(`data/logs/clipper.log`, shown on the **Logs** page).

---

## Setup

### Windows (main setup)

1. **Python 3.12** and **git**.
2. **ffmpeg** with libass (any "full" build, e.g. from gyan.dev). Unzip it to `C:\ffmpeg` so that
   `C:\ffmpeg\bin\ffmpeg.exe` exists.
3. **deno** (yt-dlp needs a JavaScript runtime for YouTube): `winget install DenoLand.Deno`.
4. **Smart App Control** (Windows 11) blocks ffmpeg and the GPU libraries. Turn it off in
   *Windows Security → App & browser control → Smart App Control*.
5. Install:
   ```powershell
   git clone <this repo> clipper; cd clipper
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -e ".[gpu]"     # without an NVIDIA GPU: pip install -e .
   copy .env.example .env
   ```
6. In `.env`, set at least `DEEPSEEK_API_KEY=` and `FFMPEG=C:/ffmpeg/bin/ffmpeg.exe`.

### macOS

```bash
brew install ffmpeg-full deno uv            # ffmpeg-full has libass (burned-in captions)
uv venv -p 3.12 && uv pip install -e .
cp .env.example .env                        # FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
```

### Run it

```powershell
.venv\Scripts\uvicorn clipper.app:app --port 8000      # macOS: .venv/bin/uvicorn ...
```

Open **http://127.0.0.1:8000**. After changing `.env` or the code, stop the server (Ctrl+C) and start it again.

**GPU:** with an NVIDIA card, Whisper runs on CUDA (float16) and clips are encoded with NVENC (the log says
`render: GPU encoder (h264_nvenc)`). Without one, both fall back to the CPU, which is slower but works the same.

---

## The pages

| Page | What it's for |
|---|---|
| **Create** | Paste a YouTube link (video or live stream) or upload a file. Set shorts/long counts, who picks the moments, framing, 4K, subtitles, voice-over, where to post. For live streams: piece length and how many pieces. |
| **Auto** | The autopilot: ON/OFF, **Stop everything**, today's posting slots, settings, and the **Activity** view of every round. |
| **Topics** | Queue a topic. It finds the most-viewed recent videos about it, checks they clip well, and clips them one after another. |
| **Jobs** | Every video being or already clipped: status, full log, hype curves, "cut it yourself", and live controls for streams. |
| **Clips** | Every clip: preview, title, score, why it was picked, where it's posted (with links), errors, upload buttons per platform, and bulk upload. |
| **Socials** | Which platforms are connected, uploads in the last 24h, limit timers, the YouTube cookies card, and the WhatsApp report. |
| **News** | News and fact posts: find stories for a topic, tick the ones to post, make the videos, post now, at a time, or on the autopilot's slots. |
| **Reports** | **What works** (views per topic, platform and clip length; best and weakest clips; the niche it would pick) and the hourly views reports. |
| **Logs** | The full log file, live, with errors in red. |
| **⚙ Settings** | Background videos for the split layout, what's stored (videos, database, logs, counts), and **Reset everything** (below). |

**Framing** (Create, Topics, Auto):
- *Full screen 9:16* fills the phone.
- *4:5*, *Square* and *Whole frame 16:9* keep more of the scene, with black bars above and below that hold the
  headline and captions.
- ***Split: clip on top + your video below*** (split-screen):
  - **Top half (1080x960):** the clip, as a wider slice than a phone crop, so more of the scene fits.
  - **Bottom half:** a **background video you provide** (gameplay, satisfying clips, parkour...), muted, starting
    at a random point and looped if it's shorter than the clip.
  - **Text:** the follow message, headline, logo and end card are the same as other clips; captions sit on the seam
    between the halves.
  - **Your library:** on **⚙ Settings → Background videos**, upload a file or **paste a YouTube link**; it
    downloads in the background (video only, up to 1080p, since it plays muted) and shows "downloading…" until it's
    ready. Delete any you don't want. Stored in `data/fillers/`, kept by "Reset everything". When Split is picked, a **Your video below** dropdown appears
    next to Framing.

**Who picks the moments:**
- *AI picks from hype curves* (fast): the strongest moments by the signals, and DeepSeek chooses among them.
- *AI reads full transcript*: DeepSeek reads everything, with or without the curves as hints.
- *I'll cut it myself*: you type the cuts on the Jobs page.

---

## Autopilot

The **Auto** page. Flip the switch ON and set:

1. **What to post**
   - **Niche** (optional, e.g. "GTA 5, GTA 6 and gaming").
     - **Empty** means it's up to the autopilot. With at least 5 posted clips that have view counts, it picks a niche
       from what got views; see [What works](#what-works-analytics-and-reports).
     - **Not enough data yet** means it searches broadly (gaming, streamers, podcasts, sports, viral moments) and takes
       what has the most momentum this week.
   - **Post to:** pick the platforms.
2. **How much and when**
   - **Clips per day.**
   - **+ News/fact posts per day**, which kind (news, facts, both), and as videos, photos or both (alternating).
   - **Posting hours** (between ... and ...), plus **clips per video**.
   - The plan is shown in plain words as you type, e.g. *"6 clips + 2 news posts a day, 10:00-22:00, one every
     1 h 30 min"*.
   - Quick presets: 3 a day, 6 a day, every hour.
3. **How the clips look:** framing, headline + subtitles, voice-over, 4K.
4. **Advanced**
   - **How choosy:** candidate videos judged per video clipped (2-8).
   - **Start finding videos at:** when the day's first round starts.
   - **Long clips per video**, **who picks the moments**, **face check** (skip videos that are mostly a person on
     camera), **use the article's picture** for news.

### What a day looks like

1. **Start hour.** It works out what today still needs: posts per day minus posted, waiting and being made.
2. **Learning.** It refreshes the view counts on every platform. If there's no niche, it learns one from them.
3. **Finding trends.**
   - DeepSeek writes search queries for the niche.
   - YouTube is searched for this week's uploads (US), and the 40 most-viewed are read.
   - DeepSeek names the 1-3 topics with the most momentum, leaning toward what got views on your channel before.
4. **Clips.** It queues just enough videos over those topics (clips per video, choosiness). The topic queue then
   does the work: clippability score, face check, download, clip, render.
5. **News.** If you asked for news/fact posts, it searches news portals about the same topics, picks the best fresh
   stories and makes the posts.
6. **Waiting.** Finished clips and posts wait as **scheduled**.
7. **Posting.** Posts go up on the day's **slots** (window ÷ posts per day), alternating clips and news by their
   share.
   - If clips run late, it catches up, but never posts closer than half a gap apart.
   - Clips that miss the window go first tomorrow.
8. **Top-ups.** If the day runs short (failed downloads, too few videos passed the checks), up to 2 **top-up**
   rounds follow, 45+ minutes apart.

### Control

- **Settings apply right away**, even while it works: queued and running topics, unfinished jobs and scheduled posts
  follow the new values.
- **OFF:** no new rounds and no posts. Videos being clipped still finish and wait.
- **■ Stop everything:** OFF, plus it stops the round in progress, every autopilot topic and job, and cancels the
  scheduled posts. The clips stay on the Clips page.
- **▶ Run a round now:** runs a round straight away.

### Watching it

- **Today:**
  - **Numbers:** posted / planned (clips and news), the next post, waiting, being made, rounds today.
  - **Slots** as a list by date, in 12-hour time, each with the clip or news post that goes up there:
    - posted ones show the time they went up and their links on each platform
    - the **next** one is highlighted, then the upcoming ones in posting order (the waiting queue, oldest first)
    - when posting runs late, it shows the real expected time with the original slot under it
    - what doesn't fit today moves to tomorrow's slots
  - **Late start:** today's slots begin from **when you switched the autopilot ON**. Switch it on at 2:00 PM with
    10 AM-10 PM and one post every 2 h, and today runs 2, 4, 6 and 8 PM; tomorrow is a full day. It also makes only
    that many posts for today.
- **Activity:** every round, section by section:
  - what it learned from your views
  - the searches it ran and what they found
  - the most-viewed videos it read
  - the topics it chose and why
  - per topic: every video it judged (score and reason, kept or skipped), face-check numbers, the videos it
    clipped, each job's steps with timings, the clips made and why, and where each went up
  - the news stories it saw, picked and made

### When something gets in the way

- **Upload limit:** the autopilot skips that platform until the 24h timer ends. If every platform is at its limit,
  rounds pause. Manual uploads are never held back.
- **YouTube bot check:** rounds wait 2 h, or until you add cookies. See
  [YouTube cookies](#youtube-cookies-bot-check).
- **Instagram:** the autopilot tries every clip, including ones over 30s. Failures show on the clip card with a red
  retry button.

---

## News & facts posts

The **News** page:

1. **Find.**
   - Type a topic (e.g. "GTA 5"), or leave it empty to use the autopilot's niche, then the niche learned from your
     views, then whatever is trending.
   - Choose **Latest news** (last 3 days) or **Did-you-know facts**, and how many posts.
   - It searches **Google News** and **Bing News** (thousands of portals, no API key) and drops stories posted
     before. DeepSeek then ticks the best ones (★, with why).
2. **Make posts** from the ticked stories:
   - **Where:** pick the platforms.
   - **Format:** **video** (a Reel/Short, every platform) or **photo** (1080x1350, Facebook and Instagram only:
     YouTube's and TikTok's APIs can't post photos, so photos skip them with a note in the log).
   - **Options:** use the article's picture, AI voice-over.
   - **When:** *I'll post them myself* · *Post each right away* · *At a time…* (first post at a date/time, then one
     every 30 min / 1 h / 2 h / 4 h) · *On the autopilot's posting slots*.
3. **Posts** lists every post with its preview, caption and status. **⏰ Schedule** sets or changes a time,
   **Post now** posts it, **Cancel time** cancels a schedule, and **Delete** removes it.

**How a post is made:**
1. **Finding the article.** Google News links are redirects, so the same headline is looked up on Bing to get the
   real URL.
2. **Reading it.** The article page is read like a browser would: its picture, its description and the first
   paragraphs.
3. **Writing.** DeepSeek writes the post **only from those facts**: a badge (BREAKING / NEWS / LEAK / RUMOR...), a
   headline, 2-3 key lines, a title, a caption, hashtags and the narration. It never invents numbers or quotes, and
   rumours are labelled.
4. **The video.** Each post is a 7-15s 1080x1920 video:
   - the article's picture with a slow zoom over a blurred copy of it, or an animated gradient
   - the badge pops in, the headline slides up, then each key fact in turn
   - "Source: IGN · 3 h ago" at the bottom
   - your logo, and the voice-over or silence

   It's a video because YouTube only takes videos, and Instagram's API only takes images from a public URL. A video
   uses the same upload path as the clips everywhere.
5. **The caption** ends with `Source: <portal> (<age>)` and the article link.
6. **No repeats.** Every story used is remembered, so it's never posted twice.

**Photos** are the same design in Instagram's portrait size, with the 2 key facts.
- **Facebook:** a normal photo post.
- **Instagram:** its API only takes images from a public URL, so the photo goes to your Page first as an unpublished
  (invisible) photo, and Instagram gets that Facebook-hosted URL.

**Activity (live log).** Every search, yours and the autopilot's, is a run with its own log on the News page:
- the search queries, and what each portal returned (with the newest story's age)
- what was skipped and why (duplicates, too old, posted before, social-media posts)
- the stories kept, and DeepSeek's picks with why
- for each post: the real article link, how much text and whether a picture was read, what DeepSeek wrote, the
  render, and where it was posted (link or error)

**Facts:**
- With `WIKI_CONTACT` set in `.env` (your email or website; Wikipedia's robot policy requires a contact), facts come
  from the topic's Wikipedia article.
- Without it, they come from readable news articles about the topic.
- Either way DeepSeek picks one surprising fact from the text and credits the source.

**Timed posts** go up at their time even while the autopilot is OFF, because you scheduled them yourself.

---

## Live streams

Paste a live stream link on **Create**. The stream is recorded in **pieces**, and each piece is clipped in the
background while the next one records, so clips come out during the stream.

```
record piece 1 ──▶ record piece 2 ──▶ record piece 3 ──▶ ...      (live chat collected the whole time)
      │                  │                  │
      ▼                  ▼                  ▼
  process 1      [last 60s of 1 + 2]  [last 60s of 2 + 3]        (several pieces processed at once)
      │                  │                  │
 DeepSeek: clips   clips / JOINED clip   clips / SKIP (nothing worth it)
```

- **Settings per job.** Record it in pieces of 2-60 minutes (default `LIVE_CHUNK_SECONDS`, 10 min), and clip N
  pieces, or 0 to go on until the stream ends or you press Stop.
  - **Shorts / Long are a maximum per piece.** The form says what that means, e.g. *"6 pieces (1 h of stream, up
    to 24 clips)"*.
  - **While it records,** the job card on **Jobs** shows "piece 3 of 6" with live controls for piece length, pieces
    to clip and clips per piece. Clip counts apply to the piece being recorded; length applies from the next piece.
- **Live chat.** Every chat message is timestamped as it arrives (through yt-dlp, using your YouTube cookies).
  Chat speed is the strongest signal on a live stream, alongside loudness; there's no "most replayed" heatmap or
  comments yet. Each piece's log says how many chat messages it got. If chat can't be read, it says why, and the
  piece is clipped on loudness only.
- **Skipping dull pieces.** For live pieces the clip counts are a maximum, not a quota.
  - If DeepSeek finds nothing worth clipping (menus, waiting, chatting about nothing), it makes **no clips** and the
    log says why: `[piece 3] DECISION: nothing worth clipping in this piece, skipped: only menus and silence`.
  - Picks scoring below 55 are dropped too.
- **Moments split between two pieces.** From the 2nd piece on, each piece is processed together with the **last 60
  seconds of the previous piece**.
  - DeepSeek knows that part was already covered, so a clip may start there only if the moment continues into the
    new piece. That makes **one clip joined across exactly two pieces**, never more.
  - Clips that would end inside the previous piece are dropped as duplicates.
  - Log: `[piece 2] DECISION: 1 clip(s) from this piece, 1 JOINED across the previous piece and this one (starts 8s
    before the boundary)`.
- **In parallel.** Each recorded piece is processed in its own thread while the next one records, so several pieces
  can be in progress at once (the log says how many). Only Whisper transcription takes turns, one piece at a time on
  the GPU.
- **Decisions in the log.** Every line from a piece starts with `[piece N]`: what it analysed, the candidates, what
  DeepSeek picked (score, title, why), what was dropped and why, joins and skips.
- **Cleanup.** The recordings and the joined pieces are deleted when the job ends.

Past streams (replays) work like normal videos, and their chat replay (via yt-dlp) is used as a signal too.

## What works (analytics) and reports

**Reports → What works** reads the views of everything posted, on every platform (refreshed hourly, or with
**Refresh views + learn**), and shows:

- **Topics** (or source videos), ranked by views per clip, with each one's best clip.
- **Platforms** and **clip lengths**, ranked by views per clip. For example, on this channel: Instagram 353 per
  clip against YouTube 2; clips under 20s 1,117 per clip against 35-60s 20.
- **Best clips** and **weakest clips** (only clips at least a day old count as weak).
- **What it would post:** the niche DeepSeek derives from all that, why, and what to avoid.

That history feeds the autopilot in two ways:
1. **No niche set:** it becomes the niche, e.g. *"Short GTA 6 and Gmod Prop Hunt funny clips"*.
2. **Every round:** it goes to DeepSeek when picking trending topics, which leans toward what got views and away
   from what flopped, while still trying new things that are clearly trending.

Clips keep their record (with views) after their file is deleted, so the history survives disk cleanups.

**Reports** (every `REPORT_MINUTES`, default 60, or **Make report now**) list views per platform with the change
since last time, the best platform and the top clips. They're saved on the Reports page and sent on WhatsApp when
that's set up.

---

## Connecting your socials

Each platform is optional. Keys go in `.env`, then **restart the server**. The **Socials** page shows a green dot for
each connected platform.

| Platform | What you need | Time | Goes in `.env` |
|---|---|---|---|
| YouTube | `client_secret.json` + click Connect | 10 min | nothing |
| Facebook | A Facebook **Page** + a Meta developer app | 20 min | `FB_PAGE_ID`, `FB_PAGE_TOKEN` |
| Instagram | A **Business/Creator** Instagram linked to that Page | +5 min | `IG_USER_ID` |
| TikTok | A TikTok developer app | 30 min + review | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN` |
| WhatsApp report | The Meta app from Facebook + your phone | 20 min + template approval | `WA_PHONE_NUMBER_ID`, `WA_TOKEN`, `WA_TO`, `WA_TEMPLATE` |
| YouTube cookies | A browser signed in to YouTube | 2 min | nothing (Socials page) |

> Developer portals rename their menus often. If a button has a slightly different name, look for the closest match.

### YouTube

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **YouTube Data API v3**.
2. **OAuth consent screen** (newer UI: *Google Auth Platform → Audience*): choose External, and under **Test users**
   add the Google account that owns your channel.
3. **Credentials → Create OAuth client ID → Desktop app**. Download the JSON and save it as `client_secret.json` in
   the project folder (it's gitignored).
4. On **Socials**, click **Connect YouTube** and sign in with the channel's account. On "Google hasn't verified this
   app", click **Advanced → Go to (app name)**. The login is saved to `data/youtube_token.json` and allows both
   uploading and reading view counts.

- **`Error 403: access_denied`:** the account isn't a test user. Add it (step 2) and connect again.
- **Login stops working after ~7 days:** apps in "Testing" get short logins. Click **Reconnect YouTube**, or publish
  the app on the consent screen.
- **Quota:** 10,000 units a day by default, and an upload costs 1,600, so about **6 uploads a day**. Separately,
  YouTube caps uploads per channel (this channel hit `uploadLimitExceeded` at 10). Uploads from unverified API
  projects may be locked to private until Google audits the app.

### YouTube cookies (bot check)

When YouTube answers *"Sign in to confirm you're not a bot"*, downloads need a signed-in browser's cookies:

1. In Chrome, install the extension **Get cookies.txt LOCALLY**.
2. Open youtube.com **signed in**. A spare Google account is safest.
3. Click the extension, then **Export**.
4. On **Socials → YouTube cookies**, pick the file. It's saved as `cookies.txt` (gitignored) and used for every
   download, search and face check straight away, with no restart.

Alternatively, set `YT_COOKIES_FROM_BROWSER=firefox` in `.env`. Chrome and Edge on Windows lock and encrypt their
cookies, so yt-dlp can't read them directly. Cookies last weeks to months; add new ones when the bot check comes back.

### Facebook (Page)

Clipper posts to a **Facebook Page**, not a personal profile. If you don't have one: facebook.com → **Pages** →
**Create new Page**.

**1. Create a Meta app.**
- [developers.facebook.com](https://developers.facebook.com/) → **My Apps** → **Create App** → use case **Other**,
  type **Business**.
- Add the products **Facebook Login for Business** and **Instagram Graph API**.
- It can stay in **Development** mode: as its admin you can post to your own Page.

**2. Get a token with the right permissions.**
- In the [Graph API Explorer](https://developers.facebook.com/tools/explorer/), pick your app and **User Token**.
- Add these permissions, then **Generate Access Token**, accepting everything (pick your Page and Instagram):

| Permission | Why |
|---|---|
| `pages_show_list`, `pages_read_engagement` | find and read your Page |
| `pages_manage_posts` | post Reels / videos |
| `read_insights` | view counts |
| `instagram_basic`, `instagram_content_publish` | post Instagram Reels |
| `instagram_manage_insights` | Instagram view counts |
| `business_management` | linked business accounts |

**A permission isn't in the list** (e.g. `pages_manage_posts`)? The Explorer only offers what the app was given. Fix
it in the app dashboard, then reload the Explorer:
- **Newer apps (left menu has "Use cases"):** **Add use case → Manage everything on your Page → Customize** and add
  the `pages_*` and `read_insights` permissions. For Instagram also add **Manage messaging & content on Instagram**.
- **Older apps:** **App Review → Permissions and features → Get** for each one. No review is needed for your own Page.
- **No Page use case at all:** the app type has no Page access. Make a new app as **Other → Business**.

**3. Make it long-lived.** Click the **ⓘ** next to the token → **Open in Access Token Tool** → **Extend Access
Token**, and copy the long token.

**4. Get the Page ID and Page token.**
- In the Explorer, with the long token, run `GET me/accounts`. Your Page looks like
  `{ "name": "...", "id": "1234567890", "access_token": "EAAB..." }`.
- Put them in `.env`:
  ```
  FB_PAGE_ID=1234567890
  FB_PAGE_TOKEN=EAAB...        # the Page's access_token from me/accounts, NOT the user token
  ```
  A Page token made from a long-lived user token **doesn't expire**.

Clips up to 90s go up as **Reels**; longer ones as normal Page videos.

### Instagram

It uses the same Meta app and token as Facebook, so do Facebook first.

1. In the Instagram app: **Settings → Account type and tools → Switch to professional account** (Business or Creator).
   Then link it on Facebook: your Page → **Settings → Linked accounts → Instagram**.
2. In the Explorer, run `GET <FB_PAGE_ID>?fields=instagram_business_account` and put the id in `.env`:
   `IG_USER_ID=17841400000000000`. Instagram uses `FB_PAGE_TOKEN` automatically; set `IG_TOKEN=` only to use a
   different one.

Everything is posted as a **Reel** (the API allows 100 a day). See
[Instagram's quirks](#limits-and-known-platform-issues).

### TikTok

Until TikTok approves your app, posts are **private** (only you see them); make them public in the TikTok app.

**1. Create a developer app.**
- At [developers.tiktok.com](https://developers.tiktok.com/): **Manage apps → Connect an app**.
- Add the products **Login Kit** and **Content Posting API** (turn on **Direct Post**), and the scopes
  `user.info.basic`, `video.upload`, `video.publish`.
- Add a **Redirect URI**, e.g. `https://example.com/callback` (nothing needs to run there).
- Copy the keys into `.env` as `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`.
- To test before approval, use **Sandbox** and add your account under **Target users**.

**2. Log in once to get a refresh token.**
1. Open this in your browser:
   ```
   https://www.tiktok.com/v2/auth/authorize/?client_key=YOUR_CLIENT_KEY&scope=user.info.basic,video.upload,video.publish&response_type=code&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback&state=clipper
   ```
2. **Authorize**. Copy the `code=` value from the address you land on (the page itself may show an error; that's
   fine).
3. Straight away (the code expires in minutes), in PowerShell:
   ```powershell
   $body = @{ client_key = "YOUR_CLIENT_KEY"; client_secret = "YOUR_CLIENT_SECRET";
              code = [uri]::UnescapeDataString("PASTE_CODE_HERE"); grant_type = "authorization_code";
              redirect_uri = "https://example.com/callback" }
   Invoke-RestMethod -Method Post -Uri "https://open.tiktokapis.com/v2/oauth/token/" -Body $body `
     -ContentType "application/x-www-form-urlencoded"
   ```
4. Put `refresh_token` in `.env` as `TIKTOK_REFRESH_TOKEN`. Clipper keeps it fresh in `data/tiktok_token.json`; it
   lasts about a year.

**3. After approval,** set `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`. TikTok gives no view counts for private posts, so
reports show it as unavailable until then.

### WhatsApp report

It uses Meta's official **WhatsApp Cloud API**, from the same Meta app.

1. **Add WhatsApp.** Your app → **Add product → WhatsApp → Set up** (pick or create a Business portfolio). On
   **WhatsApp → API Setup**, copy the **Phone number ID** under the free test number into `WA_PHONE_NUMBER_ID`.
2. **Allow your number.** **To → Manage phone number list → Add**, then verify the code. Set `WA_TO=9779800000000`
   (country code first, no `+`).
3. **Permanent token.**
   - [business.facebook.com](https://business.facebook.com/) → **Settings → System users → Add** (Admin).
   - **Assign assets:** your app and WhatsApp account, both with full control.
   - **Generate new token:** expiry **Never**, scopes `whatsapp_business_messaging` and
     `whatsapp_business_management`. Put it in `WA_TOKEN`.
4. **Template.** WhatsApp only delivers free text within 24h of you messaging the number, so hourly reports need an
   approved template.
   - **WhatsApp Manager → Message templates → Create:** category Utility, name `clipper_report`, English (US).
   - Body: `Clipper views update: {{1}}`.
   - Once approved, set `WA_TEMPLATE=clipper_report` and `WA_TEMPLATE_LANG=en_US`. The report then arrives on one
     line, with `|` between parts.
5. **Test.** Restart, then click **Socials → Send report now**. Meta may charge a small fee per template message
   (hourly is about 720 a month); raise `REPORT_MINUTES` to send fewer.

---

## Limits and known platform issues

- **Upload limits.**
  - Every upload is tried, and the platform's own answer is shown on the clip card and in the banner.
  - When the answer is "limit exceeded", a **24h timer** starts (banner and Socials page), and the header counts
    YouTube uploads in the last 24h.
  - Only the autopilot holds back for limits.
- **Instagram.**
  - Its API often rejects Reels longer than about 30s for this account (`ProcessingFailedError` / `2207076`). The
    same file sometimes goes through on a second try, so a `ProcessingFailedError` is retried once automatically.
  - Clips over `IG_MAX_SECONDS` (30) get a red **↑ Instagram (45s, may fail)** button instead of a normal one. The
    autopilot tries them anyway.
  - Instagram also rejects video over **25 Mbps**. Clips render at 18 Mbps max, and older, heavier files are
    re-encoded for Instagram automatically.
- **Disk space.**
  - Each job deletes its source video (0.5-2 GB) and live recordings once it finishes, keeping the clips.
  - `KEEP_SOURCES=1` keeps them (needed to cut more clips from a video by hand later).
- **Reset everything** (⚙ Settings page, type `RESET`):
  - **Stops** all work: the autopilot, topics and jobs.
  - **Deletes** every video, clip, news post, topic, autopilot round, report, view count, upload-limit timer and log
    on this PC.
  - **Keeps** posts already online, and never touches `.env`. By default it also keeps the autopilot settings
    (switched OFF) and the YouTube/TikTok logins and cookies; untick those to remove them too.
- **Secrets.** Tokens are scrubbed from every log line, error and report (`access_token=***`).

---

## Settings (.env)

| Setting | Default | What it does |
|---|---|---|
| `DEEPSEEK_API_KEY` | (required) | DeepSeek key, for picking moments, titles, trends, judging and news |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | DeepSeek model |
| `FFMPEG` | `ffmpeg` | ffmpeg path (needs libass); ffprobe must sit next to it |
| `WHISPER_MODEL` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `SHORT_MIN` / `SHORT_MAX` | 15 / 60 | short clip length (s) |
| `LONG_MIN` / `LONG_MAX` | 60 / 180 | long clip length (s); nothing goes over 180 (Shorts limit) |
| `STRETCH_SHORT` / `STRETCH_LONG` | 10 / 10 | how far over the max the AI may go for context |
| `CROP_ASPECT` | 1 | default framing (0.5625 = full 9:16 ... 1.78 = whole frame) |
| `LOGO` | `clipper/assets/logo.png` | watermark; empty = none |
| `END_CARD` | `FOLLOW` | animated end card; `\|` = new line, last line on the red box; empty = off |
| `FOLLOW_LINE` | empty | the message above the headline asking viewers to follow; empty = the 5 built-in messages in turn ("I'm working hard to make these videos. One follow from you would mean the world ❤️", "Please break your scrolling streak for 2 seconds and hit follow…", …), one per clip, carrying on after restarts |
| `VOICE` | system default | voice-over voice (Windows SAPI / macOS `say` name) |
| `LIVE_CHUNK_SECONDS` | 600 | default live piece length |
| `KEEP_SOURCES` | 0 | 1 = keep source videos after clipping |
| `TOPIC_MIN_MINUTES` / `TOPIC_MAX_MINUTES` | 3 / 90 | only clip videos of this length from topics/autopilot |
| `SEARCH_REGION` | US | country for YouTube search and news |
| `YT_COOKIES` / `YT_COOKIES_FROM_BROWSER` | `cookies.txt` / empty | YouTube cookies (bot check) |
| `FB_PAGE_ID`, `FB_PAGE_TOKEN`, `IG_USER_ID`, `IG_TOKEN`, `GRAPH_VERSION` | | Facebook / Instagram |
| `IG_MAX_SECONDS` | 30 | clips longer than this get the red "may fail" Instagram button; 0 = off |
| `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN`, `TIKTOK_PRIVACY` | | TikTok |
| `WA_PHONE_NUMBER_ID`, `WA_TOKEN`, `WA_TO`, `WA_TEMPLATE`, `WA_TEMPLATE_LANG` | | WhatsApp report |
| `REPORT_MINUTES` | 60 | how often views are checked and reported |
| `WIKI_CONTACT` | empty | your email or site, to use Wikipedia for fact posts |
| `CLIPPER_DATA` | `data/` | where everything is stored (tests point it at a temp folder) |

Autopilot settings are set on the Auto page (stored in the database), not in `.env`.

---

## How it works inside

### Files

| File | Job |
|---|---|
| `app.py` | FastAPI server: the UI page, `/api/...` endpoints, `/media/...` (clip files). Starts the background loops when the server starts (importing it starts nothing). |
| `config.py` | reads `.env` into constants |
| `db.py` | SQLite (`data/clipper.db`), the logging helpers (`log`, `syslog`, `step`, `scrub`), settings |
| `worker.py` | one job: download/record → signals → transcribe → pick → render → upload; live pieces; `upload_clip`; deletes the source |
| `download.py` | everything yt-dlp: info, search, download (8 parallel chunks), live recording, chat, comments, cookies, bot-check detection |
| `signals.py` | the hype curve and candidate windows |
| `transcribe.py` | faster-whisper (GPU), word timestamps |
| `pick.py` | DeepSeek: moments, titles, hashtags, search queries, clippability; the shared `_ask` |
| `render.py` | ffmpeg: framing, ASS headline/captions/end card, logo, voice-over (TTS), GPU encoder, news videos, Instagram re-encode |
| `vision.py` | face check from YouTube's storyboard frames (OpenCV YuNet) |
| `topics.py` | the topic queue: queries → search → judge → face check → one job per video |
| `autopilot.py` | the daily rounds, the posting schedule, stop/OFF, news in rounds |
| `news.py` | news/facts: Google/Bing News RSS, article reading, Wikipedia, DeepSeek pick/write, post making, timed posts |
| `insights.py` | "what works" from view counts, the history text for DeepSeek, the learned niche |
| `social.py` | Facebook / Instagram / TikTok uploads, hashtags per platform, limit timers, upload counters |
| `upload.py` | YouTube upload and view counts (OAuth) |
| `stats.py` | view counts from every platform, reports, WhatsApp |
| `static/index.html` | the whole UI (one page; it polls `/api/state` every 2s) |

### One clip, step by step

1. **Job.** `POST /api/jobs` inserts a `jobs` row, and `worker.start` runs `_run` in a background thread with a stop
   flag.
2. **Download.**
   - **Videos (`_vod`):** the video (1080p, or 4K when asked; 8 fragments in parallel), the comments with the
     **most-replayed heatmap**, and the chat replay of past streams, all at once.
   - **Live (`_live`):** pieces are recorded with ffmpeg from fresh stream URLs while a chat thread timestamps every
     message. Each piece goes to `_process` in its own thread.
3. **Signals** (`signals.py`). Each signal is scaled to 0-1, weighted and summed into a per-second hype curve:

   | Signal | Weight | Notes |
   |---|---|---|
   | heatmap | 3 | YouTube's "most replayed" data |
   | chat speed | 2 | shifted 8s back, because people type after the moment |
   | comment timestamps | 1.5 | "12:34 lmao" → 754s |
   | loudness | 1 | ffmpeg EBU R128 |

   The hottest non-overlapping windows (3× the clips wanted) become candidates.
4. **Transcribe.** Overlapping windows are merged, and Whisper gives every word with its timing. One transcription
   runs at a time.
5. **Pick.** DeepSeek gets the candidates (times, hype, transcript) and returns clips with start/end, score, title,
   headline, description, hashtags and why. `validate()` clamps everything to the windows and length limits; if
   DeepSeek fails, the raw windows are used.
6. **Render** (2 at a time).
   - Framing crop → 1080x1920, then the follow message (red box, wrapped over 2-3 lines; the 5 messages take turns) above the headline (yellow box), captions (3 words at
     a time), the logo and the voice-over (ducked under the original audio).
   - The animated end card, encoded with NVENC (18 Mbps max) or x264.
   - Output: `data/jobs/<job>/clips/<clip>.mp4`.
7. **Post.**
   - **Manual jobs:** uploaded right away if platforms were picked.
   - **Autopilot clips:** become `scheduled` and wait for their slot.
   - **News timed for later:** become `timed`.
   - Each platform's result (id + link, or the error) is stored on the clip.
8. **Finish.** The job is done, and its source video is deleted.

### Background loops (started with the server)

- **Topic queue:** runs queued topics one at a time, one video at a time.
- **Autopilot (every minute):** posts a scheduled clip when its slot has come, posts timed posts, and every 5 min
  checks whether a round is due.
- **Stats (every `REPORT_MINUTES`):** views from every platform, then a report (and WhatsApp).

### Data

Everything lives under `data/` (gitignored):

- **`clipper.db`**
  - `jobs`, `clips` (with `uploads`, `views`, status: rendered / scheduled / timed / uploaded...)
  - `topics`, `auto_rounds` (the Activity record), `news` (stories used)
  - `reports`, `limit_hits`, `settings` (autopilot settings, post log, learned niche...)
- **`jobs/<id>/`:** clips (and the source until clipping finishes)
- **`logs/clipper.log`** (rotated at 5 MB)
- **`youtube_token.json`, `tiktok_token.json`**

### Self-checks

```powershell
.venv\Scripts\python -m clipper.signals
.venv\Scripts\python -m clipper.pick
.venv\Scripts\python -m clipper.render
.venv\Scripts\python -m clipper.topics
$env:CLIPPER_DATA = "$env:TEMP\clipper-test"; .venv\Scripts\python -m clipper.insights   # writes test rows: temp data only
```

---

## Troubleshooting

| Error | Fix |
|---|---|
| `ffmpeg is not installed` / `subtitles` filter missing | `FFMPEG=` must point at a full ffmpeg build with libass. |
| ffmpeg or the GPU libraries are "blocked" | Turn off Windows Smart App Control. |
| `Sign in to confirm you're not a bot` | Add YouTube cookies on the Socials page. |
| `uploadLimitExceeded` (YouTube) | YouTube's daily per-channel cap; a 24h timer shows. Post less or wait. |
| `facebook not connected` | `FB_PAGE_ID`/`FB_PAGE_TOKEN` missing, or the server wasn't restarted. |
| `(#200)` / `(#10) ... permission` | A Facebook permission is missing: redo step 2 with every box ticked, then steps 3-4. |
| `Error validating access token` / `Session has expired` | A short-lived token: redo Facebook steps 3-4 and use the Page token. |
| Instagram `ProcessingFailedError` | Usually a clip over ~30s or a too-high bitrate; it retries once, and the red button tries again. |
| Instagram `not a business account` | Switch Instagram to Business/Creator and link it to the Page. |
| TikTok `scope_not_authorized` | Add `video.publish` / `video.upload`, then redo TikTok step 2. |
| TikTok `unaudited_client_can_only_post_to_private_accounts` | Normal before approval: keep `TIKTOK_PRIVACY=SELF_ONLY`. |
| TikTok `invalid_grant` | The refresh token expired: redo TikTok step 2. |
| WhatsApp `(#131030) Recipient ... not in allowed list` | Add and verify your number (WhatsApp step 2). |
| WhatsApp `(#132001) Template name does not exist` | Not approved yet, or `WA_TEMPLATE` / `WA_TEMPLATE_LANG` don't match. |
| WhatsApp "sent" but nothing arrives | No template and no message from you in 24h: set up the template. |
| "this job's source video is gone" when cutting by hand | Sources are deleted after clipping; set `KEEP_SOURCES=1` for videos you want to re-cut. |
| Fact posts say "no readable articles" | Set `WIKI_CONTACT` to use Wikipedia, or try a broader topic. |

Full details, with tracebacks, are on the **Logs** page and in `data/logs/clipper.log`.

---

## Legal

Only clip and post content you own or have permission to use. Content ID claims and copyright strikes are on you.
News posts use the article's facts with the source credited and linked. Article pictures belong to their owners:
turn off **Use the article's picture** if you don't have the rights.
