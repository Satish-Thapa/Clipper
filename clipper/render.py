import os
import re
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

from . import db
from .config import CROP_ASPECT, END_CARD, FFMPEG, FOLLOW_LINE, FOLLOW_LINES, LOGO, VOICE

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Cap,Arial Black,{size},&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,{margin}
Style: Top,Arial Black,{top_size},&H00FFFFFF,&H00000000,&H64000000,1,1,7,3,8,{top_margin}
Style: EndBox,Arial Black,{end_size},&H00FFFFFF,&H002E2EFF,&H002E2EFF,1,3,22,0,5,0
Style: End,Arial Black,{end_sub},&H00FFFFFF,&H00000000,&H80000000,1,1,7,3,5,0
Style: EndSmall,Arial Black,{end_small},&H00FFFFFF,&H00000000,&H80000000,1,1,6,3,5,0
Style: EndHi,Arial Black,{end_hi},&H0000E5FF,&H00000000,&H80000000,1,1,7,3,5,0
Style: Hook,Arial Black,{hook_size},&H00FFFFFF,&H002E2EFF,&H002E2EFF,1,3,9,0,8,0

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


def bar_height(aspect=CROP_ASPECT):
    """Height of each black bar (top and bottom) when a w/h=aspect slice is scaled to 1080 wide on a 1080x1920 canvas."""
    return max(0, (1920 - round(1080 / aspect)) // 2)


END_SECONDS = 2.6  # end card length; only on clips of at least 3x this


def end_seconds(text=END_CARD):
    """How long the end card stays: long enough to read (~0.3s a word), 2.6-5s."""
    return min(max(0.3 * len(text.replace("|", " ").split()) + 0.8, END_SECONDS), 5.0)


def end_card(length, text=END_CARD, w=1080, h=1920):
    """Animated call to action at the end of the clip.
    With "|" in the text (e.g. "IF YOU MADE IT TO|THE END OF THIS VIDEO,|YOU LEGALLY HAVE TO|FOLLOW ME"):
    the video dims, the lines come in one after another (white lines slide up, the second-to-last pops in yellow),
    and the LAST line slams in on a red box with an overshoot and keeps pulsing.
    Without "|": the first word pops on the red box and the rest slides up under it (the original card)."""
    text = text.upper().replace("{", "").replace("}", "").strip()
    secs = end_seconds(text)
    if not text or length < secs * 3:
        return []
    if "|" in text:
        return _end_lines([x.strip() for x in text.split("|") if x.strip()], length, secs, w, h)
    first, _, rest = text.partition(" ")
    a, b, cx, cy = _t(length - END_SECONDS), _t(length), w // 2, h // 2
    pop = (rf"{{\an5\pos({cx},{cy - 70})\fad(100,180)\fscx20\fscy20\t(0,220,\fscx118\fscy118)\t(220,380,\fscx100\fscy100)"
           r"\t(700,1000,\fscx108\fscy108)\t(1000,1300,\fscx100\fscy100)\t(1300,1600,\fscx108\fscy108)"
           r"\t(1600,1900,\fscx100\fscy100)}")
    out = [f"Dialogue: 2,{a},{b},EndBox,{pop}{first}"]
    if rest:
        out.append(f"Dialogue: 2,{a},{b},End," + rf"{{\an5\move({cx},{cy + 160},{cx},{cy + 90},250,550)\fad(450,180)}}" + rest)
    return out


def _end_lines(lines, length, secs, w, h):
    """The multi-line end card (see end_card). Each line is its own event so it can start later (the stagger)."""
    k = w / 1080  # 16:9 renders are 1920 wide: scale positions, sizes come from the styles
    start, end, cx = length - secs, length, w // 2
    hi = len(lines) - 2  # second-to-last line: yellow accent
    heights = [int((170 if i == len(lines) - 1 else 100 if i == hi else 82) * k) for i in range(len(lines))]
    y = h // 2 - sum(heights) // 2  # whole block centred on the screen
    out = [  # dim the video behind the text (a full-screen 55%-black rectangle, fading in)
        f"Dialogue: 1,{_t(start)},{_t(end)},End,"
        + rf"{{\an7\pos(0,0)\bord0\shad0\1c&H000000&\1a&H90&\fad(300,150)\p1}}m 0 0 l {w} 0 {w} {h} 0 {h}{{\p0}}"]
    for i, (line, lh) in enumerate(zip(lines, heights)):
        cy, t0 = y + lh // 2, start + 0.15 + i * 0.38  # each line 0.38s after the one before
        if i == len(lines) - 1:  # the punchline on the red box: slam in from big, overshoot, then keep pulsing
            pulse = "".join(rf"\t({ms},{ms + 300},\fscx{106 if j % 2 == 0 else 100}\fscy{106 if j % 2 == 0 else 100})"
                            for j, ms in enumerate(range(650, int((end - t0) * 1000), 300)))
            tags = rf"{{\an5\q2\pos({cx},{cy})\fad(80,150)\fscx260\fscy260\t(0,180,\fscx92\fscy92)\t(180,320,\fscx100\fscy100){pulse}}}"
            out.append(f"Dialogue: 3,{_t(t0)},{_t(end)},EndBox,{tags}{line}")
        elif i == hi:  # yellow accent: pops from small
            tags = rf"{{\an5\q2\pos({cx},{cy})\fad(120,150)\fscx40\fscy40\t(0,200,\fscx112\fscy112)\t(200,330,\fscx100\fscy100)}}"
            out.append(f"Dialogue: 2,{_t(t0)},{_t(end)},EndHi,{tags}{line}")
        else:  # white lines: slide up a little and fade in
            tags = rf"{{\an5\q2\move({cx},{cy + int(40 * k)},{cx},{cy},0,260)\fad(220,150)}}"
            out.append(f"Dialogue: 2,{_t(t0)},{_t(end)},EndSmall,{tags}{line}")
        y += lh
    return out


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


HOOK_LINE_H, HEAD_H = 42, 106  # one follow-message line / the headline box (px) on a 1080x1920 canvas


def split_head_y(hook):
    """Split layout: the follow + headline block sits 28px into the background video (below the main clip)."""
    n = len(hook_lines(hook))
    return SPLIT_CLIP_H + 28 + (n * HOOK_LINE_H + (14 if n else 0) + HEAD_H) // 2


def seam_block(y, lines=1):
    """Like top_block, but the follow message + headline centred on line y (the facecam layout's seam, so they
    don't cover the streamer's face)."""
    hook_h = lines * HOOK_LINE_H + (14 if lines else 0)
    top = y - (hook_h + HEAD_H) // 2
    return top, top + hook_h, top + hook_h + HEAD_H


def top_block(aspect=CROP_ASPECT, lines=1):
    """Where the follow message and the headline go: (hook_y, headline_y, bottom). Centred in the top bar when it's
    tall enough, else from y=110 (under YouTube's top icons), on the video. lines = follow-message lines (0 = none)."""
    bar = bar_height(aspect)
    hook_h = lines * HOOK_LINE_H + (14 if lines else 0)
    y = max(110, (bar - hook_h - HEAD_H) // 2)
    return y, y + hook_h, y + hook_h + HEAD_H


YELLOW = r"{\c&H00E5FF&}"  # ASS colours are BGR: this is the headline yellow
EMOJI_FONT = "Segoe UI Emoji"
# The word being said: yellow, pops up bigger and wiggles as it's pronounced, then settles (the rest of the line stays still).
JIGGLE = (r"{\c&H00E5FF&\t(0,90,\fscx125\fscy125\frz-5)\t(90,170,\fscx112\fscy112\frz4)"
          r"\t(170,250,\fscx115\fscy115\frz-2)\t(250,330,\fscx110\fscy110\frz0)}")
UNJIGGLE = r"{\c&HFFFFFF&\fscx100\fscy100\frz0}"


def _emoji(text):
    """Emoji in the font that has them (Arial Black doesn't), then back."""
    return re.sub(r"([\U0001F000-\U0001FAFF☀-➿]+)", rf"{{\\fn{EMOJI_FONT}}}\1{{\\fnArial Black}}", text)


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


def ass(words, start, end, vertical=True, headline="", per_line=3, aspect=CROP_ASPECT, hook=None, split=False,
        cap_margin=None, head_y=None, ending=True, shift=0.0, karaoke=False):
    """Headline (white, black outline, no box: the video shows through) in the top black bar for the whole clip + big captions in the bottom black bar,
    a few words at a time. With no bars (aspect 9/16) they sit on the video instead.
    shift: every line this many seconds later (an exact cut decodes from `shift` s before the clip starts).
    karaoke: the word being said in yellow (and bouncing)."""
    if shift:
        out = ass(words, start, end, vertical, headline, per_line, aspect, hook, split, cap_margin, head_y, ending,
                  karaoke=karaoke)
        return re.sub(r"^(Dialogue: \d+,)([\d:.]+),([\d:.]+),",
                      lambda m: f"{m[1]}{_t(_secs(m[2]) + shift)},{_t(_secs(m[3]) + shift)},", out, flags=re.M)
    ws = [w for w in words if start <= w["s"] < end]
    lines = _caption_lines(ws, start, per_line, karaoke)
    if headline:
        raw = headline.upper().replace("{", "").replace("}", "")
        text = _emoji(raw)
        if vertical:  # follow message on top, then the headline under it
            hook = follow_line() if hook is None else hook
            hl = hook_lines(hook)
            hy, ty, _ = top_block(aspect, len(hl)) if head_y is None else seam_block(head_y, len(hl))
            hook = r"\N".join(hl)
            pulse = "".join(rf"\t({ms},{ms + 400},\fscx{105 if j % 2 == 0 else 100}\fscy{105 if j % 2 == 0 else 100})"
                            for j, ms in enumerate(range(1200, min(int((end - start) * 1000), 60000), 1600)))
            if hook:
                lines.insert(0, f"Dialogue: 1,{_t(0)},{_t(end - start)},Hook,"
                                + rf"{{\an8\pos(540,{hy})\fad(200,0)\fscx40\fscy40\t(0,220,\fscx108\fscy108)\t(220,360,\fscx100\fscy100){pulse}}}{hook}")
            # a long headline (it now matches the title, up to 6 words) gets a smaller font so it stays inside 1080px
            fit = rf"\fs{max(50, round(78 * 24 / len(raw)))}" if len(raw) > 24 else ""
            lines.insert(0, f"Dialogue: 0,{_t(0)},{_t(end - start)},Top,{{\\an8\\pos(540,{ty if hook else hy}){fit}}}{text}")
        else:
            lines.insert(0, f"Dialogue: 0,{_t(0)},{_t(end - start)},Top,{text}")
    elif hook and vertical:  # follow message without a headline: same spot, on its own
        hl = hook_lines(hook)
        hy = (top_block(aspect, len(hl)) if head_y is None else seam_block(head_y, len(hl)))[0]
        lines.insert(0, f"Dialogue: 1,{_t(0)},{_t(end - start)},Hook,"
                        + rf"{{\an8\pos(540,{hy})\fad(200,0)}}" + r"\N".join(hl))
    # Headline: centred in the top bar (box is ~106px tall), but below YouTube's top icons (~120px).
    # Captions: just under the video, so they stay above YouTube's title/channel overlay at the very bottom.
    bar = bar_height(aspect)
    size = (dict(w=1080, h=1920, size=86, margin=max(250, bar - 120), top_size=78, top_margin=max(120, (bar - 106) // 2),
                 end_size=120, end_sub=84, end_small=62, end_hi=76, hook_size=31)
            if vertical
            else dict(w=1920, h=1080, size=64, margin=70, top_size=60, top_margin=50, end_size=90, end_sub=64, end_small=56, end_hi=68,
                      hook_size=40))
    if cap_margin and vertical:
        size["margin"] = cap_margin  # captions this far from the bottom (facecam at the bottom: just above it)
    if ending:
        lines += end_card(end - start, w=size["w"], h=size["h"])
    return ASS_HEAD.format(**size) + "\n".join(lines) + "\n"


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


CLIP_MAXRATE = 24  # Mbps peak for the clips: Instagram rejects Reels over 25 Mbps (on average); quality-based below it


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


def reencode(src, out, bitrate):
    """Same video at a lower bitrate (for Instagram's 25 Mbps cap); audio copied."""
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), *encoder()[:2], "-b:v", bitrate,
                    "-maxrate", bitrate, "-bufsize", "28M", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
                    str(out)], check=True, capture_output=True)
    return out


PREVIEW_W, PREVIEW_RATE = 540, "1200k"  # light copy for watching in the app (over a tunnel: ~1/10 the data)
_preview_locks, _preview_slots = {}, __import__("threading").Semaphore(2)  # at most 2 preview encodes at once


def preview(clip, out_dir, name, poster=False):
    """For the app's players: a light copy of a clip (540 wide, ~1.2 Mbps, starts playing at once; made when it's
    first played) or, with poster=True, one small still (1s in, so the title shows). Made once, remade only when the
    clip changes (e.g. a new headline). Downloads / uploads always use the full-quality clip. -> the file."""
    clip, out = Path(clip), Path(out_dir) / f"{name}.{'jpg' if poster else 'mp4'}"
    with _preview_locks.setdefault(str(out), __import__("threading").Lock()):
        if out.exists() and out.stat().st_mtime >= clip.stat().st_mtime:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + "_tmp" + out.suffix)
        if poster:
            cmd = ["-ss", f"{min(1.0, _duration(clip) / 2):.2f}", "-i", str(clip), "-frames:v", "1",
                   "-vf", f"scale={PREVIEW_W}:-2", "-q:v", "5"]
        else:
            cmd = ["-i", str(clip), "-vf", f"scale={PREVIEW_W}:-2", *encoder()[:2], "-b:v", PREVIEW_RATE,
                   "-maxrate", "1800k", "-bufsize", "3600k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                   "-movflags", "+faststart"]
        with _preview_slots:
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", *cmd, str(tmp)], check=True, capture_output=True)
        tmp.replace(out)
        return out


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


SHARPEN, CAM_SHARPEN = 0.4, 0.7  # contrast-adaptive sharpening after upscaling (0-1): the picture / the facecam panel
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
GRADE = "eq=contrast=1.07:saturation=1.25:gamma=0.97"


def cut(src, out, start, end, vertical, words=(), headline="", voice=(), aspect=CROP_ASPECT, layout="", follow=True,
        hook_text=None, clean=False, exact=False, reel=False):
    """words: burn these as captions; headline: text at the top. Pass ()/"" for none.
    voice: [(wav_path, seconds into clip)] voice-over lines; the original audio is ducked under them.
    aspect: w/h of the kept slice of the frame (9/16 = fills the phone ... 16/9 = whole frame with bars).
    layout: "split:<file>" = the clip on the top half, that background video (data/fillers) on the bottom half.
            "cam:<top|bottom>:<where>" = the streamer's facecam (cut out of the frame) and the gameplay stacked full screen.
    hook_text: shown in the follow message's place instead of it ("" = nothing there); campaigns put the required line here.
    clean: no end card and no logo (campaign clips: only the official picture and sound).
    exact: sync-safe cut for stream recordings (live clips, no text): seek a little early, then trim to the exact
           frame, so picture and sound both start at 0 (see save_master). The headline / follow line are shifted to
           match; no voice-over with it.
    reel: the reel look: captions with the word being said in yellow (and bouncing)."""
    if exact:
        assert not voice, "exact cuts carry no voice-over"
    pre = min(start, 20.0) if exact else 0.0  # exact: decode from `pre` s early, cut on the exact frame (output -ss)
    filler = filler_path(layout) if vertical else None
    cam = cam_layout(layout) if vertical and not filler else None
    if cam and cam[1] == "auto":
        box = find_cam(src, start, end, face_at=0.68 if cam[0] == "top" else 0.45)  # top: room for the headline over it
        db.here(f"   facecam: found at x {box[0]:.0%} y {box[1]:.0%} ({box[2]:.0%} x {box[3]:.0%} of the frame)" if box
                else "   facecam: no face found, using normal full-screen framing")
        cam = (cam[0], box) if box else None
    aspect = 9 / 16 if filler or cam else min(max(float(aspect or CROP_ASPECT), 9 / 16), 16 / 9)
    # Vertical: keep a centred `aspect` slice of the frame (wider than a full 9:16 crop = more gameplay),
    # fit it to 1080 wide, and pad to 1080x1920 with black bars that hold the headline and captions.
    # ponytail: plain center crop; facecam/subject tracking is the upgrade if gameplay gets cut off
    vf = (f"crop='min(iw,ih*{aspect})':ih,scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
          f"cas={SHARPEN},pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1" if vertical else f"scale=-2:1080:flags=lanczos,cas={SHARPEN}")
    hook = (_clean_hook(hook_text) if hook_text and vertical else "") if hook_text is not None else (
        follow_line() if follow and vertical else "")  # the follow message: its own on/off, with or without a headline
    logo = LOGO if LOGO and LOGO.exists() and not clean else None
    subs = ""
    if words or headline or hook or (END_CARD.strip() and not clean):
        sub = out.with_suffix(".ass")
        sub.write_text(ass(words, start, end, vertical, headline, aspect=aspect, hook=hook, split=bool(filler),
                           cap_margin=CAM_H + 40 if cam and cam[0] == "bottom" else None,
                           head_y=split_head_y(hook) if filler else None, ending=not clean, shift=pre, karaoke=reel),
                       encoding="utf-8")
        subs = f",subtitles={sub.name}"  # relative name + cwd avoids ffmpeg path escaping
    n_in = 1 + (1 if logo else 0) + len(voice)  # the background video is the last input
    if filler:
        # Split: the main clip fills the top (full width, cropped to fill, the bigger part), your background video fills
        # the bottom. The follow message + headline sit at the top of the background video, so the clip stays clean.
        bot_h = 1920 - SPLIT_CLIP_H
        head = (f"[0:v]scale=1080:{SPLIT_CLIP_H}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop=1080:{SPLIT_CLIP_H},setsar=1[clip];"
                f"[{n_in}:v]scale=1080:{bot_h}:force_original_aspect_ratio=increase,crop=1080:{bot_h},setsar=1,fps=30[fill];"
                f"[clip][fill]vstack=inputs=2:shortest=1{subs}")
    elif cam:
        # Facecam stack, full screen: the streamer's box cut out of the frame fills the whole width of a 1080xCAM_H
        # panel (cropped top/bottom inside the box only, so nothing around the facecam comes in), the gameplay (middle
        # of the frame) fills the rest. Headline + follow message sit at the top, on the video; captions on the gameplay.
        x, y, w, h = cam[1]
        cam_h, game_h = CAM_H, 1920 - CAM_H
        # Gameplay = a slice from the middle of the frame, pushed off the facecam's side so the streamer isn't in it twice.
        gx = (f"min(max((iw-ow)/2,iw*{min(x + w + 0.05, 1):.4f}),iw-ow)" if x + w / 2 < 0.4 else
              f"max(min((iw-ow)/2,iw*{max(x - 0.05, 0):.4f}-ow),0)" if x + w / 2 > 0.6 else "(iw-ow)/2")
        panels = ["[cam]", "[game]"] if cam[0] == "top" else ["[game]", "[cam]"]
        head = (f"[0:v]split=2[c][g];"
                # Quality: the facecam is a small box blown up ~3x: a light denoise first (so the stream's compression
                # blocks aren't enlarged), lanczos, then contrast-adaptive sharpening; the gameplay a lighter sharpen.
                f"[c]crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f},hqdn3d=1.5:1.5:0:0,"
                f"scale=1080:{cam_h}:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:{cam_h},cas={CAM_SHARPEN},setsar=1[cam];"
                f"[g]crop=w='min(iw,ih*{1080 / game_h:.4f})':h=ih:x='{gx}',"
                f"scale=1080:{game_h}:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:{game_h},cas={SHARPEN},setsar=1[game];"
                f"{''.join(panels)}vstack=inputs=2{subs}")
    else:
        head = f"[0:v]{vf}{subs}"
    if reel or not clean:  # the colour grade (not on campaign clips: only the official picture there)
        head = head.replace("[0:v]", f"[0:v]{GRADE},", 1)
    graph, logo_in = f"{head}[o]", []
    if logo:
        # Small logo top-right. Vertical with bars: in the top bar just above the video (clear of the headline and
        # YouTube's own top icons). Vertical without bars: just under the headline box (which sits at y 120-226),
        # so it never covers the headline. 16:9: in the video's top-right corner.
        bar = bar_height(aspect) if vertical else 0
        below = top_block(aspect, len(hook_lines(hook)))[2] if headline else 230  # bottom of the follow + headline block
        if filler:  # split: the text block is on the background video, the logo just under it
            below = seam_block(split_head_y(hook), len(hook_lines(hook)))[2] if headline or hook else SPLIT_CLIP_H
        y = f"{bar}-h-24" if bar - below >= 110 else f"{max(bar, below) if vertical else 0}+24"  # h = logo height
        logo_in = ["-i", str(LOGO.resolve())]
        graph = f"{head}[v];[1:v]scale={220 if vertical else 260}:-1[l];[v][l]overlay=W-w-24:{y}[o]"
    vo_in, amap = [], ["-map", "0:a?"]
    if voice:
        # Each line delayed to its spot -> mixed into one narration track [vo], louder than the TTS default.
        # sidechaincompress pushes the original audio down while [vo] is talking (ducking), then the two are mixed.
        k0 = 1 + len(logo_in) // 2  # index of the first voice input (after source and optional logo)
        lines = []
        for i, (wav, at) in enumerate(voice):
            vo_in += ["-i", str(wav.resolve())]
            lines.append(f"[{k0 + i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                         f"adelay={int(at * 1000)}:all=1[vo{i}]")
        graph += (";" + ";".join(lines) + ";" + "".join(f"[vo{i}]" for i in range(len(voice)))
                  + f"amix=inputs={len(voice)}:normalize=0:duration=longest,volume=1.6,asplit=2[vk][vm];"
                  "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[orig];"
                  "[orig][vk]sidechaincompress=threshold=0.02:ratio=12:attack=15:release=400[duck];"
                  "[duck][vm]amix=inputs=2:normalize=0:duration=first[a]")
        amap = ["-map", "[a]"]
    fill_in = []
    if filler:  # muted, from a random point, looped if it's shorter than the clip
        import random
        off = random.uniform(0, max(0.0, _duration(filler) - (end - start) - 1))
        fill_in = ["-stream_loop", "-1", "-ss", f"{off:.2f}", "-i", str(filler.resolve())]
    sync =["-af", "aresample=async=1:first_pts=0"] if exact and amap == ["-map", "0:a?"] else []
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start - pre:.3f}", "-t", f"{end - start + pre:.3f}",
                    "-i", str(src.resolve()), *logo_in, *vo_in, *fill_in, "-filter_complex", graph, "-map", "[o]", *amap,
                    *(["-ss", f"{pre:.3f}"] if exact else []), "-t", str(end - start), *sync,
                    *(["-avoid_negative_ts", "make_zero"] if exact else []),
                    *encoder(), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
                    "-movflags", "+faststart", out.name],
                   check=True, cwd=out.parent)


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
    a = ass([{"w": "no", "s": 10, "e": 10.4}, {"w": "way", "s": 10.4, "e": 11}, {"w": "{bro}", "s": 11, "e": 12},
             {"w": "what", "s": 12, "e": 13}], 10, 20)
    assert "0:00:00.00,0:00:02.00,Cap,NO WAY BRO" in a and "WHAT" in a and "PlayResY: 1920" in a, a
    assert "PlayResX: 1920" in ass([], 0, 1, vertical=False)
    a = ass([], 10, 20, headline="Cops remember you", hook="FOLLOW NOW")
    assert r"Top,{\an8\pos(540,166)}COPS REMEMBER YOU" in a and "Hook," in a and "}FOLLOW NOW" in a, a  # hook above
    assert r"Top,{\an8\pos(540,110)}X" in ass([], 0, 9, headline="x", hook="")  # no hook: headline on top
    assert len(hook_lines(FOLLOW_LINES[1])) in (2, 3)  # long messages wrap
    assert bar_height(1) == 420 and bar_height(9 / 16) == 0
    a = ass([], 0, 1, headline="x", aspect=1, hook="")
    assert "Cap,Arial Black,86,&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,300" in a and ",8,157" in a, a  # inside the bars
    print("render ok")
