"""Per-group display names from ``contact.db.chat_room.ext_buffer``.

The ``ext_buffer`` blob is a packed protobuf carrying one sub-message per
group member with the schema (inferred):

    field 1 (string, required): wxid
    field 2 (string, optional): 群昵称 — only present when the user set one
    field 3 (varint):           opaque flag
    field 4 (string):           inviter wxid

We only need (wxid → 群昵称) for members who actually set one. Members
without a 群昵称 fall back to ``contact.nick_name`` via ``ContactMap``.
"""

from __future__ import annotations

from wechat_daily.config import GROUP_CHAT_ID
from wechat_daily.wechat_db import get_conn


class ChatroomMembers:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    @classmethod
    def from_db(cls, group_chat_id: str = GROUP_CHAT_ID) -> "ChatroomMembers":
        conn = get_conn("contact/contact.db")
        cur = conn.cursor()
        cur.execute(
            "SELECT ext_buffer FROM chat_room WHERE username = ?",
            (group_chat_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return cls({})
        return cls(_parse_member_buffer(row[0]))

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "ChatroomMembers":
        return cls(dict(data))

    def display_name(self, wxid: str) -> str | None:
        """Return the group display name iff the member set one, else None."""
        return self._data.get(wxid)

    def items(self) -> list[tuple[str, str]]:
        return list(self._data.items())


def _parse_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def _parse_member(sub: bytes) -> dict[int, object]:
    out: dict[int, object] = {}
    i = 0
    while i < len(sub):
        tag = sub[i]
        i += 1
        fnum, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = _parse_varint(sub, i)
            out[fnum] = sub[i:i + ln].decode("utf-8", "replace")
            i += ln
        elif wt == 0:
            v, i = _parse_varint(sub, i)
            out[fnum] = v
        else:
            break
    return out


def _parse_member_buffer(buf: bytes) -> dict[str, str]:
    """Return ``{wxid: 群昵称}`` for members with a non-empty 群昵称."""
    result: dict[str, str] = {}
    i = 0
    while i < len(buf):
        tag = buf[i]
        i += 1
        if tag != 0x0A:  # outer field 1 (length-delimited)
            break
        ln, i = _parse_varint(buf, i)
        m = _parse_member(buf[i:i + ln])
        i += ln
        wxid = m.get(1)
        disp = m.get(2)
        if isinstance(wxid, str) and isinstance(disp, str) and disp:
            result[wxid] = disp
    return result
