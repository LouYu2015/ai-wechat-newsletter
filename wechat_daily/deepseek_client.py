"""DeepSeek (OpenAI-compatible) streaming client for the daily-report pipeline.

DeepSeek has no official Anthropic SDK; its API is the OpenAI-compatible
``chat/completions`` endpoint. This module wraps a single streaming call with
the stdlib ``urllib`` (matching ``scripts/ask_deepseek.py`` — no extra deps)
and exposes the pieces the pipeline needs:

- separate ``content`` vs ``reasoning_content`` deltas. V4 Pro is a reasoning
  model: its chain-of-thought streams in ``reasoning_content`` and the final
  answer in ``content`` (see https://api-docs.deepseek.com/guides/thinking_mode).
- a ``thinking`` on/off toggle — link summaries run with it off (cheaper,
  faster), report generation with it on.
- usage in DeepSeek's native shape (``prompt_tokens`` / ``completion_tokens`` /
  ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``); cost_tracker
  normalizes it to the Anthropic-shaped fields.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable

API_URL = "https://api.deepseek.com/chat/completions"


class DeepSeekError(Exception):
    """DeepSeek API call failed (HTTP error or network error)."""


_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def strip_markdown_fence(text: str) -> str:
    """Remove a single outer ```` ```markdown … ``` ```` fence if the model
    wrapped its whole reply in one. The system prompt forbids it, but reasoning
    models occasionally do it anyway; the renderer parses raw markdown, so an
    outer fence would break section detection.
    """
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text


def stream_chat(
    *,
    api_key: str,
    model: str,
    system: str,
    user: str,
    thinking: bool,
    max_tokens: int,
    temperature: float = 1.0,
    timeout: float = 600.0,
    content_cb: Callable[[str], None] | None = None,
    reasoning_cb: Callable[[str], None] | None = None,
) -> tuple[str, str, dict, str | None]:
    """Stream one DeepSeek chat completion.

    Returns ``(content, reasoning, usage, finish_reason)``. *usage* is the raw
    DeepSeek usage dict (or ``{}`` if the stream carried none). Raises
    :class:`DeepSeekError` on HTTP / network failure.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        # Ask DeepSeek to emit a final usage-only chunk on the stream.
        "stream_options": {"include_usage": True},
        # Explicit thinking toggle — don't rely on the server default.
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict = {}
    finish_reason: str | None = None

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                # Usage-only chunks (and the final chunk) carry `usage`.
                if obj.get("usage"):
                    usage = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                rc = delta.get("reasoning_content")
                if rc:
                    reasoning_parts.append(rc)
                    if reasoning_cb:
                        reasoning_cb(rc)
                c = delta.get("content")
                if c:
                    content_parts.append(c)
                    if content_cb:
                        content_cb(c)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise DeepSeekError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise DeepSeekError(f"网络错误：{e.reason}") from e

    return "".join(content_parts), "".join(reasoning_parts), usage, finish_reason
