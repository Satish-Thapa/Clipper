import subprocess

from .config import FFMPEG

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Cap,Arial Black,{size},&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,{margin}
Style: Top,Arial Black,{top_size},&H00000000,&H0000E5FF,&H0000E5FF,1,3,14,0,8,{top_margin}

[Events]
Format: Layer, Start, End, Style, Text
"""


def _t(sec):
    h, r = divmod(max(sec, 0), 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass(words, start, end, vertical=True, headline="", per_line=3):
    """Headline in a yellow box at the top for the whole clip + big captions at the bottom, a few words at a time."""
    ws = [w for w in words if start <= w["s"] < end]
    lines = []
    for i in range(0, len(ws), per_line):
        g = ws[i:i + per_line]
        text = " ".join(w["w"] for w in g).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{_t(g[0]['s'] - start)},{_t(g[-1]['e'] - start)},Cap,{text}")
    if headline:
        text = headline.upper().replace("{", "").replace("}", "")
        lines.insert(0, f"Dialogue: 0,{_t(0)},{_t(end - start)},Top,{text}")
    # Shorts: captions sit above YouTube's bottom overlay (title/buttons), headline below the top bar.
    size = (dict(w=1080, h=1920, size=86, margin=380, top_size=78, top_margin=230) if vertical
            else dict(w=1920, h=1080, size=64, margin=70, top_size=60, top_margin=50))
    return ASS_HEAD.format(**size) + "\n".join(lines) + "\n"


def cut(src, out, start, end, vertical, words=(), headline=""):
    """words: burn these as captions; headline: text at the top. Pass ()/"" for none."""
    # ponytail: plain center crop; facecam/subject tracking is the upgrade if gameplay gets cut off
    vf = "crop=ih*9/16:ih,scale=1080:1920" if vertical else "scale=-2:1080"
    if words or headline:
        sub = out.with_suffix(".ass")
        sub.write_text(ass(words, start, end, vertical, headline))
        vf += f",subtitles={sub.name}"  # relative name + cwd avoids ffmpeg path escaping
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
                    "-i", str(src.resolve()), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name],
                   check=True, cwd=out.parent)


if __name__ == "__main__":
    a = ass([{"w": "no", "s": 10, "e": 10.4}, {"w": "way", "s": 10.4, "e": 11}, {"w": "{bro}", "s": 11, "e": 12},
             {"w": "what", "s": 12, "e": 13}], 10, 20)
    assert "0:00:00.00,0:00:02.00,Cap,NO WAY BRO" in a and "WHAT" in a and "PlayResY: 1920" in a, a
    assert "PlayResX: 1920" in ass([], 0, 1, vertical=False)
    assert "Dialogue: 0,0:00:00.00,0:00:10.00,Top,COPS REMEMBER YOU" in ass([], 10, 20, headline="Cops remember you")
    print("render ok")
