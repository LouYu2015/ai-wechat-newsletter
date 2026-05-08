"""Tests for llm_extractor — uses an injected fake client to avoid real API calls."""

from __future__ import annotations

import json

import pytest

from wechat_daily.llm_extractor import ExtractionError, extract_report
from wechat_daily.models import DailyReport


# ── Fake event/response/stream/client ───────────────────────────────────────────


class _TextEvent:
    """Duck-typed text delta event matching anthropic.lib.streaming.TextEvent."""

    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, stop_reason: str, content: list | None = None) -> None:
        self.stop_reason = stop_reason
        self.content = content or []


class _FakeStream:
    def __init__(self, events: list, response: _Response) -> None:
        self._events = events
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def __iter__(self):
        yield from self._events

    def get_final_message(self) -> _Response:
        return self._response


class _FakeMessages:
    def __init__(self, events: list, response: _Response) -> None:
        self._events = events
        self._response = response
        self.calls: list[dict] = []

    def stream(self, **kwargs) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self._events, self._response)


class _FakeClient:
    def __init__(
        self,
        text_chunks: list[str] | None = None,
        stop_reason: str = "end_turn",
        fallback_blocks: list[_TextBlock] | None = None,
    ) -> None:
        events = [_TextEvent(t) for t in (text_chunks or [])]
        response = _Response(stop_reason, fallback_blocks or [])
        self.messages = _FakeMessages(events, response)


# ── Tests ───────────────────────────────────────────────────────────────────────


def test_streams_text_into_markdown(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["intro\n\n", "## 行业新闻\n\n", "### x\nbody\n"])

    report = extract_report("2026-04-30", "chat history", api_key="fake", client=client)

    assert isinstance(report, DailyReport)
    assert report.date == "2026-04-30"
    assert report.markdown == "intro\n\n## 行业新闻\n\n### x\nbody\n"
    # Saved to debug
    assert (tmp_path / "extract-2026-04-30.md").exists()
    assert (tmp_path / "extract-2026-04-30.input.txt").exists()


def test_no_tool_use_in_request(monkeypatch, tmp_path):
    """Request must not include the old tool_use parameters."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])
    extract_report("2026-04-30", "chat", api_key="fake", client=client)

    call = client.messages.calls[0]
    assert "tools" not in call
    assert "tool_choice" not in call
    assert "system" in call
    assert "messages" in call


def test_refusal_raises_and_writes_failure(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=[], stop_reason="refusal")

    with pytest.raises(ExtractionError, match="拒绝"):
        extract_report("2026-04-30", "chat", api_key="fake", client=client)
    failure = tmp_path / "extract-2026-04-30.FAILED.json"
    assert failure.exists()
    payload = json.loads(failure.read_text(encoding="utf-8"))
    assert "拒绝" in payload["reason"]


def test_max_tokens_raises(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["partial"], stop_reason="max_tokens")

    with pytest.raises(ExtractionError, match="截断"):
        extract_report("2026-04-30", "chat", api_key="fake", client=client)


def test_empty_response_raises(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=[], stop_reason="end_turn")

    with pytest.raises(ExtractionError, match="空"):
        extract_report("2026-04-30", "chat", api_key="fake", client=client)


def test_falls_back_to_response_text_blocks(monkeypatch, tmp_path):
    """If no streaming events arrived but content has text blocks, harvest them."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(
        text_chunks=[],
        stop_reason="end_turn",
        fallback_blocks=[_TextBlock("hello world")],
    )

    report = extract_report("2026-04-30", "chat", api_key="fake", client=client)
    assert report.markdown == "hello world"


def test_roster_prepended(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    roster = "## 群友花名册\n- 沉稳的狐狸：鸭哥"
    extract_report(
        "2026-04-30", "chat history", api_key="fake", client=client,
        roster_text=roster,
    )

    user_msg = client.messages.calls[0]["messages"][0]["content"]
    assert roster in user_msg
    assert "chat history" in user_msg
    assert user_msg.index(roster) < user_msg.index("chat history")


def test_no_roster_when_none(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    extract_report("2026-04-30", "chat history", api_key="fake", client=client)
    user_msg = client.messages.calls[0]["messages"][0]["content"]
    # No <group_roster>...</group_roster> block emitted (the closing tag
    # only appears when the roster is actually rendered; the literal
    # `<group_roster>` token also appears inside the rules text, so we
    # check the closing tag instead).
    assert "</group_roster>" not in user_msg


def test_progress_cb_monotonic(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    chunks = ["abc", "de", "fghij"]
    client = _FakeClient(text_chunks=chunks)

    calls: list[tuple[int, int]] = []
    extract_report(
        "2026-04-30", "chat", api_key="fake", client=client,
        progress_cb=lambda received, attempt: calls.append((received, attempt)),
    )

    counts = [c[0] for c in calls]
    assert counts == [3, 5, 10]
    assert all(attempt == 1 for _, attempt in calls)


def test_debug_md_contents_match(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["hello\n", "world\n"])

    extract_report("2026-04-30", "chat", api_key="fake", client=client)
    saved = (tmp_path / "extract-2026-04-30.md").read_text(encoding="utf-8")
    assert saved == "hello\nworld\n"
