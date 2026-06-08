"""Tests for llm_extractor — uses an injected fake client to avoid real API calls."""

from __future__ import annotations

import json

import pytest

from wechat_daily.config import debug_dir_for
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
    def __init__(
        self,
        stop_reason: str,
        content: list | None = None,
        usage: object | None = None,
    ) -> None:
        self.stop_reason = stop_reason
        self.content = content or []
        self.usage = usage


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
        usage: object | None = None,
    ) -> None:
        events = [_TextEvent(t) for t in (text_chunks or [])]
        response = _Response(stop_reason, fallback_blocks or [], usage=usage)
        self.messages = _FakeMessages(events, response)


# ── Tests ───────────────────────────────────────────────────────────────────────


def test_streams_text_into_markdown(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["intro\n\n", "## 行业新闻\n\n", "### x\nbody\n"])

    report = extract_report("2026-04-30", "chat history", api_key="fake", client=client)

    assert isinstance(report, DailyReport)
    assert report.date == "2026-04-30"
    assert report.markdown == "intro\n\n## 行业新闻\n\n### x\nbody\n"
    # Saved to debug (per-date folder: tmp_path/2026/04/2026-04-30/)
    day = debug_dir_for("2026-04-30")
    assert (day / "extract.md").exists()
    assert (day / "extract.input.txt").exists()


def test_no_tool_use_in_request(monkeypatch, tmp_path):
    """Request must not include the old tool_use parameters."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])
    extract_report("2026-04-30", "chat", api_key="fake", client=client)

    call = client.messages.calls[0]
    assert "tools" not in call
    assert "tool_choice" not in call
    assert "system" in call
    assert "messages" in call


def test_refusal_raises_and_writes_failure(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=[], stop_reason="refusal")

    with pytest.raises(ExtractionError, match="拒绝"):
        extract_report("2026-04-30", "chat", api_key="fake", client=client)
    failure = debug_dir_for("2026-04-30") / "extract.FAILED.json"
    assert failure.exists()
    payload = json.loads(failure.read_text(encoding="utf-8"))
    assert "拒绝" in payload["reason"]


def test_max_tokens_raises(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["partial"], stop_reason="max_tokens")

    with pytest.raises(ExtractionError, match="截断"):
        extract_report("2026-04-30", "chat", api_key="fake", client=client)


def test_empty_response_raises(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=[], stop_reason="end_turn")

    with pytest.raises(ExtractionError, match="空"):
        extract_report("2026-04-30", "chat", api_key="fake", client=client)


def test_falls_back_to_response_text_blocks(monkeypatch, tmp_path):
    """If no streaming events arrived but content has text blocks, harvest them."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(
        text_chunks=[],
        stop_reason="end_turn",
        fallback_blocks=[_TextBlock("hello world")],
    )

    report = extract_report("2026-04-30", "chat", api_key="fake", client=client)
    assert report.markdown == "hello world"


def test_roster_prepended(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
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
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
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
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
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
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["hello\n", "world\n"])

    extract_report("2026-04-30", "chat", api_key="fake", client=client)
    saved = (debug_dir_for("2026-04-30") / "extract.md").read_text(encoding="utf-8")
    assert saved == "hello\nworld\n"


def test_usage_cb_receives_response_usage_and_input_chars(monkeypatch, tmp_path):
    """Cost tracking hook: callback fires once on success with (usage, chars)."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)

    class _Usage:
        input_tokens = 42
        output_tokens = 7

    client = _FakeClient(text_chunks=["x"], usage=_Usage())
    seen: list[tuple[object, int]] = []

    extract_report(
        "2026-04-30", "chat history", api_key="fake", client=client,
        usage_cb=lambda usage, chars: seen.append((usage, chars)),
    )

    assert len(seen) == 1
    usage, chars = seen[0]
    assert usage.input_tokens == 42
    assert usage.output_tokens == 7
    assert chars > 0  # prompt has at least roster/instructions/chat in it


def test_usage_cb_not_called_on_failure(monkeypatch, tmp_path):
    """On refusal/max_tokens/empty, the usage hook must not fire."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=[], stop_reason="refusal")
    seen = []

    with pytest.raises(ExtractionError):
        extract_report(
            "2026-04-30", "chat", api_key="fake", client=client,
            usage_cb=lambda usage, chars: seen.append((usage, chars)),
        )
    assert seen == []


# ── prior_reports injection ────────────────────────────────────────────────────


def test_prior_reports_injected_before_chat_log(monkeypatch, tmp_path):
    """Long context order: roster → previous_reports → chat_log → instructions."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    roster = "## 群友花名册\n- 沉稳的狐狸：鸭哥"
    priors = [
        ("2026-04-28", "## 行业新闻\n\n### 老话题\nold body\n"),
        ("2026-04-29", "## 工具\n\n### 另一话题\nbody\n"),
    ]
    extract_report(
        "2026-04-30", "today's chat", api_key="fake", client=client,
        roster_text=roster,
        prior_reports=priors,
    )

    user_msg = client.messages.calls[0]["messages"][0]["content"]
    # The data block uses a closing </previous_reports> tag; the instructions
    # only mention <previous_reports> as inline code prose. Closing tag appears
    # only when the block is actually emitted.
    assert "</previous_reports>" in user_msg
    assert '<report date="2026-04-28">' in user_msg
    assert '<report date="2026-04-29">' in user_msg
    assert "老话题" in user_msg
    # Order: roster < previous_reports < chat_log
    assert user_msg.index("</group_roster>") < user_msg.index("</previous_reports>")
    assert user_msg.index("</previous_reports>") < user_msg.index("<chat_log")
    assert user_msg.index("today's chat") > user_msg.index("</previous_reports>")


def test_prior_reports_omitted_when_none(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    extract_report(
        "2026-04-30", "chat history", api_key="fake", client=client,
    )

    user_msg = client.messages.calls[0]["messages"][0]["content"]
    # The closing tag is the unique signal that the data block was emitted.
    # The opening tag also appears in the instructions text as inline code.
    assert "</previous_reports>" not in user_msg


def test_prior_reports_empty_list_omits_block(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    extract_report(
        "2026-04-30", "chat history", api_key="fake", client=client,
        prior_reports=[],
    )

    user_msg = client.messages.calls[0]["messages"][0]["content"]
    assert "</previous_reports>" not in user_msg


def test_prior_reports_with_chat_blocks(monkeypatch, tmp_path):
    """When chat_blocks (multimodal) is used, prior reports go into the prefix block."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    priors = [("2026-04-29", "yesterday's body")]
    chat_blocks = [{"type": "text", "text": "[14:00] alice: hi\n"}]
    extract_report(
        "2026-04-30", "ignored", api_key="fake", client=client,
        prior_reports=priors,
        chat_blocks=chat_blocks,
    )

    user_content = client.messages.calls[0]["messages"][0]["content"]
    assert isinstance(user_content, list)
    # Prefix is the first text block
    prefix = user_content[0]["text"]
    assert "</previous_reports>" in prefix
    assert "yesterday's body" in prefix
    assert "<chat_log" in prefix


def test_system_prompt_documents_ref_placeholder(monkeypatch, tmp_path):
    """Sanity check: the [[ref:...]] syntax is taught in the system prompt."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])
    extract_report("2026-04-30", "chat", api_key="fake", client=client)

    system = client.messages.calls[0]["system"]
    assert "[[ref:" in system
    assert "YYYY-MM-DD" in system


# ── prior_report_titles injection ──────────────────────────────────────────────


def test_prior_report_titles_injected_before_prior_reports(monkeypatch, tmp_path):
    """Long context order: roster → titles → full reports → chat_log."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    titles = [
        ("2026-04-24", "## 行业新闻\n### 旧的旧话题"),
        ("2026-04-25", "## 方法论\n### 旧话题"),
    ]
    priors = [("2026-04-29", "## 工具\n\n### 近话题\nbody")]
    extract_report(
        "2026-04-30", "today's chat", api_key="fake", client=client,
        prior_reports=priors,
        prior_report_titles=titles,
    )

    user_msg = client.messages.calls[0]["messages"][0]["content"]
    # Both data blocks present (use closing tags as canonical signal).
    assert "</previous_report_titles>" in user_msg
    assert "</previous_reports>" in user_msg
    assert "旧话题" in user_msg
    assert "近话题" in user_msg
    # Order: titles block (older days) before full-body block (newer days).
    assert user_msg.index("</previous_report_titles>") < user_msg.index("</previous_reports>")
    assert user_msg.index("</previous_reports>") < user_msg.index("<chat_log")


def test_prior_report_titles_alone_emitted(monkeypatch, tmp_path):
    """Titles can be passed without full prior_reports."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    extract_report(
        "2026-04-30", "today's chat", api_key="fake", client=client,
        prior_report_titles=[("2026-04-24", "## A\n### B")],
    )

    user_msg = client.messages.calls[0]["messages"][0]["content"]
    assert "</previous_report_titles>" in user_msg
    assert "### B" in user_msg
    # No full-body block.
    assert "</previous_reports>" not in user_msg


def test_prior_report_titles_omitted_when_none(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    extract_report("2026-04-30", "chat", api_key="fake", client=client)

    user_msg = client.messages.calls[0]["messages"][0]["content"]
    assert "</previous_report_titles>" not in user_msg


def test_prior_report_titles_empty_list_omits_block(monkeypatch, tmp_path):
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])

    extract_report(
        "2026-04-30", "chat", api_key="fake", client=client,
        prior_report_titles=[],
    )

    user_msg = client.messages.calls[0]["messages"][0]["content"]
    assert "</previous_report_titles>" not in user_msg


def test_system_prompt_documents_title_block(monkeypatch, tmp_path):
    """System prompt should mention <previous_report_titles> so the model
    knows what to do when it appears."""
    import wechat_daily.llm_extractor as mod
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    client = _FakeClient(text_chunks=["x"])
    extract_report("2026-04-30", "chat", api_key="fake", client=client)

    system = client.messages.calls[0]["system"]
    assert "previous_report_titles" in system
