"""Tests for llm_extractor — uses injected fake client to avoid real API calls."""

from __future__ import annotations

import pytest
from anthropic.lib.streaming import InputJsonEvent

from wechat_daily.llm_extractor import extract_report, ExtractionError
from wechat_daily.models import DailyReport


class _ToolBlock:
    type = "tool_use"

    def __init__(self, input_: dict) -> None:
        self.input = input_


class _Response:
    def __init__(self, stop_reason: str, content: list) -> None:
        self.stop_reason = stop_reason
        self.content = content


class _FakeStream:
    """Minimal context manager that mimics MessageStreamManager."""

    def __init__(self, response: _Response, json_chunks: list[str]) -> None:
        self._response = response
        self._chunks = json_chunks

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def __iter__(self):
        for chunk in self._chunks:
            yield InputJsonEvent(type="input_json", partial_json=chunk, snapshot={})

    def get_final_message(self) -> _Response:
        return self._response


class _FakeMessages:
    def __init__(self, response: _Response, json_chunks: list[str] | None = None) -> None:
        self._response = response
        self._chunks = json_chunks or []
        self.calls: list[dict] = []

    def stream(self, **kwargs) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self._response, self._chunks)


class _FakeClient:
    def __init__(self, response: _Response, json_chunks: list[str] | None = None) -> None:
        self.messages = _FakeMessages(response, json_chunks)


def _valid_payload() -> dict:
    return {
        "date": "2026-04-17",
        "intro": "今天讨论了一些话题。",
        "sections": [
            {
                "type": "news",
                "title": "示例新闻",
                "body": "正文",
                "comments": [],
                "tags": [],
                "public_safe": True,
                "public_safe_reason": None,
            }
        ],
    }


def test_extract_report_uses_injected_client(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(_Response("tool_use", [_ToolBlock(_valid_payload())]))

    report = extract_report("2026-04-17", "chat history", api_key="fake", client=client)

    assert isinstance(report, DailyReport)
    assert report.date == "2026-04-17"
    assert len(report.sections) == 1
    assert report.sections[0].title == "示例新闻"
    # The fake was actually called with the expected model/tool
    assert client.messages.calls, "client.messages.stream was never invoked"
    call = client.messages.calls[0]
    assert call["tools"][0]["name"] == "submit_daily_report"
    assert call["tool_choice"]["name"] == "submit_daily_report"


def test_extract_report_refusal_raises(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(_Response("refusal", []))
    with pytest.raises(ExtractionError, match="拒绝"):
        extract_report("2026-04-17", "chat", api_key="fake", client=client)
    # Failure file was written
    assert (tmp_path / "extract-2026-04-17.FAILED.json").exists()


def test_extract_report_max_tokens_raises(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(_Response("max_tokens", []))
    with pytest.raises(ExtractionError, match="截断"):
        extract_report("2026-04-17", "chat", api_key="fake", client=client)


def test_extract_report_forces_date(monkeypatch, tmp_path):
    """Even if the model returns a different date, our date_str takes precedence."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    payload = _valid_payload()
    payload["date"] = "1970-01-01"  # model hallucinated
    client = _FakeClient(_Response("tool_use", [_ToolBlock(payload)]))

    report = extract_report("2026-04-17", "chat", api_key="fake", client=client)
    assert report.date == "2026-04-17"


def test_extract_report_includes_roster_when_provided(monkeypatch, tmp_path):
    """When roster_text is passed it must appear in the user message body."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(_Response("tool_use", [_ToolBlock(_valid_payload())]))

    roster = "## 群友花名册\n- 沉稳的狐狸：鸭哥"
    extract_report(
        "2026-04-17", "chat history", api_key="fake", client=client,
        roster_text=roster,
    )

    call = client.messages.calls[0]
    user_msg = call["messages"][0]["content"]
    assert roster in user_msg
    assert "chat history" in user_msg
    # Roster must come before the chat block
    assert user_msg.index(roster) < user_msg.index("chat history")


def test_extract_report_omits_roster_when_none(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)
    client = _FakeClient(_Response("tool_use", [_ToolBlock(_valid_payload())]))

    extract_report("2026-04-17", "chat history", api_key="fake", client=client)
    call = client.messages.calls[0]
    user_msg = call["messages"][0]["content"]
    assert "花名册" not in user_msg


def test_extract_report_progress_cb_accumulates_bytes(monkeypatch, tmp_path):
    """progress_cb receives monotonically increasing byte counts from JSON deltas."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr(mod, "DEBUG_DIR", tmp_path)

    chunks = ['{"date"', ': "2026', '-04-17"', "}"]
    client = _FakeClient(
        _Response("tool_use", [_ToolBlock(_valid_payload())]),
        json_chunks=chunks,
    )

    calls: list[tuple[int, int]] = []
    extract_report(
        "2026-04-17", "chat", api_key="fake", client=client,
        progress_cb=lambda received, attempt: calls.append((received, attempt)),
    )

    assert len(calls) == len(chunks)
    # Byte counts must be strictly increasing
    counts = [c[0] for c in calls]
    assert counts == sorted(counts) and len(set(counts)) == len(counts)
    # Final count equals total bytes across all chunks
    assert counts[-1] == sum(len(c) for c in chunks)
    # All calls report attempt 1
    assert all(attempt == 1 for _, attempt in calls)
