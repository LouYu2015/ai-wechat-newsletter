"""Token-ization, optout masking, and leak detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from .message_parser import (
    Message, QuotedMessage,
    MSG_TAP, MSG_SYSTEM, MSG_QUOTE, MSG_TEXT,
)

if TYPE_CHECKING:
    from .aliases import AliasDB
    from .contacts import ContactMap


class LeakDetected(Exception):
    """Raised when a real nickname is found in the public Markdown."""


@dataclass
class TokenMap:
    """Bidirectional mapping: wxid ↔ token (= default_anon)."""
    _fwd: dict[str, str]  # wxid → token
    _rev: dict[str, str]  # token → wxid

    @classmethod
    def build(cls, wxids: list[str], alias_db: "AliasDB") -> "TokenMap":
        fwd: dict[str, str] = {}
        rev: dict[str, str] = {}
        for wxid in wxids:
            token = alias_db.token_of(wxid)
            fwd[wxid] = token
            rev[token] = wxid
        return cls(fwd, rev)

    def token(self, wxid: str) -> str:
        return self._fwd.get(wxid, wxid)

    def wxid(self, token: str) -> str | None:
        return self._rev.get(token)


def _nickname_pairs(contact_map: "ContactMap") -> list[tuple[str, str]]:
    """Return [(nickname, wxid)] sorted by nickname length desc.

    Skips empty names and nicknames with ≤ 4 codepoints (≈ 2 Chinese characters)
    to avoid false matches inside URLs and short technical tokens.
    """
    pairs: list[tuple[str, str]] = []
    for nickname in contact_map.all_nicknames():
        if len(nickname) <= 4:
            continue
        wxid = contact_map.wxid_for_nickname(nickname)
        if wxid:
            pairs.append((nickname, wxid))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def build_replace_state(
    contact_map: "ContactMap",
    token_map: TokenMap,
) -> tuple["re.Pattern[str] | None", dict[str, str]]:
    """Precompute (pattern, mapping) for _replace_names.  Call once per batch."""
    pairs = _nickname_pairs(contact_map)
    if not pairs:
        return None, {}
    pattern = re.compile('|'.join(re.escape(n) for n, _ in pairs))
    mapping = {n: token_map.token(w) for n, w in pairs}
    return pattern, mapping


def _replace_names(
    text: str,
    pattern: "re.Pattern[str] | None",
    mapping: dict[str, str],
) -> str:
    """Replace all real nicknames in *text* with tokens using precomputed state."""
    if not pattern:
        return text
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def _tap_has_optout_party(
    content: str,
    contact_map: "ContactMap",
    alias_db: "AliasDB",
) -> bool:
    """True iff a TAP message mentions an optout user's nickname.

    Scans nicknames longest-first so that e.g. optout '李' does not trigger
    when only '李明' is present in content.
    """
    optout = set(alias_db.optout_wxids())
    remaining = content
    for nickname, wxid in _nickname_pairs(contact_map):
        if nickname in remaining:
            if wxid in optout:
                return True
            # Erase so shorter nicknames (potential substrings) don't rematch.
            remaining = remaining.replace(nickname, ' ')
    return False


def tokenize_messages(
    messages: list[Message],
    contact_map: "ContactMap",
    alias_db: "AliasDB",
    progress_cb: Callable[[int, int], None] | None = None,
) -> tuple[list[Message], TokenMap]:
    """Apply full token-ization to a message list.

    Returns (tokenized_messages, token_map).
    Optout users' messages are replaced with run-length-merged placeholders.
    progress_cb(current, total) is called after each message if provided.
    """
    # Build token map from ALL known contacts (not just senders).
    # This ensures names mentioned in message bodies are always tokenized,
    # even when the mentioned person sent no messages that day.
    all_wxids: set[str] = set()
    for msg in messages:
        if msg.sender_wxid:
            all_wxids.add(msg.sender_wxid)
    for nickname in contact_map.all_nicknames():
        wxid = contact_map.wxid_for_nickname(nickname)
        if wxid:
            all_wxids.add(wxid)
    token_map = TokenMap.build(list(all_wxids), alias_db)

    # Precompute regex pattern + mapping once for the whole batch.
    pattern, mapping = build_replace_state(contact_map, token_map)

    result: list[Message] = []
    total = len(messages)

    # Optout run-length merging
    optout_run: list[Message] = []

    def flush_optout_run() -> None:
        if not optout_run:
            return
        first = optout_run[0]
        last = optout_run[-1]
        ts_start = datetime.fromtimestamp(first.create_time).strftime('%H:%M')
        if len(optout_run) == 1:
            placeholder = Message(
                create_time=first.create_time,
                local_type=first.local_type,
                sender_wxid='',
                content=f"[{ts_start}] [此消息已隐藏]",
            )
        else:
            ts_end = datetime.fromtimestamp(last.create_time).strftime('%H:%M')
            n = len(optout_run)
            placeholder = Message(
                create_time=first.create_time,
                local_type=first.local_type,
                sender_wxid='',
                content=f"[{ts_start}–{ts_end}] [某群友连续发言 {n} 条已隐藏]",
            )
        result.append(placeholder)
        optout_run.clear()

    for idx, msg in enumerate(messages):
        sender = msg.sender_wxid

        # ── TAP: redact if either party is optout ───────────────────────────
        if msg.local_type == MSG_TAP:
            flush_optout_run()
            content = msg.content
            if _tap_has_optout_party(content, contact_map, alias_db):
                result.append(Message(
                    create_time=msg.create_time, local_type=msg.local_type,
                    sender_wxid='', content='[某人做了个动作]',
                ))
            else:
                result.append(Message(
                    create_time=msg.create_time, local_type=msg.local_type,
                    sender_wxid='',
                    content=_replace_names(content, pattern, mapping),
                ))
        elif msg.local_type == MSG_SYSTEM:
            # ── System messages: tokenize names, pass through ───────────────
            flush_optout_run()
            result.append(Message(
                create_time=msg.create_time, local_type=msg.local_type,
                sender_wxid='',
                content=_replace_names(msg.content, pattern, mapping),
            ))
        elif sender and alias_db.is_optout(sender):
            # ── Optout sender: accumulate run ────────────────────────────────
            optout_run.append(msg)
        else:
            flush_optout_run()
            # ── Normal message: tokenize sender + content ────────────────────
            token = token_map.token(sender) if sender else ''
            content = _replace_names(msg.content, pattern, mapping)

            quoted = msg.quoted
            if quoted:
                if quoted.speaker_wxid and alias_db.is_optout(quoted.speaker_wxid):
                    quoted = QuotedMessage(
                        speaker_wxid=quoted.speaker_wxid,
                        speaker_name='',
                        content='[引用内容已隐藏]',
                        ref_type=quoted.ref_type,
                    )
                else:
                    quoted = QuotedMessage(
                        speaker_wxid=quoted.speaker_wxid,
                        speaker_name=_replace_names(quoted.speaker_name, pattern, mapping),
                        content=_replace_names(quoted.content, pattern, mapping),
                        ref_type=quoted.ref_type,
                    )

            result.append(Message(
                create_time=msg.create_time,
                local_type=msg.local_type,
                sender_wxid=token,
                content=content,
                quoted=quoted,
            ))

        if progress_cb:
            progress_cb(idx + 1, total)

    flush_optout_run()
    return result, token_map


def format_tokenized_messages(messages: list[Message]) -> str:
    """Format tokenized messages into plain-text chat history for LLM consumption."""
    lines: list[str] = []
    for msg in messages:
        ts = datetime.fromtimestamp(msg.create_time).strftime('%H:%M')

        if msg.local_type == MSG_TAP:
            lines.append(f"[{ts}] {msg.content}")
            continue

        if msg.local_type == MSG_SYSTEM:
            lines.append(f"[{ts}] [系统] {msg.content}")
            continue

        name = msg.sender_wxid  # already a token, or '' for placeholders

        # Optout placeholders carry their own timestamp range in content;
        # emit verbatim (the content already contains "[HH:MM] ..." prefix).
        is_placeholder = not name and (
            msg.content == '[此消息已隐藏]'
            or msg.content.startswith('[')
            and ('已隐藏]' in msg.content)
        )
        if is_placeholder:
            lines.append(msg.content)
            continue

        if not name:
            continue

        line = f"[{ts}] {name}: {msg.content}"
        if msg.quoted:
            line += f"\n  > 引用 {msg.quoted.content}"
        lines.append(line)

    return '\n'.join(lines)


def leak_check(
    markdown: str,
    contact_map: "ContactMap",
    alias_db: "AliasDB",
) -> None:
    """Raise LeakDetected if any real nickname or optout anon appears in *markdown*."""
    for nickname in contact_map.all_nicknames():
        if nickname in markdown:
            raise LeakDetected(f"真实昵称泄漏: 「{nickname}」")

    for anon in alias_db.optout_anons():
        if anon in markdown:
            raise LeakDetected(f"Optout 用户默认匿名名泄漏: 「{anon}」")

    # Paranoia check: raw wxid strings should never appear in public output
    if re.search(r'\bwxid_\w+', markdown):
        raise LeakDetected("检测到原始 wxid 字符串泄漏")
