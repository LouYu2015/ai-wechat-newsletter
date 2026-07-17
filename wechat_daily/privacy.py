"""Token-ization, optout masking, and leak detection."""

from __future__ import annotations

import dataclasses
import datetime
import re
from typing import TYPE_CHECKING, Callable

from wechat_daily import message_parser

if TYPE_CHECKING:
    from wechat_daily import aliases, contacts


class LeakDetected(Exception):
    """Raised when a real nickname is found in the public Markdown."""


@dataclasses.dataclass
class TokenMap:
    """Bidirectional mapping: wxid ↔ token (= default_anon)."""

    _fwd: dict[str, str]  # wxid → token
    _rev: dict[str, str]  # token → wxid

    @classmethod
    def build(cls, wxids: list[str], alias_db: aliases.AliasDB) -> TokenMap:
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


def _nickname_pairs(contact_map: contacts.ContactMap) -> list[tuple[str, str]]:
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
    contact_map: contacts.ContactMap,
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
    messages: list[message_parser.Message],
    contact_map: contacts.ContactMap,
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
    return _replace_unprotected(
        text,
        pattern,
        lambda m: f"{mapping[m.group(0)]}⟨{m.group(0)}⟩",
    )


_TOKENIZE_PROTECT_RE = re.compile(
    r"\]\([^)]*\)"
    r"|<https?://[^>\s]+>"
    r"|https?://[^\s)]+"
)


def _replace_unprotected(
    text: str,
    pattern: "re.Pattern[str]",
    repl,
) -> str:
    """Apply *pattern* outside Markdown/autolink URL regions only."""
    out: list[str] = []
    pos = 0
    for m in _TOKENIZE_PROTECT_RE.finditer(text):
        out.append(pattern.sub(repl, text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(pattern.sub(repl, text[pos:]))
    return "".join(out)


def _tap_has_optout_party(
    content: str,
    contact_map: contacts.ContactMap,
    alias_db: aliases.AliasDB,
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
            remaining = remaining.replace(nickname, " ")
    return False


def tokenize_messages(
    messages: list[message_parser.Message],
    contact_map: contacts.ContactMap,
    alias_db: aliases.AliasDB,
    progress_cb: Callable[[int, int], None] | None = None,
) -> tuple[list[message_parser.Message], TokenMap]:
    """Apply full token-ization to a message list.

    Returns (tokenized_messages, token_map).
    Optout users' messages are replaced with run-length-merged placeholders.
    progress_cb(current, total) is called after each message if provided.
    """
    # Lazy allocation: only senders + nicknames actually mentioned today need
    # tokens. Pre-allocating for every contact (potentially thousands across
    # private chats and other groups) would exhaust the 1600-combo namespace
    # for no benefit, since unmentioned contacts never appear in the LLM input.
    sender_wxids: set[str] = {msg.sender_wxid for msg in messages if msg.sender_wxid}
    mentioned_wxids = _scan_mentioned_wxids(messages, contact_map)
    all_wxids = sender_wxids | mentioned_wxids
    token_map = TokenMap.build(list(all_wxids), alias_db)

    # Precompute regex pattern + mapping; restrict to wxids we've tokenized.
    pattern, mapping = build_replace_state(
        contact_map,
        token_map,
        only_wxids=all_wxids,
    )

    result: list[message_parser.Message] = []
    total = len(messages)

    # Optout run-length merging
    optout_run: list[message_parser.Message] = []

    def flush_optout_run() -> None:
        if not optout_run:
            return
        first = optout_run[0]
        last = optout_run[-1]
        ts_start = datetime.datetime.fromtimestamp(first.create_time).strftime("%H:%M")
        if len(optout_run) == 1:
            placeholder = message_parser.Message(
                create_time=first.create_time,
                local_type=first.local_type,
                sender_wxid="",
                content=f"[{ts_start}] [此消息已隐藏]",
            )
        else:
            ts_end = datetime.datetime.fromtimestamp(last.create_time).strftime("%H:%M")
            n = len(optout_run)
            placeholder = message_parser.Message(
                create_time=first.create_time,
                local_type=first.local_type,
                sender_wxid="",
                content=f"[{ts_start}–{ts_end}] [某群友连续发言 {n} 条已隐藏]",
            )
        result.append(placeholder)
        optout_run.clear()

    for idx, msg in enumerate(messages):
        sender = msg.sender_wxid

        # ── TAP: redact if either party is optout ───────────────────────────
        if msg.local_type == message_parser.MSG_TAP:
            flush_optout_run()
            content = msg.content
            if _tap_has_optout_party(content, contact_map, alias_db):
                result.append(
                    message_parser.Message(
                        create_time=msg.create_time,
                        local_type=msg.local_type,
                        sender_wxid="",
                        content="[某人做了个动作]",
                    )
                )
            else:
                result.append(
                    message_parser.Message(
                        create_time=msg.create_time,
                        local_type=msg.local_type,
                        sender_wxid="",
                        content=_replace_names(content, pattern, mapping),
                    )
                )
        elif msg.local_type == message_parser.MSG_SYSTEM:
            # ── System messages: tokenize names, pass through ───────────────
            flush_optout_run()
            result.append(
                message_parser.Message(
                    create_time=msg.create_time,
                    local_type=msg.local_type,
                    sender_wxid="",
                    content=_replace_names(msg.content, pattern, mapping),
                )
            )
        elif sender and alias_db.is_optout(sender):
            # ── Optout sender: accumulate run ────────────────────────────────
            optout_run.append(msg)
        else:
            flush_optout_run()
            # ── Normal message: tokenize sender + content ────────────────────
            token = token_map.token(sender) if sender else ""
            content = _replace_names(msg.content, pattern, mapping)

            quoted = msg.quoted
            if quoted:
                if quoted.speaker_wxid and alias_db.is_optout(quoted.speaker_wxid):
                    quoted = message_parser.QuotedMessage(
                        speaker_wxid=quoted.speaker_wxid,
                        speaker_name="",
                        content="[引用内容已隐藏]",
                        ref_type=quoted.ref_type,
                    )
                else:
                    quoted = message_parser.QuotedMessage(
                        speaker_wxid=quoted.speaker_wxid,
                        speaker_name=_replace_names(quoted.speaker_name, pattern, mapping),
                        content=_replace_names(quoted.content, pattern, mapping),
                        ref_type=quoted.ref_type,
                    )

            result.append(
                message_parser.Message(
                    create_time=msg.create_time,
                    local_type=msg.local_type,
                    sender_wxid=token,
                    content=content,
                    quoted=quoted,
                    image_md5=msg.image_md5,
                    link=msg.link,
                    inline_links=msg.inline_links,
                    link_context=msg.link_context,
                )
            )

        if progress_cb:
            progress_cb(idx + 1, total)

    flush_optout_run()
    return result, token_map


def _format_one_line(
    msg: message_parser.Message,
    captions: dict[str, str] | None = None,
) -> str | None:
    """Render one Message as a chat-history line, or None to skip.

    *captions* (``{image_md5: caption}``) is the text-only DeepSeek path's
    vision substitute: when an image's md5 has a caption, the ``[图片]``
    placeholder becomes ``[图片：<caption>]``. The Claude block path passes
    ``None`` so it keeps bare placeholders + real inline images.
    """
    ts = datetime.datetime.fromtimestamp(msg.create_time).strftime("%H:%M")

    if msg.local_type == message_parser.MSG_TAP:
        return f"[{ts}] {msg.content}"
    if msg.local_type == message_parser.MSG_SYSTEM:
        return f"[{ts}] [系统] {msg.content}"

    name = msg.sender_wxid  # already a token, or '' for placeholders
    is_placeholder = not name and (
        msg.content == "[此消息已隐藏]"
        or msg.content.startswith("[")
        and ("已隐藏]" in msg.content)
    )
    if is_placeholder:
        return msg.content
    if not name:
        return None

    content = msg.content
    if captions and msg.local_type == message_parser.MSG_IMAGE and msg.image_md5:
        cap = captions.get(msg.image_md5)
        if cap:
            content = content.replace("[图片]", f"[图片：{cap}]")
    line = f"[{ts}] {name}: {content}"
    if msg.link_context:
        for context in msg.link_context.splitlines():
            if context.strip():
                line += f"\n  [网页摘要] {context.strip()}"
    if msg.quoted:
        line += f"\n  > 引用 {msg.quoted.content}"
    return line


def _date_divider(day: datetime.date) -> str:
    """跨日分界线：标明其后消息所属的日历日期（破折号为三个 U+2014）。

    窗口两端各伸入相邻天（开头带前一日尾巴、结尾伸入次日），模型靠这行判断某段
    消息属于哪一天；语义/去重规则集中在 prompt，这里只陈述日期。
    """
    return f"——— 以下消息发生在 {day.strftime('%Y-%m-%d')} ———"


def format_tokenized_messages(
    messages: list[message_parser.Message],
    captions: dict[str, str] | None = None,
) -> str:
    """Format tokenized messages into plain-text chat history for LLM consumption.

    *captions* (``{image_md5: caption}``) inlines vision-model image
    descriptions as ``[图片：…]`` for the text-only DeepSeek path. Omit it
    (the default) to keep bare ``[图片]`` placeholders.

    第一条消息前、以及每次本地日历日期变化时插入一条日期分界线；以实际输出的
    消息为准判断日期变化（被跳过的消息不触发分界线）。
    """
    lines: list[str] = []
    last_date: datetime.date | None = None
    for msg in messages:
        line = _format_one_line(msg, captions)
        if line is None:
            continue
        msg_date = datetime.datetime.fromtimestamp(msg.create_time).date()
        if msg_date != last_date:
            lines.append(_date_divider(msg_date))
            last_date = msg_date
        lines.append(line)
    return "\n".join(lines)


def format_tokenized_messages_blocks(
    messages: list[message_parser.Message],
    image_decoder,  # ImageDecoder; duck-typed `.decode(md5) -> Path | None`
) -> list[dict]:
    """Same content as `format_tokenized_messages`, but as Anthropic content blocks.

    Inline image blocks are inserted **right after** the `[图片]` line they
    correspond to. If the image can't be decoded, the text line is still
    emitted so the LLM at least sees the placeholder.
    """
    import base64

    blocks: list[dict] = []
    text_buf: list[str] = []
    last_date: datetime.date | None = None

    def flush_text() -> None:
        if text_buf:
            blocks.append({"type": "text", "text": "\n".join(text_buf)})
            text_buf.clear()

    for msg in messages:
        line = _format_one_line(msg)
        if line is None:
            continue
        msg_date = datetime.datetime.fromtimestamp(msg.create_time).date()
        if msg_date != last_date:
            text_buf.append(_date_divider(msg_date))
            last_date = msg_date
        text_buf.append(line)

        if msg.local_type == message_parser.MSG_IMAGE and msg.image_md5:
            jpeg = image_decoder.decode(msg.image_md5)
            if jpeg is not None:
                flush_text()
                data = base64.standard_b64encode(jpeg.read_bytes()).decode("ascii")
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": data,
                        },
                    }
                )

    flush_text()
    return blocks


# ── Leak detection ───────────────────────────────────────────────────────────────

LEAK_MARK_OPEN = '<mark class="leak-warn">'
LEAK_MARK_CLOSE = "</mark>"


def leak_check(
    markdown: str,
    alias_db: aliases.AliasDB,
) -> None:
    """Raise LeakDetected on the three hard-gate violations.

    Nickname leaks are no longer raised here — the group renderer wraps
    suspect occurrences with ``<mark class="leak-warn">…</mark>`` so the
    author can spot-check them visually before publishing.
    """
    for anon in alias_db.optout_anons():
        if anon in markdown:
            raise LeakDetected(f"Optout 用户默认匿名名泄漏: 「{anon}」")

    if re.search(r"\bwxid_\w+", markdown):
        raise LeakDetected("检测到原始 wxid 字符串泄漏")

    # token⟨原文⟩ disambiguation markers must never reach output. The system
    # prompt instructs the model to pick one side; any leftover ⟨ or ⟩ is a
    # model failure, surface it rather than silently ship malformed text.
    if "⟨" in markdown or "⟩" in markdown:
        raise LeakDetected("输出残留 ⟨…⟩ 同名消歧标记")


# Regions that ``mark_leaks`` must not touch:
#   <span class="mention">…</span>
#                     — already token-resolved real names (the @mention pill);
#                       marking them again double-wraps every legit reference.
#   ](url)            — markdown link target; inserting <mark> inside breaks
#                       the URL (saw ``wei<mark>xin.</mark>qq.com`` in 05-02).
#   `inline code`     — code spans with identifier-shaped contents (e.g.
#                       ``config``) collide with English nicknames.
#   <https://…>       — autolinks; same URL-corruption concern as above.
_PROTECT_RE = re.compile(
    r'<span class="mention">[^<]*</span>'
    r"|\]\([^)]*\)"
    r"|`[^`\n]+`"
    r"|<https?://[^>\s]+>"
)


def _mark_leaks_threshold_pairs(
    contact_map: contacts.ContactMap,
) -> list[tuple[str, str]]:
    """Stricter filter than ``_nickname_pairs``: ASCII ≥ 4, CJK/other ≥ 3.

    The chat-history scanner aggressively matches ≥ 2 codepoints because the
    LLM gets ``token⟨原文⟩`` and can disambiguate. ``mark_leaks`` is a human
    review signal — every false positive is noise. Real data showed 2-char
    nicknames colliding with token internals (``企鹅`` inside ``开朗的企鹅``)
    and 3-char ASCII nicknames colliding with English words; raising the
    threshold removes the worst offenders without losing real leaks (which
    tend to be longer real names anyway).
    """
    out: list[tuple[str, str]] = []
    for n, w in contact_map.all_pairs():
        if _is_ascii_word(n):
            if len(n) >= 4:
                out.append((n, w))
        else:
            if len(n) >= 3:
                out.append((n, w))
    out.sort(key=lambda p: len(p[0]), reverse=True)
    return out


def mark_leaks(markdown: str, contact_map: contacts.ContactMap) -> str:
    """Wrap occurrences of known real-name variants with a leak-warn mark.

    Called by the group renderer *after* token replacement, so the input
    contains ``<u>real_name</u>`` for every legitimately-resolved token —
    those regions are skipped via ``_PROTECT_RE`` to avoid double-wrapping.
    Markdown link URLs, inline code, and autolinks are also protected.

    Tokens (default_anons / public aliases) are not wrapped — only real-name
    variants from ``contact_map`` that survived without going through token
    resolution (the genuine leak candidates).
    """
    pairs = _mark_leaks_threshold_pairs(contact_map)
    pattern = _compile_nickname_pattern(pairs)
    if pattern is None:
        return markdown

    def wrap(m: "re.Match[str]") -> str:
        return f"{LEAK_MARK_OPEN}{m.group(0)}{LEAK_MARK_CLOSE}"

    out: list[str] = []
    pos = 0
    for pm in _PROTECT_RE.finditer(markdown):
        out.append(pattern.sub(wrap, markdown[pos : pm.start()]))
        out.append(pm.group(0))
        pos = pm.end()
    out.append(pattern.sub(wrap, markdown[pos:]))
    return "".join(out)
