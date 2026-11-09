"""Re-encode decoded chat images down to what each output channel can afford.

`image_decoder` sizes images for the **model** (1568px / q85: enough to read the
fine print in a benchmark table, and what the batch input is billed on). What
ships in a report is a different consumer with a different budget, so the specs
live here and the decoder never learns that "public version" exists:

- `fit_for_report` — the group PDF. Roomy: the PDF is local and disposable, so
  the only real constraint is not bloating a 1MB file with 3 screenshots.
- `export_public` — the GitHub Pages repo. Tight, and the one that matters:
  every byte committed there is in git history permanently. WebP at the site's
  actual column width, all images of a day allocated one shared byte budget.
"""

from __future__ import annotations

import io
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


def _webp_rungs(im: Image.Image) -> list[tuple[int, bytes]]:
    """Encode every ladder rung, keeping only those smaller than all before.

    The ladder isn't monotone in bytes (downscaling can come out *larger*), so
    a rung that saves nothing over a higher-quality one is never worth taking.
    Returns ``(ladder_index, webp_bytes)`` from best quality down.
    """
    rungs: list[tuple[int, bytes]] = []
    for i, (long_edge, quality) in enumerate(config.PUBLIC_IMG_LADDER):
        buf = io.BytesIO()
        _scaled(im, long_edge).save(buf, "WEBP", quality=quality, method=6)
        data = buf.getvalue()
        if not rungs or len(data) < len(rungs[-1][1]):
            rungs.append((i, data))
    return rungs


def export_public_day(
    jobs: dict[str, tuple[pathlib.Path, pathlib.Path]],
    budget: Optional[int] = None,
) -> dict[str, pathlib.Path]:
    """Fit one day's public images (``id -> (src, dst)``) into a shared budget.

    Every rung of every image is encoded up front, then allocated jointly:

    1. Count first. If even the smallest rungs together overshoot, drop the
       image whose smallest rung is largest, until they fit — that keeps the
       most images.
    2. Then quality, kept as even as possible: start everyone at the top rung
       and, while over budget, step down the image on the highest-quality rung
       (the larger one on a tie).

    Unlike `fit_for_report` there is no "ship it anyway" rung: what lands here
    goes into git history and cannot be taken back, so the budget is absolute
    and a dropped image is simply not published (the public renderer then
    drops the reference and the prose carries the section alone). Returns the
    ids actually written.
    """
    if budget is None:
        budget = config.PUBLIC_IMG_DAY_BUDGET
    rungs: dict[str, list[tuple[int, bytes]]] = {}
    for img_id, (src, _) in jobs.items():
        im = _load_rgb(src)
        if im is not None:
            rungs[img_id] = _webp_rungs(im)

    while rungs and sum(len(r[-1][1]) for r in rungs.values()) > budget:
        img_id = max(rungs, key=lambda k: len(rungs[k][-1][1]))
        print(
            f"[warn] 公开版图片压到最小规格仍有 {len(rungs[img_id][-1][1]) / 1024:.0f}KB，"
            f"全天 {budget / 1024:.0f}KB 预算放不下，不发布该图：{jobs[img_id][1].name}",
            file=sys.stderr,
        )
        del rungs[img_id]

    pick = dict.fromkeys(rungs, 0)
    total = sum(len(r[0][1]) for r in rungs.values())
    while total > budget:
        img_id = min(
            (k for k in pick if pick[k] < len(rungs[k]) - 1),
            key=lambda k: (rungs[k][pick[k]][0], -len(rungs[k][pick[k]][1])),
        )
        total -= len(rungs[img_id][pick[img_id]][1])
        pick[img_id] += 1
        total += len(rungs[img_id][pick[img_id]][1])

    written: dict[str, pathlib.Path] = {}
    for img_id, i in pick.items():
        dst = jobs[img_id][1]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(rungs[img_id][i][1])
        written[img_id] = dst
    return written
