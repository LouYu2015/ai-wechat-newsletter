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


def test_decompress_plain_bytes():
    data = b"hello"
    assert decompress(data) == "hello"


def test_decompress_plain_string():
    assert decompress("hello") == "hello"


def test_decompress_none():
    assert decompress(None) == ""
