"""Decode WeChat group-chat images for LLM consumption.

Pipeline per `image_md5`:
  1. find the `<md5>*.dat` variants under msg/attach/<group_hash>/YYYY-MM/Img/
  2. try them in order HD `_h.dat` → standard `.dat` → thumbnail `_t.dat`,
     decrypting each via vendored chatlog-mac/decode_image.py (V2 AES+XOR or
     legacy XOR)
  3. wxgf/HEVC → JPEG via ffmpeg first frame
  4. resize long edge ≤ 1568, re-encode JPEG q=85 → write to caller-supplied tmpdir
  5. a decode that fails *or comes out blank* falls through to the next variant

Why HD-first + blank guard: many `<md5>.dat` files are wxgf/HEVC, and ffmpeg's
first-frame extraction is unreliable — it can yield an all-white frame (no
error). Being a valid JPEG, that frame used to pre-empt the good variants, so
the captioner saw a blank image and the model replied "-" (a spurious skip).
The HD `_h.dat` is a plain PNG/JPEG raster with the real content, so it's tried
first; the blank guard (grayscale stddev ≈ 0) catches the remaining uniform
decodes. (A white frame with a black *border* has non-zero stddev, which is
exactly why ordering — not just the stddev guard — is needed.)

Outputs land in the TemporaryDirectory the caller passes in; this module never
writes to a persistent location. Each `decode()` call is memoized within the
instance so the same md5 isn't redecoded twice in one run.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Optional

from PIL import Image, ImageStat

from wechat_daily import config

# Vendored upstream decoder (chatlog-mac/decode_image.py)
sys.path.insert(0, str(config.CHATLOG_MAC_DIR))
import decode_image as _vendor_decode  # noqa: E402

_LONG_EDGE_LIMIT = 1568  # cap to avoid Anthropic's >20-image / 2000² rule
_JPEG_QUALITY = 85
# Decoded frames whose grayscale stddev is below this are uniform (blank) — a
# wxgf first-frame that came out all-white. Treat as a decode failure so the
# next variant is tried. Real screenshots (even mostly-white) sit far above 1.0;
# the observed blank frames measure exactly 0.0.
_BLANK_STDDEV = 1.0


class ImageDecoder:
    """Decode V2 .dat → JPEG, scoped to one daily report run."""

    def __init__(self, tmpdir: pathlib.Path) -> None:
        self._tmpdir = pathlib.Path(tmpdir)
        self._tmpdir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Optional[pathlib.Path]] = {}

        keys_path = config.CHATLOG_MAC_DIR / "image_keys.json"
        if keys_path.exists():
            data = json.loads(keys_path.read_text(encoding="utf-8"))
            self._aes_key = data.get("aes_key")
            self._xor_key = data.get("xor_key", 0x88)
        else:
            self._aes_key = None
            self._xor_key = 0x88

        import hashlib

        group_hash = hashlib.md5(config.GROUP_CHAT_ID.encode()).hexdigest()
        # Find <wxid>/msg/attach/<group_hash>; pick the most recently modified wxid dir
        self._attach_dir: Optional[pathlib.Path] = None
        if config.WECHAT_DATA_DIR.exists():
            wxids = sorted(
                (
                    p
                    for p in config.WECHAT_DATA_DIR.glob("wxid_*")
                    if (p / "msg" / "attach").is_dir()
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for wx in wxids:
                cand = wx / "msg" / "attach" / group_hash
                if cand.is_dir():
                    self._attach_dir = cand
                    break

    def decode(self, image_md5: str) -> Optional[pathlib.Path]:
        """Return path to a JPEG in tmpdir, or None if every fallback failed."""
        if image_md5 in self._cache:
            return self._cache[image_md5]
        result = self._decode_uncached(image_md5)
        self._cache[image_md5] = result
        return result

    def _decode_uncached(self, image_md5: str) -> Optional[pathlib.Path]:
        if not self._attach_dir or not self._aes_key:
            return None

        dats = sorted(self._attach_dir.glob(f"*/Img/{image_md5}*.dat"))
        if not dats:
            return None

        # Prefer the HD variant (_h.dat): it's a real PNG/JPEG raster. The
        # standard `.dat` is often wxgf/HEVC, whose ffmpeg first-frame extraction
        # is unreliable — it can yield an all-white frame (sometimes with a
        # border, so the blank-stddev guard alone can't catch it) that would
        # otherwise pre-empt the good variants. Order: _h → .dat → _t, each
        # gated by the blank check so a bad decode falls through to the next.
        full = next((d for d in dats if d.name == f"{image_md5}.dat"), None)
        hd = next((d for d in dats if d.name == f"{image_md5}_h.dat"), None)
        thumb = next((d for d in dats if d.name == f"{image_md5}_t.dat"), None)

        for src in (hd, full, thumb):
            if src is None:
                continue
            jpeg = self._decode_one(src, image_md5)
            if jpeg is not None:
                return jpeg
        return None

    def _decode_one(self, dat_path: pathlib.Path, image_md5: str) -> Optional[pathlib.Path]:
        decrypted_tmp = self._tmpdir / f"{image_md5}.raw.tmp"
        result_path, fmt = _vendor_decode.decrypt_dat_file(
            str(dat_path),
            str(decrypted_tmp),
            aes_key=self._aes_key,
            xor_key=self._xor_key,
        )
        if not result_path:
            return None

        decrypted = pathlib.Path(result_path)
        try:
            if fmt == "hevc":
                jpeg_path = self._wxgf_to_jpeg(decrypted, image_md5)
            else:
                jpeg_path = self._reencode_jpeg(decrypted, image_md5)
        finally:
            try:
                decrypted.unlink(missing_ok=True)
            except OSError:
                pass
        return jpeg_path

    def _wxgf_to_jpeg(self, hevc_path: pathlib.Path, image_md5: str) -> Optional[pathlib.Path]:
        """Strip wxgf header, take first HEVC frame via ffmpeg, then re-encode JPEG."""
        data = hevc_path.read_bytes()
        idx = data.find(b"\x00\x00\x00\x01")
        if idx < 0:
            return None
        raw_hevc = self._tmpdir / f"{image_md5}.hevc"
        raw_hevc.write_bytes(data[idx:])
        first_frame = self._tmpdir / f"{image_md5}.frame.jpg"
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(raw_hevc),
                "-frames:v",
                "1",
                str(first_frame),
            ],
            capture_output=True,
        )
        try:
            raw_hevc.unlink(missing_ok=True)
        except OSError:
            pass
        if proc.returncode != 0 or not first_frame.exists():
            return None
        try:
            return self._reencode_jpeg(first_frame, image_md5, src_is_tmp=True)
        finally:
            try:
                first_frame.unlink(missing_ok=True)
            except OSError:
                pass

    def _reencode_jpeg(
        self,
        src: pathlib.Path,
        image_md5: str,
        src_is_tmp: bool = False,
    ) -> Optional[pathlib.Path]:
        """Resize to long edge ≤ 1568, save as JPEG q=85."""
        try:
            im = Image.open(src)
            im.load()
        except Exception:
            return None

        # Animated GIF / multi-frame: PIL gives first frame on .convert
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        # Reject uniform (blank) frames — a wxgf first-frame that decoded to
        # all-white. Returning None lets the caller fall back to _h/_t.
        gray = im if im.mode == "L" else im.convert("L")
        if ImageStat.Stat(gray).stddev[0] < _BLANK_STDDEV:
            return None

        w, h = im.size
        long_edge = max(w, h)
        if long_edge > _LONG_EDGE_LIMIT:
            scale = _LONG_EDGE_LIMIT / long_edge
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        out = self._tmpdir / f"{image_md5}.jpg"
        im.save(out, "JPEG", quality=_JPEG_QUALITY, optimize=True)
        return out
