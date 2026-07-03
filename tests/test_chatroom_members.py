"""Unit tests for chatroom_members.py protobuf parsing."""

from __future__ import annotations

from wechat_daily import chatroom_members


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _string_field(field: int, s: str) -> bytes:
    payload = s.encode("utf-8")
    return bytes([(field << 3) | 2]) + _encode_varint(len(payload)) + payload


def _varint_field(field: int, v: int) -> bytes:
    return bytes([(field << 3) | 0]) + _encode_varint(v)


def _member(wxid: str, disp: str | None, flag: int, inviter: str) -> bytes:
    body = _string_field(1, wxid)
    if disp:
        body += _string_field(2, disp)
    body += _varint_field(3, flag)
    body += _string_field(4, inviter)
    # Outer field 1 wraps each member entry.
    return bytes([0x0A]) + _encode_varint(len(body)) + body


def test_parse_buffer_extracts_only_members_with_display():
    buf = (
        _member("wxid_alice", "鸭哥", 1, "host")
        + _member("wxid_bob", None, 1, "host")
        + _member("wxid_carol", "Carol-临时", 9, "host")
    )
    out = chatroom_members._parse_member_buffer(buf)
    assert out == {"wxid_alice": "鸭哥", "wxid_carol": "Carol-临时"}


def test_parse_buffer_empty_returns_empty():
    assert chatroom_members._parse_member_buffer(b"") == {}


def test_parse_buffer_skips_member_with_empty_display():
    # field 2 present but empty string
    buf = _member("wxid_x", "", 1, "host")
    assert chatroom_members._parse_member_buffer(buf) == {}


def test_parse_member_handles_field_order_and_types():
    body = _string_field(1, "wxid_z") + _string_field(2, "ZeeName") + _varint_field(3, 17)
    out = chatroom_members._parse_member(body)
    assert out[1] == "wxid_z"
    assert out[2] == "ZeeName"
    assert out[3] == 17


def test_chatroom_members_lookup():
    cm = chatroom_members.ChatroomMembers.from_dict({"wxid_a": "鸭哥"})
    assert cm.display_name("wxid_a") == "鸭哥"
    assert cm.display_name("wxid_other") is None
    assert cm.items() == [("wxid_a", "鸭哥")]


def test_chatroom_members_from_buffer_round_trip():
    buf = _member("wxid_alice", "鸭哥", 1, "host") + _member("wxid_bob", None, 1, "host")
    parsed = chatroom_members._parse_member_buffer(buf)
    cm = chatroom_members.ChatroomMembers.from_dict(parsed)
    assert cm.display_name("wxid_alice") == "鸭哥"
    assert cm.display_name("wxid_bob") is None
