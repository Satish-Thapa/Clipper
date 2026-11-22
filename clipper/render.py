import os
import re
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

from . import db
from .config import FFMPEG, FOLLOW_LINE, FOLLOW_LINES, LOGO, VOICE  # the last four: news posts only (switched off)

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Cap,Arial Black,86,&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,{margin}
Style: Top,Arial Black,78,&H00000000,&H00FFFFFF,&H00FFFFFF,1,3,14,0,8,120

[Events]
Format: Layer, Start, End, Style, Text
"""


def _t(sec):
    h, r = divmod(max(sec, 0), 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def follow_line():
    """The message above the headline: FOLLOW_LINE from .env, else the next of FOLLOW_LINES (in turn, remembered in
    the database so it carries on after a restart)."""
    if FOLLOW_LINE:
        return _clean_hook(FOLLOW_LINE)
    try:
        i = int(db.get_setting("follow_idx") or 0)
        db.set_setting("follow_idx", str(i + 1))
    except Exception:  # no database (e.g. a self-test): the first message
        i = 0
    return _clean_hook(FOLLOW_LINES[i % len(FOLLOW_LINES)])


def _clean_hook(t):
    return " ".join(str(t).replace("{", "(").replace("}", ")").replace("\\", "/").split())


def hook_lines(text, width=44):
    """The follow message wrapped for its box (2-3 short lines read better than one long one)."""
    import textwrap
    return textwrap.wrap(text, width)[:3] if text else []


YELLOW = r"{\c&H00E5FF&}"  # ASS colours are BGR: this is the headline yellow
EMOJI_FONT = "Segoe UI Emoji"
# The word being said: yellow, pops up bigger and wiggles as it's pronounced, then settles (the rest of the line stays still).
# The word being said: yellow, steady (no pop or wiggle: the owner wants the subtitles still); the rest stays white.
JIGGLE = r"{\c&H00E5FF&}"
UNJIGGLE = r"{\c&HFFFFFF&}"


def _emoji(text):
    """Emoji in the font that has them (Arial Black doesn't), then back."""
    return re.sub(r"([\U0001F000-\U0001FAFF☀-➿]+)", rf"{{\\fn{EMOJI_FONT}}}\1{{\\fnArial Black}}", text)


BIG_TITLE, BIG_CHARS = 84, 21  # reel title: font px (on the 1080 grid), and characters per line that fit ~950px at that size
# (measured: a line of Arial Black capitals + spaces is ~0.5 of the font size wide per character)


def _width(text):
    """Line width in characters (an emoji is about two wide)."""
    return len(text) + len(re.findall(r"[\U0001F000-\U0001FAFF☀-➿]", text))


def _rows(words, n):
    """words -> n lines, split where the longest line is shortest (every split point tried; titles are short)."""
    from itertools import combinations
    n = min(n, len(words))
    best = None
    for cuts in combinations(range(1, len(words)), n - 1):
        b = (0, *cuts, len(words))
        rows = [" ".join(words[b[i]:b[i + 1]]) for i in range(n)]
        if best is None or max(map(_width, rows)) < max(map(_width, best)):
            best = rows
    return best or [" ".join(words)]


BIG_MIN = 40  # the smallest a one-line title gets (on the 1080 grid): ~50 characters still fit across


def big_title(headline, y, length):
    """The reel title as ASS: black caps on a white box (the owner's look), always ONE line (the owner: a 2-3 line
    box blocks too much of the video): the font is as big as fits (84px max), smaller for a longer title; words that
    still don't fit at BIG_MIN are dropped from the end. Centred on y; steady (no animation)."""
    words = headline.upper().replace("{", "").replace("}", "").replace("\\", "/").split()
    fit = int(BIG_CHARS * BIG_TITLE / BIG_MIN)  # characters that fit across at the smallest size
    while len(words) > 1 and _width(" ".join(words)) > fit:
        words = words[:-1]
    rows = [" ".join(words)]
    size = max(BIG_MIN, min(BIG_TITLE, int(BIG_TITLE * BIG_CHARS / max(1, _width(rows[0])))))
    # BorderStyle 3 (the Top style): each line gets an opaque box in the outline colour (white), \bord = its padding.
    # Still: no pop-in, no pulse (the owner wants the title steady, like the subtitles).
    tags = rf"{{\an5\q2\pos(540,{y})\fs{size}\bord14\shad0\1c&H000000&\3c&HFFFFFF&\4c&HFFFFFF&}}"
    return tags + r"\N".join(_emoji(r) for r in rows)


def _caption_lines(ws, start, per_line, karaoke):
    out = []
    for i in range(0, len(ws), per_line):
        g = ws[i:i + per_line]
        said = [w["w"].upper().replace("{", "").replace("}", "") for w in g]
        if not karaoke:
            out.append(f"Dialogue: 0,{_t(g[0]['s'] - start)},{_t(g[-1]['e'] - start)},Cap,{' '.join(said)}")
            continue
        # the word being said in yellow, the rest of the line white (one event per word, back to back)
        for k, w in enumerate(g):
            a = g[0]["s"] if k == 0 else w["s"]
            b = g[k + 1]["s"] if k + 1 < len(g) else g[-1]["e"]
            text = " ".join(f"{JIGGLE}{x}{UNJIGGLE}" if j == k else x for j, x in enumerate(said))
            out.append(f"Dialogue: 0,{_t(a - start)},{_t(max(b, a + 0.05) - start)},Cap,{text}")
    return out


def ass(words, start, end, headline="", mid_y=960, cap_margin=None, shift=0.0, per_line=3):
    """The reel's text as ASS (1080x1920): the header big across mid_y for the whole clip (the facecam / split seam,
    or the middle of the screen), word-by-word subtitles at the bottom with the word being said in yellow.
    shift: every line this many seconds later (an exact cut decodes from `shift` s before the clip starts)."""
    ws = [w for w in words if start <= w["s"] < end]
    lines = _caption_lines(ws, start, per_line, True)
    if headline:
        lines.insert(0, f"Dialogue: 2,{_t(0)},{_t(end - start)},Top,{big_title(headline, mid_y, end - start)}")
    if shift:
        lines = [re.sub(r"^(Dialogue: \d+,)([\d:.]+),([\d:.]+),",
                        lambda m: f"{m[1]}{_t(_secs(m[2]) + shift)},{_t(_secs(m[3]) + shift)},", ln) for ln in lines]
    return ASS_HEAD.format(margin=cap_margin or 250) + "\n".join(lines) + "\n"


# Windows built-in SAPI voice via PowerShell. Text and paths go in as env vars, so quotes can't break the script.
SAPI_PS = """Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($env:TTS_VOICE) { $s.SelectVoice($env:TTS_VOICE) }
$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo 22050, ([System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen), ([System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($env:TTS_OUT, $f); $s.Speak($env:TTS_TEXT); $s.Dispose()"""


def can_speak():
    return shutil.which("say") is not None or shutil.which("powershell") is not None  # macOS say / Windows SAPI


def tts(text, out):
    """Speak `text` (macOS `say`, else Windows SAPI) into a 22kHz mono WAV; returns its length in seconds."""
    if shutil.which("say"):
        subprocess.run(["say", *(["-v", VOICE] if VOICE else []), "-o", str(out), "--data-format=LEI16@22050", text],
                       check=True)
    else:
        env = {**os.environ, "TTS_TEXT": text, "TTS_OUT": str(out), "TTS_VOICE": VOICE}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SAPI_PS], env=env, check=True,
                       capture_output=True)
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


CLIP_MAXRATE = 60  # Mbps peak for the 4K clips (YouTube recommends 35-68 for 2160p); Instagram gets its own copy


@lru_cache(maxsize=4)
def encoder(maxrate=CLIP_MAXRATE):
    """NVIDIA GPU encoder (NVENC) if a 1-frame test encode works, else x264 on the CPU. Upload quality: constant
    quality 16 (visually lossless for a phone), the slowest / best preset, adaptive quantization (more bits on faces,
    text and edges, fewer on flat areas), lookahead and B-frames; `maxrate` (Mbps) only caps the peaks."""
    cap = ["-maxrate", f"{maxrate}M", "-bufsize", f"{maxrate * 2}M", "-profile:v", "high"]
    best = ["-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "16", "-b:v", "0",
            "-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "8", "-rc-lookahead", "32", "-bf", "3",
            "-b_ref_mode", "middle", *cap]
    plain = ["-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "16", "-b:v", "0", *cap]
    for args, what in ((best, "h264_nvenc, best quality"), (plain, "h264_nvenc")):  # older GPUs lack temporal AQ / B-refs
        test = [FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:d=0.3:r=60", *args,
                "-pix_fmt", "yuv420p", "-f", "null", "-"]
        try:
            if subprocess.run(test, capture_output=True, timeout=30).returncode == 0:
                db.syslog(f"render: GPU encoder ({what}, peaks {maxrate} Mbps)")
                return args
        except (OSError, subprocess.SubprocessError):
            pass
    db.syslog("render: CPU encoder (libx264)")
    return ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-aq-mode", "3", *cap]


def save_master(src, out, start, end):
    """The clip's stretch of the source as it was (original framing and sound, no text): what a clip is rendered
    again from when you change its headline after the source video was deleted. Small: one clip's worth."""
    # Sync: jump to a point PRE seconds early (fast), then decode up to the exact start and cut there. Seeking straight
    # to `start` in a stream recording (.ts) lets the picture begin only at the next keyframe while the sound begins
    # at once, i.e. the video track starts later than the audio; players hide that, but apps that re-process the
    # upload (Instagram, TikTok) can drop the gap and the voice no longer matches the picture. This way both tracks
    # start together at 0, and aresample keeps the sound locked to the picture over gaps in the stream.
    pre = min(start, 20.0)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start - pre:.3f}", "-i", str(Path(src).resolve()),
                    "-ss", f"{pre:.3f}", "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a?",
                    "-af", "aresample=async=1:first_pts=0", *encoder(), "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart", str(Path(out).resolve())], check=True)


FFPROBE = (str(Path(FFMPEG).with_name("ffprobe" + Path(FFMPEG).suffix)) if Path(FFMPEG).parent != Path(".")
           else "ffprobe")


def bitrate_mbps(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=bit_rate", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) / 1e6 if out.isdigit() else 0


def reencode(src, out, bitrate, width=None):
    """Same video at a lower bitrate (Instagram: 25 Mbps cap) and, with `width`, smaller (Instagram: 1080 wide at
    most); audio copied."""
    vf = ["-vf", f"scale={width}:-2:flags=lanczos"] if width else []
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), *vf, *encoder()[:2], "-b:v", bitrate,
                    "-maxrate", bitrate, "-bufsize", "28M", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
                    str(out)], check=True, capture_output=True)
    return out


def width_of(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


PREVIEW_W, PREVIEW_RATE = 540, "1200k"  # light copy for watching in the app (over a tunnel: ~1/10 the data)
_preview_locks, _preview_slots = {}, __import__("threading").Semaphore(2)  # at most 2 preview encodes at once


def preview(clip, out_dir, name, poster=False, hd=False):
    """For the app's players: a light copy of a clip (540 wide, ~1.2 Mbps, starts playing at once; made when it's
    first played), with hd=True a 1080-wide copy (~5 Mbps, the reels' HD choice), or with poster=True one small still
    (1s in, so the title shows). Made once, remade only when the clip changes (e.g. a new headline). Downloads /
    uploads always use the full-quality clip. -> the file."""
    clip, out = Path(clip), Path(out_dir) / f"{name}{'_hd' if hd and not poster else ''}.{'jpg' if poster else 'mp4'}"
    with _preview_locks.setdefault(str(out), __import__("threading").Lock()):
        if out.exists() and out.stat().st_mtime >= clip.stat().st_mtime:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + "_tmp" + out.suffix)
        if poster:
            cmd = ["-ss", f"{min(1.0, _duration(clip) / 2):.2f}", "-i", str(clip), "-frames:v", "1",
                   "-vf", f"scale={PREVIEW_W}:-2", "-q:v", "5"]
        else:
            w, rate, peak = (1080, "5M", "7M") if hd else (PREVIEW_W, PREVIEW_RATE, "1800k")
            cmd = ["-i", str(clip), "-vf", f"scale={w}:-2", *encoder()[:2], "-b:v", rate,
                   "-maxrate", peak, "-bufsize", "14M" if hd else "3600k", "-pix_fmt", "yuv420p", "-c:a", "aac",
                   "-b:a", "128k" if hd else "96k", "-movflags", "+faststart"]
        with _preview_slots:
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", *cmd, str(tmp)], check=True, capture_output=True)
        tmp.replace(out)
        return out


def _codec(path, stream):
    return subprocess.run([FFPROBE, "-v", "error", "-select_streams", f"{stream}:0", "-show_entries", "stream=codec_name",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()


def youtube_copy(path):
    """The copy that goes to YouTube: the clip upscaled to 1440p (1440x2560 vertical / 2560x1440 wide). YouTube gives
    1440p+ uploads its better encoding (VP9/AV1, higher bitrate), so they look clearly sharper to viewers even when
    watched at 1080p; 1080p uploads get the weaker AVC encode. Instagram / TikTok scale to 1080 anyway, so they get the
    clip as it is. -> the copy's path (the caller deletes it after the upload), or `path` if it's already that big."""
    path = Path(path)
    wh = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip().split(",")
    w, h = int(wh[0]), int(wh[1])
    if min(w, h) >= 1440:
        return path
    size = "1440:2560" if h > w else "2560:1440"
    out = path.with_name(path.stem + "_yt1440.mp4")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(path),
                    "-vf", f"scale={size}:force_original_aspect_ratio=decrease:flags=lanczos,pad={size}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    *encoder(40),  # 1440p60: YouTube recommends ~24 Mbps; quality-based, peaks up to 40
                    "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)],
                   check=True, capture_output=True)
    return out


def encoder_name():
    return "GPU NVENC" if "h264_nvenc" in encoder() else "CPU x264"


CAM_SHARPEN = 0.7  # contrast-adaptive sharpening of the facecam panel (a small box blown up a lot)
GAME_H = 860  # streamer on top: the gameplay panel, zoomed a bit (keeps ~70% of a 16:9 frame's width) to fill more
CAM_H = 700  # facecam layouts: the streamer's panel (~36% of 1920, full width); the gameplay gets the other 1220px
CAM_CORNERS = {"tl": (0, 0, .3, .3), "tr": (.7, 0, .3, .3), "bl": (0, .7, .3, .3), "br": (.7, .7, .3, .3)}  # x, y, w, h


def cam_layout(layout):
    """'cam:<top|bottom>:<auto|tl|tr|bl|br|x,y,w,h>' -> (position, box or "auto"), else None.
    The box is a share of the source frame (0-1): where the streamer's facecam is."""
    parts = str(layout or "").split(":")
    if len(parts) != 3 or parts[0] != "cam" or parts[1] not in ("top", "bottom"):
        return None
    where = parts[2]
    if where == "auto":
        return parts[1], "auto"
    if where in CAM_CORNERS:
        return parts[1], CAM_CORNERS[where]
    try:
        x, y, w, h = (min(max(float(v), 0.0), 1.0) for v in where.split(","))
    except ValueError:
        return None
    w, h = min(w, 1 - x), min(h, 1 - y)
    return (parts[1], (x, y, w, h)) if w > 0.02 and h > 0.02 else None


def find_cam(src, start, end, samples=6, face_at=0.45):
    """Face detection for the facecam: YuNet on a few frames of the clip -> the streamer's (x, y, w, h) box as a
    share of the frame, or None. Takes the face seen in the most frames (a facecam doesn't move; faces in the game
    do), then frames it with some room around the head at the panel's shape."""
    import cv2
    import numpy as np
    from .vision import MODEL
    cap = cv2.VideoCapture(str(src))
    W, H = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    if not W or not H:
        return None
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (int(W), int(H)), 0.7)
    seen = []  # face boxes (x, y, w, h) in pixels, from every sampled frame
    for t in np.linspace(start + 0.5, max(start + 0.5, end - 0.5), samples):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, img = cap.read()
        if not ok:
            continue
        _, faces = det.detect(img)
        seen += [tuple(f[:4]) for f in faces] if faces is not None else []
    cap.release()
    if not seen:
        return None

    def near(a, b):  # same spot (centres within one face width)
        return abs((a[0] + a[2] / 2) - (b[0] + b[2] / 2)) < a[2] and abs((a[1] + a[3] / 2) - (b[1] + b[3] / 2)) < a[3]
    best = max(seen, key=lambda f: sum(near(f, g) for g in seen))
    group = [g for g in seen if near(best, g)]
    if len(group) < 2:  # a face in one frame only: something in the game, not a facecam
        return None
    fx, fy, fw, fh = (float(np.median([g[i] for g in group])) for i in range(4))
    ph = min(fh * 2.8, H)  # head and shoulders, tight enough to stay inside the stream's own facecam box
    pw = min(ph * 4 / 3, W)  # a webcam's 4:3 shape (wider would pull in the game next to it)
    x = min(max(fx + fw / 2 - pw / 2, 0), W - pw)
    y = min(max(fy + fh / 2 - ph * face_at, 0), H - ph)  # face_at: how far down the box the face sits
    return x / W, y / H, pw / W, ph / H


SPLIT_CLIP_H = 1180  # split layout: the main clip fills the top 1080x1180; your background video the bottom 1080x740


def filler_path(layout):
    """'split:<name>' -> data/fillers/<name> if it exists, else None (normal layout)."""
    if not layout or not str(layout).startswith("split:"):
        return None
    from .config import DATA
    p = DATA / "fillers" / str(layout)[6:]
    return p if p.is_file() else None


def _duration(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


# Colour grade for a richer, "higher quality" look: stream footage comes out flat and washed out. More saturation
# (colours pop), a little more contrast, slightly deeper midtones. Applied before the scaling and sharpening.
GRADE = "eq=contrast=1.08:saturation=1.08:gamma=0.97,vibrance=intensity=0.18"  # vibrance: dull colours pop, skin stays natural
# After scaling: a light denoise (stream compression noise looks soft and smeary when blown up) then contrast-adaptive
# sharpening: crisp edges and text without halos. This is the "looks HD" pass.
DETAIL = "hqdn3d=1:1:2:2,cas=0.55"
OUT_K = 2  # clips render at 2x 1080x1920 = 2160x3840 (4K vertical); every layout number below is on 1080x1920
TOP_Y = 330  # full / split: the header's centre line, near the top (clear of the phone apps' top icons)
LAYOUTS = ("full", "cam", "split")  # the three framings (Settings → layout); every one fills a 1080x1920 phone screen


def layout_name(layout):
    """'' / 'full' -> full, 'cam:...' -> cam, 'split:...' -> split."""
    return "cam" if str(layout).startswith("cam:") else "split" if str(layout).startswith("split:") else "full"


BRAND_LOGO = Path(__file__).parent / "assets" / "logo.png"  # your logo, burned small into the top-right corner
LOGO_W, LOGO_MARGIN, LOGO_TOP = 120, 32, 60  # px on the 1080x1920 frame (under the phone's status bar)


def settings_logo():
    from . import settings
    try:
        return settings.get().logo
    except Exception:  # no database (a self-test)
        return True


def cut(src, out, start, end, words=(), headline="", layout=""):
    """One reel: 4K vertical (2160x3840), full screen on a phone, original sound in sync.
    layout: "" = full (the video cropped to fill the screen); "cam:top:<auto|tl|tr|bl|br>" = the streamer's facecam on
            top, the gameplay below; "split:<file>" = the clip on top, that background video (data/fillers) below.
    words: subtitles at the bottom (() = none); headline: the header across the middle ("" = none).
    Sync-safe exact cut: decode from a little before `start`, then cut on the exact frame, so picture and sound both
    start at 0 (see save_master); the text is shifted to match."""
    src, out = Path(src), Path(out)
    pre = min(start, 20.0)
    filler = filler_path(layout)
    if str(layout).startswith("split:") and not filler:
        raise ValueError(f"the background video {str(layout)[6:]} is gone: pick another in Settings → Background videos")
    cam = cam_layout(layout) if not filler else None
    if cam and cam[1] == "auto":
        box = find_cam(src, start, end, face_at=0.68 if cam[0] == "top" else 0.45)  # top: room for the header over it
        db.here(f"   facecam: found at x {box[0]:.0%} y {box[1]:.0%} ({box[2]:.0%} x {box[3]:.0%} of the frame)" if box
                else "   facecam: no face found, using the full-screen framing")
        cam = (cam[0], box) if box else None
    subs = ""
    if words or headline:
        sub = out.with_suffix(".ass")
        # the header: with the streamer on top, in the middle (on the seam between streamer and gameplay); otherwise
        # at the top (under the app's own top icons). Subtitles at the bottom, as always.
        mid = (CAM_H if cam[0] == "top" else 1920 - CAM_H) if cam else TOP_Y
        cap = CAM_H + 300 if cam and cam[0] == "bottom" else None
        sub.write_text(ass(words, start, end, headline, mid, cap, shift=pre), encoding="utf-8")
        subs = f",subtitles={sub.name}"  # relative name + cwd avoids ffmpeg path escaping
    K, W, H = OUT_K, 1080 * OUT_K, 1920 * OUT_K  # rendered at 4K (2160x3840); the text is laid out on 1080x1920
    if filler:  # the clip fills the top (cropped to fill), your background video the bottom, muted
        top_h, bot_h = SPLIT_CLIP_H * K, (1920 - SPLIT_CLIP_H) * K
        head = (f"[0:v]scale={W}:{top_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={W}:{top_h},{DETAIL},setsar=1[clip];"
                f"[1:v]scale={W}:{bot_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{bot_h},setsar=1,fps=30[fill];"
                f"[clip][fill]vstack=inputs=2:shortest=1{subs}")
    elif cam:
        # The streamer's box cut out of the frame fills a full-width CAM_H panel (cropped inside the box only); the
        # gameplay is shown WHOLE (the full frame, nothing cut off: full width, 608 of 1920 tall for 16:9) right under
        # it; a blurred, darkened copy of the gameplay fills the rest of the screen (the subtitles sit there).
        x, y, w, h = cam[1]
        cam_h, game_h = CAM_H * K, GAME_H * K
        # the gameplay slice: the middle of the frame, pushed off the facecam's side so the streamer isn't in it twice
        gx = (f"min(max((iw-ow)/2,iw*{min(x + w + 0.03, 1):.4f}),iw-ow)" if x + w / 2 < 0.4 else
              f"max(min((iw-ow)/2,iw*{max(x - 0.03, 0):.4f}-ow),0)" if x + w / 2 > 0.6 else "(iw-ow)/2")
        game_y, cam_y = (cam_h, 0) if cam[0] == "top" else (0, game_h)
        # Quality: the facecam is a small box blown up a lot: a light denoise first (so the stream's compression blocks
        # aren't enlarged), lanczos, then contrast-adaptive sharpening; the gameplay the normal detail pass.
        head = (f"[0:v]split=3[c][g][b];"
                f"[b]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},boxblur=14:2,"
                f"eq=brightness=-0.28:saturation=1.1,scale={W}:{H},setsar=1[bg];"
                f"[c]crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f},hqdn3d=1.5:1.5:0:0,"
                f"scale={W}:{cam_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{cam_h},cas={CAM_SHARPEN},setsar=1[cam];"
                f"[g]crop=w='min(iw,ih*{1080 / GAME_H:.4f})':h=ih:x='{gx}',"
                f"scale={W}:{game_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{game_h},{DETAIL},setsar=1[game];"
                f"[bg][cam]overlay=0:{cam_y}[t];[t][game]overlay=0:{game_y}{subs}")
    else:  # full: the centre of the frame, cropped to 9:16 so it fills the phone (no black bars)
        head = (f"[0:v]crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)',scale={W}:{H}:flags=lanczos,"
                f"{DETAIL},setsar=1{subs}")
    graph = head.replace("[0:v]", f"[0:v]{GRADE},", 1)
    logo = BRAND_LOGO if settings_logo() and BRAND_LOGO.is_file() else None
    if logo:  # your logo, small, top right
        graph += (f"[v];[{2 if filler else 1}:v]scale={LOGO_W * K}:-1:flags=lanczos[l];"
                  f"[v][l]overlay=W-w-{LOGO_MARGIN * K}:{LOGO_TOP * K}")
    graph += "[o]"
    fill_in = []
    if filler:  # muted, from a random point, looped if it's shorter than the clip
        import random
        off = random.uniform(0, max(0.0, _duration(filler) - (end - start) - 1))
        fill_in = ["-stream_loop", "-1", "-ss", f"{off:.2f}", "-i", str(filler.resolve())]
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start - pre:.3f}", "-t", f"{end - start + pre:.3f}",
                    "-i", str(src.resolve()), *fill_in, *(["-i", str(logo.resolve())] if logo else []),
                    "-filter_complex", graph, "-map", "[o]", "-map", "0:a?",
                    "-ss", f"{pre:.3f}", "-t", str(end - start), "-af", "aresample=async=1:first_pts=0",
                    "-avoid_negative_ts", "make_zero", *encoder(), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
                    "-ar", "48000", "-movflags", "+faststart", out.name], check=True, cwd=out.parent)


NEWS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Badge,Arial Black,58,&H00FFFFFF,&H00FFFFFF,&H002E2EFF,&H002E2EFF,1,0,0,0,100,100,2,0,3,18,0,5,0,0,0,1
Style: Head,Arial Black,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,1,6,3,8,0,0,0,1
Style: Line,Arial,52,&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,1,0,0,0,100,100,0,0,3,14,0,8,0,0,0,1
Style: Dot,Arial Black,52,&H0000E5FF,&H0000E5FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,8,0,0,0,1
Style: Src,Arial,38,&H00DDDDDD,&H00DDDDDD,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,3,0,5,0,0,0,1
Style: Hook,Arial Black,28,&H00FFFFFF,&H00FFFFFF,&H002E2EFF,&H002E2EFF,1,0,0,0,100,100,0,0,3,8,0,8,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _clean(t):
    return " ".join(str(t).replace("{", "(").replace("}", ")").replace("\\", "/").split())


# Where things go: a 1080x1920 video (Reel/Short) or a 1080x1350 photo (Instagram's portrait size).
NEWS_LAYOUT = {1920: dict(hook=60, badge=250, pic=(330, 540), head=(930, 560), src=1856, facts=3),
               1350: dict(hook=40, badge=225, pic=(285, 340), head=(650, 360), src=1300, facts=2)}


def news_ass(post, source, age_h, length, picture, h=1920, hook=None):
    """Follow line, then the badge pops in, headline lines slide up one by one, then each key fact, source line at the
    bottom. h=1350: the photo layout (fewer facts)."""
    import textwrap
    L = NEWS_LAYOUT[h]
    ev = lambda layer, a, b, style, text: f"Dialogue: {layer},{_t(a)},{_t(b)},{style},,0,0,0,,{text}"
    out, end = [], length
    hook = follow_line() if hook is None else hook
    hl = hook_lines(hook, 36)  # narrow, so it never runs under the logo (top-right)
    if hl:
        out.append(ev(2, 0, end, "Hook", rf"{{\an8\pos(540,{L['hook']})\fad(200,0)}}" + r"\N".join(hl)))
    badge = max(L["badge"], L["hook"] + len(hl) * 38 + 48)
    out.append(ev(2, 0.1, end, "Badge", rf"{{\an5\pos(540,{badge})\fscx30\fscy30\t(0,220,\fscx112\fscy112)\t(220,360,\fscx100\fscy100)}}"
                  + f" {_clean(post['badge'])} "))
    y = L["head"][0] if picture else L["head"][1]
    head = _clean(post["headline"]).upper()
    # fewest lines, then the most even ones (no lone word on the last line)
    best = min((textwrap.wrap(head, w) for w in range(13, 21)),
               key=lambda ls: (len(ls), max(map(len, ls)) - min(map(len, ls))))
    for i, line in enumerate(best[:4]):
        t0 = 0.35 + i * 0.22
        out.append(ev(3, t0, end, "Head", rf"{{\an8\move(540,{y + 40},540,{y},0,300)\fad(250,0)}}{line}"))
        y += 100
    y += 36
    for i, fact in enumerate(post["lines"][:L["facts"]]):
        t0 = 1.3 + i * 1.6
        wrapped = textwrap.wrap(_clean(fact), 34)[:3]
        out.append(ev(1, t0, end, "Dot", rf"{{\an8\pos(80,{y + 4})\fad(200,0)}}▶"))
        out.append(ev(2, t0, end, "Line", rf"{{\an7\move(150,{y + 30},130,{y},0,280)\fad(250,0)}}" + r"\N".join(wrapped)))
        y += 70 * len(wrapped) + 34
    when = "" if age_h is None else f"  ·  {age_h * 60:.0f} min ago" if age_h < 1 else f"  ·  {age_h:.0f} h ago" if age_h < 48 else ""
    out.append(ev(2, 0.6, end, "Src", rf"{{\an5\pos(540,{L['src']})\fad(400,0)}}" + f"Source: {_clean(source)}{when}"))
    return NEWS_HEAD.replace("{h}", str(h)) + "\n".join(out) + "\n"


def news_seconds(post, voice_len=0):
    words = len(post["headline"].split()) + sum(len(x.split()) for x in post["lines"])
    return round(min(max(voice_len + 1.5 if voice_len else 2.5 + 0.33 * words, 7), 15), 2)


def news_video(out, post, source, age_h, picture=None, voice=None, follow=True):
    """A 1080x1920 news/fact post: the article's picture (sharp, slow zoom) over a blurred copy of it, or an animated
    gradient when there's no picture; badge, headline, key facts, "Source: ..." and the logo; voice-over or silence.
    -> its length in seconds."""
    voice_len = 0
    if voice:
        import wave
        with wave.open(str(voice)) as w:
            voice_len = w.getnframes() / w.getframerate()
    length = news_seconds(post, voice_len)
    sub = out.with_suffix(".ass")
    sub.write_text(news_ass(post, source, age_h, length, bool(picture), hook=None if follow else ""), encoding="utf-8")
    return _news_render(out, sub, length, picture, voice, 1920)


def news_image(out, post, source, age_h, picture=None, follow=True):
    """The same post as one 1080x1350 photo (Facebook / Instagram feed): the last frame, everything shown."""
    sub = out.with_suffix(".ass")
    sub.write_text(news_ass(post, source, age_h, 10, bool(picture), h=1350, hook=None if follow else ""), encoding="utf-8")
    return _news_render(out, sub, 10, picture, None, 1350, photo=True)


def _news_render(out, sub, length, picture, voice, h, photo=False):
    py, ph = NEWS_LAYOUT[h]["pic"]
    if picture:
        ins = ["-loop", "1", "-t", str(length), "-i", str(picture.resolve())]
        graph = (f"[0:v]split=2[a][b];[a]scale=1080:{h}:force_original_aspect_ratio=increase,crop=1080:{h},"
                 f"boxblur=28:2,eq=brightness=-0.32:saturation=1.2[bg];"
                 f"[b]scale=w='min(1000,iw*{ph}/ih)*(1+0.05*t/{length})':h=-2:eval=frame[fg];"
                 f"[bg][fg]overlay=(W-w)/2:{py}+({ph}-h)/2:format=auto,fps=30,format=yuv420p")
    else:
        ins = ["-f", "lavfi", "-t", str(length), "-i",
               f"gradients=s=1080x{h}:c0=0x14061f:c1=0x4a0f2e:c2=0x0b1a3a:n=3:speed=0.015:r=30"]
        graph = "[0:v]format=yuv420p"
    graph += f",subtitles={sub.name}"
    logo = []
    if LOGO and LOGO.exists():
        logo = ["-i", str(LOGO.resolve())]
        graph += "[v];[1:v]scale=190:-1[l];[v][l]overlay=W-w-28:36"
    graph += "[o]"
    k = 1 + len(logo) // 2
    if photo:  # one frame, after every animation has finished
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", *ins, *logo, "-filter_complex", graph, "-map", "[o]",
                        "-ss", str(length - 2), "-frames:v", "1", "-q:v", "2", out.name], check=True, cwd=out.parent)
        return 0
    if voice:
        aud, amap = ["-i", str(voice.resolve())], ["-map", f"{k}:a"]
        graph += f";[{k}:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay=600:all=1,volume=1.5,apad[a]"
        amap = ["-map", "[a]"]
    else:
        aud, amap = ["-f", "lavfi", "-t", str(length), "-i", "anullsrc=r=48000:cl=stereo"], ["-map", f"{k}:a"]
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *ins, *logo, *aud, "-filter_complex", graph, "-map", "[o]",
                    *amap, "-t", str(length), *encoder(), "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac",
                    "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", out.name], check=True, cwd=out.parent)
    return length


if __name__ == "__main__":
    W = [{"w": "no", "s": 10, "e": 10.4}, {"w": "way", "s": 10.4, "e": 11}, {"w": "{bro}", "s": 11, "e": 12},
         {"w": "what", "s": 12, "e": 13}]
    a = ass(W, 10, 20)
    assert "Cap,{\\c&H00E5FF&" in a and "WAY BRO" in a and "PlayResY: 1920" in a and "Dialogue: 2," not in a, a  # karaoke, no header
    a = ass([], 10, 20, headline="Speed fails 😭", mid_y=700)
    assert "\\pos(540,700)" in a and "SPEED FAILS" in a and "Segoe UI Emoji" in a, a
    assert "\\t(" not in a and "\\fscx" not in a, "the title is still: no pop-in, no pulse"
    assert "&H00000000,&H00FFFFFF,&H00FFFFFF,1,3," in a, "black text on a white box"
    a = ass(W, 10, 20, shift=5)
    assert "Dialogue: 0,0:00:05.00," in a, a  # shifted for the exact cut
    assert ",2,250" in ass([], 0, 1) and ",2,1000" in ass([], 0, 1, cap_margin=1000)
    assert [layout_name(x) for x in ("", "cam:top:auto", "split:a.mp4")] == ["full", "cam", "split"]
    assert cam_layout("cam:top:tr") == ("top", CAM_CORNERS["tr"]) and cam_layout("cam:x:y") is None
    print("render ok")
