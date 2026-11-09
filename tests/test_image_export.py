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


def _jobs(tmp_path, *srcs):
    return {
        f"id{i}": (src, tmp_path / "pub" / f"2026-09-02-{i:02d}.webp")
        for i, src in enumerate(srcs, start=1)
    }


def test_public_export_is_webp_at_top_rung_when_budget_allows(tmp_path):
    jobs = _jobs(tmp_path, _screenshot(tmp_path / "s.jpg"))
    written = image_export.export_public_day(jobs)
    dst = written["id1"]
    assert dst.stat().st_size <= config.PUBLIC_IMG_DAY_BUDGET
    with Image.open(dst) as im:
        assert im.format == "WEBP"
        assert max(im.size) == config.PUBLIC_IMG_LADDER[0][0]


def test_public_export_squeezes_quality_to_keep_every_image(tmp_path):
    """A budget too small for all-top-rung still ships every image, smaller."""
    srcs = [_photo(tmp_path / f"p{i}.jpg") for i in range(3)]
    tops = [len(image_export._webp_rungs(Image.open(s).convert("RGB"))[0][1]) for s in srcs]
    floors = [len(image_export._webp_rungs(Image.open(s).convert("RGB"))[-1][1]) for s in srcs]
    budget = (sum(tops) + sum(floors)) // 2
    written = image_export.export_public_day(_jobs(tmp_path, *srcs), budget)
    assert len(written) == 3
    assert sum(p.stat().st_size for p in written.values()) <= budget


def test_public_export_drops_the_biggest_to_keep_the_most(tmp_path):
    """When even the smallest rungs overshoot, the largest image goes first."""
    small = _screenshot(tmp_path / "s.jpg")
    big = _photo(tmp_path / "p.jpg")
    floor = len(image_export._webp_rungs(Image.open(small).convert("RGB"))[-1][1])
    written = image_export.export_public_day(_jobs(tmp_path, big, small), floor)
    assert set(written) == {"id2"}
    assert not (tmp_path / "pub" / "2026-09-02-01.webp").exists()


def test_public_export_never_ships_over_the_budget(tmp_path):
    """Unlike the PDF path there is no "ship it anyway" rung — git is forever."""
    jobs = _jobs(tmp_path, _photo(tmp_path / "p.jpg"))
    assert image_export.export_public_day(jobs, 200) == {}
    assert not jobs["id1"][1].exists()


@pytest.mark.parametrize("long_edge,quality", config.PUBLIC_IMG_LADDER)
def test_public_ladder_rungs_stay_within_spec(long_edge, quality):
    assert 800 <= long_edge <= config.PUBLIC_IMG_LONG_EDGE
    assert 50 <= quality <= 90
