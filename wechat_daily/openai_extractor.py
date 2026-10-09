"""Fallback report generation via GPT-6 Astra, used when Opus refuses.

Opus occasionally returns ``stop_reason="refusal"`` on an ordinary day's chat
(e.g. its safety classifier trips on model-distillation or bio talk). The
pipeline then hands the very same input — roster, prior reports, chat log with
native images — to :data:`wechat_daily.config.FALLBACK_REPORT_MODEL` with its
own prompt pair (:data:`prompts.ASTRA_SYSTEM_PROMPT` /
:data:`prompts.ASTRA_USER_INSTRUCTIONS`), and the result is published exactly
like an Opus report (canonical un-suffixed debug sidecars, so it also feeds
next-day continuity).

One synchronous call to the OpenAI Responses API through the stdlib
``urllib`` (matching :mod:`wechat_daily.deepseek_client` — no extra deps).
The ``flex`` service tier bills at 50% like the Batch API (which the current
key has no scope for); when flex has no capacity the call is retried once at
standard price.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from wechat_daily import config, deepseek_client, llm_extractor, models, prompts

API_URL = "https://api.openai.com/v1/responses"
MAX_OUTPUT_TOKENS = 128_000
# Flex requests queue behind standard traffic; a full report has taken ~2.5
# minutes in tests, but give queueing plenty of headroom.
TIMEOUT_S = 1800
MAX_ATTEMPTS = 3


class OpenAIError(Exception):
    """HTTP or network failure talking to the OpenAI API."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def to_openai_content(user_content: str | list[dict]) -> list[dict]:
    """Anthropic-shaped user content → Responses API ``input_*`` parts."""
    if isinstance(user_content, str):
        return [{"type": "input_text", "text": user_content}]
    parts = []
    for b in user_content:
        if b["type"] == "text":
            parts.append({"type": "input_text", "text": b["text"]})
        elif b["type"] == "image":
            src = b["source"]
            parts.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{src['media_type']};base64,{src['data']}",
                }
            )
        else:
            raise ValueError(f"unsupported content block type: {b['type']}")
    return parts


def build_request_body(date_str: str, user_content: str | list[dict], tier: str) -> dict:
    """Responses API body: Astra's own system prompt and trailing instructions.

    *user_content* is the Opus request's content (production instructions
    after the chat log); the instructions are swapped for Astra's.
    """
    content = llm_extractor.replace_instructions(
        user_content, prompts.ASTRA_USER_INSTRUCTIONS.format(date_str=date_str)
    )
    return {
        "model": config.FALLBACK_REPORT_MODEL,
        "instructions": prompts.ASTRA_SYSTEM_PROMPT,
        "input": [{"role": "user", "content": to_openai_content(content)}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "service_tier": tier,
        "store": False,
    }


def _post(body: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            code = json.loads(raw).get("error", {}).get("code") or ""
        except (ValueError, AttributeError):
            code = ""
        raise OpenAIError(f"HTTP {e.code}: {raw[:500]}", status=e.code, code=code) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise OpenAIError(f"网络错误：{e}") from None


def harvest(response: dict) -> tuple[str, str, bool]:
    """``(markdown, reasoning_summary, refused)`` from a Responses API body."""
    md, thinking, refused = [], [], False
    for item in response.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    md.append(c.get("text", ""))
                elif c.get("type") == "refusal":
                    refused = True
        elif item.get("type") == "reasoning":
            thinking += [s.get("text", "") for s in item.get("summary") or []]
    return "".join(md), "\n\n".join(thinking), refused


def usage_to_anthropic(usage: dict | None) -> dict[str, int]:
    """Responses API usage → the Anthropic-shaped dict cost_tracker prices."""
    usage = usage or {}
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    return {
        "input_tokens": max(0, int(usage.get("input_tokens") or 0) - cached),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }


def extract_report(
    date_str: str,
    user_content: str | list[dict],
    debug_text: str,
    api_key: str,
    *,
    usage_cb: Callable[[dict[str, int], bool], None] | None = None,
    note_cb: Callable[[str], None] | None = None,
    post: Callable[[dict, str], dict] = _post,
) -> models.DailyReport:
    """Generate the report with Astra; return DailyReport or raise.

    *user_content* / *debug_text* are what the refused Opus request was built
    from (:func:`llm_extractor.build_extract_user_content` output, or the
    batch content snapshot). Success writes the canonical ``extract.md`` /
    ``extract.input.txt`` / ``extract.thinking.md`` sidecars. Failures raise
    :class:`llm_extractor.ExtractionError` (unusable response, after saving
    ``extract.<model>.FAILED.json``) or :class:`OpenAIError` (API unreachable
    after retries).

    *usage_cb(usage, discounted)* fires once on success with Anthropic-shaped
    usage; *discounted* is True when the flex tier (50%) actually served it.
    *post* is injectable for tests.
    """
    if not api_key:
        raise OpenAIError("未配置 CHATGPT_API_KEY，无法改用 Astra")

    debug_text = llm_extractor.replace_instructions(
        debug_text, prompts.ASTRA_USER_INSTRUCTIONS.format(date_str=date_str)
    )
    failed_suffix = f".{config.FALLBACK_REPORT_MODEL}"
    tier = "flex"
    last_exc: OpenAIError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = post(build_request_body(date_str, user_content, tier), api_key)
            break
        except OpenAIError as e:
            last_exc = e
            if tier == "flex" and e.code == "flex_unavailable":
                tier = "default"
                if note_cb:
                    note_cb("flex 档暂无容量，改用标准价重试")
                continue
            retryable = e.status is None or e.status == 429 or e.status >= 500
            if not retryable or attempt == MAX_ATTEMPTS:
                raise
            if note_cb:
                note_cb(f"第 {attempt}/{MAX_ATTEMPTS} 次调用失败，{10 * attempt}s 后重试：{e}")
            time.sleep(10 * attempt)
    else:
        raise last_exc  # type: ignore[misc]

    markdown, thinking, refused = harvest(response)
    markdown = deepseek_client.strip_markdown_fence(markdown)
    status = response.get("status")
    if refused:
        reason = "Astra 也拒绝处理该内容"
    elif status != "completed":
        reason = f"Astra 响应未完成（status={status}，{response.get('incomplete_details')}）"
    elif not markdown.strip():
        reason = "Astra 响应为空"
    else:
        reason = ""
    if reason:
        llm_extractor._save_failure(date_str, debug_text, markdown, reason, suffix=failed_suffix)
        raise llm_extractor.ExtractionError(reason)

    llm_extractor._save_extract(date_str, markdown, debug_text, thinking)
    if usage_cb:
        usage_cb(usage_to_anthropic(response.get("usage")), response.get("service_tier") == "flex")
    return models.DailyReport(date=date_str, markdown=markdown)
