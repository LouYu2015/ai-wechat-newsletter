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

    def all_tokens(self) -> list[str]:
        """Return all tokens (default_anons) known to this map."""
        return list(self._rev.keys())


def _nickname_pairs(contact_map: "ContactMap") -> list[tuple[str, str]]:
    """Return [(nickname, wxid)] sorted by nickname length desc.

    Pulls 群昵称 + 微信昵称 from ContactMap and keeps anything ≥ 2 codepoints.
    The model still sees the original via the ``token⟨原文⟩`` form, so short-
    name false positives are recoverable downstream — being aggressive here
    is what catches 2-character nicknames like 「鸭哥」 that the previous
    ≤ 4 filter silently dropped.
    """
    pairs = [(n, w) for n, w in contact_map.all_pairs() if len(n) >= 2]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _is_ascii_word(s: str) -> bool:
    """A name made only of regex ``\\w`` ASCII characters can be safely
    bracketed by ``\\b``. CJK characters are not in ``\\w``, so applying
    ``\\b`` to e.g. 「鸭哥」 produces a boundary at *every* CJK position
    and never matches — keep those as bare literals."""
    return bool(s) and all(c.isascii() and (c.isalnum() or c == "_") for c in s)


def _compile_nickname_pattern(
    pairs: list[tuple[str, str]],
) -> "re.Pattern[str] | None":
    """Build an alternation regex that respects word boundaries for ASCII-
    only names but matches CJK names as raw substrings.

    Without ``\\b`` on ASCII pairs, a 2-character nickname like ``tea`` would
    match inside ``team`` — devastating once the ≤ 4 codepoint filter is
    relaxed for CJK. The contact table holds ~20k users; many short Latin
    aliases collide with English words.
    """
    if not pairs:
        return None
    parts: list[str] = []
    for name, _ in pairs:
        esc = re.escape(name)
        if _is_ascii_word(name):
            parts.append(rf"\b{esc}\b")
        else:
            parts.append(esc)
    return re.compile("|".join(parts))


def build_replace_state(
    contact_map: "ContactMap",
    token_map: TokenMap,
    only_wxids: set[str] | None = None,
) -> tuple["re.Pattern[str] | None", dict[str, str]]:
    """Precompute (pattern, mapping) for _replace_names. Call once per batch.

    When *only_wxids* is given, the returned pattern only matches nicknames
    whose wxid is in that set. This pairs with lazy token allocation: we only
    need to substitute names we actually plan to tokenize.
    """
    pairs = _nickname_pairs(contact_map)
    if only_wxids is not None:
        pairs = [(n, w) for n, w in pairs if w in only_wxids]
    if not pairs:
        return None, {}
    pattern = _compile_nickname_pattern(pairs)
    mapping = {n: token_map.token(w) for n, w in pairs}
    return pattern, mapping


def _scan_mentioned_wxids(
    messages: list["Message"],
    contact_map: "ContactMap",
) -> set[str]:
    """Return the set of wxids whose nickname actually appears in any message
    body, quoted content, or quoted speaker. Drives lazy token allocation."""
    pairs = _nickname_pairs(contact_map)
    if not pairs:
        return set()
    pattern = _compile_nickname_pattern(pairs)
    if pattern is None:
        return set()
    nick_to_wxid = {n: w for n, w in pairs}

    mentioned: set[str] = set()
    for msg in messages:
        chunks: list[str] = []
        if msg.content:
            chunks.append(msg.content)
        if msg.quoted:
            if msg.quoted.content:
                chunks.append(msg.quoted.content)
            if msg.quoted.speaker_name:
                chunks.append(msg.quoted.speaker_name)
        for chunk in chunks:
            for m in pattern.finditer(chunk):
                mentioned.add(nick_to_wxid[m.group(0)])
    return mentioned


def _replace_names(
    text: str,
    pattern: "re.Pattern[str] | None",
    mapping: dict[str, str],
) -> str:
    """Replace nicknames with ``token⟨原文⟩`` so the LLM can disambiguate.

    Some群友 nicknames collide with product/model/company names (e.g. "DeepSeek",
    "Cursor"). A blind nickname→token substitution silently corrupts technical
    discussion. By emitting both, we let the summarization model decide per
    occurrence whether it's a person reference (output token) or a non-person
    entity (output 原文). The system prompt forbids retaining the ⟨…⟩ markers
    in the final output, and ``leak_check`` hard-gates them as a safety net.
    """
    if not pattern:
        return text
    return pattern.sub(lambda m: f"{mapping[m.group(0)]}⟨{m.group(0)}⟩", text)


def _tap_has_optout_party(
    content: str,
    contact_map: "ContactMap",
    alias_db: "AliasDB",
) -> bool:
    """True iff a TAP message mentions an optout user's nickname.

    Scans ALL nicknames (no length filter) longest-first so that e.g. optout
    '李' does not trigger when only '李明' is present in content.
    The no-filter policy here is intentional: TAP messages are short and
    structured, so false positives from short names are unlikely, and we prefer
    to err on the side of privacy protection.
    """
    optout = set(alias_db.optout_wxids())
    # All known names from both sources; longest-first to avoid substring traps.
    all_pairs = list(contact_map.all_pairs())
    all_pairs.sort(key=lambda p: len(p[0]), reverse=True)

    remaining = content
    for nickname, wxid in all_pairs:
        if nickname in remaining:
            if wxid in optout:
                return True
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
    # Lazy allocation: only senders + nicknames actually mentioned today need
    # tokens. Pre-allocating for every contact (potentially thousands across
    # private chats and other groups) would exhaust the 1600-combo namespace
    # for no benefit, since unmentioned contacts never appear in the LLM input.
    sender_wxids: set[str] = {
        msg.sender_wxid for msg in messages if msg.sender_wxid
    }
    mentioned_wxids = _scan_mentioned_wxids(messages, contact_map)
    all_wxids = sender_wxids | mentioned_wxids
    token_map = TokenMap.build(list(all_wxids), alias_db)

    # Precompute regex pattern + mapping; restrict to wxids we've tokenized.
    pattern, mapping = build_replace_state(
        contact_map, token_map, only_wxids=all_wxids,
    )

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


# ── Leak detection ───────────────────────────────────────────────────────────────

LEAK_MARK_OPEN = '<mark class="leak-warn">'
LEAK_MARK_CLOSE = '</mark>'


def leak_check(
    markdown: str,
    alias_db: "AliasDB",
) -> None:
    """Raise LeakDetected on the three hard-gate violations.

    Nickname leaks are no longer raised here — the group renderer wraps
    suspect occurrences with ``<mark class="leak-warn">…</mark>`` so the
    author can spot-check them visually before publishing.
    """
    for anon in alias_db.optout_anons():
        if anon in markdown:
            raise LeakDetected(f"Optout 用户默认匿名名泄漏: 「{anon}」")

    if re.search(r'\bwxid_\w+', markdown):
        raise LeakDetected("检测到原始 wxid 字符串泄漏")

    # token⟨原文⟩ disambiguation markers must never reach output. The system
    # prompt instructs the model to pick one side; any leftover ⟨ or ⟩ is a
    # model failure, surface it rather than silently ship malformed text.
    if '⟨' in markdown or '⟩' in markdown:
        raise LeakDetected("输出残留 ⟨…⟩ 同名消歧标记")


def mark_leaks(markdown: str, contact_map: "ContactMap") -> str:
    """Wrap occurrences of known real-name variants with a leak-warn mark.

    Called by the group renderer only — the public renderer reads the same
    ``report.markdown`` source (which never contains marks) so it doesn't
    need a stripping step. The highlight is a review signal for the author
    before the manual ``-y`` push; it isn't a hard block.

    Names ≥ 2 codepoints are wrapped, longest-first to avoid substring
    overlap. Tokens (default_anons / public aliases) are not wrapped —
    only real-name variants from ``contact_map``.
    """
    pairs = _nickname_pairs(contact_map)
    pattern = _compile_nickname_pattern(pairs)
    if pattern is None:
        return markdown
    return pattern.sub(
        lambda m: f"{LEAK_MARK_OPEN}{m.group(0)}{LEAK_MARK_CLOSE}",
        markdown,
    )
