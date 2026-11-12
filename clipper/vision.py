"""Is a video mostly gameplay / scenes, or mostly a person on camera? Judged from YouTube's storyboard
(the small preview frames shown when you hover the seek bar), so nothing is downloaded but a few images.

A frame counts as "person" only when its BIGGEST face covers PERSON_FACE of the frame or more: a close-up that fills
the screen. Small faces never count, however many there are: a creator's facecam in a corner, a creator in a box over
footage, or a crowd / group of people who ARE the content (IRL, challenges, pranks). The ~30 frames are spread evenly
over the video, so "more than MAX_PERSON of the frames" = a face fills the screen for that share of the runtime;
a short close-up moment doesn't get a video rejected.

Measured (share of frames with a face >= 3%): gameplay 0%, GTA RP with facecam 0-4%, group/IRL videos 3-11%,
creator-in-a-box commentary 16-27%, close-up podcast 68%. Wide studio podcasts have small faces and pass here;
DeepSeek's text check (pick.clippable) is what skips those."""
from pathlib import Path

import httpx
import numpy as np
import yt_dlp

MODEL = Path(__file__).parent / "assets" / "face_detection_yunet_2023mar.onnx"  # OpenCV YuNet, MIT licence
PERSON_FACE = 0.03  # biggest face box / frame area for "fills the screen"; facecams and group shots are ~0.5-2%
MAX_PERSON = 0.4  # topics skip a video when a face fills the screen in more than this share of it
MAX_STATIC = 0.6  # ... or when this share of its frames don't change (still image, audio podcast, slideshow)
FRAMES = 30  # frames sampled across the video
STILL_DIFF = 4  # mean pixel difference (0-255) below which two sampled frames count as "the same picture"


def tiles(img, rows, cols, w, h):
    """Split one storyboard mosaic into its frames, dropping the black padding tiles at the very end."""
    out = []
    for r in range(rows):
        for c in range(cols):
            t = img[r * h:(r + 1) * h, c * w:(c + 1) * w]
            if t.shape[0] == h and t.shape[1] == w and t.mean() > 8:
                out.append(t)
    return out


def frames(url, n=FRAMES):
    """~n storyboard frames spread evenly over the video ([] if YouTube has no storyboard for it)."""
    import cv2
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "no_warnings": True}) as y:
        info = y.extract_info(url, download=False)
    sbs = [f for f in info.get("formats", []) if f.get("format_id", "").startswith("sb") and f.get("fragments")]
    if not sbs:
        return []
    sb = max(sbs, key=lambda f: f.get("width") or 0)  # sb0: 320x180 frames, 3x3 per image
    frags, per = sb["fragments"], (sb.get("rows") or 1) * (sb.get("columns") or 1)
    want = min(len(frags), max(1, -(-n // per)))  # images needed for ~n frames
    out = []
    for i in np.linspace(0, len(frags) - 1, want).round().astype(int):
        r = httpx.get(frags[i]["url"], timeout=20, follow_redirects=True)
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR) if r.is_success else None
        if img is not None:
            out += tiles(img, sb.get("rows") or 1, sb.get("columns") or 1, sb["width"], sb["height"])
    return out


def biggest_face(frame, det):
    """Area of the biggest face as a share of the frame (0 = none). Only the biggest one matters: how many people
    are in the shot doesn't. Frames are upscaled 2x first: storyboard frames are tiny and YuNet misses faces < ~10px."""
    import cv2
    big = cv2.resize(frame, (frame.shape[1] * 2, frame.shape[0] * 2))
    h, w = big.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(big)
    return 0.0 if faces is None else float(max(fw * fh for fw, fh in faces[:, 2:4])) / (w * h)


def is_person(frame, det):
    """True when a face fills the screen (close-up / talking head), not for small faces or groups."""
    return biggest_face(frame, det) >= PERSON_FACE


def static_share(fs):
    """Fraction of neighbouring sampled frames that look the same (a still image / audio podcast / slideshow)."""
    pairs = list(zip(fs, fs[1:]))
    return sum(np.abs(a.astype(np.int16) - b).mean() < STILL_DIFF for a, b in pairs) / len(pairs) if pairs else 0.0


def analyze(url):
    """-> {"person": share of frames where a person fills the screen, "static": share of unchanging frames,
    "frames": how many were checked}. All 0 if YouTube has no storyboard for the video."""
    import cv2
    fs = frames(url)
    if not fs:
        return {"person": 0.0, "static": 0.0, "frames": 0}
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (fs[0].shape[1] * 2, fs[0].shape[0] * 2), 0.6)
    return {"person": sum(is_person(f, det) for f in fs) / len(fs), "static": static_share(fs),
            "frames": len(fs)}


if __name__ == "__main__":
    import cv2
    mosaic = np.zeros((360, 960, 3), np.uint8)
    mosaic[:180, :320] = 128  # 1 bright tile + 5 black padding tiles in a 2x3 grid of 320x180
    assert len(tiles(mosaic, 2, 3, 320, 180)) == 1
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (640, 360), 0.6)
    assert not is_person(np.full((180, 320, 3), 90, np.uint8), det)  # flat grey: no face
    assert biggest_face(np.full((180, 320, 3), 90, np.uint8), det) == 0.0
    g, noise = np.full((180, 320, 3), 90, np.uint8), np.random.default_rng(0).integers(0, 255, (180, 320, 3), np.uint8)
    assert static_share([g, g, g]) == 1.0 and static_share([g, noise, g]) == 0.0
    print("vision ok")
