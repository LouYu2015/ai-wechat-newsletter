"""Re-encode decoded chat images down to what each output channel can afford.

`image_decoder` sizes images for the **model** (1568px / q85: enough to read the
fine print in a benchmark table, and what the batch input is billed on). What
ships in a report is a different consumer with a different budget, so the specs
live here and the decoder never learns that "public version" exists:

- `fit_for_report` — the group PDF. Roomy: the PDF is local and disposable, so
  the only real constraint is not bloating a 1MB file with 3 screenshots.
- `export_public` — the GitHub Pages repo. Tight, and the one that matters:
  every byte committed there is in git history permanently. WebP at the site's
  actual column width, with a per-day budget on top of the per-image cap.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Optional

from PIL import Image

from wechat_daily import config

# ── Group PDF ────────────────────────────────────────────────────────────────

# Display width: A4 210mm − 2×14mm margins = 182mm = 688 CSS px (`figure img`
# is max-width:100% in `pdf._get_pdf_css`). 1400px is ~2× that — retina-sharp
# when zoomed, nothing beyond. The byte cap is ~2× the observed median (104KB
# over a 48-image day), so a typical image passes untouched and only the tail
# gets squeezed.
_REPORT_LONG_EDGE = 1400
_REPORT_MAX_BYTES = 200_000
# Past this, drop the image rather than ship it: the prompt requires the prose
# to stand on its own, so a dropped picture costs the reader nothing.
_REPORT_HARD_BYTES = 300_000

# Screenshots (large flat areas of one background colour) show a tall grayscale
# histogram peak; photographs spread out. Measured: chat screenshots 0.50–0.56,
# photos < 0.05. Text suffers from JPEG ringing, so screenshots hold quality and
# give up pixels; photos do the reverse.
_SCREENSHOT_PEAK_SHARE = 0.2
_SCREENSHOT_LADDER = ((1400, 85), (1400, 78), (1200, 78), (1000, 75))
_PHOTO_LADDER = ((1400, 78), (1400, 68), (1200, 65), (1000, 60))


def _is_screenshot(im: Image.Image) -> bool:
    """True for screenshot-like images (one background colour dominates)."""
    gray = im if im.mode == "L" else im.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    return bool(total) and max(hist) / total >= _SCREENSHOT_PEAK_SHARE


def _load_rgb(src: pathlib.Path) -> Optional[Image.Image]:
    try:
        im = Image.open(src)
        im.load()
    except Exception:
        return None
    return im if im.mode in ("RGB", "L") else im.convert("RGB")


def _scaled(im: Image.Image, long_edge: int) -> Image.Image:
    w, h = im.size
    if max(w, h) <= long_edge:
        return im
    scale = long_edge / max(w, h)
    return im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def fit_for_report(src: pathlib.Path, dst: pathlib.Path) -> Optional[pathlib.Path]:
    """Re-encode one decoded image down to the group-PDF budget.

    Walks a (long_edge, quality) ladder until the result fits
    ``_REPORT_MAX_BYTES``, and returns ``None`` when even the last rung stays
    above ``_REPORT_HARD_BYTES`` — the caller then drops the reference. An
    image that already fits is copied through untouched.
    """
    if src.stat().st_size <= _REPORT_MAX_BYTES:
        dst.write_bytes(src.read_bytes())
        return dst

    im = _load_rgb(src)
    if im is None:
        return None

    ladder = _SCREENSHOT_LADDER if _is_screenshot(im) else _PHOTO_LADDER
    size: int | None = None
    for long_edge, quality in ladder:
        _scaled(im, long_edge).save(dst, "JPEG", quality=quality, optimize=True)
        size = dst.stat().st_size
        if size <= _REPORT_MAX_BYTES:
            return dst

    if size is not None and size <= _REPORT_HARD_BYTES:
        print(
            f"[warn] 图片压缩后仍有 {size / 1024:.0f}KB（超过 "
            f"{_REPORT_MAX_BYTES / 1024:.0f}KB 目标），照常放入群内版：{dst.name}",
            file=sys.stderr,
        )
        return dst
    print(
        f"[warn] 图片压缩后仍有 {(size or 0) / 1024:.0f}KB，超过硬上限 "
        f"{_REPORT_HARD_BYTES / 1024:.0f}KB，已放弃该图：{src.name}",
        file=sys.stderr,
    )
    dst.unlink(missing_ok=True)
    return None


# ── Public repo (git!) ───────────────────────────────────────────────────────


def export_public(src: pathlib.Path, dst: pathlib.Path) -> Optional[pathlib.Path]:
    """Write one image into the public repo as WebP, or ``None`` if it won't fit.

    Unlike `fit_for_report` there is no "ship it anyway" rung: what lands here
    goes into git history and cannot be taken back, so the cap is absolute and
    an image that misses it is simply not published (the public renderer then
    drops the reference and the prose carries the section alone).
    """
    im = _load_rgb(src)
    if im is None:
        return None

    dst.parent.mkdir(parents=True, exist_ok=True)
    size: int | None = None
    for long_edge, quality in config.PUBLIC_IMG_LADDER:
        _scaled(im, long_edge).save(dst, "WEBP", quality=quality, method=6)
        size = dst.stat().st_size
        if size <= config.PUBLIC_IMG_MAX_BYTES:
            return dst

    print(
        f"[warn] 公开版图片压到最小规格仍有 {(size or 0) / 1024:.0f}KB，超过 "
        f"{config.PUBLIC_IMG_MAX_BYTES / 1024:.0f}KB 上限，不发布该图：{dst.name}",
        file=sys.stderr,
    )
    dst.unlink(missing_ok=True)
    return None
