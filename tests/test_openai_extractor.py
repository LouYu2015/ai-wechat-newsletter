"""Tests for openai_extractor — the GPT-6 Astra fallback, with an injected fake ``post``."""

from __future__ import annotations

import json

import pytest

from wechat_daily import config, llm_extractor, openai_extractor, prompts

DATE = "2026-10-06"


@pytest.fixture
def debug_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    return tmp_path


def _content():
    """What the refused Opus request was built from (block list + debug text)."""
    return llm_extractor.build_extract_user_content(
        date_str=DATE,
        tokenized_chat="聊天",
        roster_text="花名册",
        chat_blocks=[
            {"type": "text", "text": "[10:00] 沉稳的大象: 看图\n"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"},
            },
        ],
        prior_reports=None,
        prior_report_titles=None,
    )


def _response(text="导读\n\n## 组\n\n### 话题\n正文\n", status="completed", tier="flex"):
    return {
        "status": status,
        "service_tier": tier,
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "想了想"}]},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 200},
            "output_tokens": 300,
        },
    }


class _FakePost:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.bodies: list[dict] = []

    def __call__(self, body, api_key):
        self.bodies.append(body)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_replace_instructions_swaps_tail_of_blocks_and_text():
    content, debug_text = _content()
    new = llm_extractor.replace_instructions(content, "新指令")
    assert new[:-1] == content[:-1]
    assert new[-1]["text"].endswith("</chat_log>\n\n---\n\n新指令")
    assert prompts.USER_INSTRUCTIONS[:20] not in new[-1]["text"]
    assert llm_extractor.replace_instructions(debug_text, "新指令").endswith("---\n\n新指令")


def test_request_uses_astra_prompts_and_native_images():
    content, _ = _content()
    body = openai_extractor.build_request_body(DATE, content, "flex")
    assert body["model"] == config.FALLBACK_REPORT_MODEL
    assert body["instructions"] == prompts.ASTRA_SYSTEM_PROMPT
    assert body["service_tier"] == "flex"
    parts = body["input"][0]["content"]
    assert {"type": "input_image", "image_url": "data:image/jpeg;base64,QUJD"} in parts
    assert parts[-1]["text"].endswith(prompts.ASTRA_USER_INSTRUCTIONS.format(date_str=DATE))
    assert all(p["type"] in ("input_text", "input_image") for p in parts)


def test_success_writes_canonical_sidecars_and_reports_usage(debug_dir):
    content, debug_text = _content()
    usages = []
    post = _FakePost(_response())
    report = openai_extractor.extract_report(
        DATE,
        content,
        debug_text,
        "key",
        usage_cb=lambda u, d: usages.append((u, d)),
        post=post,
    )
    assert report.markdown.startswith("导读")
    day = config.debug_dir_for(DATE)
    assert (day / "extract.md").read_text(encoding="utf-8") == report.markdown
    assert (day / "extract.thinking.md").read_text(encoding="utf-8") == "想了想"
    saved_input = (day / "extract.input.txt").read_text(encoding="utf-8")
    assert saved_input.endswith(prompts.ASTRA_USER_INSTRUCTIONS.format(date_str=DATE))
    assert usages == [
        (
            {
                "input_tokens": 800,
                "output_tokens": 300,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 200,
            },
            True,
        )
    ]


def test_flex_unavailable_retries_at_standard_price(debug_dir):
    content, debug_text = _content()
    usages = []
    post = _FakePost(
        openai_extractor.OpenAIError("busy", status=429, code="flex_unavailable"),
        _response(tier="default"),
    )
    openai_extractor.extract_report(
        DATE, content, debug_text, "key", usage_cb=lambda u, d: usages.append(d), post=post
    )
    assert [b["service_tier"] for b in post.bodies] == ["flex", "default"]
    assert usages == [False]


def test_server_error_is_retried(debug_dir, monkeypatch):
    monkeypatch.setattr(openai_extractor.time, "sleep", lambda _s: None)
    content, debug_text = _content()
    post = _FakePost(openai_extractor.OpenAIError("boom", status=500), _response())
    openai_extractor.extract_report(DATE, content, debug_text, "key", post=post)
    assert len(post.bodies) == 2


def test_client_error_is_not_retried(debug_dir):
    content, debug_text = _content()
    post = _FakePost(openai_extractor.OpenAIError("bad", status=401))
    with pytest.raises(openai_extractor.OpenAIError):
        openai_extractor.extract_report(DATE, content, debug_text, "key", post=post)
    assert len(post.bodies) == 1


@pytest.mark.parametrize(
    "response, match",
    [
        (_response(status="incomplete"), "未完成"),
        (_response(text="  "), "为空"),
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal"}]}],
            },
            "拒绝",
        ),
    ],
)
def test_unusable_response_raises_and_saves_failure(debug_dir, response, match):
    content, debug_text = _content()
    with pytest.raises(llm_extractor.ExtractionError, match=match):
        openai_extractor.extract_report(DATE, content, debug_text, "key", post=_FakePost(response))
    day = config.debug_dir_for(DATE)
    failed = json.loads((day / f"extract.{config.FALLBACK_REPORT_MODEL}.FAILED.json").read_text())
    assert match in failed["reason"]
    assert not (day / "extract.md").exists()


def test_missing_key_fails_fast(debug_dir):
    content, debug_text = _content()
    with pytest.raises(openai_extractor.OpenAIError, match="CHATGPT_API_KEY"):
        openai_extractor.extract_report(DATE, content, debug_text, "", post=_FakePost())
