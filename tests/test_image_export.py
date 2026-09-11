"""Report-copy and public-copy re-encoding budgets."""

from __future__ import annotations

import pathlib

import pytest
from PIL import Image

from wechat_daily import config, image_export


def _screenshot(path: pathlib.Path, size=(1600, 900)) -> pathlib.Path:
    """White page with a few rows of text-like bars — screenshot-shaped.

    Real screenshots are mostly flat background with sparse dark glyphs; a
    dense mark on every cell would be far harder to compress than anything
    the pipeline actually sees.
    """
    im = Image.new("RGB", size, (255, 255, 255))
    for row in range(12):
        y = 40 + row * 30
        for x in range(60, 60 + 900, 14):
            im.paste((30, 30, 30), (x, y, x + 8, y + 14))
    im.save(path, "JPEG", quality=95)
    return path


def _photo(path: pathlib.Path, size=(1600, 900)) -> pathlib.Path:
    """Every channel swept across its full range — photo-shaped histogram."""
    im = Image.new("RGB", size)
    px = im.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = ((x * 7) % 256, (y * 11) % 256, (x * y) % 256)
    im.save(path, "JPEG", quality=95)
    return path


def test_small_image_passes_through_untouched(tmp_path):
    src = tmp_path / "small.jpg"
    Image.new("RGB", (400, 300), (128, 128, 128)).save(src, "JPEG")
    dst = tmp_path / "out.jpg"
    assert image_export.fit_for_report(src, dst) == dst
    assert dst.read_bytes() == src.read_bytes()


def test_oversized_image_is_squeezed_under_the_cap(tmp_path):
    src = _photo(tmp_path / "big.jpg")
    assert src.stat().st_size > image_export._REPORT_MAX_BYTES
    dst = tmp_path / "out.jpg"
    assert image_export.fit_for_report(src, dst) == dst
    assert dst.stat().st_size <= image_export._REPORT_MAX_BYTES


def test_screenshot_and_photo_take_different_ladders(tmp_path):
    assert image_export._is_screenshot(Image.open(_screenshot(tmp_path / "s.jpg")))
    assert not image_export._is_screenshot(Image.open(_photo(tmp_path / "p.jpg")))


def test_unreadable_source_is_dropped(tmp_path):
    src = tmp_path / "broken.jpg"
    src.write_bytes(b"\xff\xd8" + b"\x00" * (image_export._REPORT_MAX_BYTES + 1))
    assert image_export.fit_for_report(src, tmp_path / "out.jpg") is None


def test_public_export_is_webp_within_the_cap(tmp_path):
    src = _screenshot(tmp_path / "s.jpg")
    dst = tmp_path / "pub" / "2026-09-02-01.webp"
    assert image_export.export_public(src, dst) == dst
    assert dst.stat().st_size <= config.PUBLIC_IMG_MAX_BYTES
    assert Image.open(dst).format == "WEBP"


def test_public_export_never_ships_over_the_cap(tmp_path, monkeypatch):
    """Unlike the PDF path there is no "ship it anyway" rung — git is forever."""
    monkeypatch.setattr(config, "PUBLIC_IMG_MAX_BYTES", 200)
    src = _photo(tmp_path / "p.jpg")
    dst = tmp_path / "pub" / "x.webp"
    assert image_export.export_public(src, dst) is None
    assert not dst.exists()


@pytest.mark.parametrize("long_edge,quality", config.PUBLIC_IMG_LADDER)
def test_public_ladder_rungs_stay_within_spec(long_edge, quality):
    assert 800 <= long_edge <= config.PUBLIC_IMG_LONG_EDGE
    assert 50 <= quality <= 90
