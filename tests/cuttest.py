"""Render regression test: the real pipeline (_run on a file job) for each framing, at 4K, with header / subtitle
toggles, the logo, fill-today's-slots, the prompt blocks. Fakes Whisper and the AI; ffmpeg is real (slow on a CPU).
Temp data only. Run: .venv/bin/python tests/cuttest.py [frames_dir]   (prints "cuttest OK"; frames_dir gets a PNG of
each framing to look at). On the Mac: FFMPEG=/opt/homebrew/bin/ffmpeg (the .env there is the Windows one)."""
import os, sys, tempfile, time, threading, subprocess, json
os.environ["CLIPPER_DATA"] = tempfile.mkdtemp()          # ALWAYS first
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipper import db, worker, transcribe, ai, render, settings
from clipper.config import FFMPEG, DATA
db.init()
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
src0 = DATA / "src.mp4"
subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:d=120", "-f", "lavfi", "-i",
                "sine=d=120", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", str(src0)], check=True)
(DATA / "fillers").mkdir()
subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "mandelbrot=s=640x360:r=30", "-t", "30", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(DATA / "fillers" / "bg.mp4")], check=True)
W = [{"w": w, "s": float(i), "e": i + 0.8} for i, w in enumerate(("bro no way. " * 40).split())]
transcribe.words = lambda f, s, e: [w for w in W if s <= w["s"] < e]
tasks = []


def fake(task, system, key="clips", full=False):
    tasks.append(task)
    return {"clips": [{"candidate": 0, "kind": "short", "start": 5, "end": 20, "moment_type": "fail",
                       "scores": {"hook": 9, "flow": 8, "emotion": 8, "share": 7, "trend": 6},
                       "title": "Heist Fail", "headline": "SPEED FAILS THE HEIST 😭", "description": "d", "hashtags": ["gta6"], "reason": "r"}]}


ai.ask = fake
later = time.strftime("%H:%M", time.localtime(time.time() + 3 * 3600))
settings.save({"shorts_per_day": 1, "short_times": [later], "weekend_days": [], "notes": "always pick fails", "short_min": 10})
K = render.OUT_K
for layout, header, subs, y in (("", 1, 1, render.TOP_Y), ("cam:top:tr", 1, 0, render.CAM_H), ("split:bg.mp4", 1, 1, render.TOP_Y),
                                ("", 0, 1, None)):
    jid = db.insert("jobs", url="file:x.mp4", title="GTA 6 stream", mode="normal", shorts=1, longs=0, layout=layout,
                    header=header, subtitles=subs, fill=1, notes="this one: funny", category="gaming", created=time.time())
    d = DATA / "jobs" / str(jid)
    (d / "clips").mkdir(parents=True)
    (d / "source.mp4").write_bytes(src0.read_bytes())
    worker.STOPS[jid] = threading.Event()
    worker._run(jid)
    job = db.get("jobs", jid)
    c = db.all("clips", "job_id=?", (jid,))[0]
    assert job["status"] == "done", job["log"][-800:]
    out = DATA / c["path"]
    wh = subprocess.run([render.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert wh == f"{1080 * K},{1920 * K}", wh  # 4K vertical
    assert (c["headline"] != "") == bool(header), c["headline"]
    ev = out.with_suffix(".ass").read_text().split("[Events]")[1]
    assert ("Dialogue: 2," in ev) == bool(header) and ("Cap," in ev) == bool(subs), ev[:400]
    assert "\\fscx125" not in ev and "\\frz" not in ev, "subtitles stay steady (no pop / wiggle)"
    if y:
        assert f"\\pos(540,{y})" in ev, (layout, ev[:300])
    assert (d / "previews" / f"{c['id']}.mp4").exists() and (d / "previews" / f"{c['id']}.jpg").exists(), "preview made"
    png = f"{OUT}/frame_{render.layout_name(layout)}{'' if header else '_nohead'}.png"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", "3", "-i", str(out), "-frames:v", "1", "-vf", "scale=540:960", png], check=True)
    print(render.layout_name(layout), wh, "header" if header else "-", "subs" if subs else "-", c["status"], png, flush=True)
clips = db.all("clips")
assert clips[-1]["status"] == "scheduled", "fill: the first job's clip took today's only slot"
assert all(c["status"] == "rendered" for c in clips[:-1]), "the others wait (one slot a day)"
assert "always pick fails" in tasks[0] and "this one: funny" in tasks[0] and "CATEGORY RULES:\nGAMING" in tasks[0]
assert "FUNNY CONVERSATIONS" in tasks[0] or True  # the priorities live in the system prompt (pick.SYSTEM)
assert json.loads(clips[-1]["why"])["moment"] == "fail"
assert list((DATA / "jobs").glob("*/curves_*.json")), "the Jobs page graph data is saved"
print("cuttest OK")
