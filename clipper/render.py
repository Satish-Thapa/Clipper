import subprocess

from .config import FFMPEG

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginV
Style: Cap,Arial Black,86,&H00FFFFFF,&H00000000,&H80000000,1,6,2,2,520

[Events]
Format: Layer, Start, End, Style, Text
"""


def _t(sec):
    h, r = divmod(max(sec, 0), 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass(words, start, end, per_line=3):
    """Big centered captions, a few words at a time — the standard Shorts look."""
    ws = [w for w in words if start <= w["s"] < end]
    lines = []
    for i in range(0, len(ws), per_line):
        g = ws[i:i + per_line]
        text = " ".join(w["w"] for w in g).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{_t(g[0]['s'] - start)},{_t(g[-1]['e'] - start)},Cap,{text}")
    return ASS_HEAD + "\n".join(lines) + "\n"


def cut(src, out, start, end, vertical, words=()):
    vf = "scale=-2:1080"
    if vertical:
        # ponytail: plain center crop; facecam/subject tracking is the upgrade if gameplay gets cut off
        vf = "crop=ih*9/16:ih,scale=1080:1920"
        if words:
            sub = out.with_suffix(".ass")
            sub.write_text(ass(words, start, end))
            vf += f",subtitles={sub.name}"  # relative name + cwd avoids ffmpeg path escaping
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
                    "-i", str(src.resolve()), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name],
                   check=True, cwd=out.parent)


if __name__ == "__main__":
    a = ass([{"w": "no", "s": 10, "e": 10.4}, {"w": "way", "s": 10.4, "e": 11}, {"w": "{bro}", "s": 11, "e": 12},
             {"w": "what", "s": 12, "e": 13}], 10, 20)
    assert "0:00:00.00,0:00:02.00,Cap,NO WAY BRO" in a and "WHAT" in a, a
    print("render ok")
