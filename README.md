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
7. [Clipping campaigns (Whop)](#clipping-campaigns-whop)
8. [What works (analytics) and reports](#what-works-analytics-and-reports)
9. [Connecting your socials](#connecting-your-socials)
10. [Limits and known platform issues](#limits-and-known-platform-issues)
11. [Settings (.env)](#settings-env)
12. [How it works inside](#how-it-works-inside)
13. [Technical deep dive](#technical-deep-dive)
14. [Troubleshooting](#troubleshooting)
15. [Legal](#legal)

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
   JOB: download (8 parallel chunks, 1440p or 4K) ◀┘                      │
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
| **Campaigns** | Paid clipping campaigns (Whop): paste the brief → rules, add the official videos → clips with captions built to the rules, a ✓/✕ checklist per clip, copy / download / post / save the post link. See [Clipping campaigns](#clipping-campaigns-whop). |
| **Jobs** | Every video being or already clipped: status, full log, hype curves, "cut it yourself", and live controls for streams. **■ Stop** stops a job at the next step: the download is aborted, and loudness, transcript, AI pick and each render check first (clips already made stay; a live recording stops too). **↻ Retry** on a failed, interrupted or stopped job runs it again as a new job with the same settings (the old one stays as a record, marked "retried as #N"; an uploaded file is reused if it's still there). At the top: **↻ Retry all failed** (failed + interrupted, not yet retried) and **■ Stop all running**. |
| **Clips** | **✏ Edit** on a clip card: change the title, the **topic on the video** (the headline), the description and the hashtags (campaign clips: the exact caption, re-checked against the rules). Text is saved at once; a new topic renders the clip again in the background from its **master copy** (`<id>_master.mp4` + `<id>_words.json` next to the clip: the clip's stretch of the source with no text, kept for every clip, so this works after the source video is deleted). Clips made before this, with their source already deleted, can still change everything but the topic. Edits don't change posts already up. **▶ Watch as reels (full screen)** (or **⛶ Reels** on a clip card): every clip full screen, one per screen, like Instagram. Scroll/swipe or ↑ ↓ to the next; the visible one autoplays and loops (space pauses, M toggles sound, Esc closes). Filter: all / not posted yet / scheduled / posted. Buttons on the right: sound, **post** now to a platform, **schedule** (when + where) or **don't post**, **why** this clip, open the file, delete; title, headline, reason and where it's posted/scheduled at the bottom, updating live. Every clip: preview, title, score, why it was picked (open **Why this clip?** for the AI's reasoning as hook → content → ending, its virality score, which AI picked it with a link to the exact prompt + reply, how the audience signals peaked in that stretch (most-replayed, chat speed, comments, loudness, from "normal" to "top 5% of the video"), and the clip's opening and closing lines; manual cuts say they were yours), where it's posted (with links), errors, upload buttons per platform, bulk upload, and **⏰ Schedule**: pick a date and time and which platforms for any clip not posted yet; it posts then by itself (even with the autopilot off) as long as the server is running. Scheduled clips show the time and platforms, with **Change** and **Cancel schedule**. Each clip keeps its own platforms, so the job's other clips aren't affected. |
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

**Tell the AI** (Create, Topics, Auto; optional, up to 1000 characters): your own instructions, added to the AI's
prompts as "extra instructions from the channel owner". They win over the general guidance when they conflict, but
never break the JSON format or the clip length limits. Examples: *"only the funny fails and police chases"*, *"no
swearing in titles"*, *"skip the intro and the sponsor read"*, *"only GTA RP servers, no reaction channels"*.
- **Create:** used when picking the moments and writing titles, headlines and descriptions.
- **Topics:** also used for the YouTube searches and for judging which videos are worth clipping; every job the topic
  starts carries them.
- **Auto:** also used for finding the day's trends; every topic and job the autopilot starts carries them (watched
  streamers' live jobs too), and changing them applies to work already queued.
- The job/topic log starts with `📝 your instructions for the AI: ...`, the card shows **📝 your AI instructions**
  (hover to read them), and **Retry** keeps them.

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
- **Fast to watch, even over a tunnel** (Clips page, reels viewer): the players show a small poster image and only
  load the video when you press play. They play a light preview (540 wide, about 1.2 Mbps, about a tenth of the
  data), made the first time a clip is played and remade when the clip changes. The reels viewer also loads the
  next reel while you watch one. **Download**, **Open** and every upload use the full-quality clip.
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
 DeepSeek: clips   clips / JOINED clip   at least 1 clip, always
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
- **The reel look (live clips, and videos with Clip style "Clean").** Each clip keeps the job's framing / layout
  (e.g. streamer on top, gameplay below) and the original sound. On top of that:
  - **A funny header:** the streamer's name and what they're doing, ending with one emoji, for example
    "SPEED THINKS ANIMALS ARE HUMAN 😭". It's at most 7 words, written by the AI (`pick.reel_note`), and the
    streamer's name comes from the channel.
  - **Layout:** the header sits at the top.
  - **Subtitles:** the streamer's words, at the bottom. The word being said turns yellow and bounces.
  - **What's left out:** the follow line, logo, end card and voice-over.
  - **Emoji:** they render in black and white, because the subtitle renderer has no colour emoji.
- **Upload only the best, for any video.** "Upload only the best N shorts / M long" applies to:
  - normal videos: once all their clips are made;
  - live streams and long videos: per piece.
  The rest wait in Clips.
- **Long clips show what you can do in the game.** For game videos, a long clip can be a full showcase of one
  activity or feature, shown start to finish and titled that way: "You Can Take Dates In GTA 6".
- **Clip a whole channel** (Create page). Paste a channel link and say how many videos. It ranks the channel's
  recent uploads and past streams by views and by how recently they went up, and takes the best overall (lowest
  rank by views + rank by date). It does them one at a time, with the settings of the form above.

  Videos longer than your limit are clipped, and their titles stay on that video's topic. Shorter ones are posted
  whole as one clip in the same format (header, subtitles), under the video's own title; the AI writes the header
  and hashtags. Videos already done are skipped, so run it again later for the new ones (`channels.py`).
  **Upload one clip every N minutes, starting at a time you pick:** the clips that are uploaded go into a queue
  instead of all at once. The best go first, and they show as ⏰ scheduled in Clips (`worker.post_or_queue`; the
  minute timer posts them, autopilot on or off).
- **Clips page: infinite scroll and "▶ Watch from here".** The Clips page shows 24 clips at a time, and more load
  as you scroll down. Each clip has a "▶ Watch from here" button that opens the full-screen reels at that clip. In
  the reels, a clip's poster loads only when it's near the screen.
- **Colour grade.** Every clip gets more saturation, a little more contrast and slightly deeper midtones
  (`render.GRADE`), since stream footage comes out flat. Campaign clips don't: they keep the official picture.
- **Upload quality.** NVENC p7 (the slowest, best preset) at constant quality 16, with spatial and temporal adaptive
  quantization (more bits on faces, text and edges), lookahead and B-frames. Peaks are capped at 24 Mbps for
  Instagram, and the 1440p YouTube copy at 40.
- **Sync-safe cuts.** Live clips (and clean ones) decode from a little
  before the clip and trim on the exact frame, so picture and sound both start at 0 and the voice stays on the
  lips, even after Instagram or TikTok re-process the upload (`render.cut(..., exact=True)`).
- **Recording keeps voice and picture together.** YouTube sends a live stream's picture and sound as two streams
  that join the live edge seconds apart. The recorder keeps YouTube's own timestamps (`-copyts`, one clock for
  both), then re-times each chunk so both start at 0: the picture is copied untouched, and the sound's late start
  becomes a second or two of silence instead of the voice landing seconds before the lips.
  Live is recorded as sharp as the stream comes, up to 1440p (4K with the 4K box), since the facecam and gameplay
  panels are crops blown up to full width.
- **Sharper clips.** The facecam panel (a small box blown up about 3x) gets a light denoise before upscaling, so the
  stream's compression blocks aren't enlarged. After the lanczos upscale, it gets contrast-adaptive sharpening
  (`CAM_SHARPEN`), and the gameplay and other framings a lighter one (`SHARPEN`, in `render.py`).
  **YouTube gets a 1440p copy** (1440x2560 / 2560x1440, ~28 Mbps), made just before the upload and deleted after.
  YouTube gives 1440p uploads its better VP9/AV1 encode, which looks clearly sharper to viewers even at 1080p.
  Instagram and TikTok get the 1080p clip, since they scale to 1080 anyway.
- **Upload the best of each piece.** On the form (and on the job card while it records): *Upload per piece: best
  [N] shorts [M] long*. Right after each piece is clipped, its N highest-scored shorts and M highest-scored longs
  are uploaded (best first) while the next piece records; the rest are only clipped and wait in **Clips**, where
  you can upload them by hand. Empty = all of that kind, 0 = none; both empty = every clip goes up as soon as it's
  made. Log: `[piece 1] UPLOADING THE BEST 1 of this piece's 5 short(s): #4 "GTA 6 Map Is HUGE" (score 91); the
  other 4 stay in Clips`. Long videos in pieces work the same.
- **At least one clip per piece.** For live pieces the clip counts are a maximum, not a quota, but every piece
  makes **at least one clip**: its single best complete moment, even in a quiet piece.
  - Extra picks scoring below 55 are dropped; the best one is kept anyway and the log says so:
    `[piece 3] DECISION: kept "..." (virality 41 < 55): the best moment of this piece, every piece makes at least one clip`.
  - If the AI returns nothing usable, the piece's strongest hype window (loudness + chat) becomes the clip.
- **Streamer vs game voices.** The transcript mixes the streamer and the game's dialogue. The AI is told to tell
  them apart (the streamer talks to chat, casually, in first person; the game has scripted lines) and to clip the
  **streamer reacting to the game**: the game moment that triggers it, then the surprise / hype / laugh / hot take.
  - A **reaction** signal is added to the graph (purple) in the AI modes: where the streamer says "oh my god", "no
    way", "bro", "what the", shouts or laughs. A reaction that lines up with a chat or audio spike and makes sense
    from the transcript is a strong pick. Log: `[piece 1] streamer reactions: 3 spot(s) with surprise / hype words (2:10, 5:40, 8:20)`.
  - **First looks.** When the title says premiere / reveal / gameplay / first time / launch, new features, the map,
    vehicles and mechanics are clip-worthy even when the streamer is calm, and the first piece's guaranteed clip is
    the first proper look at the game.
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
  DeepSeek picked (score, title, why), what was dropped and why, joins and skips. When a piece is done:
  `[piece 1] FINISHED: 2 clip(s) ready in Clips (2 from this job so far). Next: piece 2 is recording`; when the
  recording ends: `recording finished: waiting for 1 piece(s) still being clipped`, then `ALL 3 PIECE(S) FINISHED: 7
  clip(s) from this stream` (long videos too).
- **Clean starts.** The joined tail always starts on a video keyframe, so clips that begin in the overlap have
  picture and sound from the first frame (see [Live pieces start on a keyframe](#live-pieces-start-on-a-keyframe)).
- **Cleanup.** The recordings and the joined pieces are deleted when the job ends.

Past streams (replays) work like normal videos, and their chat replay (via yt-dlp) is used as a signal too.

**Long videos are clipped like streams.** A video at least `LONG_VIDEO_MINUTES` long (default 60) is downloaded once,
then cut into pieces of the job's piece length (default 10 min) and each piece goes through the same pipeline as a
live piece: its own loudness, transcript, AI pick and renders, **shorts/longs per piece**, at least one clip per piece, the
previous piece's last 60s in front so a moment on a boundary is one clip, two pieces at a time. The video's
most-replayed heatmap, chat replay and comment timestamps are shifted onto each piece's own clock. The log shows each
piece's place in the video (`[piece 4] 29:00-40:00 of the video`). A 3-hour video with 3 shorts per piece can make
up to 54 clips; use **Pieces to clip** to cap it.

**Streamers to watch** (Auto page): see [Autopilot](#autopilot). A watched streamer going live starts exactly this
kind of live job by itself.

## Clipping campaigns (Whop)

Paid clipping campaigns (Whop "Content Rewards", Clipping Culture and similar): a brand pays per view for shorts cut
from **its own official footage**, under strict rules. The **Campaigns** page runs that end to end, with **its own
flow and its own AI prompts**: it is not the viral-clip picker with extra rules bolted on.

### Using it

1. **+ New campaign** → paste the **whole brief** (what to do, caption guidelines, FTC disclosure, required lines,
   on-screen text, editing rules, requirements, not allowed) → **Analyze the brief**. Every setting the brief answers
   is filled in and marked **✓ from the brief** (name, post to, clips per video, longest clip, framing, subtitles,
   on-screen text, the required line on screen). The ones it doesn't answer are highlighted as **? your call**, each
   with its question listed above the form and a sensible default already picked. Answer those, add *Tell the AI* if
   you like, **Create campaign**. (Edit the brief and it has to be analysed again.)
2. The AI turns the brief into **rules** (open *Rules* on the campaign to check or edit them): the tags every caption
   needs (`@callofduty`), the allowed disclosures (`#Ad`, first = used), the required line ("DMZ gets free access for
   a limited time") and where it may go, how many more hashtags are allowed, min/max length, how many days a post must
   stay up, what to clip and which features to show, caption and on-screen examples, what's not allowed, the tone,
   and no music / no AI voice / official footage only. **Save rules** keeps your edits; **Read the brief again** redoes it.
3. **Add the official videos**: links (YouTube, a direct .mp4), **a folder on this PC** (download the content
   folder first: review sites like MediaSilo, Frame.io or Drive usually can't be downloaded from their link) or files.
   Each video becomes a job (it shows on the Jobs page too); campaign videos are clipped one at a time.
4. Every clip appears under its campaign with its **caption** (editable: **Save + check**, **✨ Rewrite** for a new
   one, **📋 Copy**), a **checklist** of the rules (✓/✕), and **⬇ Download**.
5. Post: **↑ TikTok / ↑ Instagram** posts through the API with exactly that caption, or post by hand (download +
   copy the caption) and paste the post link under **Save link**. Posted clips show the date they must stay up until
   (the brief's days, e.g. 30).

Nothing is ever posted by itself. TikTok's API posts **privately** until TikTok audits your app, and campaigns need
public posts with visible likes: post TikTok by hand until then. Instagram's API may reject Reels over
`IG_MAX_SECONDS`; the campaign button tries anyway, and posting by hand always works.

### What the example brief becomes

```
Ghost briefs the squad on X01 @callofduty      ← the AI's caption (true to the clip) + the required tags
DMZ gets free access for a limited time        ← the required line, always in the caption
#Ad #MW4 #DMZ                                  ← the disclosure FIRST, on its own line, then at most 3 more
```

On screen (optional): the AI's short text that matches the picture ("New DMZ location: X01") at the top, the
required line above it if you ticked *Required line on screen too*, and subtitles of what's said. No follow message,
no end card, no logo, no music, no voice-over and no background video, whatever the other settings say.

### How it works (campaign.py)

```
brief ─► C0 RULES   AI (RULES_SYSTEM) reads the brief  +  a regex pass that never misses the must-haves
                    (@tags, #Ad/#Advertisement/#Sponsored, "Use one of these lines:", "Up to N more hashtags",
                    "Minimum video length: N seconds", "stay live for N days", the content-folder link).
                    The regex wins for those, the AI fills the rest (what to clip, focus, examples, tone, not allowed).
video ─► C1 WATCH   ffmpeg scene detection (select='gt(scene,0.3)' on 320px frames) → every shot cut;
                    Whisper → the whole narration with sentence markers. No hype curves: official footage is edited,
                    so its own cuts and sentences are the natural clip edges (loudness means nothing in a trailer).
      ─► C2 PLAN    one AI call, the campaign editor prompt (PLAN_SYSTEM): what kind of video it is, the list of
                    features it shows, one feature per clip (no overlaps, no repeats), each opening on a reveal or a
                    strong line (never a logo or black screen) and closing when the thought / shot sequence closes;
                    for each clip in the same answer: caption, 0-3 hashtags, on-screen text, quality 0-100, reason.
                    No AI? Whole sequences of shots, from the start.
      ─► C3 TRIM    each edge moves to the nearest sentence edge or shot cut (2.5s reach), never through a word,
                    min = the brief's minimum + 2s, max = your "longest clip"; trimmed clips never overlap.
      ─► C4 RENDER  render.cut(..., follow=False, hook_text=<required line or "">, clean=True)
      ─► C5 CAPTION build_caption: text (hashtags/@ stripped) + every @tag, the required line, then
                    "<disclosure> #a #b #c" on the last line. A caption that repeats one already used in the campaign
                    is rewritten (CAPTION_SYSTEM). check() ticks every rule off and stores it on the clip.
```

The check covers: the tags, the disclosure being the first hashtag on its own line after the text, the hashtag
limit, the required line (in the caption, or on screen where the brief allows), the minimum/maximum length, no AI
voice-over, and no outside footage (no split layout). The caption is re-checked on every edit.

What the tool can't judge for you: that a campaign's footage really is the official footage, your page's audience
countries, engagement rate, hidden likes, or posting the same video too many times / on several accounts. Those are
yours to keep, per the brief.

---

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
4. On **Socials → YouTube cookies**, click **Add cookies.txt** and pick the file. It's used for every download,
   search and face check straight away, with no restart.

**Several sets, from any computer.** The upload happens in the browser, so it works from another computer that opens
the app (for example over your tunnel). Each file is *added* as its own set under its file name: name them apart,
e.g. `cookies laptop.txt`, because the same name replaces that set. `cookies.txt` is the main one (gitignored); the
others are kept in `data/cookies/`. The newest is used. When YouTube's bot check blocks one set, downloads switch to
the next by themselves; the card shows which is **in use**, with **Use** and **Remove** for each set.

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
| `campaign.py` | paid clipping campaigns: the brief → rules (AI + regex), the campaign queue, its own flow (shot cuts + transcript → campaign-editor plan → trim → clean render → caption built to the rules → checklist) |
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
   - **Videos (`_vod`):** the video (up to 1440p, or 4K when asked; 8 fragments in parallel), the comments with the
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
   - Framing crop → 1080x1920, then the follow message (red box, wrapped over 2-3 lines; the 5 messages take turns) above the headline (white text with a black outline, no box, so the video shows through: the clip title's own hook in 3-6 words, so the video and its title match; if the AI gives none, the title's first words; long ones get a smaller font to fit), captions (3 words at
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
- **Every prompt is logged.** Each AI call writes a line like `🧠 prompt for picking the clips → gpt-oss:20b (Local
  (Ollama)): "Video title: ..." (12,345 chars, with your instructions) full prompt + reply: /media/logs/prompts/...txt`
  into the job/topic/News run log. In the UI that path is a link, **open the full prompt + reply**: a text file with
  the exact system prompt, the task (transcript, candidates, your instructions) and the AI's JSON reply (which model,
  how long, any fallbacks). Saved in `data/logs/prompts/<day>/`, kept 7 days, deleted by Reset everything.
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
`live={"piece", "boundary"}`, the same switch live pieces use: every piece makes at least one clip (its best
moment), extra picks under 55 are dropped, and a clip may start in the overlap only if the moment continues into the new piece.

### Making every clip complete (no cut mid-thought)

1. **Sentence markers.** The transcript the AI reads has a `[seconds]` marker at **every sentence start** (and at
   least every 5s), so it picks real sentence boundaries instead of guessing between markers.
2. **The prompt's complete-piece rule.** Every clip must give the viewer the whole thing, in three parts: a **HOOK**
   in the first 1-3 seconds that stops the scroll and is clear without context, **the CONTENT** with nothing wasted
   (every few seconds something moves it forward), and **the ENDING** with the payoff delivered and the thought
   closed ("that was the whole thing", not "what happened next?"). It must also **last**: hold attention to the end,
   make sense to someone who finds it weeks later, and be worth a rewatch. A clip may not end mid-thought or
   mid-action, nor start on "and / but / so / he / it" pointing at something earlier. Before answering, the AI
   checks each clip's first line (a hook?), the middle (all worth watching?) and the last line (closed?), and writes
   the actual first and last lines into `hook` / `ending` in its answer (they're in the saved prompt + reply).
   When in doubt: one sentence earlier, one later. The campaign editor's prompt has the same three parts, tuned to
   official footage (the hook is a reveal or striking shot, never a logo; the content shows the feature properly).
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
rendering → rendered ──(manual Upload, or a campaign clip's ↑ / Save link)──────────────────────────→ uploading → uploaded
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
