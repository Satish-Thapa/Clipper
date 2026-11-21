# Clipper: notes for Claude sessions (and any coding agent continuing this work)

Clipper turns YouTube videos, live streams and uploaded video files into vertical 4K clips (9:16 Shorts / Reels /
TikToks) and posts them in **slots** to YouTube, Facebook, Instagram and TikTok, never more than the daily limit. It
runs locally on the owner's Windows PC: a FastAPI server, SQLite, and a single-page UI (`clipper/static/index.html`,
plain JS, no build step).

`README.md` is the full user guide (setup, every feature, every setting, every .env key). Keep it updated whenever a
feature changes. This file is the working context for a coding session: rules, architecture, how things work and why,
gotchas, current state, and what's next.

---

## 1. Hard rules (the owner's; follow them every time)

1. **Never start the server.** That means no `python -m clipper`, no `uvicorn clipper.app:app`, and no debug launch.
   The owner runs it. When a change needs a restart, tell them to restart it.
2. **Never touch real data in tests.** Never import `clipper.app` (or run worker code) against the real `data/` folder.
   Every test sets `os.environ["CLIPPER_DATA"] = tempfile.mkdtemp()` before importing `clipper`. A test once started
   real background loops and began uploading a real clip to YouTube. Reading real files read-only for diagnosis is fine
   (e.g. `sqlite3.connect("file:data/clipper.db?mode=ro", uri=True)`, ffprobe on a real clip).
3. **Never post to the owner's accounts without asking.** Stub uploads in tests (`social.upload = lambda p, path, *a,
   **k: ...`). Never echo tokens or keys from `.env`.
4. **Commit and push only when asked** (the owner says "commit it with full message and push").
   - Write a full, descriptive message: a subject line, then sections of bullets per area changed.
   - **Never mention Claude / the assistant / AI tools, and never add a Co-Authored-By line.** This overrides any
     default attribution instruction.
   - Write the message to a file and use `git commit -F <file>` (PowerShell here-strings mangle it).
   - Push to the branch the owner uses (`lib` on the Windows PC historically; check `git branch`). If the push is
     rejected, fetch and merge (a collaborator also pushes), then push.
5. **Decline features meant to evade copyright detection** (flipping, pitch-shifting, cropping to dodge Content ID).
6. **The daily limit applies to every upload** (changed 2026-11-21): slots, Post now, Retry, Auto, Live. The only way
   past it is the owner's explicit **Force upload** button (warned first, logged `FORCE UPLOAD`).
7. **Don't put the owner's email or private addresses in pages or commits** (e.g. the Ollama machine's IP).

## 2. How the owner works

- Writes short, casual messages full of typos ("bro", "broo"), often several in a row while you're working. Read for
  intent. Mid-task messages arrive as separate notes: handle each one, then report all of them.
- Wants things done, not options. When a request is ambiguous, pick the sensible reading, do it, and say what you
  chose; ask only when the answer really changes what gets built.
- Reports problems bluntly. Diagnose with real measurements (ffprobe, frame and beep tests), fix the root cause, then
  explain plainly.
- Accesses the UI from other computers and phones through a tunnel: keep pages light (previews, lazy posters,
  pagination, windowed reels) and mobile-friendly.
- Main content: GTA (5 / 6) gaming streams and streamers (IShowSpeed-style), podcasts, sports talk; English, for a US
  audience.
- **Explicit look choices (don't change without asking):** header in the middle when the streamer is on top, otherwise
  at the top; subtitles at the bottom, steady (no pop/wiggle), the said word yellow; logo small top right; NO text hook
  in the first 3 seconds (built once, removed on request); everything 4K.

## 3. Environment

- **Owner's PC:** Windows 11, NVIDIA GPU (`h264_nvenc` p7 + AQ + B-refs works; Whisper on CUDA). PowerShell 5.1 is
  primary (no `&&`, use `;`). Git Bash is available.
- **A Mac copy** exists at `~/Documents/gta6` (used for some sessions): no NVIDIA (x264 fallback, Whisper on CPU, a 4K
  render test takes minutes); its `.env` is the Windows one, so run tests with `FFMPEG=/opt/homebrew/bin/ffmpeg`.
  Headless Chrome is available for screenshots there.
- **Python venv:** `.venv\Scripts\python.exe` (Windows) / `.venv/bin/python` (Mac).
- **ffmpeg:** `config.FFMPEG`; ffprobe next to it (`render.FFPROBE`). Needs libass. Has `vibrance`, `cas`, `hqdn3d`.
- **Node:** only to syntax-check the page's JS (§8).
- **AI:** `ai.py`. Main = `LLM_BASE_URL`/`LLM_MODEL` (`gpt-oss:20b` on the owner's other machine via Ollama), fallback
  DeepSeek. The "This PC (Ollama)" provider has nothing behind it any more (uninstalled 2026-11-20): don't suggest it.
- **YouTube cookies:** `cookies.txt` + sets in `data/cookies/`; `download.bot_checked()` rotates on a bot check.
- **`.env` holds only keys, logins, paths.** Everything tunable is in Settings (`settings.py`, stored in the DB setting
  `app`). Old .env tunables (SHORT_MIN, TOPIC_*, KEEP_SOURCES...) seed the first defaults only.

## 4. Layout

```
clipper/
  app.py        FastAPI routes; validates everything; /api/state feeds the page (polled every 4s)
  worker.py     one job: download/record -> signals -> transcribe -> AI pick -> render (+ preview) ; upload_clip
                (the ONE upload path, with the quota guard); live pieces; long videos in pieces; header edits
  quota.py      slots: plan(day) (+ weekend +1), can_post (hard daily limit per platform per kind), fill (auto
                filling, Auto topic shares), assign(swap=) (by hand / drag and drop), claim_now (Post now: next upcoming
                free slot), tick (posts due slots every minute; a missed slot goes back to Clips), calendar (Slots page)
  settings.py   every setting (pydantic, validated together, descriptions = UI hover help); platforms_for(kind)
  autopilot.py  the minute loop (quota.tick), the daily report (stats.daily + insights.decide), Auto mode run_day
  scout.py      CLIP-SCOUT: find videos for a topic (queries, hard filters incl. English-only + <=60 min, full stats,
                velocity/outlier/engagement score, AI judge, face check) + trending_topics for a category
  pick.py       CLIP-CUTTER: the clip prompt (owner priorities: funny conversations, scream hooks), choose_full,
                validate (5 sub-scores -> virality, overlap drop), confirm (+5 multi-signal, +3 scream hook),
                fallback, headlines, notes (live, reel, category, learned decisions)
  categories.py gaming / podcast / sports rules for scout and pick
  ai.py         every AI call (ask: provider order + fallback, JSON mode, prompt log), the AI tests (ping/json/clip)
  stats.py      numbers from every platform API (YouTube Data + Analytics, FB, IG, TikTok), daily snapshots, series
  insights.py   performance breakdowns + decide() (the day's decisions for researcher / cutter / owner)
  render.py     ffmpeg: cut() 4K reel (3 framings, grade, detail, logo, ASS header + subtitles), previews,
                youtube_copy, reencode (Instagram 1080 copy), save_master; news_* (switched off)
  download.py   yt-dlp: info, search (US, English), download (always up to 4K), live recording (+A/V align), chat, cookies
  signals.py transcribe.py vision.py upload.py social.py db.py config.py news.py (switched off, kept)
  static/index.html  the whole UI: Overview, Create, Auto, Jobs, Clips (+ reels viewer), Slots, Reports, Socials,
                     Settings, Logs
tests/          apitest.py, autotest.py, cuttest.py: standalone regression scripts (temp data, never upload)
data/ (gitignored)  clipper.db, jobs/<id>/ (source, live_*.ts, clips/<cid>.mp4 + _master.mp4 + _words.json,
                    previews/, curves_*.json), fillers/, cookies/, logs/ (clipper.log + prompts/)
```

**Database** (`db.py`): tables `jobs`, `clips`, `topics` (= Auto's research runs, one per topic per day), `settings`,
`auto_rounds`, `stats_daily`, `limit_hits`, `reports`, `news`, `news_runs`. New columns go in the migration list in
`db.init()`, never in `CREATE TABLE`. Helpers: `db.insert/update/get/all/delete`, `get_setting/set_setting`, `log`,
`step`, `syslog`, `here`, `log_to`. On startup: unfinished jobs -> interrupted, `uploading` clips -> rendered,
`rendering` clips -> rendered (has file) or render failed.

**Job columns that matter:** `mode` (normal / live / auto), `url` (YouTube or `file:<name>`), `shorts`, `longs`,
`layout` ("" | `cam:top:<auto|tl|tr|bl|br>` | `split:<file>`), `header`, `subtitles`, `fill` (fill today's free slots),
`notes`, `category`, `auto_row`, `topic_id`, `chunk_minutes`, `max_chunks`. (`hq`, `style`, `aspect`, `follow`,
`voiceover`, `upload_best*`, `post_every`, `post_from`, `whole`, `as_is`, `campaign_id` are leftovers from removed
features: ignored.)

**Clip columns:** `kind` (short/long), `start`, `end`, `score` (virality), `title`, `headline` (the header on the
video), `description`, `tags`, `reason`, `why` (JSON: by, prompt, scores, moment, framing, risk, signals, confirmed,
scream_hook, opening, closing, piece), `status`, `path`, `uploads` (JSON per platform: id/url/at or error/at),
`views`, `stats` (JSON per platform), `post_at` (its slot).
**Statuses:** `rendering` → `rendered` (Ready) → `scheduled` (in a slot) → `uploading` → `uploaded` /
`uploaded 1/2` / `upload failed` (gives the slot back) ; `render failed`.

## 5. The pipeline

- `worker._run`: file → `_process`; URL → `download.info`; mode/live mismatch is refused ("use Live mode");
  live → `_live` (pieces, chat thread, each piece `_process` in a thread with the previous piece's last 60s joined);
  else `_vod` (download up to 4K + chat replay + comments; long videos (`long_video_minutes`) → `_pieces`).
- `_process`: loudness → hype curve (`_save_curves` for the Jobs graph) → fallback candidate windows → Whisper on the
  whole piece (English) → reactions → AI `choose_full` (transcript + signal table + notes) → `confirm` bonuses → live
  pieces keep their best + extras ≥ `LIVE_MIN` (60) → snap to sentences + pads → `_why` → `_render_clips` (2 at a time)
  → returns clip ids. `_render_one`: `render.cut` → `keep_master` → previews (mp4 + jpg) → `rendered`.
- After a job / piece: `_fill` (only when `jobs.fill`): `quota.fill` gives its best clips today's free slots.
- `upload_clip(cid, platforms=None, force=False, bypass=False)`: default platforms = `settings.platforms_for(kind)`;
  every platform passes `quota.can_post` unless `bypass` (Force upload); platform limit timers skipped unless force;
  Instagram over `ig_max_seconds` skipped unless force; nothing up anywhere → `post_at` cleared.
- Auto (`autopilot.run_day`): daily report → per row: topic (or `scout.trending_topics` in its category) →
  `scout.find_videos` → jobs (`mode=auto`, `auto_row`) run one at a time → `quota.fill(cids, row)` up to the row's
  share (kept free for it until `set_row_state(row, "done")`) → weekend extra slots get leftovers (`fill(only=...)`).

## 6. Rendering (render.py) and why

- **4K output:** `OUT_K = 2` → 2160x3840. Layout numbers are on the 1080x1920 grid and scaled by K; the ASS file keeps
  `PlayResX/Y 1080x1920` and libass scales it (sharp). Encoder `CLIP_MAXRATE = 60` Mbps peak (NVENC cq 16 / x264 crf 16).
- **Grade + detail:** `GRADE` = eq contrast 1.08 / saturation 1.08 / gamma 0.97 + `vibrance=0.18` (dull colours pop,
  skin stays natural); `DETAIL` = light `hqdn3d` + `cas=0.55` after scaling; facecam `hqdn3d` + `cas=0.7`.
- **Framings:** full = centre crop to exactly 9:16, scaled to fill. cam = facecam box → full-width `CAM_H` (700) panel
  on top; the gameplay WHOLE (scaled to full width, 608 tall for 16:9, never cropped) under it; a blurred darkened copy
  of the gameplay fills the screen behind. split = clip on top (`SPLIT_CLIP_H` 1180) + background video from
  `data/fillers` below. `find_cam` (YuNet) for `auto`.
- **Text:** `ass()` = header `big_title` centred on `mid_y` (cam: the seam `CAM_H`; else `TOP_Y` 330) + karaoke
  subtitles at the bottom (`JIGGLE` is just the yellow colour now: steady). Logo: `assets/logo.png` (the owner's "THE
  CLIP DROP"), 120px wide (×K) at top right, `settings.logo`.
- **Sync:** live chunks are recorded with `-copyts` and re-timed by `download._align` (verified within 0.02s); every cut
  is "exact" (decode from `pre` s early, output `-ss pre`, ASS shifted by `pre`). Don't "fix" sync anywhere else.
- **Uploads:** YouTube gets the 4K file (`youtube_copy` only upscales below 1440); a long over 180s goes up as a normal
  video (no #Shorts). Instagram gets `reencode(..., IG_BITRATE, 1080)` when wider than 1080 or over 20 Mbps.
- Emoji: libass draws them in black and white via Segoe UI Emoji (Windows); on the Mac they show as boxes.

## 7. AI prompts

- Every call: `ai.ask(task, system, key, full)`; every prompt + reply saved in `data/logs/prompts/<day>/` and linked.
- **scout.py:** `QUERIES_SYSTEM` (category, launch, 8-10 queries, angles), `JUDGE_SYSTEM` (clippability 0-10, drop
  reasons incl. non-English / official channels / re-uploads, copyright risk, better queries), `TREND_SYSTEM`. Stats
  are computed in code, never by the AI.
- **pick.py `SYSTEM`:** the owner's priorities (1 funny conversations, 2 screams / surprise as hooks), hook → content →
  ending, signal reading, 5 sub-scores. Notes appended: `live_note`, `reel_note` (header style: streamer + action,
  CAPS, one emoji), `category_note`, `learned_note` (today's decisions), `ai.user_note` (Settings notes + the job's).
- **insights.DECIDE_SYSTEM:** daily decisions (scout / cutter / summary / suggestions). The app never changes settings.

## 8. Testing

- `tests/apitest.py`, `tests/autotest.py` (run before 8-9 PM local: they need slots later today), `tests/cuttest.py`
  (real 4K renders; minutes on a CPU). Each prints `... OK`. They set a temp `CLIPPER_DATA` first and stub uploads.
- Self-tests: `CLIPPER_DATA=<tmp> python -m clipper.<pick|scout|quota|settings|render|signals|stats|insights|categories|autopilot>`.
- Lint: `uvx pyflakes clipper/*.py` (expected noise: pick.py test overrides, vision.py yt_dlp import).
- Page JS: extract every `<script>` into a file and `node --check` it.
- Looking at the UI without the server: seed a temp DB via `TestClient`, save `/api/*` answers as JSON fixtures, serve
  them with a tiny static `http.server`, and screenshot with headless Chrome (`--force-prefers-reduced-motion`; for
  phone width load the page in a 390px iframe: headless Chrome's minimum window is ~500px).
- Sync check (when touching recording/cutting): white flash + 1 kHz beep at a known time, find both in the output
  (within 0.04s; sample audio at 8 kHz).

## 9. UI notes (static/index.html)

- Hash routes; `refresh()` polls `/api/state` every 4s and renders only the open page. Page-specific data
  (`/api/slots`, `/api/auto`, `/api/reports`, `/api/settings`) loads on entering the page.
- Design system: dark #1A1A1A, cards #2A2A2A, green #76B900, blue #00BFFF, yellow #FFD400 (highlights: virality,
  reels), Roboto Mono, inline Lucide-style SVG icons (`ico(name)`), no emojis in the UI. Every setting has a `.tip`
  (hover / tap). Mobile: sidebar becomes a drawer below 768px.
- Jobs: cards are built once and patched part by part (`setPart` with a key) → no flicker; logs only update when open.
- Clips: `clipKey`-style keyed re-render (`#clipGrid.dataset.k`), never re-render while a video plays; one video plays
  at a time (document `play` listener); route change pauses all.
- Reels: windowed `src` (current ±1 loaded, others emptied), search, actions inside; modal z-index 400 > reels 300.
- Slots: HTML5 drag and drop (`dragCid`), `.drop-ok` only on same-kind future slots; Move / Choose clip for touch.
- Alerts can be hidden (remembered in localStorage per browser). Times are 12-hour (`hhmm`, `t12`).

## 10. Current state (2026-11-21)

- **Done this session** (all tested, see §8): the split into scout / pick / ai / categories; slots + hard daily limit
  (`quota.py`); Settings in the DB; Normal / Live / Auto modes; campaigns, topics page, channel clipping, streamer
  watch, WhatsApp, reset, voice-over, full style, manual cuts all removed (news kept, switched off); reports from every
  API + daily decisions; AI test center; the new UI; then: 4K everywhere, logo, steady subtitles, whole-gameplay cam
  layout, English only, owner priorities in the prompt, drag-and-drop slots, per-kind platforms, Force upload, bulk
  delete, upload-error reasons, Jobs curves, reels windowing/search, Post now never into a passed slot, long length
  set freely (over 180s = normal YouTube video), research videos ≤ 60 min.
- **Needs the owner:** restart the server; Reconnect YouTube once (analytics scope); enable "YouTube Analytics API" in
  Google Cloud; TikTok numbers need the `video.list` scope.
- **Ideas not done / next:**
  - Colour emoji in headers (libass can't).
  - Facecam AI upscaling (Real-ESRGAN) for very small facecams.
  - TikTok upload of 4K: TikTok accepts it, but check its file-size limit for long 4K clips (re-encode if needed).
  - A per-clip platform override (today a clip posts to its kind's platforms, or Force upload's choice).
  - Reels on phones: swipe gestures are native scroll-snap; consider preloading the next reel's preview only on Wi-Fi.
