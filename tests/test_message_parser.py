"""Unit tests for message_parser."""

import pytest
from wechat_daily.message_parser import (
    MSG_TEXT, MSG_QUOTE, MSG_TAP, MSG_IMAGE, MSG_SYSTEM,
    MSG_LINK_OPEN, MSG_FILE,
    parse_row, parse_sender_content, format_quoted, decompress,
)


def test_parse_sender_content_normal():
    raw = "wxid_abc123:\nhello world"
    wxid, content = parse_sender_content(raw)
    assert wxid == "wxid_abc123"
    assert content == "hello world"


def test_parse_sender_content_no_match():
    raw = "no sender here"
    wxid, content = parse_sender_content(raw)
    assert wxid == ""
    assert content == raw


def test_parse_sender_content_space_in_candidate():
    # Space in potential wxid → not a wxid
    raw = "hello world:\ncontent"
    wxid, content = parse_sender_content(raw)
    assert wxid == ""


def test_parse_row_text():
    raw = b"wxid_test:\nHello"
    msg = parse_row(1000, MSG_TEXT, raw)
    assert msg is not None
    assert msg.sender_wxid == "wxid_test"
    assert msg.content == "Hello"
    assert msg.local_type == MSG_TEXT


def test_parse_row_text_empty_content():
    raw = b"wxid_test:\n"
    msg = parse_row(1000, MSG_TEXT, raw)
    assert msg is None


def test_parse_row_image():
    raw = b"wxid_img:\n<img/>"
    msg = parse_row(1000, MSG_IMAGE, raw)
    assert msg is not None
    assert msg.content == "[图片]"
    assert msg.sender_wxid == "wxid_img"


def test_parse_row_system():
    raw = b"some system message"
    msg = parse_row(1000, MSG_SYSTEM, raw)
    assert msg is not None
    assert msg.content == "some system message"
    assert msg.sender_wxid == ""


def test_parse_row_unknown_type_returns_none():
    msg = parse_row(1000, 99999, b"unknown")
    assert msg is None


def test_format_quoted_text():
    xml = "<type>1</type><displayname>Alice</displayname><content>Hello there</content>"
    result = format_quoted(xml)
    assert result == "Alice: Hello there"


def test_format_quoted_image():
    xml = "<type>3</type><displayname>Bob</displayname><content></content>"
    result = format_quoted(xml)
    assert result == "Bob: [图片]"


def test_format_quoted_long_content_truncated():
    long_content = "A" * 200
    xml = f"<type>1</type><displayname>X</displayname><content>{long_content}</content>"
    result = format_quoted(xml)
    assert len(result) < 120  # truncated
    assert "…" in result


def test_parse_row_link_card_with_url():
    raw = (
        b"wxid_link:\n<msg><appmsg>"
        b"<title>Claude Opus 4.7 \xe5\x8f\x91\xe5\xb8\x83</title>"
        b"<url>https://mp.weixin.qq.com/s?__biz=abc&amp;mid=123&amp;idx=1</url>"
        b"</appmsg></msg>"
    )
    msg = parse_row(1000, MSG_LINK_OPEN, raw)
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
    msg = parse_row(1000, MSG_LINK_OPEN, raw)
    assert msg is not None
    assert msg.link is not None
    assert msg.link.description == "这是摘要"


def test_parse_row_link_card_url_missing_falls_back_to_title():
    raw = b"wxid_link:\n<msg><appmsg><title>just a title</title></appmsg></msg>"
    msg = parse_row(1000, MSG_LINK_OPEN, raw)
    assert msg is not None
    assert msg.content == "[链接] just a title"


def test_parse_row_link_card_brackets_in_title_sanitized():
    raw = (
        b"wxid_link:\n<msg><appmsg>"
        b"<title>[\xe9\x87\x8d\xe7\xa3\x85] hello</title>"
        b"<url>https://example.com/a</url>"
        b"</appmsg></msg>"
    )
    msg = parse_row(1000, MSG_LINK_OPEN, raw)
    assert msg is not None
    assert msg.content == "[链接] [［重磅］ hello](https://example.com/a)"


def test_decompress_plain_bytes():
    data = b"hello"
    assert decompress(data) == "hello"


def test_decompress_plain_string():
    assert decompress("hello") == "hello"


def test_decompress_none():
    assert decompress(None) == ""
