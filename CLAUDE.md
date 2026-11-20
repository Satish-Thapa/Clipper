# Clipper: notes for Claude sessions

Clipper turns YouTube videos, live streams, whole channels and uploaded files into vertical clips (9:16 Shorts / Reels /
TikToks) and posts them to YouTube, Facebook, Instagram and TikTok. It runs locally on the owner's Windows PC. It's a
FastAPI server, SQLite, and a single-page UI (`clipper/static/index.html`, plain JS, no build step).

`README.md` is the full user guide (setup, every feature, every .env key). Keep it updated whenever a feature changes.
This file is the working context for a coding session: rules, architecture, how things work and why, gotchas, and
current state.

---

## 1. Hard rules (the owner's; follow them every time)

1. **Never start the server.** That means no `python -m clipper`, no `uvicorn clipper.app:app`, and no debug launch.
   The owner runs it. When a change needs a restart, tell them to restart it.
2. **Never touch real data in tests.** Never import `clipper.app` (or run worker code) against the real `data/` folder.
   Every test sets `os.environ["CLIPPER_DATA"] = tempfile.mkdtemp()` before importing `clipper` (and a temp
   `YT_COOKIES` for cookie tests). A test once started real background loops and began uploading a real clip to
   YouTube. Reading real files read-only for diagnosis is fine (e.g. `sqlite3.connect("file:data/clipper.db?mode=ro",
   uri=True)`, ffprobe on a real clip).
3. **Never post to the owner's accounts without asking.** Stub uploads in tests (`worker.upload_clip = lambda cid,
   *a, **k: sent.append(cid)`). Never echo tokens or keys from `.env`.
4. **Commit and push only when asked** (the owner says "commit it with full message and push").
   - Write a full, descriptive message: a subject line, then sections of bullets per area changed.
   - **Never mention Claude / the assistant, and never add a Co-Authored-By line.** This overrides any default
     attribution instruction.
   - Write the message to a file and use `git commit -F <file>`. PowerShell here-strings mangle it.
   - Branch is `lib`; push to `origin lib`. If the push is rejected, fetch and merge (a collaborator also pushes), then push.
5. **Decline features meant to evade copyright detection.** That includes flipping, pitch-shifting or cropping to dodge
   Content ID. Reposting features (channel Shorts "as they are", whole videos) are fine to build, with a note that the
   owner needs the rights.
6. **Manual uploads are never held back.** Only the autopilot respects daily / hourly limits.
7. **Don't put the owner's email or private addresses in pages or commits.**

## 2. How the owner works

- Writes short, casual messages full of typos ("bro", "broo"), often several in a row while you're working. Read for
  intent. Mid-task messages arrive as separate notes: handle each one, then report all of them.
- Wants things done, not options. When a request is ambiguous, pick the sensible reading, do it, and say what you chose.
- Reports problems bluntly ("the quality is shit man", "not in sync again"). Diagnose with real measurements (ffprobe,
  frame and beep tests), fix the root cause, then explain plainly.
- Accesses the UI from another computer through a tunnel, so keep pages light (previews, lazy posters, pagination).
- Main content: GTA (5 / 6) gaming streams and streamers (e.g. IShowSpeed-style), clipped into viral Shorts.

## 3. Environment

- **OS:** Windows 11. The repo is `e:\Document\clipper\Clipper`.
- **Python venv:** `.venv\Scripts\python.exe`. Run Python with it, e.g. `& .venv\Scripts\python.exe script.py`.
- **Shells:** PowerShell 5.1 is primary: no `&&`, use `;`, and avoid `2>&1` on native exes. Git Bash is also
  available and handy for heredocs.
- **ffmpeg:** `config.FFMPEG`; ffprobe sits next to it (`render.FFPROBE`). The GPU is NVIDIA:
  - `h264_nvenc` is available, and Whisper runs on CUDA (`faster-whisper`, model `WHISPER_MODEL`).
  - The best NVENC settings (p7, spatial + temporal AQ, B-refs) work on this GPU.
- **Node:** use it only to syntax-check the page's JS (see §8).
- **AI (LLM), via the OpenAI-compatible client in `pick.py`:**
  - Main: `LLM_BASE_URL` / `LLM_MODEL` = `gpt-oss:20b` on the owner's OTHER machine (an Ollama server reached over
    the network).
  - Fallback: DeepSeek (`DEEPSEEK_API_KEY`).
  - The third provider, "This PC (Ollama)" (`PC_LLM_*`), has **nothing behind it any more**. Ollama and its
    `qwen3.5:2b` model were uninstalled from this PC on 2026-11-20 at the owner's request. Don't suggest it.
  - The provider order is set in Settings → AI models (setting `ai_order`).
- **YouTube cookies (bot check, "Sign in to confirm you're not a bot"):**
  - Files: `cookies.txt` (`YT_COOKIES`) plus extra sets in `data/cookies/*.txt`, which can be uploaded from any
    computer.
  - On a bot check, `download.bot_checked()` switches to the next set.
- **`.env` holds every key** (see `.env.example`). `config.py` reads it. A `.env` change needs a server restart; `.py`
  changes hot-reload (`python -m clipper` watches `clipper/*.py`), and the UI is read fresh on each page load.

## 4. Layout

```
clipper/
  app.py        FastAPI routes + request models (NewJob, ChannelRun, ...), /api/state feeds the whole UI
  worker.py     the pipeline: download/record -> signals -> transcribe -> AI pick -> render -> upload/queue
  download.py   everything YouTube-in: info, VOD download, live chunk recording (+ A/V alignment), chat, comments, cookies
  signals.py    loudness (ebur128) + heatmap/chat/comments -> one per-second "hype" curve, candidate windows, reactions
  transcribe.py faster-whisper word timestamps, sentence snapping
  pick.py       all AI prompts and parsing: choose / choose_full, validate, fallback, headlines, notes (live, reel, user)
  render.py     ffmpeg: cut + framing/layouts + ASS subtitles/titles + encoder, previews, YouTube 1440p copy, as-is copy
  upload.py     YouTube Data API upload (OAuth token in data/)
  social.py     Facebook / Instagram / TikTok uploaders, upload status, limit timers
  channels.py   "Clip a whole channel": pick recent videos/Shorts, one job each, in the background
  autopilot.py  daily posting plan, trend research rounds, streamer live-watch, upload slots
  topics.py     topic queue: topic -> search queries -> trending videos -> one job per video
  news.py       News & facts posts (photo/video), and timed_tick() which posts every "timed" clip when due
  campaign.py   paid clipping campaigns (Whop style): their own flow, prompts and caption rules
  insights.py   learns what performs from view counts; stats.py view counts + WhatsApp report
  vision.py     "mostly a person on camera?" check from YouTube storyboards (YuNet face model in assets/)
  reset.py      Reset everything (keeps logins optionally); db.py SQLite + migrations + logs; config.py .env
  static/index.html  the whole UI (pages: Create, Autopilot, Topics, Campaigns, Jobs, Clips, Schedule, Socials, News,
                     Reports, Logs, Settings) + full-screen reels viewer
data/ (gitignored)  clipper.db, jobs/<id>/ (source, live_*.ts, clips/<cid>.mp4, previews/), cookies/, logs/, fillers/
```

**Database** (`db.py`): the tables are `jobs`, `clips`, `topics`, `campaigns`, `settings`, `auto_rounds`, `reports`,
`news`, `news_runs` and `limit_hits`.

- **New columns** are added through the migration list in `db.init()` (`("table", "col TYPE DEFAULT ...")`, wrapped
  in try/except). Add new ones there, never in `CREATE TABLE`.
- **Helpers:** `db.insert(table, **row)`, `db.update(table, id, **cols)`, `db.get`, `db.all(table, where, args)`
  (newest first), `db.get_setting` / `db.set_setting`.
- **Logging:**
  - `db.log(job_id, text)` writes to a job's log.
  - `db.step(job_id, text)` is a context manager that logs "..." and then "done in Ns".
  - `db.syslog(text)` writes to the system log.

**Recent job columns:**
- `style`: "clean" or "full";
- `channel`: the streamer / channel name;
- `whole`: post the whole video as one clip;
- `as_is`: re-post unchanged;
- `post_every`: minutes between queued posts;
- `post_from`: epoch time before which nothing posts;
- `upload_best`, `upload_best_long`;
- `chunk_minutes`, `max_chunks`;
- `layout`, `aspect`, `hq`, `notes`, `follow`, `voiceover`, `subtitles`, `mode`.

**Clip columns** include `kind` (short/long), `start`, `end`, `score`, `title`, `headline`, `description`, `tags`
(JSON), `reason`, `why` (JSON for the "Why this clip?" panel, plus `piece` / `whole` / `as_is`), `status`, `path`,
`uploads` (JSON per platform), `views`, `post_at`, `post_to`.

**Clip statuses:**
- `rendering` / `rendered` / `render failed`
- `uploading` / `uploaded`
- `timed`: posts at `post_at` via `news.timed_tick`, every minute, autopilot on or off.
- `scheduled`: waits for an autopilot slot.
- `paused (streamer live)`

## 5. The pipeline (worker.py)

### Job start (`_run`)
1. `download.info(url)`, then the job's `title`, `is_live` and `channel` are saved.
2. Then one of these branches:
   - `as_is` → `_as_is`
   - live → `_live`
   - otherwise → `_vod`
3. An uploaded file (`url` starting with `file:`) goes straight to `_process`.

### Videos (`_vod`)
1. Downloads at up to `download.height_for(hq)` (1440p by default, 2160p with the "4K" box), plus the chat replay and
   timestamped comments.
2. Long videos (`LONG_VIDEO_MINUTES` and up, not manual, not `whole`) are cut into pieces by `_pieces` and handled
   like live pieces, with the previous piece's last `JOIN_SECONDS` in front.

### Live (`_live`)
1. Records fixed chunks (`chunk_minutes`) with `download.record_live_chunk`.
2. Each chunk is processed in a background thread, with the shorts / longs counts applying **per piece**.
3. Every piece makes at least one clip.
4. Clips below `pick.LIVE_MIN` are dropped.

### `_process(job, src, title, d, heatmap, chat, comments, live)` steps
1. Loudness.
2. Hype curve.
3. Candidate windows.
4. Whisper: on the candidates only in "hype" mode, on the full video in the "ai" / "ai+hype" modes (plus "reactions").
5. AI pick (`pick.choose` / `choose_full`). The AI note is built from `live_note`, `reel_note` (clean style) and
   `user_note` (the owner's "Tell the AI" text, which wins).
6. Snap the picks to sentences, plus padding.
7. Render (`_render_clips` → `_render_one`, two clips at a time).
8. Upload or queue.

Two branches inside `_process`:
- `whole` jobs go to `_whole`, which makes one clip of the whole video, with the AI writing only the header, description
  and tags. The title stays the video's own.
- Manual mode stops after the curves; the cuts come later from `manual_cuts`.

### `_render_one`
- **`clean_style(job)` (live jobs always, or `style == "clean"`)** gives the reel look: `render.cut(..., words, head,
  clean=True, exact=True, reel=True)`. `head = pick.reel_headline(...)` is at most 7 words with the emoji kept. It's
  saved as the clip's `headline`, so the Clips page edits what's on the video. For non-live clips, `keep_master`
  saves `<cid>_master.mp4` + `<cid>_words.json`, so the header can be re-rendered after the source is deleted.
- **Full style** keeps the old look: headline + subtitles + follow message + optional AI voice-over, logo and end card.
- **Re-rendering after a header edit** happens in `_rerender`, which keeps the same style.

### Uploading
- Everything goes through **`post_or_queue(job, cid)`**:
  - With no `post_every` and no future `post_from`, the clip uploads now (`upload_clip`).
  - Otherwise it becomes `timed`, at the next free slot of one global queue (setting `post_queue_next`; guarded by a
    lock, since two renders can finish together).
  - The slot is never before `post_from`, and slots are spaced `post_every` minutes apart.
  - `news.timed_tick` posts it when the time comes.
- **"Upload only the best"** (`_best_limits`, `_upload_best`):
  - Live and long-video pieces: after each piece, its best N shorts / M longs by score go up.
  - Normal videos: once all their clips are made.
  - The others stay in Clips.
- **Topic jobs from the autopilot** get status `scheduled`, and `autopilot.upload_tick` posts them on its slots.
- **Stop:** `STOPS[job_id]` (an Event); `_check(jid)` raises `Stopped` at step boundaries. Sources are deleted after a
  job (`drop_sources`) unless `KEEP_SOURCES=1`.

## 6. Rendering details (render.py) and why they're that way

### Live audio/video sync, the big recurring bug, now fixed at the source
- **The cause:** YouTube HLS sends picture and sound as two streams that join the live edge at different points, and
  ffmpeg zeroes each input's clock separately. That gave a voice/lips offset.
- **The fix in `record_live_chunk`:**
  - `-copyts`, so both streams share one clock;
  - no `-t` (with `-copyts` it writes an empty file), so the chunk is stopped on the wall clock by sending `q`;
  - then `download._align()` re-times the chunk: `-c:v copy` plus `aresample=async=1:first_pts=0`. The audio's late
    start becomes silence.
- **Verified within 0.02s.** Don't "fix" sync anywhere else (aresample in transcribe/signals was tried and reverted).

### Exact cuts (`cut(..., exact=True)`)
- The cut is sync-safe: input `-ss start-pre` and output `-ss pre` (pre ≤ 20s), so both tracks start on the exact frame
  at 0.
- ASS times are shifted by `pre`, because the subtitles filter sees the input timeline.
- No voice-over is allowed with it.
- `save_master` uses the same method.

### Layouts (`layout`)
- `cam:top|bottom:<auto|tl|tr|bl|br|x,y,w,h>` is the facecam stack. The streamer's box is cut out of the frame and
  blown up into a 1080×`CAM_H` (700) panel; the gameplay fills the other 1220px. `find_cam` uses YuNet for "auto".
- `split:<file>` puts the clip on top (`SPLIT_CLIP_H` 1180) and a background video from `data/fillers` below.
- Otherwise `aspect` (0.5625 ... 1.7778) sets a centred slice, padded to 1080×1920.

### Picture quality
- **Facecam:** `hqdn3d` before the upscale, lanczos, then `cas=CAM_SHARPEN` (0.7). Gameplay and other framings get
  `cas=SHARPEN` (0.4).
- **Colour grade:** `GRADE` (`eq` contrast 1.07, saturation 1.25, gamma 0.97) is applied to `[0:v]` for every clip
  except campaign clips (`clean and not reel`), which must keep the official picture.
- **`encoder(maxrate)`:**
  - NVENC p7, `-cq 16`, spatial and temporal AQ, lookahead 32, B-frames with `b_ref_mode middle`.
  - It falls back to plain p7, then x264 `slow` at CRF 16.
  - It's cached with `lru_cache`.
  - Clips peak at 24 Mbps, because Instagram rejects Reels over 25 Mbps; `social.py` re-encodes as a safety net.
- **`youtube_copy(path)`:**
  - Before a YouTube upload, the clip is upscaled to 1440×2560 (`encoder(40)`).
  - YouTube gives 1440p+ uploads the better VP9/AV1 encode, so it looks clearly sharper.
  - The copy is deleted after the upload; if making it fails, the 1080p clip is uploaded.
- **`as_is_copy`:** H.264 is copied untouched; VP9/AV1 is re-encoded once at top quality, because Instagram and TikTok
  want H.264.

### Reel look (`reel=True`)
- **Header:** `ass(mid_y=...)` → `big_title()`.
  - It's big (up to `BIG_TITLE` = 110px), white caps with a thick outline, centred on the **middle**: with a facecam,
    the seam between streamer and gameplay (`CAM_H` or `1920-CAM_H`); without one, y = 960.
  - It uses 1-3 lines, split where the longest line is shortest (`_rows`; `_width` counts an emoji as 2 characters).
  - It pops in, then gently pulses.
- **Subtitles:** at the **bottom**, karaoke style. The word being said is yellow (`&H00E5FF&`, BGR) and pops / wiggles
  (`JIGGLE` / `UNJIGGLE`). With the facecam at the bottom they sit higher (`cap_margin = CAM_H + 300`).
- **Emoji:** `_emoji()` switches the font to Segoe UI Emoji. libass draws them in black and white, since it has no
  colour emoji.
- **The owner's explicit choices: title in the middle, subtitles at the bottom, NO 3-second hook at the start.** The
  hook was built and then removed on request. Don't re-add it.

### Full-style text
- `ass()` handles the follow line, headline (`top_block` / `seam_block`), captions (`Cap` style), end card and logo.
- `follow_line()` cycles through `FOLLOW_LINES`, or uses `FOLLOW_LINE` from .env.

### Previews for the UI
- `preview()` makes 540-wide, 1.2 Mbps copies plus jpg posters, cached by mtime.
- They're served at `/api/clips/{id}/preview` and `/poster` (`Cache-Control: private, max-age=86400`, and `?v=` busts
  it).
- Download / Open use the full file at `/media/...`.

## 7. AI prompts (pick.py)

- **`SYSTEM` / `FULL_TASK` / `pick_text(counts)`:**
  - The model gets the video's title and transcript (with `[sec]` markers), plus the signals.
  - It returns `{"clips": [{candidate, kind, start, end, virality, title, headline, description, hashtags, hook,
    ending, reason}]}`.
  - Its numbers are clamped by `validate()`.
- **`KIND_DESC`:** a LONG in a game video can be a full "what you can do in the game" showcase ("You Can Take Dates In
  GTA 6"); the owner asked for this.
- **`reel_note(streamer)`:** the header is the streamer's name plus what they're doing, a bit funny, 4-7 words in caps,
  ending with ONE emoji from `REEL_EMOJI` ("SPEED THINKS ANIMALS ARE HUMAN 😭").
- **`reel_headline(text, title, n=7)`** and **`short_headline(text, n)`** cut cleanly at punctuation and drop weak
  trailing words (`WEAK_END`). `headline_for()` falls back to the title.
- **`live_note`** is for live / long-video pieces. **`user_note`** is the owner's instructions, which win over the
  general guidance.
- **Channel runs** append a topic note: every clip stays on that video's topic.
- **Every prompt is logged** under `data/logs/prompts/` and linked from the job log (`pick.LAST.link`).
- **`pick.fallback`** uses the raw hype windows when the AI fails.
- **`fit_counts`** caps the requested counts by video length.

## 8. Testing (how it's done here)

There's no test suite in the repo. The tests are standalone scripts in the session scratchpad. The scratchpad is
temporary, so rewrite them as needed. The pattern:

```python
import os, sys, tempfile, time, threading, subprocess, json
os.environ["CLIPPER_DATA"] = tempfile.mkdtemp()          # ALWAYS first
sys.path.insert(0, r"e:\Document\clipper\Clipper")
from clipper import db, worker, pick, transcribe, render, channels
from clipper.config import FFMPEG, DATA
db.init()
# synthetic source:
subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=120",
                "-f", "lavfi", "-i", "sine=d=120", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
transcribe.words = lambda f, s, e: [w for w in W if s <= w["s"] < e]      # fake words
pick._ask = lambda task, system=None, key=None, full=False: {"clips": [...]}  # fake AI (capture `task` to check prompts)
worker.upload_clip = lambda cid, *a, **k: sent.append(cid)               # never upload
worker.STOPS[jid] = threading.Event()
worker._process(db.get("jobs", jid), src, "title", d)
```

### Useful checks
- **Sync:** put a white flash and a 1 kHz beep at a known time in the source, then find both in the output.
  - Frames: `crop` + `scale=16:16,format=gray` as rawvideo; the mean brightness above 200 marks the flash.
  - Audio: s16le at 8 kHz, an 80-sample moving average of |x|; above 1000 marks the beep.
  - They should agree within 0.04s. Don't sample audio at 1 kHz: it aliases the 1 kHz beep away.
- **Looks:** extract a frame with `-ss T -frames:v 1 -vf scale=540:960 out.png` into the scratchpad and Read the PNG
  to look at it.
- **API:** use `fastapi.testclient.TestClient(app.app)` with `worker.start = lambda jid: None`, on temp data only.
- **Page JS:** extract every `<script>` from index.html into `page.js` and run `node --check page.js`. To test a UI
  function's logic, slice it out of page.js and `eval` it in Node with stubs (`$`, `esc`, `PLATFORMS`, ...).
- **Pure checks:** `python -m clipper.pick` runs pick.py's self-tests.

### The regression scripts used last
- Each prints `... OK`:
  - `vodclean.py`: clean style, best-of upload, re-render, full style;
  - `livepiece.py`;
  - `uptest.py`: upload per piece;
  - `edittest.py`;
  - `camptest.py`;
  - `qualtest.py`;
  - `reeltest.py`: sync, title and subtitle positions;
  - `chantest.py`: channel plan, Shorts plan, whole video, as-is Short, app routes;
  - `queuetest.py`;
  - `createqueue.py`;
  - `previewtest.py`;
  - `cookietest.py`.
- Recreate the relevant one when you change that area. Render tests take minutes, so run long suites in the background.

## 9. UI notes (static/index.html)

- **Polling:** `refresh()` polls `/api/state` and calls every page's render function. Each render keeps a key
  (`clipKey`, `schedKey`, `campKey`...) to skip re-rendering when nothing changed. Reset the key (`clipKey = ""`) after
  an action.
- **Clips page:**
  - Infinite scroll: `clipShown` / `CLIP_PAGE` = 24, with an IntersectionObserver on `#clipMore`.
  - "▶ Watch from here" calls `openReels(id)`; if the reels filter hides that clip, it switches to "all".
  - Edit and schedule forms open inline.
- **Reels viewer (`#reels`):** full screen with scroll-snap. Posters load lazily (`data-poster`, `reelNear` observer);
  the next reel preloads.
- **Schedule page (`renderSchedule`):** timed clips grouped by day ("Today" / "Tomorrow" / date) in time order, with a
  countdown and thumbnail. Each has ▶ Watch / Change time (`schedForm`) / ↑ Post now (`postNow`: unschedule, then
  upload) / Don't post. Autopilot-waiting clips come after.
- **Create page:**
  - Clip style select: "Clean, like live" (default) or "Full". The `.full-only` boxes are disabled when clean.
  - Upload only the best N shorts / M long.
  - Upload one clip every N min (`post_every`) and Start posting at (`post_from`).
  - Live-stream options.
  - "Tell the AI" notes.
- **"Clip a whole channel" box (`#chf` → `/api/channel`):**
  - Fields: channel link, Recent videos, clips per video (shorts / long), upload only the best, Recent Shorts (posted
    as they are), clip videos longer than X min (shorter ones are posted whole), upload every N min, start posting at,
    and its own Upload-to platforms.
  - Style and framing still come from the main form.
  - Picking: the N newest uploads and past streams (tabs taken in turn), sorted by views, most viewed first. Shorts
    are picked the same way.
- **Gotcha:** `form.style` is the CSS object. Use `form.elements.style`.

## 10. Current state (2026-11-20)

- **Last commits on `lib`:** `698d158` (big middle title, channel by views, Shorts as they are) and `3a92b3e` (reel
  look, channel clipping, upload queue, quality).
- **Uncommitted, built and tested, waiting for the owner to say "commit":**
  - the "Upload one clip every N min" and "Start posting at" fields on the Create clips form (`NewJob.post_every` /
    `post_from`, file jobs too);
  - the new **Schedule** page.

  README is updated for both.
- **Known limits / ideas not done:**
  - Colour emoji (libass can't draw them).
  - AI upscaling of the facecam (Real-ESRGAN) was offered and not built.
  - A smaller facecam panel (`CAM_H`) would be sharper; it's the owner's call.
  - All channel runs and Create jobs share one post queue.
