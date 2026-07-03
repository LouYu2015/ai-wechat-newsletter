"""Markdown extraction: tokenized chat → DailyReport(markdown) via Claude.

Plain-text output, no tool use. The prompt (see :mod:`wechat_daily.prompts`)
fixes the markdown structure (intro + ## type sections + ### topic three-part
blocks + tags footer + per-section `[章节不公开：原因]` hide markers). The
renderer parses the markdown and produces both the group and public versions.

Two execution paths share the request construction and response finalization
in this module:

- :func:`extract_report` — synchronous streaming call (``--no-batch``);
- :mod:`wechat_daily.batch_extractor` — Batch API at 50% pricing (default).
"""

from __future__ import annotations

import re
import time

import httpx

from wechat_daily import config, models, prompts

# Body markdown ## / ### header line.
_BODY_HEADER_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


class ExtractionError(Exception):
    """Claude returned an unusable response (refusal or max_tokens cutoff)."""


def _default_client(api_key: str):
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(600.0, connect=30.0),
    )


def build_extract_user_content(
    *,
    date_str: str,
    tokenized_chat: str,
    roster_text: str | None,
    chat_blocks: list[dict] | None,
    prior_reports: list[tuple[str, str]] | None,
    prior_report_titles: list[tuple[str, str]] | None,
) -> tuple[str | list[dict], str]:
    """Assemble the extraction user message; shared by the main and AB-compare
    runs so the only difference between the two report versions is the model.

    Long input first (best practice for multi-doc prompts): roster →
    previous_report_titles → previous_reports → chat_log are wrapped in XML tags
    at the top; processing rules / audience profile / 导读 requirements follow the
    chat log so the model reads them with the data fresh. Title-only block goes
    before full-body block (it covers the older days) so the model scans
    "old → new".

    Returns ``(user_content, debug_text)``. *user_content* is a block list when
    *chat_blocks* is given (inline native images), else a flat string (images
    appear as ``[图片]`` placeholders). *debug_text* is always the flat string
    used for the debug sidecar.
    """
    from wechat_daily import prior_report

    parts: list[str] = []
    if roster_text:
        parts.append(f"<group_roster>\n{roster_text}\n</group_roster>\n\n")
    if prior_report_titles:
        parts.append(prior_report.format_prior_report_titles_block(prior_report_titles) + "\n")
    if prior_reports:
        parts.append(prior_report.format_prior_reports_block(prior_reports) + "\n")
    parts.append(f'<chat_log date="{date_str}">\n')
    prefix = "".join(parts)
    suffix = "\n</chat_log>\n\n---\n\n" + prompts.USER_INSTRUCTIONS.format(date_str=date_str)

    if chat_blocks is not None:
        user_content: str | list[dict] = [
            {"type": "text", "text": prefix},
            *chat_blocks,
            {"type": "text", "text": suffix},
        ]
        debug_text = (
            prefix + "".join(b["text"] for b in chat_blocks if b.get("type") == "text") + suffix
        )
    else:
        user_content = prefix + tokenized_chat + suffix
        debug_text = user_content

    return user_content, debug_text


# ── Shared request construction / response finalization ─────────────────────────
# Everything below this banner is shared by the streaming path (extract_report)
# and the batch path (batch_extractor) — both must produce byte-identical
# requests and identical debug sidecars so the two modes are interchangeable.

MAX_OUTPUT_TOKENS = 128_000


def build_request_params(model: str, user_content: str | list[dict]) -> dict:
    """Kwargs for one report-generation request (streaming and batch).

    ``display="summarized"`` so thinking content is available: on Opus 4.6
    that's already the default (no-op), but Fable 5 / Opus 4.7+ default to
    "omitted" and would return empty thinking text. display affects
    visibility only — thinking is billed the same.
    """
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "high"},
        "system": prompts.SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }


def harvest_message_text(response) -> tuple[str, str]:
    """Extract ``(markdown, thinking_text)`` from a final Message's blocks."""
    blocks = getattr(response, "content", None) or []
    markdown = "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text")
    thinking = "".join(
        getattr(b, "thinking", "") or "" for b in blocks if getattr(b, "type", None) == "thinking"
    )
    return markdown, thinking


def finalize_response(
    date_str: str,
    debug_text: str,
    response,
    *,
    markdown: str = "",
    thinking_text: str = "",
    suffix: str = "",
) -> str:
    """Validate a final Message, persist debug sidecars, return the markdown.

    Shared tail of the streaming and batch paths. *markdown* /
    *thinking_text* are the stream-accumulated buffers when streaming; when
    empty (batch path, or an SDK event-shape change) they're harvested from
    ``response.content``. Raises :class:`ExtractionError` on refusal,
    max_tokens truncation, or an empty body — after saving a
    ``extract{suffix}.FAILED.json`` sidecar.
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        _save_failure(date_str, debug_text, markdown, "Claude 拒绝处理该内容", suffix=suffix)
        raise ExtractionError("Claude 拒绝处理该内容（stop_reason=refusal）")

    if stop_reason == "max_tokens":
        _save_failure(date_str, debug_text, markdown, "响应被 max_tokens 截断", suffix=suffix)
        raise ExtractionError("响应被 max_tokens 截断，请增大 max_tokens 后重试")

    harvested_md, harvested_think = harvest_message_text(response)
    if not markdown:
        markdown = harvested_md
    if not thinking_text:
        thinking_text = harvested_think

    if not markdown.strip():
        _save_failure(date_str, debug_text, markdown, "响应为空", suffix=suffix)
        raise ExtractionError("响应为空")

    _save_extract(date_str, markdown, debug_text, thinking_text, suffix=suffix)
    return markdown


def extract_report(
    date_str: str,
    tokenized_chat: str,
    api_key: str,
    progress_cb=None,
    client=None,
    roster_text: str | None = None,
    thinking_cb=None,
    header_cb=None,
    attempt_cb=None,
    text_cb=None,
    usage_cb=None,
    chat_blocks: list[dict] | None = None,
    prior_reports: list[tuple[str, str]] | None = None,
    prior_report_titles: list[tuple[str, str]] | None = None,
    model: str = config.CLAUDE_MODEL,
    debug_suffix: str = "",
) -> models.DailyReport:
    """Stream a markdown daily report from Claude; return DailyReport(date, markdown).

    *model* selects the Anthropic model. Defaults to the published main version
    (Fable 5); the AB-compare path passes ``claude-opus-4-6`` to run the exact
    same prompt / native-image input through a different model. *debug_suffix*
    (e.g. ``.opus-4-6``) keeps the compare run's debug sidecars from colliding
    with the canonical un-suffixed ones (which feed next-day continuity).

    *client* may be injected for testing (any object with ``messages.stream``
    matching the Anthropic SDK surface). If None, a default client is built
    from *api_key*.

    *roster_text* is the rendered 群友花名册 (see ``wechat_daily.roster``);
    when provided it's prepended to the user message so the model can resolve
    informal references back to tokens.

    *thinking_cb(received_bytes, attempt)* is invoked as adaptive-thinking
    content streams in (separate from the visible text body).

    *header_cb(kind, level, title, attempt)* fires when a ``##`` / ``###``
    markdown header is detected in the visible body (kind always ``"body"``;
    level 0 for ``##``, 1 for ``###``).

    *text_cb(kind, delta, attempt)* fires for every streamed delta with the
    raw text — ``kind="thinking"`` for adaptive-thinking deltas,
    ``kind="body"`` for visible body text.

    *attempt_cb(attempt)* fires when a retry begins (attempt >= 2), so
    front-ends can render a separator in their log.

    *usage_cb(usage, input_chars)* fires once on successful completion with
    the response's ``usage`` object (Anthropic SDK shape — has
    ``input_tokens``, ``output_tokens``, etc.) and the prompt's character
    count. Used by the CLI to log token usage and estimate cost without this
    module having to know about :mod:`wechat_daily.cost_tracker`.
    """
    import anthropic  # for APIStatusError below

    if client is None:
        client = _default_client(api_key)

    user_content, debug_text = build_extract_user_content(
        date_str=date_str,
        tokenized_chat=tokenized_chat,
        roster_text=roster_text,
        chat_blocks=chat_blocks,
        prior_reports=prior_reports,
        prior_report_titles=prior_report_titles,
    )

    max_retries = 3
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        if attempt > 1 and attempt_cb:
            attempt_cb(attempt)
        try:
            buffer_parts: list[str] = []
            thinking_parts: list[str] = []
            received = 0
            thinking_received = 0
            text_line_buf = ""

            def _flush_body_headers(buf: str) -> str:
                """Emit body headers for any complete lines in *buf*; return remainder."""
                if "\n" not in buf or not header_cb:
                    return buf
                *complete, remainder = buf.split("\n")
                for line in complete:
                    m = _BODY_HEADER_RE.match(line)
                    if m:
                        level = len(m.group(1)) - 2  # ## → 0, ### → 1
                        header_cb("body", level, m.group(2).strip(), attempt)
                return remainder

            with client.messages.stream(**build_request_params(model, user_content)) as stream:
                for event in stream:
                    etype = getattr(event, "type", None)

                    # Visible text body — SDK helper TextEvent (has .text + type=="text").
                    delta = getattr(event, "text", None)
                    if isinstance(delta, str) and etype == "text":
                        buffer_parts.append(delta)
                        received += len(delta)
                        text_line_buf = _flush_body_headers(text_line_buf + delta)
                        if text_cb:
                            text_cb("body", delta, attempt)
                        if progress_cb:
                            progress_cb(received, attempt)
                        continue

                    # Adaptive-thinking deltas come through as raw
                    # content_block_delta events with delta.type == "thinking_delta".
                    if etype == "content_block_delta":
                        d = getattr(event, "delta", None)
                        if d is not None and getattr(d, "type", None) == "thinking_delta":
                            t = getattr(d, "thinking", "")
                            if isinstance(t, str) and t:
                                thinking_parts.append(t)
                                thinking_received += len(t)
                                if text_cb:
                                    text_cb("thinking", t, attempt)
                                if thinking_cb:
                                    thinking_cb(thinking_received, attempt)
                response = stream.get_final_message()

            markdown = finalize_response(
                date_str,
                debug_text,
                response,
                markdown="".join(buffer_parts),
                thinking_text="".join(thinking_parts),
                suffix=debug_suffix,
            )
            if usage_cb:
                usage_cb(getattr(response, "usage", None), len(debug_text))
            return models.DailyReport(date=date_str, markdown=markdown)

        except ExtractionError:
            raise
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(5 * attempt)
        except anthropic.APIStatusError as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(30)

    _save_failure(date_str, debug_text, None, str(last_exc), suffix=debug_suffix)
    raise last_exc  # type: ignore[misc]


def _save_extract(
    date_str: str,
    markdown: str,
    user_content: str,
    thinking_text: str = "",
    suffix: str = "",
) -> None:
    """Save successful extraction to debug/.

    *suffix* (e.g. ``.deepseek-v4-pro``) keeps the compare run's sidecars from
    colliding with the canonical un-suffixed ones. Files land in the per-date
    folder ``debug/{date}/`` (see :func:`wechat_daily.config.debug_dir_for`).
    """
    d = config.debug_dir_for(date_str)
    d.mkdir(exist_ok=True, parents=True)
    (d / f"extract{suffix}.md").write_text(markdown, encoding="utf-8")
    # Sidecar: the FULL LLM input (roster + tokenized chat) for post-mortem
    # audit — no truncation, so a run can always be reproduced byte-for-byte.
    (d / f"extract{suffix}.input.txt").write_text(user_content, encoding="utf-8")
    if thinking_text:
        (d / f"extract{suffix}.thinking.md").write_text(thinking_text, encoding="utf-8")


def _save_failure(
    date_str: str,
    user_content: str,
    partial_markdown: str | None,
    reason: str,
    suffix: str = "",
) -> None:
    """Persist failure details to ``debug/{date}/`` for post-mortem inspection."""
    import json

    d = config.debug_dir_for(date_str)
    d.mkdir(exist_ok=True, parents=True)
    path = d / f"extract{suffix}.FAILED.json"
    payload = {
        "reason": reason,
        "partial_markdown": partial_markdown,
        "input": user_content,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
