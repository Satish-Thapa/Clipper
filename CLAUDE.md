# CLAUDE.md: Clipper

YouTube link (video or live stream) or an uploaded video file → AI picks the best moments → renders vertical
9:16 clips with a headline, captions, logo and optional AI voice-over → auto-uploads to YouTube as Shorts.
Channel brand: **NAH THAT'S AI**. Content is mostly GTA, but the prompts are written for any content.

Read `README.md` (setup, YouTube OAuth) and `explain.md` (file-by-file walkthrough) for more.
Note: parts of `explain.md` are older than the features below, so trust the code when they disagree.

## Run

```bash
uv venv -p 3.12 && uv pip install -e .     # Windows: .venv\Scripts\...
cp .env.example .env                        # fill DEEPSEEK_API_KEY, FFMPEG path
.venv/bin/uvicorn clipper.app:app --port 8000   # http://127.0.0.1:8000
```

- **The user runs the server in their own terminal and watches the logs.** Never start it in the background
  yourself; ask them to restart it after changes.
- Self-checks (no test framework): `python -m clipper.pick`, `clipper.signals`, `clipper.render`,
  `clipper.transcribe`. Each prints `... ok`. Run them after touching those files.
- For end-to-end tests, set `CLIPPER_DATA=<temp dir>` so the real `data/clipper.db` isn't touched.
  Importing `clipper.app` runs `db.init()` (migrations, and it marks unfinished jobs as "interrupted").

## Pipeline (`worker._process`, commented step by step in the code)

1. **Loudness** per second (ffmpeg ebur128, LUFS).
2. **Hype curve** = weighted, normalised sum of heatmap ×3 (YouTube most-replayed), chat ×2, timestamped
   comments ×1.5, audio ×1. Saved to `data/jobs/<id>/curves_<file>.json` for the UI graph.
   The name must NOT start with `source.`, because `glob("source.*")` finds the video.
3. **Clip counts are capped by video length** (`pick.fit_counts`): a 1-min video gives 1 short and 0 long.
4. **Candidate windows** from the curve (`WINDOW` in worker.py), transcribed with faster-whisper
   (CUDA float16 on the NVIDIA GPU via `pip install -e .[gpu]`, else CPU int8 on all cores).
5. **DeepSeek picks** (`pick.py`). Job `mode`:
   - `hype`: chooses among the candidate windows.
   - `ai+hype`: reads the FULL transcript, with the curves as hints it may override.
   - `ai`: full transcript, no curves.
   - `manual`: only builds the curves; the user types cuts in the UI.
   `validate()` clamps everything. AI picks are then snapped to sentence boundaries (`transcribe.snap`),
   because the LLM only sees a time marker every ~5s.
6. **Render** (`render.cut`): a centred `CROP_ASPECT` slice (default 1 = square) on a 1080×1920 black canvas.
   The headline (3-4 words, yellow box) goes in the top bar, captions in the bottom bar, and the logo
   (`clipper/assets/logo.png`) top-right just above the video. Optional voice-over is mixed in with ducking.
   Encodes with `h264_nvenc` (GPU) if a test encode works, else `libx264`; `RENDER_JOBS` (2) clips at once.
   Source is up to 1080p, or up to 4K when the job's "4K quality" box (`hq`) is ticked; 8 chunks download at once. Animated `END_CARD` ("FOLLOW FOR MORE")
   for the last 2.6s, drawn in the ASS file. With `CROP_ASPECT=0.5625` the logo sits under the headline.
7. **Upload** (`social.py` dispatches; `upload.py` is YouTube): to the platforms chosen per job (`jobs.platforms`,
   JSON). YouTube, Facebook Page (Reels ≤90s, else Page video), Instagram Reels, TikTok. Results per platform go to
   `clips.uploads` (JSON); "Upload" again retries only the failed ones. FB/IG/TikTok keys are in `.env` (README).

**Topics** (`topics.py`): a queue. Topic → DeepSeek search queries (`pick.search_queries`) → yt-dlp YouTube search
(this week, widening to month/any) → DeepSeek scores each candidate's clippability (`pick.clippable`; below
`MIN_CLIPPABLE`=6 means viewers need the full video, so it's skipped) → the most-viewed that pass → one normal job each, run one after another
(`worker.run`, blocking). Queued topics survive restarts; already-clipped videos are never picked again.
Then `vision.py` checks ~30 YouTube storyboard frames per candidate with OpenCV YuNet: skipped if a person fills the
screen (`MAX_PERSON`) or the picture is static (`MAX_STATIC`); a small corner facecam passes. Searches use
`SEARCH_REGION` (US). **Stats** (`stats.py`): views per platform into `clips.views`, hourly WhatsApp report via
Meta's WhatsApp Cloud API (`WA_PHONE_NUMBER_ID`/`WA_TOKEN`/`WA_TO`; `WA_TEMPLATE` = approved template,
always used when set, because free text only arrives within 24h of the user messaging the number). CallMeBot was
dropped: its bot was full. YouTube views need the readonly scope (reconnect).
**UI** is one page with hash tabs: #create #topics #jobs #clips #socials #logs.
**Logging**: `db.log(id, msg, table, exc=)` writes the job/topic log, the terminal AND `data/logs/clipper.log`
(rotating, tracebacks when `exc=True`); `db.syslog` for non-job events; `db.step(...)` logs a step with its duration.
**Upload limits: the app never holds uploads back** (user's explicit choice; don't add pausing/retrying). Every
upload is attempted, the platform's own error is stored in `clips.uploads` and shown on the clip card + a banner.
If the error says the limit is exceeded (`social.LIMIT_RE`), `limit_hits` (platform, hit_at) is saved and the UI
counts down 24h from it. Uploads in the last 24h are counted for display only.

## Decisions the user made (don't undo without asking)

- **Two clip kinds only: `short` and `long`.** A "catchy" kind was added and then explicitly removed.
- **Lengths** (in `.env`): short 15-60s, long 60-180s, with a 10s stretch buffer that the AI may use only
  when needed for context. A hard cap of 180s on everything (the YouTube Shorts limit, `SHORT_HARD_MAX`).
  The max is a ceiling, not a target: shorter is fine if the clip is complete and understandable.
- **Everything is vertical 9:16**, including long clips.
- **A short and a long may reuse the same moment**, but only for the very best moments. Clips of the same
  kind must not overlap.
- **Voice-over** (checkbox, off by default): DeepSeek writes 2-5 commentary lines per clip, read by the free
  local TTS, with the original audio ducked under them. Any voice-over failure must still render the clip.
- **LLM is DeepSeek** via the `openai` package with `base_url=https://api.deepseek.com` (not Claude/OpenAI).
- **Style:** minimal code, stdlib/ffmpeg over new dependencies, `ponytail:` comments mark deliberate
  shortcuts. No Pillow: the logo was drawn with ffmpeg `drawtext`.

## Moving to Windows: known gaps (fix these first)

- **Voice-over uses macOS `say`** (`render.tts` / `render.can_speak`). On Windows `can_speak()` is False, so
  voice-over is silently skipped and logged. Port `render.tts` to a Windows TTS, e.g. PowerShell
  `System.Speech.Synthesis.SpeechSynthesizer.SetOutputToWaveFile` (free, built in, no new dependency).
  It must write a WAV and return its duration. `VOICE` in `.env` holds the voice name.
- **ffmpeg needs libass** (for burned-in subtitles) and libfreetype. Use a "full" Windows build
  (e.g. gyan.dev full) and set `FFMPEG=C:\path\to\ffmpeg.exe` in `.env`.
- **Deno** is needed by yt-dlp for YouTube (`winget install deno`).
- **Fonts:** the ASS captions use "Arial Black", which ships with Windows. Regenerating the logo used
  macOS's `Impact.ttf` path; on Windows it's `C:\Windows\Fonts\impact.ttf`.
- **Watch for path issues:** `render.cut` runs ffmpeg with `cwd=out.parent` and a relative `.ass` name, to
  avoid escaping in the `subtitles=` filter. Keep it that way; Windows drive-letter colons break that filter.
- **Not in git** (copy by hand or recreate): `.env`, `client_secret.json` (YouTube OAuth, Google Cloud
  project "Mock-Sathi", user added as a test user), `data/` (db, videos, `youtube_token.json`: reconnect
  YouTube in the UI).

## Open ideas (not built)

- Facecam/subject tracking instead of a centre crop.
- Overlapping live chunks, so moments aren't split at chunk boundaries.
- Custom thumbnails: the API can't set them for Shorts, only the YouTube mobile app can.
- YouTube API audit, so uploads aren't forced private for an unverified project.
