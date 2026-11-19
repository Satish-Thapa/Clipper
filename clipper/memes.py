"""Comic meme layouts for posts, drawn with Pillow (colour emoji work, speech bubbles need real shapes):

  dialogue  2-4 game frames stacked, each with a speech bubble: a little conversation between characters
            ("What are your plans for today?" / "Me and Big Smoke are going to buy some beer.")
  logic     one picture with a big HEADER at the top ("VICE CITY LOGIC:", "BIG SMOKE:"), an optional speech bubble,
            and the punchline at the bottom ("FALL 10 STORIES... GET UP, SWIM, AND CONTINUE LIKE NOTHING HAPPENED")

render(meme, out, handle, logo) -> a 1080x1350 JPEG (the feed's 4:5)."""
from pathlib import Path

from .config import DATA
from .nostalgia import TEXT_FONTS, _Writer, _wrap

W, H = 1080, 1350
BUBBLE_FONTS = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
                "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf", "/usr/share/fonts/truetype/msttcorefonts/arialbd.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def _cover(path, w, h):
    """The picture scaled and centre-cropped to fill w x h, slightly sharpened and graded."""
    from PIL import Image, ImageEnhance, ImageFilter
    im = Image.open(path).convert("RGB")
    k = max(w / im.width, h / im.height)
    im = im.resize((max(w, int(im.width * k + 0.5)), max(h, int(im.height * k + 0.5))), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    im = im.crop((x, y, x + w, y + h)).filter(ImageFilter.UnsharpMask(2, 60, 2))
    im = ImageEnhance.Contrast(im).enhance(1.05)
    return ImageEnhance.Color(im).enhance(1.08).convert("RGBA")


def bubble(img, text, x, y, max_w, side="left", size=40, tail=True):
    """A white speech bubble with a black outline and a tail pointing down toward the speaker. (x, y) = its top
    corner on `side`. -> its height."""
    from PIL import ImageDraw
    wr = _Writer(size, BUBBLE_FONTS, stroke=0)
    lines = _wrap(wr, text, max_w - 2 * int(size * 0.7))
    lh = int(size * 1.2)
    bw = int(max(wr.width(ln) for ln in lines) + 2 * size * 0.7) if lines else 0
    bh = lh * len(lines) + int(size * 0.9)
    left = x if side == "left" else x - bw
    d = ImageDraw.Draw(img)
    r = int(size * 0.8)
    box = (left, y, left + bw, y + bh)
    if tail:  # the tail first, so the bubble's body covers its base
        tx = left + bw * (0.3 if side == "left" else 0.7)
        pts = [(tx - size * 0.45, y + bh - 4), (tx + size * 0.45, y + bh - 4),
               (tx + (size * 0.2 if side == "left" else -size * 0.9), y + bh + size * 1.1)]
        d.polygon(pts, fill="white", outline="black", width=4)
    d.rounded_rectangle(box, r, fill="white", outline="black", width=4)
    if tail:  # hide the outline where the tail joins the body
        d.line([(tx - size * 0.42, y + bh - 2), (tx + size * 0.42, y + bh - 2)], fill="white", width=6)
    ty = y + int(size * 0.42)
    for ln in lines:
        wr.draw(img, left + (bw - wr.width(ln)) / 2, ty, ln, fill=(17, 17, 17))
        ty += lh
    return bh + (int(size * 1.1) if tail else 0)


def _shout(img, text, y, size, anchor="top", fill=(255, 255, 255)):
    """Big outlined meme text across the width (Impact, black outline), shrunk until it fits in 3 lines."""
    for s in (size, int(size * 0.88), int(size * 0.76), int(size * 0.66), int(size * 0.58)):
        wr = _Writer(s, TEXT_FONTS, stroke=max(3, s // 14))
        lines = _wrap(wr, text.upper(), W - 70)
        if len(lines) <= 3:
            break
    lh = int(s * 1.08)
    y0 = y if anchor == "top" else y - lh * len(lines)
    for k, ln in enumerate(lines):
        wr.draw(img, (W - wr.width(ln)) / 2, y0 + k * lh, ln, fill=fill)
    return lh * len(lines)


def render_dialogue(meme, out):
    from PIL import ImageDraw
    panels = [p for p in meme["panels"] if p.get("file") and (DATA / p["file"]).exists()][:4]
    if not panels:
        raise RuntimeError("no picture found for the meme")
    from PIL import Image
    img = Image.new("RGBA", (W, H), "white")
    gap, n = 8, len(panels)
    ph = (H - gap * (n - 1)) // n
    size = 44 if n <= 2 else 38 if n == 3 else 34
    for k, p in enumerate(panels):
        y = k * (ph + gap)
        img.alpha_composite(_cover(DATA / p["file"], W, ph), (0, y))
        lines = [ln for ln in (p.get("lines") or []) if ln.get("text")][:2]
        for j, ln in enumerate(lines):  # one bubble: alternate sides down the strip; two: left then right
            side = ("left" if k % 2 == 0 else "right") if len(lines) == 1 else ("left" if j == 0 else "right")
            text = f"{ln['who']}: {ln['text']}" if ln.get("who") and meme.get("names") else ln["text"]
            bubble(img, text, 34 if side == "left" else W - 34, y + 26, int(W * 0.56), side, size)
    ImageDraw.Draw(img)
    return img


def render_logic(meme, out):
    from PIL import Image, ImageDraw
    p = next((p for p in meme["panels"] if p.get("file") and (DATA / p["file"]).exists()), None)
    if not p:
        raise RuntimeError("no picture found for the meme")
    img = _cover(DATA / p["file"], W, H)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # dark fades behind the top and bottom text
    d = ImageDraw.Draw(shade)
    for i in range(300):
        a = int(150 * (1 - i / 300) ** 1.6)
        d.line([(0, i), (W, i)], fill=(0, 0, 0, a))
        d.line([(0, H - 1 - i), (W, H - 1 - i)], fill=(0, 0, 0, a))
    img.alpha_composite(shade)
    top = 30
    if meme.get("header"):
        top += _shout(img, meme["header"], 30, 92, fill=(255, 214, 10)) + 24
    if meme.get("bubble"):
        bubble(img, meme["bubble"], W - 40, top + 10, int(W * 0.5), "right", 46)
    if meme.get("bottom"):
        _shout(img, meme["bottom"], H - 64, 86, anchor="bottom")  # clear of the handle in the corner
    return img


def render(meme, out, handle="", logo=None):
    from PIL import Image
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img = render_dialogue(meme, out) if meme["layout"] == "dialogue" else render_logic(meme, out)
    if logo and Path(logo).exists():
        lg = Image.open(logo).convert("RGBA")
        lg = lg.resize((110, max(1, int(lg.height * 110 / lg.width))), Image.LANCZOS)
        img.alpha_composite(lg, (W - lg.width - 24, H - lg.height - 60))
    if handle:
        wr = _Writer(28, BUBBLE_FONTS, stroke=2)
        wr.draw(img, W - wr.width(handle) - 24, H - 44, handle)
    img.convert("RGB").save(out, quality=92)
    return out
