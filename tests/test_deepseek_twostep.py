"""Tests for the DeepSeek two-step (outline → write) report pipeline.

Stubs ``deepseek_client.stream_chat`` so no network is hit; asserts the two
passes run in order, temperature 0.6 is applied, the outline is fed into the
write pass, and the Opus few-shot prompt never reaches DeepSeek.
"""

from __future__ import annotations

import pytest

import wechat_daily.deepseek_client as ds_client
import wechat_daily.llm_extractor as mod
from wechat_daily.config import debug_dir_for
from wechat_daily.llm_extractor import ExtractionError, extract_report_deepseek


class _StubStreamChat:
    """Records each stream_chat call and returns queued (content, reasoning)."""

    def __init__(self, replies: list[tuple[str, str]]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        content, reasoning = self._replies.pop(0)
        # Drive the callbacks like the real client does.
        if kwargs.get("reasoning_cb") and reasoning:
            kwargs["reasoning_cb"](reasoning)
        if kwargs.get("content_cb") and content:
            kwargs["content_cb"](content)
        return content, reasoning, {"completion_tokens": 10}, "stop"


def _patch(monkeypatch, tmp_path, replies):
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    monkeypatch.setattr("wechat_daily.config.get_deepseek_key", lambda: "fake-key")
    stub = _StubStreamChat(replies)
    monkeypatch.setattr(ds_client, "stream_chat", stub)
    return stub


def test_two_step_outline_then_write(monkeypatch, tmp_path):
    outline = "## A. 话题清单\n- [行业新闻] X | token | 内容 | 保留 | 全新\n## B. 章节树\n## 行业新闻\n### X"
    report_md = "导读\n\n## 行业新闻\n\n### X\nbody\n\n---\ntags: x"
    stub = _patch(monkeypatch, tmp_path, [(outline, "plan-thinking"), (report_md, "write-thinking")])

    report = extract_report_deepseek(
        "2026-06-07", "chat history", model="deepseek-v4-pro",
        debug_suffix=".deepseek-v4-pro",
    )

    assert report.markdown == report_md
    assert len(stub.calls) == 2, "should make exactly two API calls (outline + write)"

    outline_call, write_call = stub.calls
    # Pass 1 is the outline pass; pass 2 writes against it.
    assert "只产出今天的【选题大纲】" in outline_call["user"]
    assert outline in write_call["user"], "outline must be fed into the write pass"
    assert "选题大纲" in write_call["system"] or "写手" in write_call["system"]

    # Temperature 0.6 (not the client default 1.0) on both passes.
    assert outline_call["temperature"] == pytest.approx(0.6)
    assert write_call["temperature"] == pytest.approx(0.6)

    # The Opus few-shot example must never reach DeepSeek.
    assert "简短示例" not in outline_call["user"]
    assert "简短示例" not in write_call["user"]

    # Debug sidecars for both passes exist (per-date folder).
    day = debug_dir_for("2026-06-07")
    assert (day / "extract.deepseek-v4-pro.outline.md").exists()
    assert (day / "extract.deepseek-v4-pro.md").exists()


def test_usage_cb_fires_for_both_passes(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, [("outline", "t1"), ("body\n\n---\ntags: a", "t2")])
    seen: list[int] = []
    extract_report_deepseek(
        "2026-06-07", "chat", model="deepseek-v4-pro",
        usage_cb=lambda usage, chars: seen.append(chars),
    )
    assert len(seen) == 2, "cost must be tracked for outline AND write calls"


def test_write_truncation_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    monkeypatch.setattr("wechat_daily.config.get_deepseek_key", lambda: "fake-key")

    class _Truncating:
        def __init__(self):
            self.n = 0
        def __call__(self, **kwargs):
            self.n += 1
            # outline ok, write truncated
            return ("outline" if self.n == 1 else "partial"), "", {}, ("stop" if self.n == 1 else "length")

    monkeypatch.setattr(ds_client, "stream_chat", _Truncating())
    with pytest.raises(ExtractionError, match="max_tokens 截断"):
        extract_report_deepseek("2026-06-07", "chat", model="deepseek-v4-pro")
