import os
import shutil
import subprocess
import wave
from functools import lru_cache

from . import db
from .config import CROP_ASPECT, END_CARD, FFMPEG, LOGO, VOICE

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


def end_card(length, text=END_CARD, w=1080, h=1920):
    """Animated call to action for the last END_SECONDS: the first word ("FOLLOW") pops in on a red box with an
    overshoot and then pulses; the rest ("FOR MORE") slides up and fades in under it."""
    if not text.strip() or length < END_SECONDS * 3:
        return []
    first, _, rest = text.upper().replace("{", "").replace("}", "").strip().partition(" ")
    a, b, cx, cy = _t(length - END_SECONDS), _t(length), w // 2, h // 2
    pop = (rf"{{\an5\pos({cx},{cy - 70})\fad(100,180)\fscx20\fscy20\t(0,220,\fscx118\fscy118)\t(220,380,\fscx100\fscy100)"
           r"\t(700,1000,\fscx108\fscy108)\t(1000,1300,\fscx100\fscy100)\t(1300,1600,\fscx108\fscy108)"
           r"\t(1600,1900,\fscx100\fscy100)}")
    out = [f"Dialogue: 2,{a},{b},EndBox,{pop}{first}"]
    if rest:
        out.append(f"Dialogue: 2,{a},{b},End," + rf"{{\an5\move({cx},{cy + 160},{cx},{cy + 90},250,550)\fad(450,180)}}" + rest)
    return out


def ass(words, start, end, vertical=True, headline="", per_line=3, aspect=CROP_ASPECT):
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
        lines.insert(0, f"Dialogue: 0,{_t(0)},{_t(end - start)},Top,{text}")
    # Headline: centred in the top bar (box is ~106px tall), but below YouTube's top icons (~120px).
    # Captions: just under the video, so they stay above YouTube's title/channel overlay at the very bottom.
    bar = bar_height(aspect)
    size = (dict(w=1080, h=1920, size=86, margin=max(250, bar - 120), top_size=78, top_margin=max(120, (bar - 106) // 2),
                 end_size=120, end_sub=84)
            if vertical
            else dict(w=1920, h=1080, size=64, margin=70, top_size=60, top_margin=50, end_size=90, end_sub=64))
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
                    "-maxrate", "25M", "-bufsize", "50M", "-profile:v", "high"]
    except (OSError, subprocess.SubprocessError):
        pass
    db.syslog("render: CPU encoder (libx264)")
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high"]


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
        y = f"{bar}-h-24" if bar >= 200 else f"{max(bar, 230) if vertical else 0}+24"  # h = logo height
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


if __name__ == "__main__":
    a = ass([{"w": "no", "s": 10, "e": 10.4}, {"w": "way", "s": 10.4, "e": 11}, {"w": "{bro}", "s": 11, "e": 12},
             {"w": "what", "s": 12, "e": 13}], 10, 20)
    assert "0:00:00.00,0:00:02.00,Cap,NO WAY BRO" in a and "WHAT" in a and "PlayResY: 1920" in a, a
    assert "PlayResX: 1920" in ass([], 0, 1, vertical=False)
    assert "Dialogue: 0,0:00:00.00,0:00:10.00,Top,COPS REMEMBER YOU" in ass([], 10, 20, headline="Cops remember you")
    assert bar_height(1) == 420 and bar_height(9 / 16) == 0
    a = ass([], 0, 1, headline="x", aspect=1)
    assert "Cap,Arial Black,86,&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,300" in a and ",8,157" in a, a  # inside the bars
    print("render ok")
