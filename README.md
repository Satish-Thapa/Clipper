# Clipper

Clipper turns YouTube videos, live streams and video files into vertical clips (9:16 Shorts / Reels / TikToks) and
posts them in **slots**: the times you set, never more than your daily limit, on any platform.

- **Three modes.**
  - **Normal:** a YouTube link or a video file from your computer, clipped once.
  - **Live:** a YouTube stream that is live now, recorded in pieces; each piece is clipped while the next records.
  - **Auto:** every day at your research time, it finds the best videos for up to three topics (gaming, podcast or
    sports), clips them, and fills each topic's share of the day's slots with its best clips.
- **Slots and a hard daily limit.** You set how many shorts and longs post per day, at what times, and where each
  kind posts (e.g. shorts everywhere, longs only to YouTube). Weekend days get +1 of each. Drag clips between slots. Nothing (manual, Live, Auto, a retry) ever posts more in a day.
  You can make as many clips as you like: the extra ones wait in **Clips** until you place them.
- **4K, best quality.** Sources are always downloaded in the best quality up to 4K, and clips render in 4K vertical
  (2160x3840) with a colour grade, denoise and sharpening pass. YouTube, Facebook and TikTok get the 4K file;
  Instagram (its API takes 1920px at most) gets a 1080x1920 copy made for it.
- **Three framings, all full screen on a phone:** the video full screen; the streamer on top with the gameplay under
  it (zoomed a little to fill more of the screen; a blurred copy of the game fills the strip below); or your clip on top with a background
  video below (split).
- **On the video:** your logo small in the top-right corner, the topic header (in the middle when the streamer is on
  top, otherwise at the top; black text on a white box, still, always one line: a longer header gets smaller letters, never a second line) and steady word-by-word subtitles at the bottom (the
  word being said in yellow). The header names the creator only for a big individual creator ("SPEED ..."); for a
  smaller channel, a show or a network it just says what happens.
  Header, subtitles and logo can each be switched off.
- **English only, for a US audience:** Auto only picks English videos (searched in the US), Whisper transcribes in
  English, and YouTube uploads are marked English.
- **The AI** reads the whole transcript and uses the audience signals (YouTube's most-replayed heatmap, chat bursts,
  timestamped comments, loudness, the streamer's reactions) as hints. **Tell the AI** adds your own instructions.
- **Reports** from every platform's API (views, likes, comments, shares, saves, watch time, % watched, subscribers),
  refreshed every day. The AI reads them and writes the day's decisions for the researcher and the clip picker.
- Posts to **YouTube Shorts, Facebook Reels, Instagram Reels and TikTok**.

Everything runs on your PC. It uses the NVIDIA GPU for transcription and encoding when there is one.

## Contents

- [Setup](#setup)
- [The pages](#the-pages)
- [Slots and the daily limit](#slots-and-the-daily-limit)
- [Auto mode](#auto-mode)
- [How a clip is made](#how-a-clip-is-made)
- [Reports and decisions](#reports-and-decisions)
- [Connecting your socials](#connecting-your-socials)
- [Settings](#settings)
- [.env](#env)
- [How it works inside](#how-it-works-inside)
- [Limits and known platform issues](#limits-and-known-platform-issues)
- [Troubleshooting](#troubleshooting)
- [Legal](#legal)

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
6. In `.env`, set at least an AI (`DEEPSEEK_API_KEY=`, or `LLM_BASE_URL` + `LLM_MODEL`) and
   `FFMPEG=C:/ffmpeg/bin/ffmpeg.exe`.

### macOS

```bash
brew install ffmpeg-full deno uv            # ffmpeg-full has libass (burned-in subtitles)
uv venv -p 3.12 && uv pip install -e .
cp .env.example .env                        # FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
```

### Run it

```powershell
.venv\Scripts\python -m clipper              # hot reload: restarts by itself when you change the code
```

Open **http://127.0.0.1:8000** (works on a phone too, e.g. over a tunnel).
- **Hot reload:** it watches `clipper/*.py` and restarts when one changes. A video being clipped at that moment is
  marked "interrupted" (Jobs → Retry). The page needs only a browser refresh.
- **Changing `.env`:** stop the server (Ctrl+C) and start it again. Everything else is changed on the Settings page.
- **Options:** `--port 8080`, or `--no-reload` for a plain server. macOS: `.venv/bin/python -m clipper`.

**GPU:** with an NVIDIA card, Whisper runs on CUDA and clips are encoded with NVENC (the log says
`render: GPU encoder (h264_nvenc)`). Without one, both fall back to the CPU: slower, same result.

---

## The pages

The menu is on the left (on a phone: the ☰ button). Every setting has an **i**: hover it (or tap it) to see what
it does.

| Page | What it's for |
|---|---|
| **Overview** | Today: shorts / longs posted against the limit, today's slots, what's working, Auto's state, warnings. |
| **Create** | **Normal** (a YouTube link or a file) or **Live** (a stream live now). Clips to make, framing, header, subtitles, **Put the best clips in free slots: Off / Today / This week / This month**, Tell the AI. |
| **Auto** | Switch Auto on, your up-to-three topics and their slots, the research time, Run now / Stop, today's research and decisions. |
| **Jobs** | Every video being clipped: a step bar (download / recording → transcribe → AI pick → render → done), its clips (with virality), the reason when it failed, the log, the **hype curves** with the picked clips marked (hover or tap the graph to see the time, each line's value and the clip there; tick or untick each line in the legend to show or hide it, remembered per browser), Stop, Retry; a live job's piece length / pieces / clips per piece can change while it records. Select several to delete. |
| **Clips** | Every clip with its length, the 4K file's size, its virality score, moment type and the AI's five scores (hook, flow, emotion, share, trend), **upload progress** while it posts (per platform: percent, MB sent, speed, time left, or the step it's on: making its copy, processing on the platform; also in the reels), and **why an upload failed** (hide the message with ×). **Post** (pick where; the next free slot today), **Slot** (any free slot ahead), Unslot, **Retry** (pick the platforms; the failed ones are ticked), **Force** (post now to the platforms you pick, past every limit, warnings first), Edit, Why this clip?, Delete. Filters: Ready, In a slot, Posted, Failed; **search**; **sort** by virality, newest, latest posted, most views or length. Select several to delete. Only one video plays at a time. |
| **Reels** | (Clips → Watch as reels) Full-screen, swipe / up-down arrow keys, a **quality** picker at the top (**Fast** = the light 540p copy, the default and quickest over a tunnel; **HD** = a 1080p copy, made the first time; **4K** = the full file; remembered per browser, switching keeps your place), a **seek bar** at the bottom (drag it to skip parts; left / right arrows jump 5s), the clip's length and file size, **search** at the top (the same as Clips'), the details fade after 2 seconds of playing and come back when you pause, all the clip's info, and Post / Slot / Force / Info / Delete right there. Only the reels near the one you watch keep a video loaded, so scrolling stays smooth. |
| **Slots** | The next 7 days: every slot with its clip and state (free, kept for an Auto topic, scheduled, posted, passed). Where **shorts** post and where **longs** post (ticked here). **Drag** a scheduled clip onto another slot of its kind to move it (onto a taken one: they swap); on a phone use **Move**. **Choose clip** (with thumbnails) fills a free slot. A slot whose time has passed never takes a clip. **Clear** on a day takes every clip off that day's slots still ahead (back to Clips; posted ones stay). |
| **Cheat** | Paste reel links from **Facebook, Instagram or YouTube** (one per line). Each one is downloaded at the best quality there is (up to 4K), made into a **4K copy as it is** (vertical 2160x3840 / wide 3840x2160, fitted, never cropped, no text added) and put in a free slot (today / this week / this month, or wait in Clips). Up to 3 min = a short, longer = a long (15 min max). The **AI writes the title** (and a one-line description + hashtags) **built on the video's own title**: it keeps its subject and key words and just cleans it up (hashtags, emoji, @handles, "| Facebook" tails, caps); the caption only fills gaps, or is used when the title is generic ("Facebook", "Video by ..."); the description ends with a credit line for its creator; if every AI is down the original title is kept (edit in Clips). Every Cheat video gets **virality 90**, so it wins slots over most AI-picked clips. The daily limit still applies. Each link shows its state, its slot and why it failed. Reposting other people's videos can bring copyright claims. |
| **Reports** | Every number from the platforms, views per day, breakdowns (platform, category, moment type, length, posting hour, weekday, shorts vs longs, topic), YouTube channel analytics, best and weakest clips, the day's decisions. |
| **Socials** | Connected platforms, uploads in the last 24h, limit timers and errors; Connect / Reconnect YouTube; YouTube cookies. |
| **Settings** | Slots and limits, how new clips look, Tell the AI, clip lengths, sources, the researcher, background videos, AI models (with tests), **Reset** (erases jobs, their videos, Auto's runs, reports, numbers and logs; one checkbox, **Keep the clips** (ticked by default): only the clips stay and their slots are cleared; type RESET. Always kept: your Settings, background videos, logins, the platforms' limit timers and today's upload count, so the daily limit still holds). |
| **Logs** | The tail of `data/logs/clipper.log` (every job, Auto run and system event, with tracebacks). |

---

## Slots and the daily limit

- **A day's plan:** one slot per short time and one per long time (Settings → Slots). One slot = one clip, posted at
  that time to the platforms ticked for its kind (Slots page: *Shorts post to* / *Longs post to*).
- **Weekend:** on the weekend days you tick (default Friday and Saturday), each kind you post at all gets one extra
  slot, at *Weekend extra short at* / *Weekend extra long at*. (In the US and UK the weekend is usually Saturday and
  Sunday: change the days to suit your audience.)
- **The hard limit:** every upload, whatever started it (a slot, Post now, Retry, Auto), is checked first: a platform
  never gets more shorts (or longs) in a day than that day's slots. The job log says why when one is held back:
  `#12 not posted to youtube: today's youtube quota is used: 3/3 short(s) posted`.
- **Extra clips wait.** Make as many clips as you like; the ones without a slot wait in **Clips** as *Ready*.
- **Filling slots:**
  - **By hand:** Clips → **Slot** (any free slot in the next 7 days), or Slots → **Choose clip**.
  - **Post now:** takes the next *upcoming* free slot of the clip's kind today and posts straight away. A slot whose
    time has passed is never used; when none is left today, it says so.
  - **Force upload** (Clips / Reels): posts now to the platforms you tick, **past the daily limit, the slots and the
    platforms' own limits**. It shows every warning first, and the log marks it `FORCE UPLOAD`. A forced post still
    counts in today's numbers, so a later slot that day may then be held back on that platform.
  - **Put the best clips in free slots** (Create: Today / This week / This month): as the job's clips are made,
    the highest virality takes the earliest free slot of that time, never one kept for an Auto topic today.
    The rest wait.
  - **Auto:** each topic fills its own share (see below).
- **Posting:** every minute the app posts each clip whose slot time has come. If the app was off at a slot's time,
  that clip goes back to Clips (it never posts a day late).
- **A failed upload:** the reason shows on the clip. If it went up nowhere, it gives its slot back and waits in Clips;
  **Retry** then takes the next free slot today, like Post now. If it went up on some platforms, **Retry** tries the
  others (the limit still applies). Retry asks which platforms to try: the failed ones are ticked, the ones it's
  already posted on can't be picked, and you can add one it never went to.
- **Changing slot times:** clips sitting in a slot that no longer exists go back to Clips.

---

## Auto mode

Every day at the **research time**:

1. **The report.** It fetches every number from every platform, saves today's snapshot, and the AI writes the day's
   **decisions** (see [Reports](#reports-and-decisions)). This also runs when Auto is off.
2. **Each topic** (up to three, each **gaming**, **podcast** or **sports**):
   - **Topic:** yours (e.g. "GTA 6"), or, when empty, the AI picks what's trending in that category today from
     YouTube's most-viewed uploads of the week (ranked by views per hour, at most 3 per channel), leaning on the
     day's decisions and avoiding topics covered in the last 3 days.
   - **Finding the videos** (the researcher, below): the best `videos` for the topic.
   - **Clipping** them one at a time. Clips per video: yours (*Shorts / video*, *Longs / video*), or automatic = its
     slots ÷ videos + 1 spare to choose from (a topic with 2 short slots and 1 video cuts 3 shorts; with no long
     slots it cuts no longs).
   - **Framing:** gaming topics always use **streamer on top** (the facecam is found by itself; a video with no
     facecam is rendered full screen); podcast and sports topics use your default framing from Settings.
   - Every step, decision and its reason goes into the **live log** on the Auto page (which topic and why, every
     video kept or skipped and why, how many clips each job made with their virality, which clip got which slot).
   - **Filling its slots:** its best clips (by score) take its **short slots** and **long slots** of today. Until the
     topic is done, that share is kept free for it (the Slots page shows "kept for an Auto topic").
3. **Weekend:** the extra +1 slots get the best Auto clips left over.

The slots you give to Auto topics can't add up to more than the day's slots; the rest are for your own clips.
**Run now** runs today's research at once (even with Auto off). **Stop** aborts it and switches Auto off.

### The researcher (finding videos)

For each topic (`scout.find_videos`):

1. **Plan:** the AI names the category (Auto topics keep yours), 8-10 searches a US viewer would type this week
   (the topic, angles like "first reaction" / "funny moments", the big creators, this week's news), the trending
   angles, and whether it's a game's launch week.
2. **Search:** YouTube (US) uploads from this week; this month only while too few pass.
3. **Hard filters** (logged with the reason): clipped before, live, **not English** (YouTube's language, or a title
   mostly in another script), **podcasts under 300K views or from a channel under 100K subscribers** (none left = the
   topic is skipped), shorter than *Min video length* (sports: 4 min)
   or **longer than an hour** (*Max video length*, 60 min at most), older than *Max age* (launch week: 7 days), or
   dead on arrival (under 5,000 views after 48h).
4. **Full stats** for the shortlist: likes, comments, channel followers, heatmap, exact upload time.
5. **Score**, computed by the app (never by the AI), each part ranked 0-1 within the batch:
   `100 × (0.40 velocity + 0.25 outlier + 0.15 engagement + 0.10 freshness + 0.10 clippability)`.
   Velocity = views per hour; outlier = views ÷ channel followers; engagement = (likes + 3 × comments) ÷ views;
   freshness = 1 under a day, 0.8 under two, 0.5 under a week, else 0.2; clippability = the AI's 0-10 (+0.2 with a heatmap).
6. **The AI judges** each video with the category's rules: clippability 0-10 (under 6 = skipped), why, the moments
   it expects, copyright risk. It drops non-English videos, official league / broadcaster / studio channels, music
   videos and re-uploads; copyright risk "high" is skipped too. It suggests 3 better searches, tried when too few pass.
7. **Face check** (*Skip talking heads*), best score first, until enough pass. The topic's research log lists every
   video: `OK`, `skipped` (with why) or `spare`.

---

## How a clip is made

1. **Download** (the best quality up to 4K) plus the chat replay and timestamped comments; a live stream is
   recorded in pieces instead (picture and sound locked together, verified within 0.02s).
2. **Long videos** (Settings → *Clip in pieces from*, 60 min by default) are clipped in pieces like a stream; each
   piece gets the previous piece's last minute in front, so a moment on a boundary can still be one clip.
3. **Signals:** loudness, YouTube's most-replayed heatmap, chat bursts, timestamped comments → one hype curve.
4. **Whisper** transcribes the whole video with word timings; the streamer's reactions ("no way", "bro", shouting) are
   found in it.
5. **The AI picks the moments** from the whole transcript, the signals as hints. It is told:
   - **Your priorities:** funny conversations and banter first; then attention grabbers (a loud scream, a shocked
     "WHAT?!", surprise, panic, rage), which make the best hooks: the clip opens ON the scream.
   - **The hook comes first:** the first 2 seconds must stop the scroll (a bold line, a question, a shout, mid-action),
     never "um", greetings or setup. The hook is 40% of the clip's score; a clip that opens on a scream (very loud
     audio + shout words in its first 4 seconds) gets +3.
   - Build every clip as hook → content → ending; never end mid-thought; 20-60s is the sweet spot for shorts.
   - How to read the signals (a heatmap peak is the payoff, the setup is 5-20s before; chat reacts 5-15s late).
   - The category's rules (gaming / podcast / sports), today's decisions from the report, and your *Tell the AI*.
   - It scores each clip on hook, flow, emotion, share and trend (0-10); the app turns that into the 0-100 virality,
     +5 when 2+ audience signals peak inside the clip. Same-kind clips overlapping a better one are dropped.
   - Live pieces always make their best clip; extra ones need virality 60+.
6. **Snap** to whole sentences: the end runs on to the end of its sentence; the start is right on the first word of
   the moment (no lead-in footage).
7. **Render** in 4K (2160x3840) with the framing, the colour grade (contrast + vibrance) and detail pass (denoise +
   sharpening), your logo, the header and the subtitles. A master copy is kept so the header can be edited later, and
   the light preview the app plays is made straight away.

Every prompt the AI gets is saved, with its reply, and linked from the job log and from *Why this clip?*.

---

## Reports and decisions

Fetched every day at the research time (and with **Refresh now**):

| Platform | Numbers |
|---|---|
| YouTube | Data API: views, likes, comments. Analytics API: minutes watched, average view duration and % watched, subscribers gained, shares, plus the channel's views / minutes / subscribers per day and where the views come from (28 days). |
| Facebook | Reel plays (or video views), likes, comments, average watch time. |
| Instagram | Views, reach, likes, comments, shares, saves, average Reel watch time. |
| TikTok | Views, likes, comments, shares, once TikTok lists the posts (app posts stay private until the app is audited). |

A daily snapshot of every clip's numbers draws **views gained per day**. The breakdowns show views per clip by
platform, category, moment type, length, posting hour, weekday, shorts vs longs and topic.

**The day's decisions:** the AI reads all of it and writes a line for the **researcher** (which categories, topics and
creators to lean into), a line for the **clip picker** (which moment types, lengths and hooks got views), a summary,
and up to three **suggestions** for you (e.g. "move the 10:00 short to 19:00"). The researcher and the clip picker read
their lines in their prompts that day. The app never changes your settings by itself.

---

## Connecting your socials

Each platform is optional. Keys go in `.env`, then **restart the server**. The **Socials** page shows a green dot for
each connected platform.

| Platform | What you need | Goes in `.env` |
|---|---|---|
| YouTube | `client_secret.json` + click Connect | nothing |
| Facebook | A Facebook **Page** + a Meta developer app | `FB_PAGE_ID`, `FB_PAGE_TOKEN` |
| Instagram | A **Business/Creator** Instagram linked to that Page | `IG_USER_ID` |
| TikTok | A TikTok developer app | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN` |
| YouTube cookies | A browser signed in to YouTube | nothing (Socials page) |

### YouTube

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **YouTube Data API v3**
   and **YouTube Analytics API**.
2. **OAuth consent screen** (newer UI: *Google Auth Platform → Audience*): choose External, and under **Test users**
   add the Google account that owns your channel.
3. **Credentials → Create OAuth client ID → Desktop app**. Download the JSON and save it as `client_secret.json` in
   the project folder (it's gitignored).
4. On **Socials**, click **Connect YouTube** and sign in with the channel's account. On "Google hasn't verified this
   app", click **Advanced → Go to (app name)**. The login allows uploading, reading the numbers and read-only
   analytics. **A login from before analytics was added:** click **Reconnect YouTube** once (Socials shows
   "no analytics: reconnect").

- **`Error 403: access_denied`:** the account isn't a test user. Add it (step 2) and connect again.
- **Login stops working after ~7 days:** apps in "Testing" get short logins. Reconnect, or publish the app.
- **Quota:** 10,000 units a day by default, and an upload costs 1,600, so about **6 uploads a day**. YouTube also caps
  uploads per channel (`uploadLimitExceeded`).

### YouTube cookies (bot check)

When YouTube answers *"Sign in to confirm you're not a bot"*, downloads need a signed-in browser's cookies:

1. In Chrome, install the extension **Get cookies.txt LOCALLY**.
2. Open youtube.com **signed in**. A spare Google account is safest.
3. Click the extension, then **Export**.
4. On **Socials → YouTube cookies**, click **Add cookies.txt**. It's used for every download, search and face check at
   once.

Each file is **added** as its own set (name them apart, e.g. `cookies laptop.txt`; the same name replaces it), from
any computer that opens the app. When YouTube's bot check blocks one set, downloads switch to the next. Or set
`YT_COOKIES_FROM_BROWSER=firefox` in `.env`. Cookies last weeks to months.

### Facebook (Page)

Clipper posts to a **Facebook Page**. If you don't have one: facebook.com → **Pages** → **Create new Page**.

1. **A Meta app:** [developers.facebook.com](https://developers.facebook.com/) → **My Apps** → **Create App** → use
   case **Other**, type **Business**. Add **Facebook Login for Business** and **Instagram Graph API**. It can stay in
   Development mode.
2. **A token:** in the [Graph API Explorer](https://developers.facebook.com/tools/explorer/), pick your app and
   **User Token**, add `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `read_insights`,
   `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights`, `business_management`, then
   **Generate Access Token** (pick your Page and Instagram). A permission missing from the list? Newer apps: **Add use
   case → Manage everything on your Page → Customize**; older apps: **App Review → Permissions and features → Get**.
3. **Long-lived:** the **ⓘ** next to the token → **Open in Access Token Tool** → **Extend Access Token**.
4. **Page ID and token:** with the long token, `GET me/accounts`; put `FB_PAGE_ID=<id>` and
   `FB_PAGE_TOKEN=<that page's access_token>` (not the user token) in `.env`. It doesn't expire.

Clips up to 90s go up as **Reels**; longer ones as normal Page videos.

### Instagram

Same Meta app and token as Facebook. Switch the Instagram account to **Business or Creator** and link it to the Page
(Page → Settings → Linked accounts → Instagram). Then `GET <FB_PAGE_ID>?fields=instagram_business_account` and set
`IG_USER_ID=<id>`. Everything posts as a **Reel**.

### TikTok

Until TikTok approves your app, posts are **private**.

1. At [developers.tiktok.com](https://developers.tiktok.com/): **Manage apps → Connect an app**, add **Login Kit** and
   **Content Posting API** (Direct Post), scopes `user.info.basic`, `video.upload`, `video.publish` (and `video.list`
   for numbers), a redirect URI (e.g. `https://example.com/callback`). Keys → `TIKTOK_CLIENT_KEY`,
   `TIKTOK_CLIENT_SECRET`.
2. Open `https://www.tiktok.com/v2/auth/authorize/?client_key=YOUR_CLIENT_KEY&scope=user.info.basic,video.upload,video.publish,video.list&response_type=code&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback&state=clipper`,
   authorize, copy the `code=` value, and straight away:
   ```powershell
   $body = @{ client_key = "YOUR_CLIENT_KEY"; client_secret = "YOUR_CLIENT_SECRET";
              code = [uri]::UnescapeDataString("PASTE_CODE_HERE"); grant_type = "authorization_code";
              redirect_uri = "https://example.com/callback" }
   Invoke-RestMethod -Method Post -Uri "https://open.tiktokapis.com/v2/oauth/token/" -Body $body `
     -ContentType "application/x-www-form-urlencoded"
   ```
   Put `refresh_token` in `.env` as `TIKTOK_REFRESH_TOKEN` (kept fresh in `data/tiktok_token.json`).
3. After approval, set `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`.

---

## Settings

Everything you tune is on the **Settings** page (saved in the database). Hover the **i** next to each one in the app;
in short:

| Section | Settings |
|---|---|
| **Slots and daily limits** | Shorts per day + a time for each; longs per day + a time for each; weekend days and the extra slots' times; *Shorts post to* and *Longs post to* (also on the Slots page). |
| **How new clips look** | Framing (Full screen / Streamer on top / Split), facecam position (Auto or a corner), the split's background video, topic header, subtitles, logo top right. The Create page starts from these; Auto uses them. |
| **Tell the AI** | Your instructions for every mode (a video can add its own; the more specific one wins). |
| **Clip length** | Short min / max (180s at most: YouTube Shorts' limit), long min / max (**your call**, up to an hour; a long over 180s posts to YouTube as a normal video, still vertical), the **stretch** (how far a clip may run past its max when the content is better for it, e.g. the moment keeps building or a second payoff lands; while Facebook is ticked for shorts, a short stops at 90s so it stays a Facebook Reel), lead-in, room after, how far an edge may move to reach a sentence. |
| **Sources** | Clip in pieces from (min), live piece length, keep source videos, Instagram max length. |
| **Researcher (Auto)** | Min / max video length (60 min at most: longer videos are never picked), max age, search country, skip talking heads. |
| **Background videos** | For the split layout: upload a file or download one from YouTube. One in use can't be deleted. |
| **AI models** | The order (main + fallbacks), the model on each local server, and three tests per AI: **Speed**, **JSON** (follows the exact format) and **Real clip** (a sample stream through the real clip-picking prompt: valid answer, times inside the video, all 5 scores, found the obvious moment), with seconds and tokens per second. Test another model before switching to it. |

Settings that used to be in `.env` (SHORT_MIN, TOPIC_MIN_MINUTES, KEEP_SOURCES, ...) are read from `.env` once, the
first time, so nothing tuned there is lost; after that the Settings page is the place. You can delete them from `.env`.

---

## .env

Only keys, logins and paths:

| Key | What |
|---|---|
| `FFMPEG` | Full path to ffmpeg (with libass). |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` | DeepSeek. |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Any OpenAI-compatible AI, e.g. Ollama on another machine (`http://192.168.1.20:11434/v1`). |
| `PC_LLM_BASE_URL`, `PC_LLM_MODEL` | A small model on this PC, the last fallback. |
| `WHISPER_MODEL` | faster-whisper model (`small` default; `medium` / `large-v3` are better and slower). |
| `YT_COOKIES`, `YT_COOKIES_FROM_BROWSER`, `YT_CLIENT_SECRETS` | YouTube cookies file / browser, OAuth client file. |
| `FB_PAGE_ID`, `FB_PAGE_TOKEN`, `IG_USER_ID`, `IG_TOKEN`, `GRAPH_VERSION` | Facebook / Instagram. |
| `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN`, `TIKTOK_PRIVACY` | TikTok. |
| `CLIPPER_DATA` | Where the data folder is (default `data/`). |
| `LOGO`, `FOLLOW_LINE`, `VOICE`, `WIKI_CONTACT` | News & facts posts only (switched off for now). The clips' logo is `clipper/assets/logo.png`: replace that file to change it. |

---

## How it works inside

| File | What it does |
|---|---|
| `app.py` | The API the page talks to; validates everything. |
| `worker.py` | One job: download / record → signals → transcribe → AI pick → render; live pieces; `upload_clip` (the quota check on every upload). |
| `quota.py` | The slots: the day's plan, weekend +1, `can_post` (the hard limit), filling slots (Auto shares), by-hand placing, Post now, the minute tick. |
| `settings.py` | Every setting, validated in one place, with the hover help. |
| `autopilot.py` | The minute loop (posting), the daily report, Auto mode's research and slot filling. |
| `scout.py` | The researcher: queries, filters, stats, AI judge, face check, trending topics. |
| `pick.py` | The clip picker: the prompt, scores → virality, signal bonus, overlap check, headers. |
| `categories.py` | Gaming / podcast / sports rules for both. |
| `ai.py` | Every AI call: provider order and fallbacks, JSON mode, the prompt log, the AI tests. |
| `stats.py` / `insights.py` | Numbers from every platform, daily snapshots / what works and the day's decisions. |
| `download.py`, `signals.py`, `transcribe.py`, `render.py`, `vision.py`, `upload.py`, `social.py` | yt-dlp, the hype curve, Whisper, ffmpeg, the face detector, YouTube API, Facebook / Instagram / TikTok uploads. |
| `news.py` | News & facts posts (switched off; kept for later). |
| `static/index.html` | The whole page (plain JS, no build step). |

**Clip statuses:** `rendering` → `rendered` (Ready, waits in Clips) → `scheduled` (in a slot) → `uploading` →
`uploaded` (or `uploaded 1/2`, or `upload failed`, which gives the slot back).

**Data:** `data/clipper.db` (SQLite), `data/jobs/<id>/clips/` (clips + master copies), `data/fillers/`,
`data/cookies/`, `data/logs/` (the log + every AI prompt for 7 days).

**Self-checks:** `python -m clipper.<module>` for `pick`, `scout`, `quota`, `settings`, `render`, `signals`,
`stats`, `insights`, `categories`, `autopilot` (with `CLIPPER_DATA` set to an empty temp folder).
**Regression tests:** `tests/apitest.py` (every API rule: settings, jobs, slots, Post now, the quota guard, force
upload, bulk delete, AI tests), `tests/autotest.py` (Auto's day: topic shares, weekend leftovers),
`tests/cuttest.py` (a real 4K render of every framing). They always use a temp data folder and never upload.

---

## Limits and known platform issues

- **Platform upload limits.** When a platform says its limit is reached, a 24h timer starts (banner and Socials) and
  that platform is skipped until then.
- **Instagram.** Its API often rejects Reels longer than ~30s for this account (`ProcessingFailedError`). Clips over
  *Instagram max* skip Instagram; it also rejects video over 25 Mbps, so heavier files are re-encoded for it.
- **Disk space.** Each job deletes its source video (0.5-2 GB) and recordings when it finishes, keeping the clips and
  their small master copies (Settings → *Keep source videos* keeps them).
- **Secrets.** Tokens are scrubbed from every log line, error and report.

## Troubleshooting

| Error | Fix |
|---|---|
| `ffmpeg is not installed` / `subtitles` filter missing | `FFMPEG=` must point at a full ffmpeg build with libass. |
| ffmpeg or the GPU libraries are "blocked" | Turn off Windows Smart App Control. |
| `Sign in to confirm you're not a bot` | Add YouTube cookies on the Socials page. |
| `today's youtube quota is used` | Working as intended: today's slots of that kind are used. Put the clip in a later slot. |
| `this is a live stream: use Live mode` / `isn't live right now` | Pick the other mode on the Create page. |
| `uploadLimitExceeded` (YouTube) | YouTube's own per-channel cap; a 24h timer shows. |
| Reports: "YouTube analytics not granted yet" | Enable the YouTube Analytics API (YouTube step 1), then Reconnect YouTube. |
| `(#200)` / `(#10) ... permission` (Facebook) | A permission is missing: redo Facebook step 2 with every box ticked, then 3-4. |
| Instagram `ProcessingFailedError` | Usually a clip over ~30s; it retries once. |
| TikTok `scope_not_authorized` / `invalid_grant` | Add the scopes, or the refresh token expired: redo TikTok step 2. |

Full details, with tracebacks, are on the **Logs** page and in `data/logs/clipper.log`.

## Legal

Only clip and post content you own or have permission to use. Content ID claims and copyright strikes are on you.
The researcher skips official league / broadcaster channels and re-uploads, but you decide what goes up.
