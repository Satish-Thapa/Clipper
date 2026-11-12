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
4. **Candidate windows** from the curve (`WINDOW` in worker.py), transcribed with faster-whisper.
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
7. **Upload** (`upload.py`): every clip is ≤180s and vertical, so it is a Short.

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
