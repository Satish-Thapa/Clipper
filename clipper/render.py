import os
import shutil
import re
import subprocess
import sys
import wave
from functools import lru_cache
from pathlib import Path

from . import db
from . import config

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Cap,Arial Black,{size},&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,{margin}
Style: Top,Arial Black,{top_size},&H00000000,&H0000E5FF,&H0000E5FF,1,3,14,0,8,{top_margin}
Style: EndBox,Arial Black,{end_size},&H00FFFFFF,&H002E2EFF,&H002E2EFF,1,3,22,0,5,0
Style: End,Arial Black,{end_sub},&H00FFFFFF,&H00000000,&H80000000,1,1,7,3,5,0
Style: EndSmall,Arial Black,{end_small},&H00FFFFFF,&H00000000,&H80000000,1,1,6,3,5,0
Style: EndHi,Arial Black,{end_hi},&H0000E5FF,&H00000000,&H80000000,1,1,7,3,5,0
Style: Mark,Arial,{mark_size},&H50FFFFFF,&H00000000,&H90000000,1,1,2,1,2,0

[Events]
Format: Layer, Start, End, Style, Text
"""


def _t(sec):
    h, r = divmod(max(sec, 0), 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def bar_height(aspect=None):
    """Height of each black bar (top and bottom) when a w/h=aspect slice is scaled to 1080 wide on a 1080x1920 canvas."""
    aspect = aspect or config.CROP_ASPECT
    return max(0, (1920 - round(1080 / aspect)) // 2)


END_SECONDS = 2.6  # end card length; only on clips of at least 3x this


def _default_end():
    """Settings → End card holds one variant per line; without a chosen one, the first."""
    return next((v.strip() for v in (config.END_CARD or "").splitlines() if v.strip()), "")


def end_seconds(text=None):
    """How long the end card stays: long enough to read (~0.3s a word), 2.6-5s."""
    text = _default_end() if text is None else text
    return min(max(0.3 * len(text.replace("|", " ").split()) + 0.8, END_SECONDS), 5.0)


def end_card(length, text=None, w=1080, h=1920):
    """Animated call to action at the end of the clip.
    With "|" in the text (e.g. "IF YOU MADE IT TO|THE END OF THIS VIDEO,|YOU LEGALLY HAVE TO|FOLLOW ME"):
    the video dims, the lines come in one after another (white lines slide up, the second-to-last pops in yellow),
    and the LAST line slams in on a red box with an overshoot and keeps pulsing.
    Without "|": the first word pops on the red box and the rest slides up under it (the original card)."""
    text = (_default_end() if text is None else text).upper().replace("{", "").replace("}", "").strip()
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
    def fit(line, base):  # shrink a long line so it fits the width (Arial Black caps: ~0.8 x size per character)
        size = min(base * k, 0.9 * w / (0.8 * max(len(line), 1)))
        return rf"\fs{int(size)}" if size < base * k else ""
    for i, (line, lh) in enumerate(zip(lines, heights)):
        cy, t0 = y + lh // 2, start + 0.15 + i * 0.38  # each line 0.38s after the one before
        line_fs = fit(line, 120 if i == len(lines) - 1 else 76 if i == hi else 62)
        if i == len(lines) - 1:  # the punchline on the red box: slam in from big, overshoot, then keep pulsing
            pulse = "".join(rf"\t({ms},{ms + 300},\fscx{106 if j % 2 == 0 else 100}\fscy{106 if j % 2 == 0 else 100})"
                            for j, ms in enumerate(range(650, int((end - t0) * 1000), 300)))
            tags = rf"{{\an5\q2\pos({cx},{cy}){line_fs}\fad(80,150)\fscx260\fscy260\t(0,180,\fscx92\fscy92)\t(180,320,\fscx100\fscy100){pulse}}}"
            out.append(f"Dialogue: 3,{_t(t0)},{_t(end)},EndBox,{tags}{line}")
        elif i == hi:  # yellow accent: pops from small
            tags = rf"{{\an5\q2\pos({cx},{cy}){line_fs}\fad(120,150)\fscx40\fscy40\t(0,200,\fscx112\fscy112)\t(200,330,\fscx100\fscy100)}}"
            out.append(f"Dialogue: 2,{_t(t0)},{_t(end)},EndHi,{tags}{line}")
        else:  # white lines: slide up a little and fade in
            tags = rf"{{\an5\q2\move({cx},{cy + int(40 * k)},{cx},{cy},0,260){line_fs}\fad(220,150)}}"
            out.append(f"Dialogue: 2,{_t(t0)},{_t(end)},EndSmall,{tags}{line}")
        y += lh
    return out


# Output frames. 9:16 is the vertical style with black bars holding the headline and captions; the others are the
# picture cropped to that shape with the text on it (IG/FB feed posts 4:5, square 1:1, YouTube 16:9).
CANVAS = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080), "16:9": (1920, 1080)}
SIZES = {  # caption / headline / end card / watermark sizes and margins per frame
    "4:5": dict(size=70, margin=90, top_size=60, top_margin=60, end_size=100, end_sub=72, end_small=54, end_hi=64, mark_size=34),
    "1:1": dict(size=64, margin=70, top_size=56, top_margin=50, end_size=92, end_sub=66, end_small=50, end_hi=60, mark_size=32),
    "16:9": dict(size=64, margin=70, top_size=60, top_margin=50, end_size=90, end_sub=64, end_small=56, end_hi=68, mark_size=32),
}


def canvas_of(vertical):
    """Older callers pass vertical=True/False; new ones a frame name like "4:5"."""
    if vertical is True:
        return "9:16"
    if vertical is False:
        return "16:9"
    return vertical if vertical in CANVAS else "9:16"


def ass(words, start, end, vertical=True, headline="", per_line=3, aspect=None, end_text=None):
    """Headline in a yellow box at the top for the whole clip + big captions at the bottom, a few words at a time.
    9:16: in the black bars above and below the picture (on it when there are no bars). Other frames: on the picture."""
    canvas = canvas_of(vertical)
    vertical = canvas == "9:16"
    # sound tags from YouTube's captions ([Music], [Cheering], [Applause]...) help find moments but aren't speech
    ws = [w for w in words if start <= w["s"] < end and not re.fullmatch(r"\W*\[[^\]]*\]\W*", w["w"].strip())]
    lines = []
    for i in range(0, len(ws), per_line):
        g = ws[i:i + per_line]
        text = " ".join(w["w"] for w in g).upper().replace("{", "").replace("}", "")
        text = text.encode("utf-8", "ignore").decode("utf-8").replace("\ufffd", "").strip()  # broken characters out
        t0 = g[0]["s"] - start
        t1 = min(max(g[-1]["e"] - start, t0 + 0.3), end - start)  # never zero-length: ffmpeg's subtitle reader then
        if t1 <= t0:                                                  # drops other lines too (the headline went missing)
            continue
        lines.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Cap,{text}")
    if headline:
        text = headline.upper().replace("{", "").replace("}", "")
        lines.insert(0, f"Dialogue: 0,{_t(0)},{_t(end - start)},Top,{text}")
    # Headline: centred in the top bar (box is ~106px tall), but below YouTube's top icons (~120px).
    # Captions: just under the video, so they stay above YouTube's title/channel overlay at the very bottom.
    bar = bar_height(aspect) if vertical else 0
    w, h = CANVAS[canvas]
    size = (dict(w=w, h=h, size=86, margin=max(250, bar - 120), top_size=78, top_margin=max(120, (bar - 106) // 2),
                 end_size=120, end_sub=84, end_small=62, end_hi=76, mark_size=40)
            if vertical else dict(w=w, h=h, **SIZES[canvas]))
    mark = config.WATERMARK.strip().replace("{", "").replace("}", "")
    if mark:  # your watermark text (Settings → Branding), faint, for the whole clip: at the bottom edge of the
        # picture when vertical (clear of the captions in the bar below it), bottom-right corner when 16:9
        pos = (rf"\an2\pos(540,{1920 - bar - 24})" if vertical else r"\an3\pos(1880,1050)" if canvas == "16:9"
               else rf"\an2\pos({w // 2},{h - 20})")
        lines.append(f"Dialogue: 1,{_t(0)},{_t(end - start)},Mark,{{{pos}}}{mark}")
    lines += end_card(end - start, text=end_text, w=size["w"], h=size["h"])
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
        subprocess.run(["say", *(["-v", config.VOICE] if config.VOICE else []), "-o", str(out), "--data-format=LEI16@22050", text],
                       check=True)
    else:
        env = {**os.environ, "TTS_TEXT": text, "TTS_OUT": str(out), "TTS_VOICE": config.VOICE}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SAPI_PS], env=env, check=True,
                       capture_output=True)
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


def _encoder_works(codec):
    test = [config.FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
            "-frames:v", "1", "-c:v", codec, "-f", "null", "-"]
    try:
        return subprocess.run(test, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=1)
def encoder():
    """GPU encoder if a 1-frame test encode works (NVIDIA NVENC on Windows/Linux, Apple VideoToolbox on macOS),
    else x264 on the CPU."""
    if sys.platform != "darwin" and _encoder_works("h264_nvenc"):
        db.syslog("render: GPU encoder (h264_nvenc)")
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                "-maxrate", "18M", "-bufsize", "36M", "-profile:v", "high"]
    if sys.platform == "darwin" and _encoder_works("h264_videotoolbox"):
        db.syslog("render: GPU encoder (h264_videotoolbox)")
        return ["-c:v", "h264_videotoolbox", "-b:v", "14M", "-maxrate", "18M", "-bufsize", "36M", "-profile:v", "high"]
    db.syslog("render: CPU encoder (libx264)")
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-maxrate", "18M", "-bufsize", "36M", "-profile:v", "high"]
    # ^ 18 Mbps cap: Instagram rejects Reels over 25 Mbps, and 1080x1920 looks no better above ~15


def ffprobe():
    """ffprobe next to the ffmpeg in use (same folder, same extension)."""
    ff = config.FFMPEG
    return str(Path(ff).with_name("ffprobe" + Path(ff).suffix)) if Path(ff).parent != Path(".") else "ffprobe"


def bitrate_mbps(path):
    out = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=bit_rate", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) / 1e6 if out.isdigit() else 0


def frame_b64(src, t, width=384):
    """One frame at second t as a small JPEG, base64 (for a vision model). "" if it can't be read."""
    import base64
    r = subprocess.run([config.FFMPEG, "-loglevel", "error", "-ss", str(max(t, 0)), "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "6", "-"],
                       capture_output=True)
    return base64.b64encode(r.stdout).decode() if r.returncode == 0 and r.stdout else ""


def duration(path):
    out = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def keep_last(src, out, seconds, bitrate):
    """The last `seconds` of a clip (its payoff and reaction), re-encoded at `bitrate`: for platforms whose API refuses
    longer videos (Instagram)."""
    start = max(duration(src) - seconds, 0)
    subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{seconds:.3f}",
                    *encoder()[:2], "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", "28M", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-af", "afade=t=in:d=0.25", "-movflags", "+faststart", str(out)],
                   check=True, capture_output=True)
    return out


def reencode(src, out, bitrate):
    """Same video at a lower bitrate (for Instagram's 25 Mbps cap); audio copied."""
    subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-i", str(src), *encoder()[:2], "-b:v", bitrate,
                    "-maxrate", bitrate, "-bufsize", "28M", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
                    str(out)], check=True, capture_output=True)
    return out


def encoder_name():
    enc = encoder()
    return "GPU NVENC" if "h264_nvenc" in enc else "GPU VideoToolbox" if "h264_videotoolbox" in enc else "CPU x264"


def cut(src, out, start, end, vertical, words=(), headline="", voice=(), aspect=None, progress=None, end_text=None):
    """vertical: the frame, "9:16" / "4:5" / "1:1" / "16:9" (True = 9:16, False = 16:9).
    words: burn these as captions; headline: text at the top. Pass ()/"" for none.
    voice: [(wav_path, seconds into clip)] voice-over lines; the original audio is ducked under them.
    aspect: 9:16 only: w/h of the kept slice of the frame (9/16 = fills the phone ... 16/9 = whole frame with bars).
    progress: called with 0..1 while it renders."""
    canvas = canvas_of(vertical)
    vertical = canvas == "9:16"
    aspect = min(max(float(aspect or config.CROP_ASPECT), 9 / 16), 16 / 9)
    w, h = CANVAS[canvas]
    # 9:16: keep a centred `aspect` slice of the frame (wider than a full 9:16 crop = more of the scene),
    # fit it to 1080 wide, and pad to 1080x1920 with black bars that hold the headline and captions.
    # Other frames: the centre of the picture cropped to that shape, scaled to fill it.
    # ponytail: plain center crop; subject tracking is the upgrade if the action gets cut off
    vf = (f"crop='min(iw,ih*{aspect})':ih,scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1" if vertical
          else f"crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})',scale={w}:{h}:flags=lanczos,setsar=1")
    if words or headline or (_default_end() if end_text is None else end_text).strip() or config.WATERMARK.strip():
        sub = out.with_suffix(".ass")
        sub.write_text(ass(words, start, end, canvas, headline, aspect=aspect, end_text=end_text), encoding="utf-8")
        vf += f",subtitles={sub.name}"  # relative name + cwd avoids ffmpeg path escaping
    graph, logo_in = f"[0:v]{vf}[o]", []
    if config.LOGO and config.LOGO.exists():
        # Small logo top-right. Vertical with bars: in the top bar just above the video (clear of the headline and
        # YouTube's own top icons). Vertical without bars: just under the headline box (which sits at y 120-226),
        # so it never covers the headline. 16:9: in the video's top-right corner.
        bar = bar_height(aspect) if vertical else 0
        y = f"{bar}-h-24" if bar >= 200 else f"{max(bar, 230) if vertical else 0}+24"  # h = logo height
        logo_in = ["-i", str(config.LOGO.resolve())]
        graph = f"[0:v]{vf}[v];[1:v]scale={220 if vertical else 260 if canvas == '16:9' else 180}:-1[l];[v][l]overlay=W-w-24:{y}[o]"
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
    cmd = [config.FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
           "-i", str(src.resolve()), *logo_in, *vo_in, "-filter_complex", graph, "-map", "[o]", *amap,
           *encoder(), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
           "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out.name]
    # -progress writes "out_time_us=..." lines as it goes: that's the render's percentage for the UI
    p = subprocess.Popen(cmd, cwd=out.parent, stdout=subprocess.PIPE, text=True)
    total = max(end - start, 0.1) * 1e6
    for line in p.stdout:
        if progress and line.startswith("out_time_us=") and line[12:].strip().isdigit():
            progress(min(int(line[12:]) / total, 1.0))
    if p.wait():
        raise subprocess.CalledProcessError(p.returncode, cmd)


if __name__ == "__main__":
    a = ass([{"w": "no", "s": 10, "e": 10.4}, {"w": "way", "s": 10.4, "e": 11}, {"w": "{bro}", "s": 11, "e": 12},
             {"w": "what", "s": 12, "e": 13}], 10, 20)
    assert "0:00:00.00,0:00:02.00,Cap,NO WAY BRO" in a and "WHAT" in a and "PlayResY: 1920" in a, a
    assert "PlayResX: 1920" in ass([], 0, 1, vertical=False)
    assert "PlayResY: 1350" in ass([], 0, 1, "4:5") and "PlayResX: 1080" in ass([], 0, 1, "1:1")
    assert "Dialogue: 0,0:00:00.00,0:00:10.00,Top,COPS REMEMBER YOU" in ass([], 10, 20, headline="Cops remember you")
    assert bar_height(1) == 420 and bar_height(9 / 16) == 0
    a = ass([], 0, 1, headline="x", aspect=1)
    assert "Cap,Arial Black,86,&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,300" in a and ",8,157" in a, a  # inside the bars
    print("render ok")
