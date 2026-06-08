"""Unit tests for image_captioner.py (Gemini stubbed, no network)."""

from pathlib import Path

import pytest

from wechat_daily import image_captioner
from wechat_daily.image_captioner import caption_images, count_image_targets
from wechat_daily.message_parser import Message, MSG_IMAGE, MSG_TEXT
from wechat_daily.privacy import format_tokenized_messages


# ── Fakes ────────────────────────────────────────────────────────────────────────

class _FakeUsage:
    prompt_token_count = 100
    candidates_token_count = 20
    cached_content_token_count = 0


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage_metadata = _FakeUsage()


class _FakeModels:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[bytes] = []

    def generate_content(self, *, model, contents, config):
        # contents[0] is the image Part; its bytes == the md5 (see _FakeDecoder).
        data = contents[0].inline_data.data
        md5 = data.decode()
        self.calls.append(md5)
        val = self.mapping.get(md5, "默认描述")
        if val == "RAISE":  # simulate an API error / safety block for this image
            raise RuntimeError("gemini boom")
        return _FakeResp(val)


class _FakeClient:
    last: "_FakeClient | None" = None

    def __init__(self, *, api_key, http_options=None) -> None:
        self.models = _FakeModels(_FakeClient._mapping)
        _FakeClient.last = self


class _FakeDecoder:
    """Writes each md5's bytes as its 'jpeg' so the fake model can identify it."""

    def __init__(self, tmp: Path, missing: set[str] = frozenset()) -> None:
        self.tmp = tmp
        self.missing = set(missing)

    def decode(self, md5: str):
        if md5 in self.missing:
            return None
        p = self.tmp / f"{md5}.jpg"
        p.write_bytes(md5.encode())
        return p


@pytest.fixture
def patch_genai(monkeypatch):
    """Patch google.genai.Client; configure captions via the returned setter."""
    import google.genai as genai

    def configure(mapping: dict[str, str]):
        _FakeClient._mapping = mapping
        monkeypatch.setattr(genai, "Client", _FakeClient)

    return configure


def _img(md5: str, t: int = 0) -> Message:
    return Message(create_time=t, local_type=MSG_IMAGE, sender_wxid="A",
                   content="[图片]", image_md5=md5)


def _txt(text: str, t: int = 0) -> Message:
    return Message(create_time=t, local_type=MSG_TEXT, sender_wxid="A", content=text)


# ── Tests ────────────────────────────────────────────────────────────────────────

def test_count_and_dedup(patch_genai, tmp_path):
    patch_genai({"aaa": "猫图", "bbb": "狗图"})
    msgs = [_img("aaa"), _txt("聊点别的"), _img("aaa"), _img("bbb")]

    assert count_image_targets(msgs) == 2  # deduped by md5

    captions, stats = caption_images(msgs, _FakeDecoder(tmp_path), "key", max_workers=1)

    assert captions == {"aaa": "猫图", "bbb": "狗图"}
    assert stats.total == 2 and stats.captioned == 2
    # generate_content called once per unique md5, not per occurrence.
    assert sorted(_FakeClient.last.models.calls) == ["aaa", "bbb"]


def test_skip_token_not_injected(patch_genai, tmp_path):
    patch_genai({"aaa": "-", "bbb": "有效描述"})
    msgs = [_img("aaa"), _img("bbb")]

    captions, stats = caption_images(msgs, _FakeDecoder(tmp_path), "key", max_workers=1)

    assert "aaa" not in captions
    assert captions["bbb"] == "有效描述"
    assert stats.skipped == 1 and stats.captioned == 1


def test_failed_decode_counted(patch_genai, tmp_path):
    patch_genai({"bbb": "ok"})
    msgs = [_img("aaa"), _img("bbb")]
    decoder = _FakeDecoder(tmp_path, missing={"aaa"})

    captions, stats = caption_images(msgs, decoder, "key", max_workers=1)

    assert captions == {"bbb": "ok"}
    assert stats.failed == 1


def test_one_image_error_does_not_abort_batch(patch_genai, tmp_path):
    patch_genai({"aaa": "RAISE", "bbb": "幸存描述"})
    msgs = [_img("aaa"), _img("bbb")]

    captions, stats = caption_images(msgs, _FakeDecoder(tmp_path), "key", max_workers=1)

    assert captions == {"bbb": "幸存描述"}  # aaa errored out, bbb still captioned
    assert stats.failed == 1 and stats.captioned == 1


def test_usage_cb_fires_per_caption(patch_genai, tmp_path):
    patch_genai({"aaa": "x", "bbb": "y"})
    msgs = [_img("aaa"), _img("bbb")]
    seen = []

    caption_images(msgs, _FakeDecoder(tmp_path), "key", max_workers=1,
                   usage_cb=lambda u, d, c: seen.append((u, d, c)))

    assert len(seen) == 2
    usage, dur, chars = seen[0]
    assert usage["prompt_token_count"] == 100
    assert usage["candidates_token_count"] == 20
    assert chars > 0


def test_no_images_returns_empty(patch_genai, tmp_path):
    patch_genai({})
    captions, stats = caption_images([_txt("hi")], _FakeDecoder(tmp_path), "key")
    assert captions == {} and stats.total == 0


def test_progress_cb_fires(patch_genai, tmp_path):
    patch_genai({"aaa": "x", "bbb": "y"})
    msgs = [_img("aaa"), _img("bbb")]
    seen = []
    caption_images(msgs, _FakeDecoder(tmp_path), "key", max_workers=1,
                   progress_cb=lambda cur, tot, lbl: seen.append((cur, tot)))
    assert seen[-1] == (2, 2)


# ── Injection into the flat chat history (privacy.format_tokenized_messages) ──────

def test_caption_injection_replaces_placeholder():
    msgs = [_img("aaa")]
    out = format_tokenized_messages(msgs, {"aaa": "一张猫的照片"})
    assert "[图片：一张猫的照片]" in out
    assert "[图片]" not in out.replace("[图片：", "")  # only the captioned form


def test_no_captions_keeps_bare_placeholder():
    msgs = [_img("aaa")]
    assert "[图片]" in format_tokenized_messages(msgs)
    assert "：" not in format_tokenized_messages(msgs)


def test_uncaptioned_image_stays_bare():
    msgs = [_img("aaa"), _img("bbb")]
    out = format_tokenized_messages(msgs, {"aaa": "猫"})
    lines = out.splitlines()
    assert "[图片：猫]" in lines[0]
    assert lines[1].endswith("[图片]")  # bbb has no caption
