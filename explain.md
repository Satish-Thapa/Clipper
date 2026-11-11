# How Clipper Works (file by file)

This document follows one link from the moment you click the button to the clip appearing on YouTube, in the order the files run.

## 0. Startup: `config.py` → `db.py` → `app.py`

When you run `uvicorn clipper.app:app`, the files load in this order:

1. **`config.py`** reads your `.env` and turns it into Python constants: `DEEPSEEK_API_KEY`, `FFMPEG` (the ffmpeg path), `WHISPER_MODEL`, `LIVE_CHUNK_SECONDS`, and `DATA` (the `data/` folder). Every other file imports its settings from here.
2. **`db.py`** gets `db.init()` called from `app.py`. It creates `data/clipper.db` (SQLite) with two tables:
   - `jobs`: one row per link you submit (url, title, status, log, how many shorts/longs).
   - `clips`: one row per clip (start/end, score, title, file path, YouTube id).

   It also marks any job that was "running" when the server last died as `interrupted`, because those threads no longer exist.
3. **`app.py`** creates the web server. It serves the UI page and the `data/` folder at `/media/...`, so the browser can play the clip files.

## 1. You submit a link: `static/index.html` → `app.py`

**`static/index.html`** is the entire UI: one page with HTML, CSS and JavaScript.

- When you click **Start clipping**, it sends `POST /api/jobs` with `{url, shorts, longs, auto_upload}`.
- Every 2 seconds it calls `GET /api/state`, which returns every job and clip, and redraws the page. That polling is how you see what's happening.

**`app.py` → `create_job`**:

- Checks the input (the URL must start with http, counts must be in range).
- Inserts a row into `jobs`.
- Calls `worker.start(job_id)` and returns right away, so the UI never waits on the heavy work.

## 2. Background thread: `worker.py`

`worker.start()` creates a **stop flag** (`threading.Event`) for the job and starts a **background thread** running `_run()`.

`_run()`:

1. Calls `download.info(url)` to fetch the title and whether the video is live.
2. Sends normal videos to `_vod()` and live streams to `_live()`.
3. Sets the final status to `done`, `stopped` or `failed`.

Every step calls `db.log(job_id, "...")`, which appends a line to the job's `log` column. That's the log you see in the UI.

### 2a. Normal video path: `_vod()` uses `download.py`

Three downloads run at the same time:

| Call in `download.py` | What it gets |
|---|---|
| `download_vod()` | the video file, at most 1080p, saved as `data/jobs/<id>/source.mp4` (via yt-dlp) |
| `info(url, comments=True)` | up to 500 comments, plus the **"most replayed" heatmap** |
| `chat_replay_times()` | the time of every chat message, if it was a past stream (via chat-downloader) |

`comment_times()` then scans the comments with a regex for things like `12:34` and turns them into seconds (754).

All of that is passed to `_process()`.

### 2b. Live path: `_live()` uses `download.py`

1. Starts a `LiveChat` thread that records the clock time of every chat message as it arrives.
2. Loops until the stream ends or you press Stop:
   - `record_live_chunk()` asks yt-dlp for fresh stream URLs every time (they expire), then runs ffmpeg to save **10 minutes** to `live_0000.ts`, `live_0001.ts`, and so on. YouTube serves live video and audio separately, so ffmpeg merges the two.
   - When you press Stop, ffmpeg gets sent `q` so it closes the file cleanly.
   - Each finished chunk goes to `_process()` in **its own thread**, so the next chunk records while this one gets clipped.
3. Chat clock times are converted to "seconds into this chunk" before being passed along.

## 3. `_process()`: the core pipeline, the same for both paths

### Step 1: find the hype moments with `signals.py`

1. `loudness(src)`: ffmpeg's `ebur128` filter measures how loud every second is.
2. `hype_curve()` builds one "hype" score per second of the video, combining:
   - **heatmap** (weight 3), YouTube's own data on which parts get replayed
   - **chat** (weight 2), messages per second, shifted 8 seconds earlier because people type after the moment
   - **comments** (weight 1.5), the timestamps people typed
   - **audio** (weight 1), screaming and hype show up as loud peaks

   Before they're added together, `norm()` rescales each signal to 0–1 so no single one dominates.
3. `top_windows(curve, length, k)` slides a window along the curve and keeps the hottest stretches that don't overlap:
   - Shorts: 40s windows. Long clips: 300s windows.
   - It finds **3× the number of clips you asked for**, so DeepSeek has a choice.
   - Each window gets some padding (10s for shorts, 60s for long) so DeepSeek can adjust the start and end.

These windows are the **candidates**.

### Step 2: transcribe with `transcribe.py`

- `merge()` joins overlapping candidate windows so no audio is transcribed twice.
- `words()` has ffmpeg decode that stretch of audio to raw 16 kHz, and **faster-whisper** returns every word with its exact start and end time.
  - The model is loaded once (`lru_cache`), and a lock lets only one transcription run at a time.
- `text_with_times()` turns those words into text like `[58.5] who's back [63.2] we're gonna...`, so DeepSeek can see the timing.

### Step 3: pick the clips with `pick.py`

`choose()` sends all the candidates (window times, hype score, transcript) to **DeepSeek** with a prompt asking for the best N shorts and M long clips. It returns JSON with start/end, a virality score, title, description, hashtags, and why the clip will do well.

`validate()` never trusts the LLM's numbers:

- Each clip is clamped inside its candidate window.
- Durations are forced into range: shorts 15–59s, long clips 60–600s.
- Garbage entries are dropped and counts are capped.

If DeepSeek fails, `fallback()` uses the raw hype windows with a generic title.

### Step 4: render with `render.py`

Each chosen clip gets a row in `clips` with status `rendering`, then `cut()` runs ffmpeg:

- **Short:** crops the center of the 16:9 frame to 9:16, scales to 1080×1920, and burns in captions. `ass()` builds a subtitle file showing 3 words at a time, in caps, white with a black outline, using Whisper's word timings.
- **Long:** cuts the clip and keeps it 16:9 at 1080p.

The output is `data/jobs/<id>/clips/<clip_id>.mp4`. The row becomes `rendered`, and the UI shows it on its next refresh.

### Step 5: upload with `worker.upload_clip()` → `upload.py`

This runs if auto-upload is on, or when you click **Upload**.

- If YouTube isn't connected, the clip is marked "not uploaded" and nothing else happens.
- Otherwise `upload.upload()` refreshes the saved Google token, adds `#Shorts` to Shorts, and uploads through the **YouTube Data API**: public, Gaming category, with tags.
- The returned video ID is stored, and the UI shows **▶ on YouTube**.

**Connect YouTube** (`/api/youtube/connect` → `upload.connect()`) runs Google sign-in in your browser once and saves the token to `data/youtube_token.json`.

## The whole flow

```
index.html ──POST──▶ app.py ──▶ db (jobs row) ──▶ worker.start → background thread
                                                     │
                     ┌───────────────────────────────┴──────────────┐
                 _vod (download.py)                        _live (download.py)
             video + heatmap + comments + chat      10-min chunks + live chat, loop
                     └───────────────┬──────────────────────────────┘
                                 _process
     signals.py → hype curve → candidate windows
     transcribe.py → word timestamps for the candidates
     pick.py → DeepSeek chooses the clips + writes titles (validated)
     render.py → ffmpeg cut / 9:16 crop / captions → mp4
     upload.py → YouTube (public)
                                     │
index.html ◀──GET /api/state every 2s── db (jobs + clips) ◀── progress written at every step
```

## What's on disk

Everything is under `data/` (gitignored):

- `clipper.db`: the jobs and clips tables.
- `jobs/<id>/`: the downloaded source or live chunks, plus the finished clips.
- `youtube_token.json`: your saved YouTube sign-in.

## Self-checks

Run these to check the core logic still works:

```bash
python -m clipper.signals
python -m clipper.pick
python -m clipper.render
```
