"""Parse raw DB rows into structured Message objects."""

from __future__ import annotations

import dataclasses
import html
import re
import subprocess
from typing import Optional

# ── Message type constants ──────────────────────────────────────────────────────
MSG_TEXT      = 1
MSG_IMAGE     = 3
MSG_VOICE     = 34
MSG_VIDEO     = 43
MSG_STICKER   = 47
MSG_CARD      = 42
MSG_SYSTEM    = 10000
MSG_QUOTE     = 244813135921
MSG_LINK_OPEN = 4294967345
MSG_LINK_CARD = 21474836529
MSG_FILE      = 25769803825
MSG_GIF       = 34359738417
MSG_FORWARD   = 81604378673
MSG_MINIAPP   = 154618822705
MSG_TAP       = 266287972401


@dataclasses.dataclass
class QuotedMessage:
    speaker_wxid: str    # may be empty
    speaker_name: str    # display name from <displayname>
    content: str         # rendered text of the quoted message
    ref_type: str        # raw <type> value


@dataclasses.dataclass
class LinkMeta:
    title: str = ""
    url: str = ""
    description: str = ""


@dataclasses.dataclass
class Message:
    create_time: int
    local_type: int
    sender_wxid: str
    content: str                        # human-readable text
    quoted: Optional[QuotedMessage] = None
    raw: str = dataclasses.field(default="", repr=False)  # original decompressed blob
    image_md5: Optional[str] = None     # for MSG_IMAGE: <img md5="..."> in XML
    link: Optional[LinkMeta] = None
    inline_links: list[LinkMeta] = dataclasses.field(default_factory=list)
    link_context: str = ""              # fetched/summarized public webpage context


# ── Helpers ─────────────────────────────────────────────────────────────────────

def decompress(data) -> str:
    if isinstance(data, bytes) and data[:4] == b'\x28\xb5\x2f\xfd':
        result = subprocess.run(
            ['zstd', '-d', '-', '--stdout'],
            input=data, capture_output=True,
        )
        return result.stdout.decode('utf-8', errors='replace')
    if isinstance(data, bytes):
        return data.decode('utf-8', errors='replace')
    return data or ''


def split_content(raw: str, sender_wxid: str) -> tuple[str, str]:
    """Return (sender_wxid, content) with the ``wxid:\\n`` prefix stripped off.

    Sender identity is authoritative — it comes from the DB's ``real_sender_id``
    column (resolved via ``Name2Id``), passed in by the caller. The embedded
    ``wxid:\\n`` prefix is used *only* to strip it back off the content, and only
    when it exactly matches that known sender. The database owner's own messages
    carry no prefix at all, so nothing is stripped and the content stays intact —
    which is precisely the bug this fixes (those messages used to resolve to an
    empty sender and get dropped).
    """
    prefix = f"{sender_wxid}:\n"
    content = raw[len(prefix):] if sender_wxid and raw.startswith(prefix) else raw
    return sender_wxid, content


def xml_text(xml_str: str, tag: str) -> str:
    m = re.search(rf'<{tag}>(.*?)</{tag}>', xml_str, re.DOTALL)
    return m.group(1).strip() if m else ''


def format_quoted(refermsg: str) -> str:
    """Render a <refermsg> block as a short human-readable string."""
    ref_type = xml_text(refermsg, 'type')
    displayname = xml_text(refermsg, 'displayname')
    content = xml_text(refermsg, 'content')
    prefix = f"{displayname}: " if displayname else ""

    if ref_type == '1':
        text = content[:100] + ('…' if len(content) > 100 else '')
        return prefix + text
    elif ref_type == '3':
        return prefix + '[图片]'
    elif ref_type == '34':
        return prefix + '[语音]'
    elif ref_type == '43':
        return prefix + '[视频]'
    elif ref_type == '47':
        return prefix + '[表情包]'
    elif ref_type == '49':
        title = xml_text(content, 'title') if content else ''
        return prefix + (title if title else '[消息]')
    else:
        return prefix + '[消息]'


def _parse_quoted(refermsg_xml: str) -> QuotedMessage:
    ref_type = xml_text(refermsg_xml, 'type')
    displayname = xml_text(refermsg_xml, 'displayname')
    fromusername = xml_text(refermsg_xml, 'fromusername')
    rendered = format_quoted(refermsg_xml)
    return QuotedMessage(
        speaker_wxid=fromusername,
        speaker_name=displayname,
        content=rendered,
        ref_type=ref_type,
    )


# ── Main parser ─────────────────────────────────────────────────────────────────

def parse_row(
    create_time: int,
    local_type: int,
    message_content,
    sender_wxid: str,
) -> Message | None:
    """Parse a single DB row into a Message, or return None if it should be skipped.

    *sender_wxid* is the authoritative sender resolved from the row's
    ``real_sender_id`` column via ``Name2Id`` (see ``chat_extractor``). It is the
    single source of sender identity — notably it carries the owner's own
    prefix-less messages, which have no ``wxid:\\n`` prefix to recover from.
    """
    raw = decompress(message_content)

    if local_type == MSG_TEXT:
        sender_wxid, content = split_content(raw, sender_wxid)
        content = content.strip()
        if not content:
            return None
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content=content, raw=raw)

    elif local_type == MSG_QUOTE:
        sender_wxid, xml = split_content(raw, sender_wxid)
        title = xml_text(xml, 'title').strip()
        if not title:
            return None
        refermsg_m = re.search(r'<refermsg>(.*?)</refermsg>', xml, re.DOTALL)
        quoted = _parse_quoted(refermsg_m.group(1)) if refermsg_m else None
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content=title,
                       quoted=quoted, raw=raw)

    elif local_type == MSG_TAP:
        title = xml_text(raw, 'title').strip()
        if not title:
            return None
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid='', content=title, raw=raw)

    elif local_type == MSG_SYSTEM:
        text = raw.strip()
        if not text:
            return None
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid='', content=text, raw=raw)

    elif local_type in (MSG_LINK_CARD, MSG_LINK_OPEN):
        sender_wxid, xml = split_content(raw, sender_wxid)
        title = xml_text(xml, 'title')
        url = html.unescape(xml_text(xml, 'url'))
        description = xml_text(xml, 'des')
        if title and url:
            # Markdown link form so the LLM can preserve it verbatim. Sanitize
            # `[]` in title since they would break the link syntax.
            safe_title = title.replace('[', '［').replace(']', '］')
            label = f'[链接] [{safe_title}]({url})'
        elif title:
            label = '[链接] ' + title
        else:
            label = '[链接]'
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content=label, raw=raw,
                       link=LinkMeta(title=title, url=url, description=description))

    elif local_type == MSG_FILE:
        sender_wxid, xml = split_content(raw, sender_wxid)
        title = xml_text(xml, 'title')
        label = '[文件] ' + title if title else '[文件]'
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content=label, raw=raw)

    elif local_type == MSG_FORWARD:
        sender_wxid, xml = split_content(raw, sender_wxid)
        title = xml_text(xml, 'title')
        label = '[合并转发] ' + title if title else '[合并转发]'
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content=label, raw=raw)

    elif local_type == MSG_MINIAPP:
        sender_wxid, xml = split_content(raw, sender_wxid)
        title = xml_text(xml, 'title')
        label = '[小程序] ' + title if title else '[小程序]'
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content=label, raw=raw)

    elif local_type == MSG_IMAGE:
        sender_wxid, _ = split_content(raw, sender_wxid)
        # image_md5 (== .dat filename) lives in message_resource.MessageResourceInfo,
        # not in this XML. chat_extractor fills it via a post-pass.
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content='[图片]', raw=raw)

    elif local_type == MSG_VOICE:
        sender_wxid, _ = split_content(raw, sender_wxid)
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content='[语音]', raw=raw)

    elif local_type == MSG_VIDEO:
        sender_wxid, _ = split_content(raw, sender_wxid)
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content='[视频]', raw=raw)

    elif local_type == MSG_STICKER:
        sender_wxid, _ = split_content(raw, sender_wxid)
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content='[表情包]', raw=raw)

    elif local_type in (MSG_CARD, MSG_GIF):
        sender_wxid, _ = split_content(raw, sender_wxid)
        label = '[名片]' if local_type == MSG_CARD else '[GIF]'
        return Message(create_time=create_time, local_type=local_type,
                       sender_wxid=sender_wxid, content=label, raw=raw)

    return None
