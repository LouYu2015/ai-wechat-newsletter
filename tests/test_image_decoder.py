"""Unit tests for image_decoder.py (no WeChat data / keys needed).

Covers the two behaviours added to stop wxgf images that decode to an all-white
first frame from producing spurious caption "skips":
  1. ``_reencode_jpeg`` rejects a uniform (blank) frame by returning None.
  2. ``_decode_uncached`` falls through ``.dat`` → ``_h.dat`` → ``_t.dat`` and
     keeps going when an earlier variant fails / decodes blank.
"""

from __future__ import annotations

import pathlib

from PIL import Image

from wechat_daily import image_decoder


def _bare_decoder(tmp_path: pathlib.Path) -> image_decoder.ImageDecoder:
    """An ImageDecoder with __init__ bypassed (no key/attach-dir discovery)."""
    dec = image_decoder.ImageDecoder.__new__(image_decoder.ImageDecoder)
    dec._tmpdir = tmp_path
    dec._cache = {}
    return dec


def test_reencode_rejects_blank_white(tmp_path):
    dec = _bare_decoder(tmp_path)
    blank = tmp_path / "blank.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(blank)
    assert dec._reencode_jpeg(blank, "md5blank") is None


def test_reencode_keeps_real_image(tmp_path):
    dec = _bare_decoder(tmp_path)
    src = tmp_path / "real.png"
    im = Image.new("RGB", (200, 200), (255, 255, 255))
    for x in range(0, 200, 4):  # stripes → non-zero stddev
        for y in range(200):
            im.putpixel((x, y), (0, 0, 0))
    im.save(src)
    out = dec._reencode_jpeg(src, "md5real")
    assert out is not None and out.exists()
    assert Image.open(out).size == (200, 200)


def _make_dat_tree(attach: pathlib.Path, md5: str, variants: tuple[str, ...]) -> None:
    img_dir = attach / "2026-06" / "Img"
    img_dir.mkdir(parents=True, exist_ok=True)
    for suf in variants:
        (img_dir / f"{md5}{suf}.dat").write_bytes(b"x")


def test_hd_variant_preferred_over_dat(tmp_path, monkeypatch):
    """`_h.dat` (real raster) is tried before the wxgf `.dat`."""
    attach = tmp_path / "attach"
    md5 = "deadbeef"
    _make_dat_tree(attach, md5, ("", "_h", "_t"))

    dec = _bare_decoder(tmp_path)
    dec._attach_dir = attach
    dec._aes_key = "k"

    tried: list[str] = []

    def fake_decode_one(dat_path: pathlib.Path, image_md5: str):
        tried.append(pathlib.Path(dat_path).name)
        if pathlib.Path(dat_path).name == f"{md5}_h.dat":  # HD → real content
            return tmp_path / f"{md5}.jpg"
        return None

    monkeypatch.setattr(dec, "_decode_one", fake_decode_one)
    result = dec.decode(md5)

    assert result == tmp_path / f"{md5}.jpg"
    # HD tried first; .dat / _t never reached once HD succeeds
    assert tried == [f"{md5}_h.dat"]


def test_fallback_dat_then_thumb_when_hd_blank(tmp_path, monkeypatch):
    """HD blank → standard `.dat`; if that's blank too → thumbnail."""
    attach = tmp_path / "attach"
    md5 = "cafef00d"
    _make_dat_tree(attach, md5, ("", "_h", "_t"))

    dec = _bare_decoder(tmp_path)
    dec._attach_dir = attach
    dec._aes_key = "k"

    tried: list[str] = []

    def fake_decode_one(dat_path: pathlib.Path, image_md5: str):
        tried.append(pathlib.Path(dat_path).name)
        if pathlib.Path(dat_path).name == f"{md5}_t.dat":
            return tmp_path / f"{md5}.jpg"
        return None  # HD + full both blank

    monkeypatch.setattr(dec, "_decode_one", fake_decode_one)
    assert dec.decode(md5) == tmp_path / f"{md5}.jpg"
    assert tried == [f"{md5}_h.dat", f"{md5}.dat", f"{md5}_t.dat"]


def test_dat_used_when_no_hd_variant(tmp_path, monkeypatch):
    """No `_h.dat` present → fall straight to the standard `.dat`."""
    attach = tmp_path / "attach"
    md5 = "12345678"
    _make_dat_tree(attach, md5, ("", "_t"))

    dec = _bare_decoder(tmp_path)
    dec._attach_dir = attach
    dec._aes_key = "k"

    tried: list[str] = []

    def fake_decode_one(dat_path: pathlib.Path, image_md5: str):
        tried.append(pathlib.Path(dat_path).name)
        return tmp_path / f"{md5}.jpg" if pathlib.Path(dat_path).name == f"{md5}.dat" else None

    monkeypatch.setattr(dec, "_decode_one", fake_decode_one)
    assert dec.decode(md5) == tmp_path / f"{md5}.jpg"
    assert tried == [f"{md5}.dat"]


def test_decode_memoized(tmp_path, monkeypatch):
    attach = tmp_path / "attach"
    md5 = "0a0a0a0a"
    _make_dat_tree(attach, md5, ("",))
    dec = _bare_decoder(tmp_path)
    dec._attach_dir = attach
    dec._aes_key = "k"

    calls: list[str] = []
    monkeypatch.setattr(
        dec,
        "_decode_one",
        lambda dat_path, image_md5: (calls.append(image_md5), tmp_path / "x.jpg")[1],
    )
    assert dec.decode(md5) == tmp_path / "x.jpg"
    assert dec.decode(md5) == tmp_path / "x.jpg"
    assert calls == [md5]  # second call served from cache
