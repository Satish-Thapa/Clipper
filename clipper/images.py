"""Pictures for posts: real gameplay frames from YouTube videos about the post's subject.

  find(query)       YouTube search -> the top few videos -> a still frame from each (a 1-second piece is downloaded,
                    one frame kept; the video's thumbnail if that fails) -> local JPEGs.
  best(paths, why)  the vision model (Settings → AI → Vision model) picks the frame that fits the post best; without
                    one, the first (most-viewed video).
Google Images isn't used: scraping it breaks Google's terms, and its pictures belong to other people. Gameplay frames
are the same footage the clips use. Results are cached per search for a day (data/imgcache)."""
import hashlib
import json
import random
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from . import config, db
from .config import DATA

CACHE = DATA / "imgcache"
CACHE_HOURS = 24
MIN_S, MAX_S = 45, 60 * 60
FRAME_WAIT = 45  # seconds to wait for the frames; the ones not back by then are skipped  # videos between 45 s and an hour: real footage, not a 10-hour loop


def _frame(entry, out):
    """One still from a video: a 1.2 s piece around 35% in, middle frame kept. Falls back to the thumbnail, saved as
    t<name> (find() uses thumbnails only when no real frame came back: they often carry the creator's text / logos)."""
    from . import download
    vid, dur = entry["id"], entry.get("duration") or 300
    tmp = out.parent / f"{out.stem}_part"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        t = max(5.0, dur * random.uniform(0.3, 0.45))
        download.download_section(f"https://www.youtube.com/watch?v={vid}", tmp / "p.%(ext)s", t, t + 1.2, height=720,
                                  tries=1, cookies=False)
        src = next(tmp.glob("p.*"))
        subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-ss", "0.6", "-i", str(src), "-frames:v", "1",
                        "-q:v", "2", str(out)], check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    except Exception:
        try:  # the thumbnail: always there, but often has the creator's text on it
            r = httpx.get(f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg", timeout=15)
            if not r.is_success:
                r = httpx.get(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", timeout=15)
            if r.is_success and len(r.content) > 5000:
                out = out.with_name("t" + out.name)
                out.write_bytes(r.content)
        except httpx.HTTPError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out if out.exists() and out.stat().st_size > 5000 else None


def find(query, n=4):
    """-> up to n local JPEGs of gameplay frames for `query` (most-viewed videos first). [] if nothing works."""
    from . import download
    query = (query or "").strip()
    if not query:
        return []
    key = hashlib.sha1(query.lower().encode()).hexdigest()[:16]
    folder = CACHE / key
    if folder.exists() and time.time() - folder.stat().st_mtime < CACHE_HOURS * 3600:
        got = sorted(folder.glob("[0-9]*.jpg")) or sorted(folder.glob("t*.jpg"))
        if got:
            return got[:n]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        es = download.search(query, n=12, recent=None, cookies=False)
    except Exception as e:
        db.syslog(f"images: search '{query}' failed: {e!r}")
        return []
    es = [e for e in es if MIN_S <= (e.get("duration") or 0) <= MAX_S and e.get("live_status") not in ("is_live", "is_upcoming")]
    es = sorted(es, key=lambda e: -(e.get("view_count") or 0))[:n]
    import concurrent.futures as cf
    ex = ThreadPoolExecutor(n or 1)
    futs = [ex.submit(_frame, e, folder / f"{k}.jpg") for k, e in enumerate(es)]
    done, _ = cf.wait(futs, timeout=FRAME_WAIT)  # whatever has arrived by then; a stalled download isn't waited for
    ex.shutdown(wait=False)
    got = [f.result() for f in futs if f in done and not f.exception() and f.result()]
    got = [g for g in got if not g.name.startswith("t")] or got  # real frames beat thumbnails
    (folder / "query.json").write_text(json.dumps({"query": query, "videos": [e["id"] for e in es]}))
    return got


PICK_WAIT = 60  # seconds for the vision model's choice; past that, the first frame (most-viewed video) is used


def best(paths, why, scope=""):
    """The vision model's pick of the frame that fits `why` (the post) best; the first without a vision model, or
    when the choice takes longer than PICK_WAIT."""
    if len(paths) <= 1 or not config.VISION_MODEL.strip():
        return paths[0] if paths else None
    import concurrent.futures as cf
    ex = cf.ThreadPoolExecutor(1)
    try:
        return ex.submit(_best, paths, why, scope).result(timeout=PICK_WAIT)
    except Exception as e:
        db.syslog(f"images: choosing a frame took too long or failed ({e!r}), using the first")
        return paths[0]
    finally:
        ex.shutdown(wait=False)


def _best(paths, why, scope=""):
    import base64
    from . import pick
    imgs = []
    for k, p in enumerate(paths):
        small = subprocess.run([config.FFMPEG, "-loglevel", "error", "-nostdin", "-i", str(p), "-vf", "scale=448:-2", "-f",
                                "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-"], capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=30).stdout
        imgs.append((f"picture {k}:", base64.b64encode(small).decode()))
    try:
        r = pick._ask(f"A social media post needs a background picture. The post: {why}\n"
                      + (f"The page's rules (the picture must fit them too, e.g. the right game and era): {scope}\n" if scope else "")
                      + "Which picture fits it best (the right game and scene, clear subject, recognisable, no big text, "
                      "watermarks or logos on it, not blurry)? Reply as JSON: {\"best\": <number>}",
                      "You choose pictures for posts.", "best", config.VISION_MODEL, images=imgs)
        k = int(r if not isinstance(r, list) else r[0])
        return paths[k] if 0 <= k < len(paths) else paths[0]
    except Exception as e:
        db.syslog(f"images: vision pick failed ({e!r}), using the first")
        return paths[0]


TIME_LIMIT = 120  # seconds: a picture that takes longer isn't worth holding the post up for


def picture(query, why, dest, scope=""):
    """find + best, copied to `dest` (a JPEG next to the post). -> dest, or None when no picture could be found in
    TIME_LIMIT seconds (the post then goes ahead as a plain card)."""
    import concurrent.futures as cf
    ex = cf.ThreadPoolExecutor(1)
    try:
        got = ex.submit(find, query).result(timeout=TIME_LIMIT)
    except cf.TimeoutError:
        db.syslog(f"images: '{query}' took over {TIME_LIMIT}s, skipped")
        return None
    finally:
        ex.shutdown(wait=False)
    pick_ = best(got, why, scope)
    if not pick_:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(pick_, dest)
    return dest
