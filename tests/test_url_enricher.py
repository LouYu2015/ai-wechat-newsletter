from __future__ import annotations

import httpx

from wechat_daily.config import LINK_SUMMARY_MODEL
from wechat_daily.message_parser import LinkMeta, Message, MSG_LINK_OPEN, MSG_TEXT
from wechat_daily.url_enricher import enrich_link_messages, fetch_url_text


class FakeHTTP:
    def __init__(self, routes: dict[str, tuple[str, str]]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    def get(self, url: str):
        self.urls.append(url)
        text, content_type = self.routes[url]
        return httpx.Response(
            200,
            text=text,
            headers={"content-type": content_type},
            request=httpx.Request("GET", url),
        )


class TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _TextEvent:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeStream:
    def __init__(self, text: str, usage: object | None = None) -> None:
        self._text = text
        self._usage = usage

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield _TextEvent(self._text)

    def get_final_message(self):
        # Only present when constructed with a usage object — otherwise the
        # production code's try/except AttributeError path is exercised.
        if self._usage is None:
            raise AttributeError("test stub: no final message")
        return type("Response", (), {"content": [], "usage": self._usage})()


class FakeMessages:
    def __init__(self, text: str, usage: object | None = None) -> None:
        self.text = text
        self.usage = usage
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return type("Response", (), {"content": [TextBlock(self.text)]})()

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(self.text, self.usage)


class FakeAnthropic:
    def __init__(
        self, text: str = "这是网页摘要", usage: object | None = None,
    ) -> None:
        self.messages = FakeMessages(text, usage=usage)


def _link_msg(title="Title", url="https://example.com/a", des="card des"):
    return Message(
        create_time=1000,
        local_type=MSG_LINK_OPEN,
        sender_wxid="wxid",
        content=f"[链接] [{title}]({url})",
        link=LinkMeta(title=title, url=url, description=des),
    )


class _EchoMarkerAnthropic:
    """Summary client that echoes whichever MARKER<n> token is in the prompt.

    Lets a parallel run assert each fetched page's summary lands on its own
    message (no cross-thread mixups).
    """

    def __init__(self) -> None:
        outer = self

        class _Msgs:
            def stream(self, **kwargs):
                prompt = kwargs["messages"][0]["content"]
                marker = next(
                    (m for m in ("MARKER0", "MARKER1", "MARKER2") if m in prompt),
                    "NONE",
                )
                return _FakeStream(f"summary-{marker}")

        self.messages = _Msgs()


def test_enrich_parallel_maps_each_summary_to_its_own_message():
    # Three distinct long pages (>SHORT_THRESHOLD) → summary path, run in pool.
    msgs = []
    routes = {}
    for i in range(3):
        url = f"https://example.com/p{i}"
        body = f"MARKER{i} " + ("正文内容很长所以走摘要路径。" * 80)  # >800 chars
        routes[url] = (body, "text/plain")
        msgs.append(_link_msg(title=f"T{i}", url=url))

    stats = enrich_link_messages(
        msgs,
        api_key="k",
        http_client=FakeHTTP(routes),
        anthropic_client=_EchoMarkerAnthropic(),
        max_workers=3,
    )

    assert stats.total == 3 and stats.summarized == 3
    # Each message carries exactly its own page's summary — no shuffling.
    for i, m in enumerate(msgs):
        assert f"summary-MARKER{i}" in m.link_context


def test_fetch_wechat_js_content():
    url = "https://mp.weixin.qq.com/s?mid=1"
    html = """
    <html><head>
      <meta property="og:description" content="og 摘要内容">
    </head><body><div>nav</div><div id="js_content">
      <h1>标题</h1><p>第一段正文。</p><script>ignore()</script><p>第二段正文。</p>
    </div></body></html>
    """
    client = FakeHTTP({url: (html, "text/html")})
    text, og = fetch_url_text(url, client)
    assert "第一段正文" in text
    assert "第二段正文" in text
    assert "ignore" not in text
    assert og == "og 摘要内容"


def test_fetch_x_uses_fxtwitter():
    url = "https://x.com/example_user/status/2049645973350363168?s=46"
    api = "https://api.fxtwitter.com/example_user/status/2049645973350363168"
    client = FakeHTTP({
        api: (
            '{"tweet":{"text":"tweet body","author":{"name":"Theo"}}}',
            "application/json",
        )
    })
    text, og = fetch_url_text(url, client)
    assert client.urls == [api]
    assert text == "Theo: tweet body"
    assert og == ""


def test_fetch_github_blob_uses_raw_url():
    url = "https://github.com/acme/repo/blob/main/README.md"
    raw = "https://raw.githubusercontent.com/acme/repo/main/README.md"
    client = FakeHTTP({raw: ("# README\nbody", "text/plain")})
    assert fetch_url_text(url, client) == ("# README\nbody", "")
    assert client.urls == [raw]


def test_fetch_superlinear_uses_circle_internal_api():
    url = "https://www.superlinear.academy/c/share-your-insights/fde"
    spaces = "https://www.superlinear.academy/internal_api/spaces?include_sidebar=true"
    post = "https://www.superlinear.academy/internal_api/spaces/2231585/posts/fde?"
    client = FakeHTTP({
        url: ("<html><title>shell</title></html>", "text/html"),
        spaces: ('{"records":[{"slug":"share-your-insights","id":2231585}]}', "application/json"),
        post: (
            """
            {
              "tiptap_body": {
                "body": {
                  "type": "doc",
                  "content": [
                    {"type": "heading", "attrs": {"level": 2}, "content": [
                      {"type": "text", "text": "章节标题"}
                    ]},
                    {"type": "paragraph", "content": [
                      {"type": "text", "text": "正文 "},
                      {"type": "mention", "circle_ios_fallback_text": "@Person"},
                      {"type": "text", "text": " 内容"}
                    ]},
                    {"type": "blockquote", "content": [
                      {"type": "paragraph", "content": [
                        {"type": "text", "text": "引用内容"}
                      ]}
                    ]}
                  ]
                }
              }
            }
            """,
            "application/json",
        ),
    })
    text, og = fetch_url_text(url, client)
    assert client.urls == [url, spaces, post]
    assert "章节标题" in text
    assert "正文 @Person 内容" in text
    assert "引用内容" in text
    assert og == ""


def test_enrich_summarizes_fetched_text():
    url = "https://example.com/a"
    html = "<article><p>" + ("正文 " * 500) + "</p></article>"
    msg = _link_msg(url=url)
    client = FakeHTTP({url: (html, "text/html")})
    anthropic = FakeAnthropic("摘要结果")

    stats = enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=anthropic,
    )

    assert stats.summarized == 1
    assert msg.link_context == "摘要结果"
    call = anthropic.messages.calls[0]
    # An injected client is always honored (the Anthropic fallback path); the
    # model passed through is whatever LINK_SUMMARY_MODEL is configured to.
    assert call["model"] == LINK_SUMMARY_MODEL
    assert "thinking" not in call


def test_enrich_usage_cb_called_per_summary():
    """Each successful LLM summary triggers usage_cb(usage, duration, chars)."""
    class _Usage:
        input_tokens = 1234
        output_tokens = 567

    url = "https://example.com/a"
    html = "<article><p>" + ("正文 " * 500) + "</p></article>"
    msg = _link_msg(url=url)
    client = FakeHTTP({url: (html, "text/html")})
    anthropic = FakeAnthropic("摘要结果", usage=_Usage())

    seen: list[tuple] = []
    stats = enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=anthropic,
        usage_cb=lambda u, d, c: seen.append((u, d, c)),
    )

    assert stats.summarized == 1
    assert len(seen) == 1
    usage, duration_s, input_chars = seen[0]
    assert usage.input_tokens == 1234
    assert duration_s >= 0
    assert input_chars > 0


def test_enrich_usage_cb_not_called_on_short_path():
    """The short-path (no LLM) shouldn't emit a usage record."""
    msg = _link_msg(
        title="小红书标题",
        url="https://www.xiaohongshu.com/discovery/item/1",
        des="卡片摘要",
    )
    seen = []
    stats = enrich_link_messages(
        [msg], api_key="fake",
        http_client=FakeHTTP({}),
        anthropic_client=FakeAnthropic(),
        usage_cb=lambda u, d, c: seen.append((u, d, c)),
    )
    assert stats.short == 1
    assert seen == []


def test_enrich_uses_short_path_when_fetch_returns_empty():
    msg = _link_msg(
        title="小红书标题",
        url="https://www.xiaohongshu.com/discovery/item/1",
        des="卡片摘要",
    )
    stats = enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=FakeHTTP({}),
        anthropic_client=FakeAnthropic(),
    )
    assert stats.short == 1
    assert msg.link_context == "标题：小红书标题\n卡片预览：卡片摘要"


def test_enrich_short_path_skips_llm_when_total_below_threshold():
    url = "https://example.com/short"
    html = (
        '<html><head>'
        '<meta property="og:description" content="只有 og 描述这一段">'
        '</head><body><article><p>正文太短</p></article></body></html>'
    )
    msg = Message(
        create_time=1000,
        local_type=MSG_TEXT,
        sender_wxid="wxid",
        content=f"看看这个 {url}",
    )
    client = FakeHTTP({url: (html, "text/html")})
    anthropic = FakeAnthropic("不应被调用")

    stats = enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=anthropic,
    )

    assert stats.short == 1
    assert stats.summarized == 0
    assert anthropic.messages.calls == []  # LLM not invoked
    assert "网页描述：只有 og 描述这一段" in msg.link_context
    assert "正文：正文太短" in msg.link_context


def test_enrich_failed_when_no_inputs():
    url = "https://example.com/empty"
    msg = Message(
        create_time=1000,
        local_type=MSG_TEXT,
        sender_wxid="wxid",
        content=f"参考 {url}",
    )
    # Empty HTML body → no main text, no og.
    client = FakeHTTP({url: ("<html></html>", "text/html")})

    stats = enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=FakeAnthropic(),
    )

    assert stats.failed == 1
    assert stats.short == 0
    assert stats.summarized == 0
    assert msg.link_context == ""


def test_summarize_prompt_includes_card_and_og_tags():
    url = "https://example.com/long"
    html = "<article><p>" + ("正文 " * 500) + "</p></article>"
    msg = _link_msg(url=url, des="卡片描述内容")
    client = FakeHTTP({url: (html, "text/html")})
    anthropic = FakeAnthropic("摘要")

    enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=anthropic,
    )

    call_messages = anthropic.messages.calls[0]["messages"]
    prompt = call_messages[0]["content"]
    assert "<card_preview>卡片描述内容</card_preview>" in prompt
    # og missing in this fixture → tag should be omitted
    assert "<meta_description>" not in prompt
    assert "<content>" in prompt


def test_enrich_extracts_inline_plain_url():
    url = "https://example.com/inline"
    msg = Message(
        create_time=1000,
        local_type=MSG_TEXT,
        sender_wxid="wxid",
        content=f"可以看看这个 {url}",
    )
    client = FakeHTTP({url: ("<article><p>" + ("正文 " * 500) + "</p></article>", "text/html")})

    stats = enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=FakeAnthropic("内联摘要"),
    )

    assert stats.summarized == 1
    assert msg.inline_links[0].url == url
    assert msg.link_context == "内联摘要"


def test_enrich_extracts_inline_markdown_link_title():
    url = "https://example.com/md"
    msg = Message(
        create_time=1000,
        local_type=MSG_TEXT,
        sender_wxid="wxid",
        content=f"推荐 [好文章]({url})",
    )
    client = FakeHTTP({url: ("<article><p>" + ("正文 " * 500) + "</p></article>", "text/html")})

    enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=FakeAnthropic("摘要"),
    )

    assert msg.inline_links[0].title == "好文章"
    assert msg.inline_links[0].url == url


def test_enrich_deduplicates_card_and_inline_url():
    url = "https://example.com/same"
    card = _link_msg(url=url)
    text = Message(
        create_time=1001,
        local_type=MSG_TEXT,
        sender_wxid="wxid",
        content=f"同一个链接 {url}",
    )
    client = FakeHTTP({url: ("<article><p>" + ("正文 " * 500) + "</p></article>", "text/html")})

    stats = enrich_link_messages(
        [card, text],
        api_key="fake",
        http_client=client,
        anthropic_client=FakeAnthropic("摘要"),
    )

    assert stats.total == 1
    assert client.urls == [url]
    assert card.link_context == "摘要"
    assert text.link_context == ""


def test_inline_url_stops_at_chinese_punctuation_and_text():
    """Regression: a URL followed directly by Chinese punctuation/text without
    whitespace used to be swallowed whole (e.g. the trailing 「，关键是…」 ended
    up inside the URL), which then 404'd in fetch and logged FAILED."""
    url = "https://github.com/zarazhangrui/beautiful-html-templates"
    msg = Message(
        create_time=1000,
        local_type=MSG_TEXT,
        sender_wxid="wxid",
        content=f"{url}，关键是贼方便。昨天我碰巧看到，写材料的时候就用了。",
    )
    client = FakeHTTP({
        "https://raw.githubusercontent.com/zarazhangrui/beautiful-html-templates/main/README.md":
        ("# README\n" + ("body " * 200), "text/plain"),
    })
    # Without the regex fix this would request the URL+Chinese-tail and fail.
    enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=FakeAnthropic("摘要"),
    )
    assert msg.inline_links[0].url == url


def test_douyin_is_skipped_like_xiaohongshu():
    msg = _link_msg(
        title="抖音标题",
        url="https://www.douyin.com/video/7636968103511816817",
        des="抖音卡片摘要",
    )
    client = FakeHTTP({})  # No routes — fetch must not be attempted.
    stats = enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=FakeAnthropic(),
    )
    assert client.urls == []  # douyin was short-circuited, no HTTP call made
    assert stats.short == 1
    assert msg.link_context == "标题：抖音标题\n卡片预览：抖音卡片摘要"


def test_default_client_sends_browser_like_headers():
    """Regression: openai.com (and other Cloudflare/Vercel-fronted sites)
    return 403 challenge pages when only User-Agent is set. The default
    httpx.Client should ship full browser-like headers."""
    import httpx as _httpx
    from wechat_daily.url_enricher import _DEFAULT_HEADERS

    captured: dict[str, str] = {}

    def handler(request: _httpx.Request) -> _httpx.Response:
        captured.update({k: v for k, v in request.headers.items()})
        return _httpx.Response(
            200,
            text=(
                "<html><head>"
                '<meta property="og:description" content="OpenAI launches DeployCo.">'
                "<title>OpenAI launches the Deployment Company</title>"
                "</head><body><article>" + ("正文 " * 500) + "</article></body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    transport = _httpx.MockTransport(handler)
    msg = Message(
        create_time=1000,
        local_type=MSG_TEXT,
        sender_wxid="wxid",
        content="https://openai.com/index/openai-launches-the-deployment-company/",
    )
    with _httpx.Client(
        transport=transport,
        timeout=5.0,
        follow_redirects=True,
        headers=_DEFAULT_HEADERS,
    ) as client:
        enrich_link_messages(
            [msg],
            api_key="fake",
            http_client=client,
            anthropic_client=FakeAnthropic("摘要"),
        )
    assert "sec-fetch-mode" in captured
    assert captured["sec-fetch-mode"] == "navigate"
    assert "accept-language" in captured
    # br must NOT be advertised — httpx can't decode brotli without the
    # optional `brotli` package, which would give us a binary blob.
    assert "br" not in captured.get("accept-encoding", "")


def test_enrich_appends_multiple_inline_link_contexts():
    url1 = "https://example.com/one"
    url2 = "https://example.com/two"
    msg = Message(
        create_time=1000,
        local_type=MSG_TEXT,
        sender_wxid="wxid",
        content=f"两个链接 {url1} {url2}",
    )
    client = FakeHTTP({
        url1: ("<article><p>" + ("正文一 " * 500) + "</p></article>", "text/html"),
        url2: ("<article><p>" + ("正文二 " * 500) + "</p></article>", "text/html"),
    })
    anthropic = FakeAnthropic("摘要")

    enrich_link_messages(
        [msg],
        api_key="fake",
        http_client=client,
        anthropic_client=anthropic,
    )

    assert msg.link_context == "摘要\n摘要"
    assert client.urls == [url1, url2]


def test_enrich_routes_to_deepseek_when_no_client(monkeypatch):
    """With LINK_SUMMARY_MODEL=deepseek-* and no injected client, summaries go
    through deepseek_client.stream_chat (thinking disabled), not Anthropic."""
    import wechat_daily.url_enricher as ue
    import wechat_daily.config as cfg

    monkeypatch.setattr(ue, "LINK_SUMMARY_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(cfg, "get_deepseek_key", lambda: "fake-key")

    calls: list[dict] = []

    def fake_stream_chat(**kwargs):
        calls.append(kwargs)
        # (content, reasoning, usage, finish_reason)
        return "DS 摘要", "", {"prompt_tokens": 10, "completion_tokens": 5}, "stop"

    import wechat_daily.deepseek_client as dc
    monkeypatch.setattr(dc, "stream_chat", fake_stream_chat)

    url = "https://example.com/a"
    html = "<article><p>" + ("正文 " * 500) + "</p></article>"
    msg = _link_msg(url=url)
    client = FakeHTTP({url: (html, "text/html")})

    stats = enrich_link_messages([msg], api_key="ignored", http_client=client)

    assert stats.summarized == 1
    assert msg.link_context == "DS 摘要"
    assert len(calls) == 1
    assert calls[0]["model"] == "deepseek-v4-pro"
    assert calls[0]["thinking"] is False
