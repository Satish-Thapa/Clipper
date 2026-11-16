import os
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
Style: Top,Arial Black,{top_size},&H00000000,&H0000E5FF,&H0000E5FF,1,3,14,0,8,{top_margin}
Style: EndBox,Arial Black,{end_size},&H00FFFFFF,&H002E2EFF,&H002E2EFF,1,3,22,0,5,0
Style: End,Arial Black,{end_sub},&H00FFFFFF,&H00000000,&H80000000,1,1,7,3,5,0
Style: EndSmall,Arial Black,{end_small},&H00FFFFFF,&H00000000,&H80000000,1,1,6,3,5,0
Style: EndHi,Arial Black,{end_hi},&H0000E5FF,&H00000000,&H80000000,1,1,7,3,5,0
Style: Hook,Arial Black,{hook_size},&H00FFFFFF,&H002E2EFF,&H002E2EFF,1,3,10,0,8,0

[Events]
Format: Layer, Start, End, Style, Text
"""


def _t(sec):
    h, r = divmod(max(sec, 0), 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


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
    """The line above the headline: FOLLOW_LINE from .env, else one of the built-in lines at random."""
    import random
    return (FOLLOW_LINE or random.choice(FOLLOW_LINES)).upper().replace("{", "").replace("}", "")


HOOK_H, HEAD_H = 62, 106  # follow line box / headline box heights (px) on a 1080x1920 canvas


def top_block(aspect=CROP_ASPECT):
    """Where the follow line and the headline go: (hook_y, headline_y, bottom). Centred in the top bar when it's
    tall enough, else from y=110 (under YouTube's top icons), on the video."""
    bar = bar_height(aspect)
    y = max(110, (bar - HOOK_H - 10 - HEAD_H) // 2)
    return y, y + HOOK_H + 10, y + HOOK_H + 10 + HEAD_H


def ass(words, start, end, vertical=True, headline="", per_line=3, aspect=CROP_ASPECT, hook=None):
    """Headline in a yellow box in the top black bar for the whole clip + big captions in the bottom black bar,
    a few words at a time. With no bars (aspect 9/16) they sit on the video instead."""
    ws = [w for w in words if start <= w["s"] < end]
    lines = []
    for i in range(0, len(ws), per_line):
        g = ws[i:i + per_line]
        text = " ".join(w["w"] for w in g).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{_t(g[0]['s'] - start)},{_t(g[-1]['e'] - start)},Cap,{text}")
    if headline:
        text = headline.upper().replace("{", "").replace("}", "")
        if vertical:  # follow line on top, then the headline under it
            hy, ty, _ = top_block(aspect)
            hook = follow_line() if hook is None else hook
            pulse = "".join(rf"\t({ms},{ms + 400},\fscx{105 if j % 2 == 0 else 100}\fscy{105 if j % 2 == 0 else 100})"
                            for j, ms in enumerate(range(1200, min(int((end - start) * 1000), 60000), 1600)))
            if hook:
                lines.insert(0, f"Dialogue: 1,{_t(0)},{_t(end - start)},Hook,"
                                + rf"{{\an8\pos(540,{hy})\fad(200,0)\fscx40\fscy40\t(0,220,\fscx108\fscy108)\t(220,360,\fscx100\fscy100){pulse}}}{hook}")
            lines.insert(0, f"Dialogue: 0,{_t(0)},{_t(end - start)},Top,{{\\an8\\pos(540,{ty if hook else hy})}}{text}")
        else:
            lines.insert(0, f"Dialogue: 0,{_t(0)},{_t(end - start)},Top,{text}")
    # Headline: centred in the top bar (box is ~106px tall), but below YouTube's top icons (~120px).
    # Captions: just under the video, so they stay above YouTube's title/channel overlay at the very bottom.
    bar = bar_height(aspect)
    size = (dict(w=1080, h=1920, size=86, margin=max(250, bar - 120), top_size=78, top_margin=max(120, (bar - 106) // 2),
                 end_size=120, end_sub=84, end_small=62, end_hi=76, hook_size=44)
            if vertical
            else dict(w=1920, h=1080, size=64, margin=70, top_size=60, top_margin=50, end_size=90, end_sub=64, end_small=56, end_hi=68,
                      hook_size=40))
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


@lru_cache(maxsize=1)
def encoder():
    """NVIDIA GPU encoder (NVENC) if a 1-frame test encode works, else x264 on the CPU."""
    test = [FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
            "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        if subprocess.run(test, capture_output=True, timeout=30).returncode == 0:
            db.syslog("render: GPU encoder (h264_nvenc)")
            return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                    "-maxrate", "18M", "-bufsize", "36M", "-profile:v", "high"]
    except (OSError, subprocess.SubprocessError):
        pass
    db.syslog("render: CPU encoder (libx264)")
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-maxrate", "18M", "-bufsize", "36M", "-profile:v", "high"]
    # ^ 18 Mbps cap: Instagram rejects Reels over 25 Mbps, and 1080x1920 looks no better above ~15


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


def encoder_name():
    return "GPU NVENC" if "h264_nvenc" in encoder() else "CPU x264"


def cut(src, out, start, end, vertical, words=(), headline="", voice=(), aspect=CROP_ASPECT):
    """words: burn these as captions; headline: text at the top. Pass ()/"" for none.
    voice: [(wav_path, seconds into clip)] voice-over lines; the original audio is ducked under them.
    aspect: w/h of the kept slice of the frame (9/16 = fills the phone ... 16/9 = whole frame with bars)."""
    aspect = min(max(float(aspect or CROP_ASPECT), 9 / 16), 16 / 9)
    # Vertical: keep a centred `aspect` slice of the frame (wider than a full 9:16 crop = more gameplay),
    # fit it to 1080 wide, and pad to 1080x1920 with black bars that hold the headline and captions.
    # ponytail: plain center crop; facecam/subject tracking is the upgrade if gameplay gets cut off
    vf = (f"crop='min(iw,ih*{aspect})':ih,scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1" if vertical else "scale=-2:1080")
    if words or headline or END_CARD.strip():
        sub = out.with_suffix(".ass")
        sub.write_text(ass(words, start, end, vertical, headline, aspect=aspect))
        vf += f",subtitles={sub.name}"  # relative name + cwd avoids ffmpeg path escaping
    graph, logo_in = f"[0:v]{vf}[o]", []
    if LOGO and LOGO.exists():
        # Small logo top-right. Vertical with bars: in the top bar just above the video (clear of the headline and
        # YouTube's own top icons). Vertical without bars: just under the headline box (which sits at y 120-226),
        # so it never covers the headline. 16:9: in the video's top-right corner.
        bar = bar_height(aspect) if vertical else 0
        below = top_block(aspect)[2] if headline else 230  # bottom of the follow line + headline block
        y = f"{bar}-h-24" if bar - below >= 110 else f"{max(bar, below) if vertical else 0}+24"  # h = logo height
        logo_in = ["-i", str(LOGO.resolve())]
        graph = f"[0:v]{vf}[v];[1:v]scale={220 if vertical else 260}:-1[l];[v][l]overlay=W-w-24:{y}[o]"
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
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
                    "-i", str(src.resolve()), *logo_in, *vo_in, "-filter_complex", graph, "-map", "[o]", *amap,
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
Style: Hook,Arial Black,40,&H00FFFFFF,&H00FFFFFF,&H002E2EFF,&H002E2EFF,1,0,0,0,100,100,1,0,3,10,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _clean(t):
    return " ".join(str(t).replace("{", "(").replace("}", ")").replace("\\", "/").split())


# Where things go: a 1080x1920 video (Reel/Short) or a 1080x1350 photo (Instagram's portrait size).
NEWS_LAYOUT = {1920: dict(hook=150, badge=250, pic=(330, 540), head=(930, 560), src=1856, facts=3),
               1350: dict(hook=122, badge=205, pic=(265, 360), head=(650, 360), src=1300, facts=2)}  # hook under the logo


def news_ass(post, source, age_h, length, picture, h=1920, hook=None):
    """Follow line, then the badge pops in, headline lines slide up one by one, then each key fact, source line at the
    bottom. h=1350: the photo layout (fewer facts)."""
    import textwrap
    L = NEWS_LAYOUT[h]
    ev = lambda layer, a, b, style, text: f"Dialogue: {layer},{_t(a)},{_t(b)},{style},,0,0,0,,{text}"
    out, end = [], length
    hook = follow_line() if hook is None else hook
    if hook:
        out.append(ev(2, 0, end, "Hook", rf"{{\an5\pos(540,{L['hook']})\fad(200,0)}} {_clean(hook)} "))
    out.append(ev(2, 0.1, end, "Badge", rf"{{\an5\pos(540,{L['badge']})\fscx30\fscy30\t(0,220,\fscx112\fscy112)\t(220,360,\fscx100\fscy100)}}"
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


def news_video(out, post, source, age_h, picture=None, voice=None):
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
    sub.write_text(news_ass(post, source, age_h, length, bool(picture)), encoding="utf-8")
    return _news_render(out, sub, length, picture, voice, 1920)


def news_image(out, post, source, age_h, picture=None):
    """The same post as one 1080x1350 photo (Facebook / Instagram feed): the last frame, everything shown."""
    sub = out.with_suffix(".ass")
    sub.write_text(news_ass(post, source, age_h, 10, bool(picture), h=1350), encoding="utf-8")
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
    assert r"Top,{\an8\pos(540,182)}COPS REMEMBER YOU" in a and "Hook," in a and "}FOLLOW NOW" in a, a  # hook above
    assert r"Top,{\an8\pos(540,110)}X" in ass([], 0, 9, headline="x", hook="")  # no hook: headline on top
    a = ass([], 0, 9, headline="x")
    assert any(x in a for x in FOLLOW_LINES)  # random built-in line
    assert bar_height(1) == 420 and bar_height(9 / 16) == 0
    a = ass([], 0, 1, headline="x", aspect=1)
    assert "Cap,Arial Black,86,&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,300" in a and ",8,157" in a, a  # inside the bars
    print("render ok")
