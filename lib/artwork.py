"""Design files: load, analyse (tone, transparency, size) and place on the print canvas.

Mayzing recommends 4200 x 4800 px @ 300 dpi for raster artwork. Bulk clone puts the new
artwork where the origin campaign's artwork sits, so every file of a batch gets the same
canvas: the design is trimmed to its visible pixels, fitted to ARTWORK_WIDTH_FRACTION of
the canvas width (never taller than the canvas), centred and placed at the top.
A file that already has the canvas size is used pixel for pixel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

import config

Image.MAX_IMAGE_PIXELS = 23000 * 23000   # Mayzing's raster limit


@dataclass
class Analysis:
    width: int = 0
    height: int = 0
    transparent_share: float = 0.0
    tone: str = "mixed"            # dark | light | mixed (alpha-weighted luminance)
    luminance: float = 0.0
    flags: list[str] = field(default_factory=list)
    enlarge: float = 1.0           # canvas factor (> 1 = the design was enlarged)


def load_rgba(path: Path) -> Image.Image:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGBA")


def transparent_share(im: Image.Image) -> float:
    a = np.asarray(im.getchannel("A"))
    return float((a < 250).mean()) if a.size else 0.0


def design_tone(im: Image.Image) -> tuple[str, float]:
    """(tone, luminance) of the visible pixels; an opaque file is judged on all pixels."""
    small = im.copy()
    small.thumbnail((600, 600))
    arr = np.asarray(small).astype(np.float32)
    rgb, a = arr[..., :3], arr[..., 3] / 255.0
    lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    w = a.sum()
    mean = float((lum * a).sum() / w) if w > 0 else 0.0
    if mean <= config.DESIGN_TONE_DARK_MAX:
        return "dark", mean
    if mean >= config.DESIGN_TONE_LIGHT_MIN:
        return "light", mean
    return "mixed", mean


def trim(im: Image.Image) -> Image.Image:
    alpha = np.asarray(im.getchannel("A"))
    ys, xs = np.nonzero(alpha >= config.ARTWORK_TRIM_ALPHA)
    if not len(xs):
        return im
    box = (max(0, int(xs.min()) - 2), max(0, int(ys.min()) - 2),
           min(im.width, int(xs.max()) + 3), min(im.height, int(ys.max()) + 3))
    return im if box == (0, 0, im.width, im.height) else im.crop(box)


def fit_size(size: tuple[int, int]) -> tuple[int, int]:
    cw, ch = config.ARTWORK_CANVAS_SIZE
    w, h = size
    scale = (cw * config.ARTWORK_WIDTH_FRACTION) / w
    max_h = ch * (1 - config.ARTWORK_TOP_FRACTION - config.ARTWORK_BOTTOM_FRACTION)
    if h * scale > max_h:
        scale = max_h / h
    return max(1, round(w * scale)), max(1, round(h * scale))


def is_canvas(size: tuple[int, int]) -> bool:
    return tuple(size) == tuple(config.ARTWORK_CANVAS_SIZE)


def place_on_canvas(im: Image.Image) -> tuple[Image.Image, float]:
    """(canvas image, enlarge factor)."""
    if is_canvas(im.size):
        return im, 1.0
    cw, ch = config.ARTWORK_CANVAS_SIZE
    im = trim(im)
    new_size = fit_size(im.size)
    factor = new_size[0] / im.width
    if new_size != im.size:
        im = im.resize(new_size, Image.LANCZOS if factor < 1 else Image.BICUBIC)
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    canvas.alpha_composite(im, ((cw - new_size[0]) // 2, round(ch * config.ARTWORK_TOP_FRACTION)))
    return canvas, factor


def prepare(src: Path, out_png: Path, thumb_png: Path) -> Analysis:
    """Analyse `src`, write the print file and a preview. Returns the analysis + flags."""
    im = load_rgba(src)
    a = Analysis(width=im.width, height=im.height)
    a.transparent_share = transparent_share(im)
    a.tone, a.luminance = design_tone(im)
    if a.transparent_share >= 0.999:
        a.flags.append("NO_DESIGN")
    if a.transparent_share < config.MIN_TRANSPARENT_SHARE:
        a.flags.append("OPAQUE")

    if config.ARTWORK_MODE == "original":
        out = im
    else:
        out, a.enlarge = place_on_canvas(im)
        if is_canvas(im.size):
            a.flags.append("PRINT_READY")
        if a.enlarge >= config.ENLARGE_BAD_FACTOR:
            a.flags.append(f"TOO_SMALL:x{a.enlarge:.1f}")
        elif a.enlarge >= config.ENLARGE_WARN_FACTOR:
            a.flags.append(f"UPSCALE:x{a.enlarge:.1f}")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_png, format="PNG", dpi=(config.OUTPUT_DPI, config.OUTPUT_DPI), optimize=False)
    if out_png.stat().st_size > config.MAX_UPLOAD_BYTES:
        a.flags.append("FILE_TOO_BIG")
    thumb = out.copy()
    thumb.thumbnail((config.THUMB_SIZE, config.THUMB_SIZE), Image.LANCZOS)
    thumb_png.parent.mkdir(parents=True, exist_ok=True)
    thumb.save(thumb_png, format="PNG")
    return a


def readable_copy(src: Path, tone: str) -> Image.Image:
    """RGB image for the OCR model: a transparent design flattened onto a background that
    contrasts with it (dark art -> white, light art -> black, mixed -> grey)."""
    im = load_rgba(src)
    rgb = {"dark": (255, 255, 255), "light": (0, 0, 0)}.get(tone, config.OCR_MIXED_BACKGROUND)
    bg = Image.new("RGBA", im.size, (*rgb, 255))
    flat = Image.alpha_composite(bg, im).convert("RGB")
    flat.thumbnail((config.VISION_MAX_SIDE, config.VISION_MAX_SIDE), Image.LANCZOS)
    return flat
