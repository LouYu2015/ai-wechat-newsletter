"""Unit tests for message_parser."""

from wechat_daily import message_parser


def test_parse_row_text():
    raw = b"wxid_test:\nHello"
    msg = message_parser.parse_row(1000, message_parser.MSG_TEXT, raw, "wxid_test")
    assert msg is not None
    assert msg.sender_wxid == "wxid_test"
    assert msg.content == "Hello"
    assert msg.local_type == message_parser.MSG_TEXT


def test_parse_row_text_empty_content():
    raw = b"wxid_test:\n"
    msg = message_parser.parse_row(1000, message_parser.MSG_TEXT, raw, "wxid_test")
    assert msg is None


def test_split_content_authoritative_sender_strips_prefix():
    # Sender supplied (from real_sender_id) and the content carries its prefix.
    wxid, content = message_parser.split_content("wxid_real:\nhello", "wxid_real")
    assert wxid == "wxid_real"
    assert content == "hello"


def test_split_content_owner_prefixless_kept_intact():
    # The owner's own messages have no prefix; content must stay whole even when
    # it happens to start with a 'token:\n'-looking line (legacy heuristic would
    # have wrongly stripped it).
    wxid, content = message_parser.split_content("李雷:\n你好", "wxid_owner")
    assert wxid == "wxid_owner"
    assert content == "李雷:\n你好"


def test_split_content_empty_sender_keeps_content_whole():
    # With no authoritative sender there is nothing to strip — the prefix
    # heuristic is gone, so content is returned untouched.
    wxid, content = message_parser.split_content("wxid_abc:\nhi", "")
    assert wxid == ""
    assert content == "wxid_abc:\nhi"


def test_parse_row_uses_authoritative_sender_for_prefixless_owner_msg():
    # The owner's own group message: no embedded prefix, sender comes from the
    # DB's real_sender_id. Previously this resolved to '' and got dropped.
    msg = message_parser.parse_row(1000, message_parser.MSG_TEXT, b"\xe4\xbd\xa0\xe5\xa5\xbd", "wxid_owner")
    assert msg is not None
    assert msg.sender_wxid == "wxid_owner"
    assert msg.content == "你好"


def test_parse_row_system_ignores_supplied_sender():
    # System rows stay senderless even though the DB hands us a real_sender_id.
    msg = message_parser.parse_row(1000, message_parser.MSG_SYSTEM, b"someone joined", "wxid_owner")
    assert msg is not None
    assert msg.sender_wxid == ""


def test_parse_row_image():
    raw = b"wxid_img:\n<img/>"
    msg = message_parser.parse_row(1000, message_parser.MSG_IMAGE, raw, "wxid_img")
    assert msg is not None
    assert msg.content == "[图片]"
    assert msg.sender_wxid == "wxid_img"


def test_parse_row_system():
    raw = b"some system message"
    msg = message_parser.parse_row(1000, message_parser.MSG_SYSTEM, raw, "")
    assert msg is not None
    assert msg.content == "some system message"
    assert msg.sender_wxid == ""


def test_parse_row_unknown_type_returns_none():
    msg = message_parser.parse_row(1000, 99999, b"unknown", "")
    assert msg is None


def test_format_quoted_text():
    xml = "<type>1</type><displayname>Alice</displayname><content>Hello there</content>"
    result = message_parser.format_quoted(xml)
    assert result == "Alice: Hello there"


def test_format_quoted_image():
    xml = "<type>3</type><displayname>Bob</displayname><content></content>"
    result = message_parser.format_quoted(xml)
    assert result == "Bob: [图片]"


def test_format_quoted_long_content_truncated():
    long_content = "A" * 200
    xml = f"<type>1</type><displayname>X</displayname><content>{long_content}</content>"
    result = message_parser.format_quoted(xml)
    assert len(result) < 120  # truncated
    assert "…" in result


def test_parse_row_link_card_with_url():
    raw = (
        b"wxid_link:\n<msg><appmsg>"
        b"<title>Claude Opus 4.7 \xe5\x8f\x91\xe5\xb8\x83</title>"
        b"<url>https://mp.weixin.qq.com/s?__biz=abc&amp;mid=123&amp;idx=1</url>"
        b"</appmsg></msg>"
    )
    msg = message_parser.parse_row(1000, message_parser.MSG_LINK_OPEN, raw, "wxid_link")
    assert msg is not None
    assert msg.sender_wxid == "wxid_link"
    assert msg.content == (
        "[链接] [Claude Opus 4.7 发布]"
        "(https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1)"
    )
    assert msg.link is not None
    assert msg.link.title == "Claude Opus 4.7 发布"
    assert msg.link.url == "https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1"


def test_parse_row_link_card_extracts_description():
    raw = (
        b"wxid_link:\n<msg><appmsg>"
        b"<title>hello</title>"
        b"<des>\xe8\xbf\x99\xe6\x98\xaf\xe6\x91\x98\xe8\xa6\x81</des>"
        b"<url>https://example.com/a</url>"
        b"</appmsg></msg>"
    )
    msg = message_parser.parse_row(1000, message_parser.MSG_LINK_OPEN, raw, "wxid_link")
    assert msg is not None
    assert msg.link is not None
    assert msg.link.description == "这是摘要"


def test_parse_row_link_card_url_missing_falls_back_to_title():
    raw = b"wxid_link:\n<msg><appmsg><title>just a title</title></appmsg></msg>"
    msg = message_parser.parse_row(1000, message_parser.MSG_LINK_OPEN, raw, "wxid_link")
    assert msg is not None
    assert msg.content == "[链接] just a title"


def test_parse_row_link_card_brackets_in_title_sanitized():
    raw = (
        b"wxid_link:\n<msg><appmsg>"
        b"<title>[\xe9\x87\x8d\xe7\xa3\x85] hello</title>"
        b"<url>https://example.com/a</url>"
        b"</appmsg></msg>"
    )
    msg = message_parser.parse_row(1000, message_parser.MSG_LINK_OPEN, raw, "wxid_link")
    assert msg is not None
    assert msg.content == "[链接] [［重磅］ hello](https://example.com/a)"


def test_decompress_plain_bytes():
    data = b"hello"
    assert message_parser.decompress(data) == "hello"


def test_decompress_plain_string():
    assert message_parser.decompress("hello") == "hello"


def test_decompress_none():
    assert message_parser.decompress(None) == ""
