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
12. [Technical deep dive](#technical-deep-dive)
13. [Troubleshooting](#troubleshooting)
14. [Legal](#legal)

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
.venv\Scripts\python -m clipper              # hot reload: restarts by itself when you change the code
```

Open **http://127.0.0.1:8000**.
- **Hot reload:** it watches `clipper/*.py` and restarts when one changes. A video being clipped at that moment is
  marked "interrupted"; queued topics carry on. The UI page needs only a browser refresh.
- **Changing `.env`:** stop the server (Ctrl+C) and start it again.
- **Options:** `--port 8080`, or `--no-reload` for a plain server.
- **The old way still works:** `.venv\Scripts\uvicorn clipper.app:app --port 8000`.
- **In VS Code:** Run and Debug → **Clipper server (hot reload)**.
- **macOS:** use `.venv/bin/python -m clipper`.

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
| **Clips** | Every clip: preview, title, score, why it was picked, where it's posted (with links), errors, upload buttons per platform, bulk upload, and **⏰ Schedule**: pick a date and time and which platforms for any clip not posted yet; it posts then by itself (even with the autopilot off) as long as the server is running. Scheduled clips show the time and platforms, with **Change** and **Cancel schedule**. Each clip keeps its own platforms, so the job's other clips aren't affected. |
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
  - **Top (the bigger part, 1080×1180):** the main clip, filling the full width edge to edge (cropped to fill, no
    bars), with no text over it.
  - **Below it (1080×740):** a **background video you provide** (gameplay, satisfying clips, parkour...), muted,
    starting at a random point and looped if it's shorter than the clip.
  - **Text:** the follow message and headline sit at the top of the background video, with the logo under them and
    the captions near the bottom, so the main clip stays clean. The end card is the same as other clips.
  - **Your library:** on **⚙ Settings → Background videos**, upload a file or **paste a YouTube link**; it
    downloads in the background (video only, up to 1080p, since it plays muted) and shows "downloading…" until it's
    ready. Delete any you don't want. Stored in `data/fillers/`, kept by "Reset everything". When Split is picked, a **Your video below** dropdown appears
    next to Framing.
- ***Streamer on top, gameplay below*** / ***Gameplay on top, streamer below*** (for streams with a facecam,
  live or not): full screen, no bars. The streamer's facecam is cut out of the frame and fills the full width of a
  1080×700 panel (about a third of the screen), and the gameplay (the middle of the frame, pushed away from the
  facecam's side so the streamer isn't in it twice) fills the other 1080×1220. Works on Create, Topics and Auto
  (and for watched streamers), for live streams and normal videos.
  - **Where's the streamer** appears next to Framing:
    - *Find the face automatically:* face detection (YuNet) on a few frames of each clip. It takes the face that stays
      in the same spot (a facecam doesn't move, faces in the game do) and frames head and shoulders. If no face is
      found, that clip uses normal full-screen framing, and the job log says so.
    - *Top-left / top-right / bottom-left / bottom-right corner:* the corner's 30% × 30% of the frame.
    - *I'll draw a box…* (Create page): paste the link first. Clipper grabs one real frame (the live edge of a live
      stream, or a bit into a video) and you drag a rectangle around the streamer. Exact, best when the facecam has an
      odd size or spot.
  - **Text:** the follow message and headline are always at the very top, on the video (over the top of the
    facecam panel when the streamer is on top; auto-framing leaves room above the head for them). Captions sit near
    the bottom, or just above the facecam when the streamer is below. How the face is found:
    [Technical deep dive](#finding-the-streamers-face-facecam-layouts).

**Follow message** (Create, Topics, Auto and News, on by default): the red "please follow" line at the top (the 5
messages in `FOLLOW_LINES` in `config.py` take turns, or `FOLLOW_LINE` in `.env` for always the same one). It has its
own checkbox, separate from **Headline + subtitles**: untick it to leave it off a job, or keep it on a clip that has
no headline or subtitles. On the Auto page it applies to the autopilot's clips and its news/fact posts.

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
3. **How the clips look:** framing (every framing from Create, including the streamer layouts with automatic face
   finding), headline + subtitles, follow message, voice-over, 4K, and the **face check** switch (skip videos that are
   mostly a person talking; off = clip them too).
   - **Mix framings:** tick several (9:16, 4:5, 1:1, 16:9, streamer on top, streamer below, split + background
     video) and each topic gets the next one in turn, so the channel doesn't look the same all day. None ticked =
     always the Framing above. The Activity view shows which framing each topic got.
4. **Streamers to watch:** YouTube channels (one per line, `@name` or a channel link) and **Watch these streamers**.
   Every 3 minutes each one is checked; when one goes **live**:
   - the autopilot **pauses**: the running round and its topics stop, and every scheduled/timed post is **held**
     (shown as "⏸ held while a streamer is live" on the Clips page);
   - the stream is clipped live as a normal live job, in the layout you pick (streamer on top / below / full
     screen), pieces of 5-30 min, N clips per piece, posting to the autopilot's platforms;
   - when no watched stream is being clipped any more, the held posts go back on schedule and the autopilot is
     switched back ON if it was on. Several streamers can be live at once (one job each).
   - The table under it shows each channel: 🔴 LIVE (and its job) or ⚫ offline, and when it was last checked.
5. **Advanced**
   - **How choosy:** candidate videos judged per video clipped (2-8).
   - **Start finding videos at:** when the day's first round starts.
   - **Long clips per video**, **who picks the moments**, **use the article's picture** for news.

### What a day looks like

1. **Start hour.** It works out what today still needs: posts per day minus posted, waiting and being made.
2. **Learning.** It refreshes the view counts on every platform. If there's no niche, it learns one from them.
3. **Finding trends** (see [Trend research](#trend-research-how-a-round-picks-topics) for the details).
   - The main AI writes search queries for the niche.
   - Each query is searched twice on YouTube (US): uploads **this week** and uploads **today**.
   - Shorts are dropped, videos uploaded today count double, live streams go to the top, and no channel gets more
     than 3 of the 40 videos the AI reads.
   - The AI names the topics with the most momentum and scores each 0-10. Below 4 is dropped, and a topic covered in
     the last 3 days is skipped unless it scores 9+. It leans toward what got views on your channel before.
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
- **Don't post** (Clips page): any clip waiting for an autopilot slot, held for a live stream, or timed for later can
  be taken off the schedule. It stays on the Clips page, unposted; **⏰ Schedule** can put it back on any time.

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
- **Clean starts.** The joined tail always starts on a video keyframe, so clips that begin in the overlap have
  picture and sound from the first frame (see [Live pieces start on a keyframe](#live-pieces-start-on-a-keyframe)).
- **Cleanup.** The recordings and the joined pieces are deleted when the job ends.

Past streams (replays) work like normal videos, and their chat replay (via yt-dlp) is used as a signal too.

**Long videos are clipped like streams.** A video at least `LONG_VIDEO_MINUTES` long (default 60) is downloaded once,
then cut into pieces of the job's piece length (default 10 min) and each piece goes through the same pipeline as a
live piece: its own loudness, transcript, AI pick and renders, **shorts/longs per piece**, dull pieces skipped, the
previous piece's last 60s in front so a moment on a boundary is one clip, two pieces at a time. The video's
most-replayed heatmap, chat replay and comment timestamps are shifted onto each piece's own clock. The log shows each
piece's place in the video (`[piece 4] 29:00-40:00 of the video`). A 3-hour video with 3 shorts per piece can make
up to 54 clips; use **Pieces to clip** to cap it.

**Streamers to watch** (Auto page): see [Autopilot](#autopilot). A watched streamer going live starts exactly this
kind of live job by itself.

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
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | empty (= DeepSeek) | use any other model instead: cloud with a key or local Ollama ([below](#use-another-ai-model-any-provider-or-local-ollama)) |
| `FFMPEG` | `ffmpeg` | ffmpeg path (needs libass); ffprobe must sit next to it |
| `WHISPER_MODEL` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `SHORT_MIN` / `SHORT_MAX` | 20 / 90 | short clip length (s) |
| `LONG_MIN` / `LONG_MAX` | 90 / 180 | long clip length (s); nothing goes over 180 (Shorts limit) |
| `CLIP_PAD_BEFORE` / `CLIP_PAD_AFTER` | 0.8 / 1.5 | breathing room (s) before the first word and after the last, once a clip is snapped to whole sentences |
| `SNAP_REACH` | 8 | how far (s) a clip edge may move to reach the start/end of a sentence |
| `LONG_VIDEO_MINUTES` | 60 | videos this long or longer are clipped like a stream, in pieces (0 = never) |
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

### Use another AI model (any provider or local Ollama)

Every AI call (picking moments, titles, voice-over, news, autopilot) goes to DeepSeek unless `LLM_BASE_URL` is
set. Then it goes to any OpenAI-compatible API instead. The top bar shows **AI: (model name)** in place of the
DeepSeek balance.

**Cloud, with an API key:** set all three and restart.

| Provider | `LLM_BASE_URL` |
|---|---|
| OpenAI | `https://api.openai.com/v1` |
| OpenRouter (Claude, Gemini, Llama, … with one key) | `https://openrouter.ai/api/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` |

Set `LLM_API_KEY` to that provider's key and `LLM_MODEL` to the model name exactly as the provider writes it.

**Local, with Ollama on another machine** (e.g. a PC with a GPU on your home network):

1. Install [Ollama](https://ollama.com) on that machine and pull a model. JSON-reliable models work best.
   Rough guide by GPU memory: 8 GB → `qwen2.5:7b`, 16 GB → `qwen2.5:14b`, 24 GB+ → `qwen2.5:32b`.
   ```bash
   ollama pull qwen2.5:14b
   ```
2. **Let other machines reach it.** Set the environment variable `OLLAMA_HOST=0.0.0.0` on that machine, then
   restart Ollama:
   - Windows: System Properties → Environment Variables → New (user), then quit Ollama from the tray and start it again.
   - Linux: `sudo systemctl edit ollama`, add `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]`, then
     `sudo systemctl restart ollama`.
   - macOS: `launchctl setenv OLLAMA_HOST 0.0.0.0`, then restart the Ollama app.

   Allow port **11434** through that machine's firewall.
3. **Give it a big context.** Ollama's default context is only a few thousand tokens and silently cuts long
   prompts, which would cut off full transcripts. Also set `OLLAMA_CONTEXT_LENGTH=32768` (same way as step 2).
   On older Ollama versions, make a bigger copy of the model instead and use its name as `LLM_MODEL`:
   ```bash
   printf 'FROM qwen2.5:14b\nPARAMETER num_ctx 32768\n' > Modelfile
   ollama create qwen2.5-32k -f Modelfile
   ```
4. Find that machine's IP (`ipconfig` on Windows, `ip addr` on Linux). From the Clipper machine, check it answers:
   ```bash
   curl http://192.168.1.50:11434/v1/models
   ```
5. In Clipper's `.env`, leave `LLM_API_KEY` empty, then restart Clipper:
   ```
   LLM_BASE_URL=http://192.168.1.50:11434/v1
   LLM_MODEL=qwen2.5:14b
   ```

LM Studio works the same way, using its server URL (`http://(ip):1234/v1`).

**Pick the AI and its fallbacks** without editing `.env` or restarting: open **⚙ Settings → AI models**, choose the **Main** AI
and up to two **Fallbacks** (DeepSeek, Local (Ollama), This PC (Ollama)), pick the local models from
the lists (read live from each Ollama), then click **Save**. Every AI call (clips, titles, voice-over, autopilot, news) tries them in
that order. If one is down, returns an error or gives a broken answer, the next one gets the same request, and the
log says so. AIs with no key in `.env` are skipped. Each AI has a **Test** button that asks it "What model
are you?" and shows its reply. With nothing
saved, the order is Local → DeepSeek → This PC when `LLM_BASE_URL` is set, otherwise
DeepSeek → Local → This PC.

**This PC** is a small model running on the Clipper computer itself, the last resort when the network or every
other AI is down. Install [Ollama](https://ollama.com) on this computer, run `ollama pull qwen3.5:2b` (a 2B model
fits a 4 GB GPU like a GTX 1650), and it shows up as ready. `PC_LLM_BASE_URL` (default
`http://localhost:11434/v1`) and `PC_LLM_MODEL` set it in `.env`. Small models are weaker at picking and titles,
so keep it last.

Notes:
- **Quality and speed:** local models are usually weaker than DeepSeek at picking moments and writing titles,
  and slower on full transcripts. For long videos, the "hype curves" mode sends much shorter prompts.
- **JSON:** if a provider doesn't support JSON mode, Clipper retries without it and pulls the JSON out of
  the reply.
- **No password:** Ollama has no login, so only expose it on your home network, never to the internet.

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
| `pick.py` | the AI: moments, titles, hashtags, search queries, clippability; `_ask` with the provider order and fallbacks (DeepSeek / Ollama on another machine / Ollama on this PC), `ai_name` for the logs |
| `render.py` | ffmpeg: framing, split and **facecam** layouts (`cam_layout`, `find_cam`), ASS headline/captions/end card, logo, voice-over (TTS), GPU encoder, news videos, Instagram re-encode |
| `vision.py` | face check from YouTube's storyboard frames (OpenCV YuNet) |
| `topics.py` | the topic queue: queries → search → judge → face check → one job per video |
| `autopilot.py` | the daily rounds, trend research, framing mix, the posting schedule, stop/OFF, news in rounds, the **streamer watch** (pause, live job, resume) |
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
- **Autopilot (every minute):** posts a scheduled clip when its slot has come, posts timed posts, every 3 min checks
  the watched streamers, and every 5 min checks whether a round is due (not while a watched streamer is live).
- **Stats (every `REPORT_MINUTES`):** views from every platform, then a report (and WhatsApp).

### Data

Everything lives under `data/` (gitignored):

- **`clipper.db`**
  - `jobs`, `clips` (with `uploads`, `views`, `post_at` + `post_to` for timed posts, status: rendered / scheduled /
    timed / paused (streamer live) / uploaded...)
  - `topics`, `auto_rounds` (the Activity record), `news` (stories used)
  - `reports`, `limit_hits`, `settings` (autopilot settings, post log, learned niche, AI order, `live_watch` per
    channel, `live_pause` while a streamer is live, `mix_idx`...)
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

## Technical deep dive

How each part actually works, which tool does what, and why it's built that way.

### Tools

| Tool | Used for | Where |
|---|---|---|
| **Python 3.12** + **FastAPI** / **uvicorn** | the server and every `/api/...` endpoint; background loops run as daemon threads | `app.py`, `__main__.py` |
| **SQLite** | jobs, clips, topics, rounds, settings; migrations are `ALTER TABLE ADD COLUMN` on start | `db.py` |
| **yt-dlp** | video info, YouTube search, downloads (8 fragments at once), live recording URLs, chat (`live_chat` track), comments, storyboards, a channel's `/live` page | `download.py`, `vision.py`, `autopilot.py` |
| **ffmpeg** / **ffprobe** | loudness (EBU R128), cutting pieces (`-c copy`), every render (filter graphs, libass subtitles), one-frame grabs, NVENC / x264 encoding | `signals.py`, `render.py`, `worker.py`, `download.py` |
| **faster-whisper** | word-level transcripts on the GPU, one at a time (a lock) | `transcribe.py` |
| **OpenCV** + **YuNet** (`face_detection_yunet_2023mar.onnx`, MIT) | face detection: the face check and the streamer facecam finder | `vision.py`, `render.py` |
| **NumPy** | hype curves, frame maths, medians | `signals.py`, `vision.py`, `render.py` |
| **OpenAI Python SDK** | one client for every AI: DeepSeek's API and Ollama's OpenAI-compatible `/v1` | `pick.py` |
| **Ollama** | local models: gpt-oss:20b on the other machine, qwen3.5:2b on this PC | `pick.py` |
| **httpx** | platform APIs (Facebook, Instagram, TikTok), Ollama model lists, storyboard images, RSS | `social.py`, `pick.py`, `vision.py`, `news.py` |
| **Windows SAPI** (PowerShell) / macOS `say` | voice-over TTS | `render.py` |
| **NVENC** (GTX 1650) | GPU H.264 encoding, picked by a 1-frame test encode at start; x264 on the CPU otherwise | `render.py` |

### Finding the streamer's face (facecam layouts)

Two different face jobs use the same detector, **YuNet** (OpenCV's `FaceDetectorYN`, a small CNN that returns a box
and confidence for every face):

1. **Face check** (`vision.py`, before a video is chosen): "is this video mostly a person talking?"
2. **Facecam finder** (`render.find_cam`, while rendering a clip): "where is the streamer's webcam in the frame?"

**Facecam finder, step by step** (`Where's the streamer → Find the face automatically`):

1. **Sample frames.** OpenCV opens the downloaded source and reads 6 frames spread evenly over the clip
   (`np.linspace(start+0.5, end-0.5, 6)`), seeking by milliseconds.
2. **Detect.** YuNet runs on each full-resolution frame with a 0.7 confidence threshold. Every face box
   `(x, y, w, h)` from every frame goes into one list. Game characters, faces on posters and the streamer all
   end up there.
3. **Keep the face that doesn't move.** A facecam is a fixed overlay, so the streamer's face sits in the same spot in
   every frame; faces in the game move around. For each box it counts how many boxes are "near" it (centres
   within one face width/height), and keeps the box with the most neighbours. Fewer than 2 = no steady face =
   no facecam: that clip falls back to normal full-screen framing and the job log says
   `facecam: no face found, using normal full-screen framing`.
4. **Median.** The median x, y, w, h of that group gives a stable face box (one blurry frame can't throw it off).
5. **Frame head and shoulders.** The crop box is `2.8 × face height` tall and 4:3 wide (a webcam's shape; wider
   would pull in the game next to the facecam). The face sits 68% of the way down the box when the streamer is on
   top (so the headline drawn over the top of the panel lands above the head) and 45% when below. The box is
   clamped inside the frame.
6. **Result** is a share of the frame (0-1), logged as `facecam: found at x 5% y 69% (24% x 31% of the frame)`.

**The other two ways:**

- **Corner presets:** a fixed 30% × 30% box in that corner (`CAM_CORNERS`).
- **Draw a box** (Create page): `GET /api/frame?url=` gets one real frame. yt-dlp resolves the stream URL
  (720p), ffmpeg reads one frame with yt-dlp's HTTP headers: the live edge for a live stream, or 30% in (max 10 min)
  for a video. Long single-file videos hang on a direct seek, so it tries a quick seek (12s), then decoding 20s
  from the start, then the very first frame (streams that just ended can't be seeked yet). The page shows the frame,
  you drag a rectangle (pointer events, stored as 0-1 fractions), and the layout is saved as
  `cam:top:x,y,w,h`. YouTube's signed URLs never reach an error message.

**How the layout is drawn** (one ffmpeg filter graph, `render.cut`):

```
[0:v] split ─┬─ crop facecam box ─ scale to 1080 wide (fill) ─ crop 1080x700 ───────────── [cam]
             └─ crop a slice of the frame (1080:1220 shape) away from the facecam's side ─ scale/crop 1080x1220 ─ [game]
[cam][game] vstack  (or [game][cam] for "streamer below")  →  1080x1920  →  subtitles (headline, follow, captions)  →  logo
```

- **Full screen:** both panels fill the width; the facecam is only cropped top/bottom *inside its own box*.
- **Gameplay slice:** the middle of the frame, pushed away from the facecam's side (`x + w + 5%` when the facecam is
  on the left, `x - 5%` when on the right), so the streamer isn't shown twice.
- **Text:** the follow message and headline are drawn at the top, on the video. Captions sit near the bottom when the
  streamer is on top, and just above the facecam when it's below (`cap_margin = CAM_H + 40`).

**Face check** (`vision.py`): no download. It reads YouTube's **storyboard** (the tiny preview mosaics shown when
you hover the seek bar), splits them into ~30 frames spread over the video, upscales each 2× (YuNet misses faces
under ~10px), and finds the biggest face. A frame counts as "person" only when one face covers 3%+ of the frame (a
close-up); facecams and crowds are smaller. More than 40% such frames = mostly a person talking = skipped. It also
measures how many neighbouring frames are identical (still image / slideshow / audio podcast).

### AI routing and fallbacks

- **Settings → AI models** saves an order (`ai_order`), e.g. `local, deepseek, pc`.
- Every AI call goes through `pick._ask`: it tries each provider in order with the OpenAI SDK (`chat.completions`,
  JSON mode; plain mode when a model rejects JSON mode), strips `<think>` blocks and code fences, parses the JSON.
  Connection errors, API errors or broken JSON → the next provider gets the same request.
- **Local** (other machine) has a 30-min read timeout for long transcripts but a 10s connect timeout, so a machine
  that's off fails fast. **This PC** sends `reasoning_effort: none` (qwen3.5:2b answers in 1-4s instead of ~12s).
- Logs name the AI before and after: `STEP 5 asking gpt-oss:20b (Local (Ollama)) to pick...`, then
  `🤖 AI: gpt-oss:20b answered in 4.2s` (or `failed ..., trying the next one` and `(fallback)`). The 🤖 line goes
  into the log of the job/topic/News run it belongs to (a thread-local "where to log" set by `db.step` and
  `db.log_to`).

### Trend research (how a round picks topics)

1. **Queries:** the AI turns the niche (or "anything trending" / the niche learned from your views) into 4-6 YouTube
   searches.
2. **Two searches per query:** this week (20 results) and **today** (12 results), through yt-dlp's flat search (no
   download).
3. **Ranking:** Shorts and videos under 90s are dropped (nothing to clip); score = views × 2 if uploaded today
   (rising now), live streams first; then at most 3 videos per channel, top 40.
4. **The AI reads** those 40 (title, channel, views, minutes, `new`, `live`) plus the topics covered in the last 3
   days and your channel's history ("what got views, what flopped"), and returns topics with a 0-10 score
   (momentum × how well it clips).
5. **Filtering:** below 4 → dropped; same as a topic from the last 3 days (75%+ word overlap) → dropped unless it
   scores 9+; duplicates within the round → dropped. Best score first.
6. **Activity view** records all of it: queries with week/today counts, the 40 videos, the "smart" notes (how many new
   today, Shorts dropped, per-channel cap, topics avoided) and the dropped topics with scores.
7. **Framing mix:** each queued topic takes the next framing from the mix (`mix_idx` in settings) and keeps it even
   when settings change while it runs.

### Streamer watch

A small state machine in `autopilot.watch_tick`, run every 3 minutes by the autopilot loop (also while it's OFF):

```
for each channel:
  job running for it?  → still live, nothing to do
  watch off?           → skip
  yt-dlp  <channel>/live  (process=False: one page, no download)
     "not currently live"  → offline
     a video id            → confirm it's live (not "upcoming") → new stream (not the one already clipped)?
                                → pause the autopilot (once) → start a live job → remember job + video id
none running and paused  → resume
```

- **Pause** (`_pause_for_live`): stores `{was_enabled, held: {clip id: status}}` in `live_pause`, sets the
  running round's abort flag, stops autopilot topics, marks scheduled/timed clips `paused (streamer live)`, and
  switches the autopilot OFF.
- **Resume** (`_resume_after_live`): puts each held clip back to its old status (timed posts whose time passed
  post on the next minute), and switches the autopilot back ON if it was on. **■ Stop everything** clears the pause
  so it won't switch back on by itself.
- A stream you stop by hand isn't restarted (the video id is remembered); the channel's **next** stream is.

### Long videos in pieces

`worker._pieces`: ffprobe gives the length; ffmpeg cuts piece *k* from `k×L − 60s` to `(k+1)×L` with `-c copy`
(no re-encode, a second or two per piece). Copying starts at the keyframe before the cut, so the real start is
worked out as `end − piece length`, and the boundary (seconds of the previous piece in front) from that. Heatmap
ranges, chat and comment times are moved onto the piece's clock and clipped to it. Each piece runs `_process` with
`live={"piece", "boundary"}`, the same switch live pieces use: the AI may skip a dull piece, picks under 55 are
dropped, and a clip may start in the overlap only if the moment continues into the new piece.

### Making every clip complete (no cut mid-thought)

1. **Sentence markers.** The transcript the AI reads has a `[seconds]` marker at **every sentence start** (and at
   least every 5s), so it picks real sentence boundaries instead of guessing between markers.
2. **The prompt's story check.** Every clip must have a SETUP (the first line a stranger needs), THE MOMENT, and THE
   LANDING (the reaction or resolution finishing). A clip may not end mid-thought or mid-action, nor start on
   "and / but / so / he / it" pointing at something earlier. Before answering, the AI re-reads each clip's first and
   last sentence and moves the edges until they stand on their own. When in doubt: one sentence earlier, one later.
3. **Snapping.** In code, each edge is moved out to the start/end of its sentence (up to `SNAP_REACH` seconds), using
   Whisper's punctuation and word timings.
4. **Breathing room.** `CLIP_PAD_BEFORE` before the first word and `CLIP_PAD_AFTER` after the last, so a clip doesn't
   open on a clipped syllable and the reaction has time to land. Never past the video's end or the hard max.

### Live pieces start on a keyframe

A live piece is processed with the previous piece's last 60s in front. That tail is joined with ffmpeg's concat
demuxer and `-c copy`, so it must start **on a video keyframe** (live streams have one every ~2-5s): `inpoint` is the
exact timestamp of the last keyframe before the wanted start, found with ffprobe (`packet=pts_time,flags`). Starting
anywhere else keeps the audio from there but the video only from the next keyframe, which showed as a stuck or black
first few seconds on clips that started in the overlap.

### Post statuses

```
rendering → rendered ──(manual Upload)──────────────────────────→ uploading → uploaded
              │  └─(⏰ Schedule: time + platforms → post_at, post_to)→ timed ──(time comes, news.timed_tick)→ uploading
              └─(autopilot clip)→ scheduled ──(its slot, upload_tick)→ uploading
   scheduled / timed ──(watched streamer live)→ paused (streamer live) ──(stream over)→ back to scheduled / timed
   scheduled / timed / paused ──(Don't post)→ rendered        ■ Stop everything: scheduled → not uploaded (autopilot stopped)
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
