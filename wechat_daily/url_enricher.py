"""Fetch and summarize link-card targets for LLM context."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlparse

import httpx

from .config import LINK_SUMMARY_MODEL
from .message_parser import (
    MSG_LINK_CARD, MSG_LINK_OPEN, MSG_SYSTEM, MSG_TAP, LinkMeta, Message,
)


ProgressCB = Callable[[int, int, str, str], None]

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Browser-like headers. openai.com and other Cloudflare/Vercel-fronted sites
# return 403 challenge pages when the request has only User-Agent. `br` is
# omitted from Accept-Encoding because httpx doesn't decode brotli without
# the optional `brotli` package.
_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_SUMMARY_PROMPT = """\
你正在为「微信 AI 技术讨论群日报」准备链接摘要。这些摘要会用于让日报编辑生成群聊的总结。

日报的读者有两类：

1. **群友本人**——他们就是被引用的人，会回看自己说过什么，其他人有什么回应，想看到因为消息太多而错过的信息。
2. **群外类似背景的中文程序员/AI 实战派**——日常写代码、用 agent、关心模型迭代，订阅科技博主的文章和新闻，对其他人的技术分享十分好奇。

他们已知、不需要科普的概念：LLM、agent、RAG、context window、prompt cache、tool use、subagent、harness、skills、evals、MCP、Claude Code / Codex / Cursor 等常见工具。

下面给你两段资料：
1. `<surrounding_chat>`：链接前后约 10 条群聊消息，仅作"为什么群里要分享这条 / 在讨论哪个点"的背景。**不要把它写进摘要、不要引用群友发言。**
2. `<webpage>` 里是网页元数据与正文。其中 `content` 是抓取到的正文，`card_preview` 是微信卡片预览（分享者发出时看到的那段话），`meta_description` 是网页 og:description / meta description。**以 content 为主**，其他字段用于交叉印证、或在 content 不足时作为补充信息。

任务：写一段中文摘要，纯文本一段（无 Markdown 标题、无 bullet 列表、无小标题），长度 300–800 字。

要求：
- 紧扣网页本身：保留具体事实、数据、版本号、结论、关键例子、作者核心判断。
- 利用上下文：如果群里讨论的是网页中的某个具体点（一段提示、一个工具、一处争议），优先把那个点交代清楚，让日报作者能直接对接群聊。
- 不评价聊天记录、不引用群友发言。
- 禁用元描述句式：「本文/这篇文章介绍了……」「作者认为……」「文章指出……」。直接讲清网页里的事就好。

<surrounding_chat>
{surrounding}
</surrounding_chat>

{webpage_block}

直接输出摘要正文。
"""

_SUMMARY_PROMPT_NO_CONTEXT = """\
你正在为「微信 AI 技术讨论群日报」准备链接摘要。

读者两类：
1. 群友本人——他们就是分享/讨论这条链接的人，会回看自己说过什么。
2. 群外类似背景的中文程序员/AI 实战派——日常写代码、用 agent、关心模型迭代。

他们已知、不需要科普的概念：LLM、agent、RAG、context window、prompt cache、tool use、subagent、harness、skills、evals、MCP、Claude Code / Codex / Cursor 等常见工具与模型版本号。

`<webpage>` 里是网页元数据与正文：`content` 为抓取到的正文，`card_preview` 为微信卡片预览，`meta_description` 为 og:description / meta description。**以 content 为主**，其他字段用于交叉印证、或在 content 不足时作为补充信息。

任务：写一段中文摘要，纯文本一段（无 Markdown 标题、无 bullet 列表、无小标题），长度 300–800 字。

要求：
- 紧扣网页本身：保留具体事实、数据、版本号、结论、关键例子、作者核心判断。
- 不需要匿名化网页内容。
- 禁用元描述句式：「本文/这篇文章介绍了……」「作者认为……」「文章指出……」。直接讲清网页里的事就好。
- 反 AI 味词：深入探讨、赋能、助力、重塑、范式、拥抱变化、值得注意的是、综上所述。

{webpage_block}

直接输出摘要正文。
"""


@dataclass
class EnrichStats:
    total: int = 0
    fetched: int = 0     # debug-only: URLs where http fetch returned non-empty text
    summarized: int = 0  # total inputs ≥ SHORT_THRESHOLD, LLM produced summary
    short: int = 0       # 0 < total inputs < SHORT_THRESHOLD, raw concat used
    failed: int = 0      # no inputs at all (no title/desc/og/text)


SHORT_THRESHOLD = 800


class _TextExtractor(HTMLParser):
    def __init__(self, target_id: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.target_id = target_id
        self.capture = target_id is None
        self.depth = 0
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attr_map = dict(attrs)
        starts_target = (
            self.target_id is not None
            and not self.capture
            and attr_map.get("id") == self.target_id
        )
        if starts_target:
            self.capture = True
            self.depth = 1
        elif self.capture and self.target_id is not None:
            self.depth += 1

        if not self.capture:
            return

        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag in {"article", "section", "p", "div", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self.capture:
            return
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if self.target_id is not None:
            self.depth -= 1
            if self.depth <= 0:
                self.capture = False

    def handle_data(self, data: str) -> None:
        if self.capture and not self.skip_depth:
            s = data.strip()
            if s:
                self.parts.append(s + " ")

    def text(self) -> str:
        text = html.unescape("".join(self.parts))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()


def enrich_link_messages(
    messages: list[Message],
    api_key: str,
    progress_cb: ProgressCB | None = None,
    http_client: httpx.Client | None = None,
    anthropic_client=None,
    summary_delta_cb: Callable[[str], None] | None = None,
    usage_cb: Callable[[object, float, int], None] | None = None,
) -> EnrichStats:
    """Mutate link-card messages with short webpage summaries.

    Failures are contained per URL. If fetching or summarization fails, the
    original link-card description/title is used as a weak fallback.

    *usage_cb(usage, duration_s, input_chars)* fires once per successful
    LLM summary with the response's usage object, wall-clock seconds spent,
    and the prompt's character count — wired into ``cost_tracker.log_call``
    by the CLI.
    """
    targets = _collect_link_targets(messages)
    stats = EnrichStats(total=len(targets))
    if not targets:
        return stats

    own_http = http_client is None
    if http_client is None:
        http_client = httpx.Client(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=True,
            headers=_DEFAULT_HEADERS,
        )

    try:
        for idx, (host_idx, msg, link) in enumerate(targets, start=1):
            label = _short_label(link.title, link.url)

            title = (link.title or "").strip()
            card_desc = (link.description or "").strip()

            text = ""
            og = ""
            try:
                if progress_cb:
                    progress_cb(idx, stats.total, "抓取", label)
                text, og = fetch_url_text(link.url, http_client)
                if text:
                    stats.fetched += 1
            except Exception:
                text = ""
                og = ""
            text = text.strip()
            og = og.strip()

            total_chars = len(title) + len(card_desc) + len(og) + len(text)

            if total_chars == 0:
                stats.failed += 1
                _log_failed(link.url, label)
                continue

            if total_chars < SHORT_THRESHOLD:
                ctx = _build_short_context(title, card_desc, og, text)
                if ctx:
                    _append_link_context(msg, ctx)
                    stats.short += 1
                else:
                    stats.failed += 1
                    _log_failed(link.url, label)
                continue

            try:
                if progress_cb:
                    progress_cb(idx, stats.total, "摘要", label)
                surrounding = _build_surrounding(messages, host_idx)
                summary, summary_usage, summary_duration, summary_chars = summarize_text(
                    title=link.title,
                    url=link.url,
                    text=text,
                    card_description=card_desc,
                    og_description=og,
                    api_key=api_key,
                    client=anthropic_client,
                    surrounding=surrounding,
                    delta_cb=summary_delta_cb,
                )
                if summary.strip():
                    _append_link_context(msg, summary)
                    stats.summarized += 1
                    if usage_cb:
                        usage_cb(summary_usage, summary_duration, summary_chars)
                    continue
            except Exception:
                pass

            # Summarize raised or returned empty: fall back to short-style raw concat.
            ctx = _build_short_context(title, card_desc, og, text)
            if ctx:
                _append_link_context(msg, ctx)
                stats.short += 1
            else:
                stats.failed += 1
                _log_failed(link.url, label)
    finally:
        if own_http:
            http_client.close()

    return stats


def _build_short_context(title: str, card_desc: str, og: str, text: str) -> str:
    rows: list[str] = []
    if title:
        rows.append(f"标题：{title}")
    if card_desc:
        rows.append(f"卡片预览：{card_desc}")
    if og:
        rows.append(f"网页描述：{og}")
    if text:
        rows.append(f"正文：{text}")
    return "\n".join(rows)


def _log_failed(url: str, label: str) -> None:
    import sys
    print(f"[link-enrich] FAILED url={url} label={label}", file=sys.stderr)


def count_link_targets(messages: list[Message]) -> int:
    return len(_collect_link_targets(messages))


def _append_link_context(msg: Message, context: str) -> None:
    context = _clean_text(context)
    if not context:
        return
    if msg.link_context:
        msg.link_context += "\n" + context
    else:
        msg.link_context = context


def _collect_link_targets(
    messages: list[Message],
) -> list[tuple[int, Message, LinkMeta]]:
    targets: list[tuple[int, Message, LinkMeta]] = []
    seen: set[str] = set()

    for idx, msg in enumerate(messages):
        links: list[LinkMeta] = []
        if msg.local_type in (MSG_LINK_CARD, MSG_LINK_OPEN) and msg.link and msg.link.url:
            links.append(msg.link)

        inline_links = _extract_inline_links(msg.content)
        if inline_links:
            msg.inline_links = inline_links
            links.extend(inline_links)

        for link in links:
            if link.url in seen:
                continue
            seen.add(link.url)
            targets.append((idx, msg, link))

    return targets


def _build_surrounding(
    messages: list[Message],
    host_idx: int,
    window: int = 10,
) -> str:
    """Format ±window non-system messages around messages[host_idx].

    Senders are anonymized to letter codes (A, B, C, ...) assigned in order
    of first appearance within the window. The host message is marked with
    a `[本次要总结的链接]` prefix so the model knows which line is the link
    being summarized. Hidden-message placeholders are kept verbatim.
    """
    start = max(0, host_idx - window)
    end = min(len(messages), host_idx + window + 1)

    sender_letter: dict[str, str] = {}
    next_ord = ord("A")
    lines: list[str] = []

    for j in range(start, end):
        m = messages[j]
        if m.local_type in (MSG_SYSTEM, MSG_TAP):
            continue
        ts = datetime.fromtimestamp(m.create_time).strftime("%H:%M")
        content = (m.content or "").strip()
        if not content:
            continue
        marker = "[本次要总结的链接] " if j == host_idx else ""
        if not m.sender_wxid:
            # Hidden-message placeholder or system-style line without a sender.
            lines.append(f"[{ts}] {marker}{content}")
            continue
        if m.sender_wxid not in sender_letter:
            sender_letter[m.sender_wxid] = chr(next_ord)
            next_ord += 1
        label = sender_letter[m.sender_wxid]
        lines.append(f"[{ts}] {label}: {marker}{content}")

    return "\n".join(lines)


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
# Inline URLs must stop at non-URL characters. The previous `[^\s<>()]+` was
# too permissive — it greedily swallowed trailing Chinese punctuation/text
# (e.g. "https://github.com/x/y，关键是…"), producing un-fetchable garbage.
# This class is the RFC 3986 unreserved + sub-delim + gen-delim set minus
# `()` (kept as terminators for markdown-wrapped URLs).
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&'*+,;=%\[\]]+")


def _extract_inline_links(text: str) -> list[LinkMeta]:
    links: list[LinkMeta] = []
    seen: set[str] = set()

    protected: list[tuple[int, int]] = []
    for m in _MARKDOWN_LINK_RE.finditer(text):
        title = m.group(1).strip()
        url = _trim_url(m.group(2))
        if url and url not in seen:
            links.append(LinkMeta(title=title, url=url))
            seen.add(url)
        protected.append((m.start(2), m.end(2)))

    for m in _URL_RE.finditer(text):
        if any(start <= m.start() < end for start, end in protected):
            continue
        url = _trim_url(m.group(0))
        if url and url not in seen:
            links.append(LinkMeta(url=url))
            seen.add(url)

    return links


def _trim_url(url: str) -> str:
    return html.unescape(url).rstrip(".,，。；;：:!?！？\"'）】》")


def fetch_url_text(url: str, client: httpx.Client) -> tuple[str, str]:
    """Fetch a URL and return (main_text, og_description).

    `og_description` is non-empty only for HTML responses where
    `<meta property="og:description">` or `<meta name="description">` is set.
    Specialized paths (twitter via fxtwitter, github raw, circle JSON) return
    `og=""`.
    """
    host = _host(url)

    # Domains that gate content behind JS / device fingerprinting / login.
    # Plain HTTP GETs only ever return a shell, so skip them outright.
    if "xiaohongshu.com" in host or "douyin.com" in host:
        return "", ""

    if "superlinear.academy" in host:
        text = _fetch_circle_post_text(url, client)
        if text:
            return text, ""

    if host in {"x.com", "twitter.com"}:
        return _fetch_tweet(url, client), ""

    raw_url = _github_raw_url(url)
    if raw_url:
        response = client.get(raw_url)
        response.raise_for_status()
        return _clean_text(response.text), ""

    response = client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")

    if "mp.weixin.qq.com" in host:
        body = _extract_html_text(response.text, target_id="js_content")
        og = _extract_og_description(response.text)
        return body, og

    if "text/plain" in content_type:
        return _clean_text(response.text), ""

    body = _extract_readable_text(response.text)
    og = _extract_og_description(response.text)
    return body, og


def summarize_text(
    *,
    title: str,
    url: str,
    text: str,
    api_key: str,
    client=None,
    surrounding: str = "",
    card_description: str = "",
    og_description: str = "",
    delta_cb: Callable[[str], None] | None = None,
) -> tuple[str, object, float, int]:
    """Stream a webpage summary from the link-summary model.

    Returns ``(summary_text, usage, duration_s, prompt_chars)``. ``usage``
    is whatever the SDK returns from ``stream.get_final_message().usage``
    (or ``None`` if the test stub doesn't implement it). ``prompt_chars`` is
    the length of the rendered user prompt, used by the CLI to compute a
    tok/char ratio in the cost summary.
    """
    import time

    webpage_block = _build_webpage_block(
        title=title,
        url=url,
        text=text,
        card_description=card_description,
        og_description=og_description,
    )
    if surrounding.strip():
        prompt = _SUMMARY_PROMPT.format(
            surrounding=surrounding,
            webpage_block=webpage_block,
        )
    else:
        prompt = _SUMMARY_PROMPT_NO_CONTEXT.format(webpage_block=webpage_block)

    # DeepSeek backend (current AB-test config): no Anthropic SDK, thinking off
    # for summaries. The Anthropic path below is kept as a fallback in case
    # LINK_SUMMARY_MODEL is pointed back at a Claude model.
    if LINK_SUMMARY_MODEL.startswith("deepseek") and client is None:
        return _summarize_deepseek(prompt, delta_cb)

    if client is None:
        import anthropic
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(120.0, connect=15.0),
        )

    parts: list[str] = []
    usage = None
    t0 = time.perf_counter()
    with client.messages.stream(
        model=LINK_SUMMARY_MODEL,
        max_tokens=2500,
        system="你是一个网页内容摘要器。直接输出摘要正文。",
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for event in stream:
            etype = getattr(event, "type", None)
            delta = getattr(event, "text", None)
            if isinstance(delta, str) and etype == "text":
                parts.append(delta)
                if delta_cb:
                    delta_cb(delta)
        # get_final_message is the documented way to read usage off a
        # finished stream. Test stubs that only implement __iter__ will
        # raise AttributeError here — leave usage=None and move on.
        try:
            usage = getattr(stream.get_final_message(), "usage", None)
        except AttributeError:
            usage = None
    duration_s = time.perf_counter() - t0
    return _clean_text("".join(parts)), usage, duration_s, len(prompt)


def _summarize_deepseek(
    prompt: str,
    delta_cb: Callable[[str], None] | None,
) -> tuple[str, object, float, int]:
    """Link-summary via DeepSeek (OpenAI-compatible), thinking disabled.

    Same return contract as :func:`summarize_text`. Raises ``RuntimeError``
    if the key is missing — :func:`enrich_link_messages` catches summary
    failures and falls back to raw-concat context.
    """
    import time

    from . import deepseek_client
    from .config import get_deepseek_key

    key = get_deepseek_key()
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，无法生成链接摘要")

    t0 = time.perf_counter()
    content, _reasoning, usage, _finish = deepseek_client.stream_chat(
        api_key=key,
        model=LINK_SUMMARY_MODEL,
        system="你是一个网页内容摘要器。直接输出摘要正文。",
        user=prompt,
        thinking=False,
        max_tokens=2500,
        content_cb=delta_cb,
    )
    duration_s = time.perf_counter() - t0
    return _clean_text(content), usage, duration_s, len(prompt)


def _fetch_tweet(url: str, client: httpx.Client) -> str:
    api_url = _fxtwitter_url(url)
    if not api_url:
        return ""
    response = client.get(api_url)
    response.raise_for_status()
    data = response.json()
    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict):
        return ""
    author = tweet.get("author") or {}
    author_name = author.get("name") or author.get("screen_name") or ""
    text = tweet.get("text") or ""
    if not text and isinstance(tweet.get("raw_text"), dict):
        text = tweet["raw_text"].get("text") or ""
    return _clean_text(f"{author_name}: {text}" if author_name else text)


def _fetch_circle_post_text(url: str, client: httpx.Client) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3 or parts[0] != "c":
        return ""

    space_slug = parts[1]
    post_slug = parts[2]
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Seed cookies and Cloudflare/session state. The HTML itself usually only
    # contains a shell; Circle renders the post from internal JSON.
    client.get(url)

    spaces_response = client.get(f"{origin}/internal_api/spaces?include_sidebar=true")
    spaces_response.raise_for_status()
    space_id = _circle_space_id(spaces_response.json(), space_slug)
    if space_id is None:
        return ""

    post_response = client.get(
        f"{origin}/internal_api/spaces/{space_id}/posts/{post_slug}?"
    )
    post_response.raise_for_status()
    data = post_response.json()

    tiptap = data.get("tiptap_body")
    text = _tiptap_text(tiptap)
    if text:
        return _clean_text(text)

    body = data.get("body")
    if isinstance(body, dict):
        plain = body.get("body_plain_text") or body.get("plain_text") or ""
        if plain:
            return _clean_text(plain)
        html_body = body.get("html") or body.get("body") or ""
        if html_body:
            return _extract_html_text(str(html_body))

    return _clean_text(data.get("truncated_content") or "")


def _circle_space_id(payload, slug: str) -> int | None:
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return None
    for item in records:
        if isinstance(item, dict) and item.get("slug") == slug:
            space_id = item.get("id")
            return int(space_id) if isinstance(space_id, int | str) else None
    return None


def _tiptap_text(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    root = payload.get("body") if isinstance(payload.get("body"), dict) else payload
    return _tiptap_node_text(root)


def _tiptap_node_text(node) -> str:
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text") or node.get("circle_ios_fallback_text") or "")
    if node_type in {"mention", "entity"}:
        return str(node.get("circle_ios_fallback_text") or "")
    if node_type == "hardBreak":
        return "\n"

    parts = [_tiptap_node_text(child) for child in node.get("content") or []]
    text = "".join(parts)
    if node_type in {
        "paragraph", "heading", "blockquote", "listItem",
        "bulletList", "orderedList", "codeBlock",
    }:
        return text + "\n"
    return text


def _extract_readable_text(source: str) -> str:
    try:
        import trafilatura
    except ImportError:
        return _extract_html_text(source)

    extracted = trafilatura.extract(
        source,
        include_comments=False,
        include_tables=False,
        output_format="txt",
    )
    if extracted:
        return _clean_text(extracted)
    return _extract_html_text(source)


def _extract_html_text(source: str, target_id: str | None = None) -> str:
    parser = _TextExtractor(target_id=target_id)
    parser.feed(source[:3_000_000])
    return parser.text()


def _build_webpage_block(
    *,
    title: str,
    url: str,
    text: str,
    card_description: str,
    og_description: str,
) -> str:
    parts = [
        f"<title>{(title or '(无标题)').strip()}</title>",
        f"<url>{url}</url>",
    ]
    if card_description.strip():
        parts.append(f"<card_preview>{card_description.strip()}</card_preview>")
    if og_description.strip():
        parts.append(f"<meta_description>{og_description.strip()}</meta_description>")
    parts.append(f"<content>\n{text[:50000]}\n</content>")
    return "<webpage>\n" + "\n".join(parts) + "\n</webpage>"


class _MetaCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_description = ""
        self.description = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "meta":
            return
        d = {k: (v or "") for k, v in attrs}
        prop = d.get("property", "").lower()
        name = d.get("name", "").lower()
        content = d.get("content", "")
        if prop == "og:description" and not self.og_description:
            self.og_description = content
        elif name == "description" and not self.description:
            self.description = content


def _extract_og_description(html_text: str) -> str:
    parser = _MetaCollector()
    try:
        parser.feed(html_text[:300_000])
    except Exception:
        pass
    return _clean_text(parser.og_description or parser.description)


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _short_label(title: str, url: str) -> str:
    text = title.strip() or _host(url) or url
    return text[:48] + ("..." if len(text) > 48 else "")


def _fxtwitter_url(url: str) -> str | None:
    m = re.search(r"(?:x|twitter)\.com/([^/?#]+)/status/(\d+)", url)
    if not m:
        return None
    return f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}"


def _github_raw_url(url: str) -> str | None:
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)", url)
    if not m:
        return None
    owner, repo, ref, path = m.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


def dump_fetch_diagnostics(messages: list[Message]) -> str:
    """Return compact JSON diagnostics useful for manual link-fetch probes."""
    rows = []
    for msg in messages:
        if msg.link:
            rows.append({
                "title": msg.link.title,
                "url": msg.link.url,
                "des_chars": len(msg.link.description),
                "context_chars": len(msg.link_context),
            })
    return json.dumps(rows, ensure_ascii=False, indent=2)
