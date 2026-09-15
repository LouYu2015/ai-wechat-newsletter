"""Fetch and summarize link-card targets for LLM context."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime
import html
import html.parser
import json
import re
import urllib.parse
from typing import Callable

import httpx

from wechat_daily import config, message_parser

ProgressCB = Callable[[int, int, str, str], None]


class _NullReporter:
    """No-op lane reporter so workers can call start/phase/delta/done freely."""

    def start(self, *a) -> None: ...
    def phase(self, *a) -> None: ...
    def delta(self, *a) -> None: ...
    def done(self, *a, **k) -> None: ...


_NULL_REPORTER = _NullReporter()

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
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
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

# Link-summary prompt, chosen by blind LLM-judge rounds on real links (see
# data/experiment/20260915-deepseek-flash-vs-pro-link-summary). Key lessons:
# asking to "keep facts and numbers" made models retell the page in order;
# a pick-first rule plus an explicit delete list is what actually compressed.
# Assembled from pieces so the with/without-chat variants can't drift apart.
_SUMMARY_SYSTEM = (
    "你是日报编辑的资料员：把群里分享的网页压缩成给编辑看的要点摘要。直接输出摘要正文。"
)

_PROMPT_HEAD = """\
这份摘要是给「微信 AI 技术讨论群日报」编辑的背景资料，不直接发表。编辑拿它做三件事：判断这条链接值不值得写、看懂群友在围绕网页里的哪一点讨论、挑一两个事实或数字写进日报。需要更多细节时编辑会自己点开原文——所以摘要的任务是**帮编辑抓住重点**，不是替代原文，更不是按原文顺序复述。
读者是中文程序员 / AI 实战派，LLM、agent、RAG、context window、prompt cache、tool use、MCP、skills、evals、Claude Code / Codex / Cursor 等不需要解释。"""

_PROMPT_CHAT = """

<surrounding_chat>
{surrounding}
</surrounding_chat>

上面是链接前后约 10 条群聊（发言人已匿名，`[本次要总结的链接]` 标出分享那一条）。它只用来判断群友关心网页里的哪一点：如果能看出群友在讨论网页里的某个具体点，把网页中关于这一点的内容写清楚。摘要里不要复述、引用或评价群友发言，不要替群友补充理由，也不要猜测「群友可能关心什么」——看不出关联时就当没有这段群聊。"""

_PROMPT_PAGE = """

{webpage_block}

`content` 是抓取到的正文，`card_preview` 是微信卡片预览（分享者发出时看到的那段话），`meta_description` 是网页描述。以 content 为主，其他字段用于交叉印证或在 content 不足时补充。"""

_PROMPT_PICK_CHAT = "群友正在讨论的那个点（如果看得出来）必须在其中，哪怕它在原文里只占一小段。"

_PROMPT_RULES = """

## 先选，再写
动笔前先想清楚：这个网页最核心的一句话是什么？支撑它的最关键的 3–5 个点是什么？<pick_chat>只写这些，其余一律删。

值得留下的点：
- 新消息 / 核心结论本身，以及谁发布、谁说的。
- 能改变判断的硬数字：关键 benchmark 分数、价格、版本号、日期、前后对比。一个论点配一两个最有说服力的数字即可。
- 反常识的发现、争议、作者的明确判断和理由。
- 负面信息和适用边界：官方自己承认的缺陷、风险、局限、「不适合做什么」——这些往往比正面宣传更值得留。
- 可以直接拿去用的做法。

直接删掉的东西：
- 实验口径与计算参数（筛选条件、分位数定义、电价功率等）、次要对比、长尾型号或数据。
- 安装步骤、命令、配置项、路径、依赖、FAQ、排障——最多一句「支持 X / 提供 Y」。
- 官方宣传语、营销话术、背景科普、作者 / 公众号 / 转载信息、结尾号召、相关推荐。
- 重复论证同一观点的多个例子（留一个）、原文引语（最多留一句最有冲击力的）。

## 准确性（最容易出错的地方）
- 数字照抄原文，不自己换算或约略：原文写「两倍以上」就不要写成「约两倍」，原文给了 1.9 亿和 6000 万就直接写这两个数。
- 保留原文的限定和不确定性：「可能 / likely / 据报道 / 据称 / 初步」不能写成确定的事实；「无幻觉满分」不能简化成「满分」。
- 分清是谁的话：当事人原话、网页作者或媒体自己的叙述、第三方评论，归属不要张冠李戴。整篇是二手转述、单方声明、编译稿或带产品植入的软文时，用半句话点明。
- 只写网页里有的内容，不补外部知识，不猜数字。正文明显不完整（只抓到片段、页面壳、登录墙）时开头注明「只抓到部分内容」。

## 写法
- 中文纯文本，不用 Markdown（标题、列表符号、加粗、链接都不要），不贴 URL。
- 第一句就是核心结论，不铺垫。超过约 400 字时按主题分成 2–3 段，每段开头一句点明这段讲什么；不要写成一整块。
- 长度硬性目标：一般 300–600 字；短新闻、短帖、单条推文 100–250 字；长篇报告或信息特别密集的长文最多 900 字。每段 2–4 句。超出时优先删次要数字和第二个例子，而不是删核心结论、负面信息或群友讨论的点。
- 直接陈述事实，不写「本文 / 这篇文章 / 作者认为 / 文章指出 / 文中还提到」这类转述壳；标明来源时直接写人名或机构名。
- 不写你自己对价值的评语（「最值得写」「很有参考价值」「最出圈的一句」），取舍交给编辑。

直接输出摘要正文。
"""

_SUMMARY_PROMPT = (
    _PROMPT_HEAD
    + _PROMPT_CHAT
    + _PROMPT_PAGE
    + _PROMPT_RULES.replace("<pick_chat>", _PROMPT_PICK_CHAT)
)
_SUMMARY_PROMPT_NO_CONTEXT = _PROMPT_HEAD + _PROMPT_PAGE + _PROMPT_RULES.replace("<pick_chat>", "")


@dataclasses.dataclass
class EnrichStats:
    total: int = 0
    fetched: int = 0  # debug-only: URLs where http fetch returned non-empty text
    summarized: int = 0  # total inputs ≥ SHORT_THRESHOLD, LLM produced summary
    short: int = 0  # 0 < total inputs < SHORT_THRESHOLD, raw concat used
    failed: int = 0  # no inputs at all (no title/desc/og/text)


SHORT_THRESHOLD = 800

# Thinking tokens count toward max_tokens. The old 2500 cap truncated ~26% of
# real summaries (Sept 2026) — many to an empty answer, which fell back to raw
# page text. 16000 left every tested page (up to 220k chars) room to finish.
_DEEPSEEK_MAX_TOKENS = 16000
_DEEPSEEK_ATTEMPTS = 2


class _TextExtractor(html.parser.HTMLParser):
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
            self.target_id is not None and not self.capture and attr_map.get("id") == self.target_id
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
    messages: list[message_parser.Message],
    api_key: str,
    reporter=None,
    http_client: httpx.Client | None = None,
    anthropic_client=None,
    usage_cb: Callable[[object, float, int], None] | None = None,
    max_workers: int = 5,
) -> EnrichStats:
    """Mutate link-card messages with short webpage summaries.

    Fetch + summary for each target run concurrently in a thread pool (each
    target is an independent fetch + one LLM call). Workers report their
    lifecycle to *reporter* (a :class:`wechat_daily.lanes_ui.Lanes`, or any
    object with ``start/phase/delta/done``) keyed by the link's url, so a live
    parallel-lanes UI can stream each summary into its own lane without
    interleaving. Result *application* still happens single-threaded in target
    order, so the per-message ``link_context`` is byte-identical to the old
    sequential loop — the canonical Opus input is unaffected.

    Failures are contained per URL. If fetching or summarization fails, the
    original link-card description/title is used as a weak fallback.

    *usage_cb(usage, duration_s, input_chars)* fires once per successful
    LLM summary with the response's usage object, wall-clock seconds spent,
    and the prompt's character count — wired into ``cost_tracker.log_call``
    by the CLI.
    """
    rep = reporter if reporter is not None else _NULL_REPORTER
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

    workers = max(1, min(max_workers, len(targets)))

    def _work(
        item: tuple[int, tuple[int, message_parser.Message, message_parser.LinkMeta]],
    ) -> dict:
        """Fetch + summarize one target; report lane events keyed by url.

        Pure w.r.t. shared report state: only mutates the reporter (thread-safe)
        and returns a result the main thread applies. ``link_context`` is never
        touched here.
        """
        _order, (host_idx, msg, link) = item
        uid = link.url
        label = _short_label(link.title, link.url)
        rep.start(uid, label)
        title = (link.title or "").strip()
        card_desc = (link.description or "").strip()

        text = ""
        og = ""
        try:
            rep.phase(uid, "抓取")
            text, og = fetch_url_text(link.url, http_client)
        except Exception:
            text, og = "", ""
        text = text.strip()
        og = og.strip()

        base = {"msg": msg, "url": link.url, "label": label, "fetched": bool(text)}
        total_chars = len(title) + len(card_desc) + len(og) + len(text)

        if total_chars == 0:
            rep.done(uid, "failed", error="无正文")
            return {**base, "kind": "failed", "context": None, "usage": None}

        if total_chars < SHORT_THRESHOLD:
            ctx = _build_short_context(title, card_desc, og, text)
            rep.done(uid, "short" if ctx else "failed", error=None if ctx else "无正文")
            return {
                **base,
                "kind": "short" if ctx else "failed",
                "context": ctx or None,
                "usage": None,
            }

        try:
            rep.phase(uid, "摘要")
            surrounding = _build_surrounding(messages, host_idx)
            summary, s_usage, s_dur, s_chars = summarize_text(
                title=link.title,
                url=link.url,
                text=text,
                card_description=card_desc,
                og_description=og,
                api_key=api_key,
                client=anthropic_client,
                surrounding=surrounding,
                delta_cb=lambda d: rep.delta(uid, d),
            )
            if summary.strip():
                rep.done(uid, "summary")
                return {
                    **base,
                    "kind": "summary",
                    "context": summary,
                    "usage": (s_usage, s_dur, s_chars),
                }
        except Exception:
            pass

        # Summarize raised or returned empty: fall back to short-style raw concat.
        ctx = _build_short_context(title, card_desc, og, text)
        rep.done(uid, "short" if ctx else "failed", error=None if ctx else "摘要失败")
        return {**base, "kind": "short" if ctx else "failed", "context": ctx or None, "usage": None}

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            # pool.map preserves input order → results are in target order, so
            # applying them below reproduces the sequential loop's behavior.
            results = list(pool.map(_work, enumerate(targets)))
    finally:
        if own_http:
            http_client.close()

    for r in results:
        if r["fetched"]:
            stats.fetched += 1
        if r["kind"] == "summary":
            _append_link_context(r["msg"], r["context"])
            stats.summarized += 1
            if usage_cb and r["usage"]:
                usage_cb(*r["usage"])
        elif r["kind"] == "short":
            _append_link_context(r["msg"], r["context"])
            stats.short += 1
        else:
            stats.failed += 1
            _log_failed(r["url"], r["label"])

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


def count_link_targets(messages: list[message_parser.Message]) -> int:
    return len(_collect_link_targets(messages))


def _append_link_context(msg: message_parser.Message, context: str) -> None:
    context = _clean_text(context)
    if not context:
        return
    if msg.link_context:
        msg.link_context += "\n" + context
    else:
        msg.link_context = context


def _collect_link_targets(
    messages: list[message_parser.Message],
) -> list[tuple[int, message_parser.Message, message_parser.LinkMeta]]:
    targets: list[tuple[int, message_parser.Message, message_parser.LinkMeta]] = []
    seen: set[str] = set()

    for idx, msg in enumerate(messages):
        links: list[message_parser.LinkMeta] = []
        if (
            msg.local_type in (message_parser.MSG_LINK_CARD, message_parser.MSG_LINK_OPEN)
            and msg.link
            and msg.link.url
        ):
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
    messages: list[message_parser.Message],
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
        if m.local_type in (message_parser.MSG_SYSTEM, message_parser.MSG_TAP):
            continue
        ts = datetime.datetime.fromtimestamp(m.create_time).strftime("%H:%M")
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


def _extract_inline_links(text: str) -> list[message_parser.LinkMeta]:
    links: list[message_parser.LinkMeta] = []
    seen: set[str] = set()

    protected: list[tuple[int, int]] = []
    for m in _MARKDOWN_LINK_RE.finditer(text):
        title = m.group(1).strip()
        url = _trim_url(m.group(2))
        if url and url not in seen:
            links.append(message_parser.LinkMeta(title=title, url=url))
            seen.add(url)
        protected.append((m.start(2), m.end(2)))

    for m in _URL_RE.finditer(text):
        if any(start <= m.start() < end for start, end in protected):
            continue
        url = _trim_url(m.group(0))
        if url and url not in seen:
            links.append(message_parser.LinkMeta(url=url))
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

    # DeepSeek backend (current AB-test config): no Anthropic SDK, thinking on
    # for summaries. The Anthropic path below is kept as a fallback in case
    # LINK_SUMMARY_MODEL is pointed back at a Claude model.
    if config.LINK_SUMMARY_MODEL.startswith("deepseek") and client is None:
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
        model=config.LINK_SUMMARY_MODEL,
        max_tokens=2500,
        system=_SUMMARY_SYSTEM,
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
    """Link-summary via DeepSeek (OpenAI-compatible), thinking enabled.

    Same return contract as :func:`summarize_text`. Raises ``RuntimeError``
    if the key is missing — :func:`enrich_link_messages` catches summary
    failures and falls back to raw-concat context.

    An empty answer or an API error is retried once before giving up: the
    fallback dumps the raw page text (up to 50k chars) into the report input,
    so one extra cheap call is always worth it. Usage is summed across
    attempts so the cost log stays honest.
    """
    import time

    from wechat_daily import config, deepseek_client

    key = config.get_deepseek_key()
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，无法生成链接摘要")

    t0 = time.perf_counter()
    usage: dict = {}
    content = ""
    for attempt in range(_DEEPSEEK_ATTEMPTS):
        try:
            content, _reasoning, attempt_usage, _finish = deepseek_client.stream_chat(
                api_key=key,
                model=config.LINK_SUMMARY_MODEL,
                system=_SUMMARY_SYSTEM,
                user=prompt,
                thinking=True,
                max_tokens=_DEEPSEEK_MAX_TOKENS,
                content_cb=delta_cb,
            )
        except deepseek_client.DeepSeekError:
            if attempt == _DEEPSEEK_ATTEMPTS - 1:
                raise
            continue
        for k, v in attempt_usage.items():
            if isinstance(v, int | float):
                usage[k] = usage.get(k, 0) + v
        if _clean_text(content):
            break
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
    parsed = urllib.parse.urlparse(url)
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

    post_response = client.get(f"{origin}/internal_api/spaces/{space_id}/posts/{post_slug}?")
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
        "paragraph",
        "heading",
        "blockquote",
        "listItem",
        "bulletList",
        "orderedList",
        "codeBlock",
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


class _MetaCollector(html.parser.HTMLParser):
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
    host = urllib.parse.urlparse(url).netloc.lower()
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


def dump_fetch_diagnostics(messages: list[message_parser.Message]) -> str:
    """Return compact JSON diagnostics useful for manual link-fetch probes."""
    rows = []
    for msg in messages:
        if msg.link:
            rows.append(
                {
                    "title": msg.link.title,
                    "url": msg.link.url,
                    "des_chars": len(msg.link.description),
                    "context_chars": len(msg.link_context),
                }
            )
    return json.dumps(rows, ensure_ascii=False, indent=2)
