from __future__ import annotations

import httpx

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


class FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return type("Response", (), {"content": [TextBlock(self.text)]})()


class FakeAnthropic:
    def __init__(self, text: str = "这是网页摘要") -> None:
        self.messages = FakeMessages(text)


def _link_msg(title="Title", url="https://example.com/a", des="card des"):
    return Message(
        create_time=1000,
        local_type=MSG_LINK_OPEN,
        sender_wxid="wxid",
        content=f"[链接] [{title}]({url})",
        link=LinkMeta(title=title, url=url, description=des),
    )


def test_fetch_wechat_js_content():
    url = "https://mp.weixin.qq.com/s?mid=1"
    html = """
    <html><body><div>nav</div><div id="js_content">
      <h1>标题</h1><p>第一段正文。</p><script>ignore()</script><p>第二段正文。</p>
    </div></body></html>
    """
    client = FakeHTTP({url: (html, "text/html")})
    text = fetch_url_text(url, client)
    assert "第一段正文" in text
    assert "第二段正文" in text
    assert "ignore" not in text


def test_fetch_x_uses_fxtwitter():
    url = "https://x.com/example_user/status/2049645973350363168?s=46"
    api = "https://api.fxtwitter.com/example_user/status/2049645973350363168"
    client = FakeHTTP({
        api: (
            '{"tweet":{"text":"tweet body","author":{"name":"Theo"}}}',
            "application/json",
        )
    })
    text = fetch_url_text(url, client)
    assert client.urls == [api]
    assert text == "Theo: tweet body"


def test_fetch_github_blob_uses_raw_url():
    url = "https://github.com/acme/repo/blob/main/README.md"
    raw = "https://raw.githubusercontent.com/acme/repo/main/README.md"
    client = FakeHTTP({raw: ("# README\nbody", "text/plain")})
    assert fetch_url_text(url, client) == "# README\nbody"
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
    text = fetch_url_text(url, client)
    assert client.urls == [url, spaces, post]
    assert "章节标题" in text
    assert "正文 @Person 内容" in text
    assert "引用内容" in text


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
    assert call["model"] == "claude-sonnet-4-6"
    assert "thinking" not in call


def test_enrich_falls_back_to_description_when_fetch_fails():
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
    assert stats.fallback == 1
    assert msg.link_context == "链接卡片摘要：卡片摘要"


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
