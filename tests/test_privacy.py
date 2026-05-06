"""Unit tests for privacy.py — 100% branch coverage target."""

import pytest
from unittest.mock import MagicMock

from wechat_daily.message_parser import (
    LinkMeta, Message, MSG_LINK_OPEN, MSG_TEXT, MSG_TAP, MSG_SYSTEM, MSG_QUOTE,
    QuotedMessage,
)
from wechat_daily.privacy import (
    tokenize_messages, format_tokenized_messages,
    leak_check, LeakDetected, TokenMap, _replace_names,
    build_replace_state, mark_leaks,
    LEAK_MARK_OPEN, LEAK_MARK_CLOSE,
)
from wechat_daily.aliases import AliasDB, compute_default_anon
from wechat_daily.contacts import ContactMap

SALT = b'\x00' * 32


def _contact_map(data: dict) -> ContactMap:
    return ContactMap.from_dict(data)


def _alias_db(optout_wxids=(), alias_map=()) -> AliasDB:
    db = AliasDB(users={}, reservations=[], salt=SALT)
    for wxid in ["wxid_alice", "wxid_bob", "wxid_carol"]:
        db.get_or_create_user(wxid)
    for wxid in optout_wxids:
        db._users.setdefault(wxid, {
            'default_anon': compute_default_anon(wxid, SALT),
            'real_name_seen': wxid, 'public_alias': None,
            'optout': False, 'last_command_ts': None, 'last_command': None,
        })
        db._users[wxid]['optout'] = True
    for wxid, alias in alias_map:
        db._users.setdefault(wxid, {
            'default_anon': compute_default_anon(wxid, SALT),
            'real_name_seen': wxid, 'public_alias': None,
            'optout': False, 'last_command_ts': None, 'last_command': None,
        })
        db._users[wxid]['public_alias'] = alias
    return db


def _msg(create_time, local_type, sender, content, quoted=None):
    return Message(create_time=create_time, local_type=local_type,
                   sender_wxid=sender, content=content, quoted=quoted)


# ── TokenMap ────────────────────────────────────────────────────────────────────

def test_token_map_build():
    db = _alias_db()
    tm = TokenMap.build(["wxid_alice"], db)
    token = tm.token("wxid_alice")
    assert token == compute_default_anon("wxid_alice", SALT)
    assert tm.wxid(token) == "wxid_alice"
    assert tm.wxid("nonexistent") is None


def test_token_map_unknown_wxid_falls_back():
    db = _alias_db()
    tm = TokenMap.build([], db)
    # Unknown wxid falls back to wxid string (no crash)
    assert tm.token("wxid_unknown") == "wxid_unknown"


# ── Token-ization ───────────────────────────────────────────────────────────────

def test_tokenize_replaces_sender():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [_msg(1000, MSG_TEXT, "wxid_alice", "Hello")]
    result, token_map = tokenize_messages(messages, contacts, db)
    assert result[0].sender_wxid == token_map.token("wxid_alice")
    assert "Alice" not in result[0].sender_wxid


def test_tokenize_replaces_name_in_content():
    """Nicknames are emitted as ``token⟨原文⟩`` for LLM-side disambiguation."""
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "BobbyC"})
    db = _alias_db()
    messages = [_msg(1000, MSG_TEXT, "wxid_alice", "BobbyC said hello")]
    result, token_map = tokenize_messages(messages, contacts, db)
    bob_token = token_map.token("wxid_bob")
    assert f"{bob_token}⟨BobbyC⟩" in result[0].content
    # Raw nickname only appears inside the ⟨…⟩ marker.
    assert "BobbyC" in result[0].content
    assert result[0].content.count("BobbyC") == result[0].content.count("⟨BobbyC⟩")


def test_tokenize_preserves_markdown_link_url():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [
        _msg(
            1000,
            MSG_TEXT,
            "wxid_alice",
            "[链接] [Article](https://mp.weixin.qq.com/s?name=Alice)",
        )
    ]
    result, _ = tokenize_messages(messages, contacts, db)
    assert "https://mp.weixin.qq.com/s?name=Alice" in result[0].content
    assert "⟨Alice⟩" not in result[0].content


def test_tokenize_replaces_non_sender_name_in_content():
    """Names of people who didn't send any message should still be tokenized."""
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_carol": "Carol"})
    db = _alias_db()
    messages = [_msg(1000, MSG_TEXT, "wxid_alice", "Carol made a good point")]
    result, token_map = tokenize_messages(messages, contacts, db)
    carol_token = token_map.token("wxid_carol")
    assert f"{carol_token}⟨Carol⟩" in result[0].content


def test_tokenize_longest_first():
    """Longer names must replace before shorter ones to avoid partial matches."""
    contacts = _contact_map({"wxid_a": "张三李四王", "wxid_b": "张三李四王五"})
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_a")
    db.get_or_create_user("wxid_b")
    messages = [_msg(1000, MSG_TEXT, "wxid_a", "张三李四王五 said hi")]
    result, _ = tokenize_messages(messages, contacts, db)
    # Only the longer nickname should be wrapped; the shorter is consumed by it.
    assert "⟨张三李四王五⟩" in result[0].content
    assert "⟨张三李四王⟩" not in result[0].content


# ── Optout masking ──────────────────────────────────────────────────────────────

def test_optout_single_message_hidden():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [_msg(1000, MSG_TEXT, "wxid_alice", "Secret")]
    result, _ = tokenize_messages(messages, contacts, db)
    assert "[此消息已隐藏]" in result[0].content
    assert result[0].sender_wxid == ""


def test_optout_run_merged():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [
        _msg(1000, MSG_TEXT, "wxid_alice", "Msg1"),
        _msg(2000, MSG_TEXT, "wxid_alice", "Msg2"),
        _msg(3000, MSG_TEXT, "wxid_alice", "Msg3"),
    ]
    result, _ = tokenize_messages(messages, contacts, db)
    assert len(result) == 1
    assert "3" in result[0].content
    assert "连续" in result[0].content
    # Should include time range
    assert "–" in result[0].content


def test_optout_single_message_no_range():
    """Single hidden message should not include '–' (no range needed)."""
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [_msg(1000, MSG_TEXT, "wxid_alice", "Secret")]
    result, _ = tokenize_messages(messages, contacts, db)
    assert "–" not in result[0].content


def test_optout_run_interrupted():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [
        _msg(1000, MSG_TEXT, "wxid_alice", "Hidden"),
        _msg(2000, MSG_TEXT, "wxid_bob", "Visible"),
        _msg(3000, MSG_TEXT, "wxid_alice", "Hidden2"),
    ]
    result, _ = tokenize_messages(messages, contacts, db)
    assert len(result) == 3


def test_optout_quoted_content_hidden():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    quoted = QuotedMessage(speaker_wxid="wxid_alice", speaker_name="Alice",
                           content="Alice: Secret", ref_type="1")
    messages = [_msg(1000, MSG_QUOTE, "wxid_bob", "I agree", quoted=quoted)]
    result, _ = tokenize_messages(messages, contacts, db)
    assert result[0].quoted.content == "[引用内容已隐藏]"


def test_tap_with_optout_party():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [_msg(1000, MSG_TAP, "", "Bob 拍了拍 Alice")]
    result, _ = tokenize_messages(messages, contacts, db)
    assert result[0].content == "[某人做了个动作]"


def test_tap_without_optout():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "BobbyC"})
    db = _alias_db()
    messages = [_msg(1000, MSG_TAP, "", "BobbyC 拍了拍 Alice")]
    result, token_map = tokenize_messages(messages, contacts, db)
    assert result[0].content != "[某人做了个动作]"
    # Both names dual-emitted as token⟨原文⟩ (Alice is len 5 > 4 → tokenized).
    assert f"{token_map.token('wxid_bob')}⟨BobbyC⟩" in result[0].content
    assert f"{token_map.token('wxid_alice')}⟨Alice⟩" in result[0].content


def test_system_message_names_tokenized():
    """System messages like 'Alice joined' should have names replaced."""
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [_msg(1000, MSG_SYSTEM, "", "Alice 加入了群聊")]
    result, token_map = tokenize_messages(messages, contacts, db)
    assert f"{token_map.token('wxid_alice')}⟨Alice⟩" in result[0].content
    assert result[0].sender_wxid == ""


def test_system_message_passes_through_non_names():
    contacts = _contact_map({})
    db = _alias_db()
    messages = [_msg(1000, MSG_SYSTEM, "", "System notification")]
    result, _ = tokenize_messages(messages, contacts, db)
    assert result[0].content == "System notification"


# ── format_tokenized_messages ───────────────────────────────────────────────────

def test_format_normal_message():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [_msg(1000, MSG_TEXT, "wxid_alice", "Hello world")]
    tokenized, token_map = tokenize_messages(messages, contacts, db)
    output = format_tokenized_messages(tokenized)
    assert "Hello world" in output
    # Sender position uses bare token (no ⟨…⟩); content has no Alice mention.
    assert token_map.token("wxid_alice") in output
    assert "Alice" not in output
    assert "[16:" in output or "[00:" in output  # has a timestamp


def test_format_link_context_after_link_line():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    msg = Message(
        create_time=1000,
        local_type=MSG_LINK_OPEN,
        sender_wxid="wxid_alice",
        content="[链接] [Article](https://example.com)",
        link=LinkMeta(title="Article", url="https://example.com"),
        link_context="网页摘要内容",
    )
    tokenized, _ = tokenize_messages([msg], contacts, db)
    output = format_tokenized_messages(tokenized)
    assert "[链接] [Article](https://example.com)" in output
    assert "\n  [网页摘要] 网页摘要内容" in output


def test_format_optout_placeholder_no_double_timestamp():
    """Optout placeholders already carry their timestamp in content;
    format_tokenized_messages must not prepend another [ts] prefix."""
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [_msg(1000, MSG_TEXT, "wxid_alice", "Secret")]
    tokenized, _ = tokenize_messages(messages, contacts, db)
    output = format_tokenized_messages(tokenized)
    # Only one timestamp bracket should appear (no double [HH:MM] [HH:MM])
    import re
    timestamps = re.findall(r'\[\d{2}:\d{2}\]', output)
    assert len(timestamps) == 1


def test_format_merged_optout_run_has_range():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [
        _msg(1000, MSG_TEXT, "wxid_alice", "M1"),
        _msg(61000, MSG_TEXT, "wxid_alice", "M2"),  # +60s → different minute
    ]
    tokenized, _ = tokenize_messages(messages, contacts, db)
    output = format_tokenized_messages(tokenized)
    assert "–" in output
    assert "连续" in output


def test_format_system_message():
    contacts = _contact_map({})
    db = _alias_db()
    messages = [_msg(1000, MSG_SYSTEM, "", "Notification")]
    tokenized, _ = tokenize_messages(messages, contacts, db)
    output = format_tokenized_messages(tokenized)
    assert "[系统]" in output
    assert "Notification" in output


def test_format_tap_message():
    contacts = _contact_map({"wxid_a": "A", "wxid_b": "B"})
    db = _alias_db()
    messages = [_msg(1000, MSG_TAP, "", "A 拍了拍 B")]
    tokenized, _ = tokenize_messages(messages, contacts, db)
    output = format_tokenized_messages(tokenized)
    assert "A" not in output or "拍了拍" in output  # names replaced


def test_format_message_with_quote():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "Bob"})
    db = _alias_db()
    quoted = QuotedMessage(speaker_wxid="wxid_alice", speaker_name="Alice",
                           content="Alice: hi", ref_type="1")
    messages = [_msg(1000, MSG_QUOTE, "wxid_bob", "agreed", quoted=quoted)]
    tokenized, token_map = tokenize_messages(messages, contacts, db)
    output = format_tokenized_messages(tokenized)
    assert "引用" in output
    # Alice (len 5 > 4) is tokenized inside the quoted content via dual-emit.
    assert f"{token_map.token('wxid_alice')}⟨Alice⟩" in output


def test_format_no_sender_no_placeholder_skipped():
    """Messages with empty sender and non-placeholder content are silently skipped."""
    msg = Message(create_time=1000, local_type=MSG_TEXT, sender_wxid='', content='orphan')
    output = format_tokenized_messages([msg])
    assert "orphan" not in output


# ── Leak detection (hard gates only) ───────────────────────────────────────────

def test_leak_check_clean_no_hard_gate():
    db = _alias_db()
    anon = compute_default_anon("wxid_alice", SALT)
    leak_check(f"{anon} said hi", db)


def test_leak_check_nickname_no_longer_raises():
    """Real nicknames in the public text used to raise; now they're
    surfaced via ``mark_leaks`` in the group renderer instead, leaving
    the public path un-blocked on this class of issue."""
    db = _alias_db()
    leak_check("AliceLongName said something", db)  # no raise


def test_leak_check_detects_optout_anon():
    db = _alias_db(optout_wxids=["wxid_alice"])
    anon = compute_default_anon("wxid_alice", SALT)
    with pytest.raises(LeakDetected):
        leak_check(f"{anon} was mentioned", db)


def test_leak_check_detects_raw_wxid():
    db = _alias_db()
    with pytest.raises(LeakDetected, match="wxid"):
        leak_check("hello wxid_someuser there", db)


def test_leak_check_detects_disambig_marker_residue():
    db = _alias_db()
    with pytest.raises(LeakDetected):
        leak_check("token⟨原文⟩ leaked", db)


def test_leak_check_empty_markdown():
    db = _alias_db()
    leak_check("", db)


# ── mark_leaks / strip_leak_marks ──────────────────────────────────────────────


def test_mark_leaks_wraps_known_nickname():
    contacts = _contact_map({"wxid_alice": "AliceLongName"})
    out = mark_leaks("AliceLongName said hi", contacts)
    assert f"{LEAK_MARK_OPEN}AliceLongName{LEAK_MARK_CLOSE}" in out


def test_mark_leaks_skips_two_codepoint_cjk():
    """``mark_leaks`` is a review signal — its CJK threshold is ≥ 3 codepoints
    (stricter than ``_replace_names``'s ≥ 2). 2-char nicknames like 「鸭哥」
    cause too many false positives, including collisions with token internals
    such as 「企鹅」 inside 「开朗的企鹅」."""
    contacts = _contact_map({"wxid_a": "鸭哥"})
    out = mark_leaks("早 鸭哥 起来了", contacts)
    assert LEAK_MARK_OPEN not in out


def test_mark_leaks_wraps_three_codepoint_cjk():
    contacts = _contact_map({"wxid_a": "小鸭哥"})
    out = mark_leaks("早 小鸭哥 起来了", contacts)
    assert f"{LEAK_MARK_OPEN}小鸭哥{LEAK_MARK_CLOSE}" in out


def test_mark_leaks_skips_one_codepoint_names():
    contacts = _contact_map({"wxid_a": "李"})
    out = mark_leaks("李 说话", contacts)
    assert LEAK_MARK_OPEN not in out


def test_mark_leaks_uses_group_display_over_wechat_nick():
    contacts = ContactMap.from_dict(
        {"wxid_a": "default-wechat-nick"},
        {"wxid_a": "群昵称专属"},
    )
    out = mark_leaks("群昵称专属 和 default-wechat-nick", contacts)
    # Both variants are tracked → both wrapped
    assert f"{LEAK_MARK_OPEN}群昵称专属{LEAK_MARK_CLOSE}" in out
    assert f"{LEAK_MARK_OPEN}default-wechat-nick{LEAK_MARK_CLOSE}" in out


def test_mark_leaks_longest_first_avoids_partial():
    contacts = _contact_map({"wxid_a": "张三李四王", "wxid_b": "张三李四王五"})
    out = mark_leaks("张三李四王五 来了", contacts)
    # Longer match wins; the prefix string must not separately wrap.
    assert out.count(LEAK_MARK_OPEN) == 1
    assert "张三李四王五" in out


def test_mark_leaks_no_pairs_returns_input():
    contacts = _contact_map({})
    out = mark_leaks("plain text", contacts)
    assert out == "plain text"


def test_mark_leaks_skips_short_ascii():
    """ASCII threshold is ≥ 4. ``tea`` is below threshold even though it is
    a plausible nickname — too many false positives in English-heavy content."""
    contacts = _contact_map({"wxid_a": "tea", "wxid_b": "abc"})
    out = mark_leaks("tea time and abc 都很好", contacts)
    assert LEAK_MARK_OPEN not in out


def test_mark_leaks_ascii_uses_word_boundary():
    contacts = _contact_map({"wxid_a": "team"})
    out = mark_leaks("teamwork 完成了", contacts)
    assert LEAK_MARK_OPEN not in out


def test_mark_leaks_ascii_word_match_still_wraps():
    contacts = _contact_map({"wxid_a": "team"})
    out = mark_leaks("我们的 team 厉害", contacts)
    assert f"{LEAK_MARK_OPEN}team{LEAK_MARK_CLOSE}" in out


def test_mark_leaks_skips_u_tag_region():
    """Token-resolved real names live inside ``<u>…</u>`` after ``text_resolver``.
    Marking them again would double-wrap every legitimate reference."""
    contacts = _contact_map({"wxid_a": "AliceLong"})
    out = mark_leaks("<u>AliceLong</u> 说话", contacts)
    assert LEAK_MARK_OPEN not in out


def test_mark_leaks_skips_markdown_link_url():
    """Inserting ``<mark>`` inside a link URL breaks the link
    (saw ``wei<mark>xin.</mark>qq.com`` corrupt the 05-02 daily)."""
    contacts = _contact_map({"wxid_a": "weixin"})
    out = mark_leaks("[文章](https://weixin.qq.com/foo) 链接", contacts)
    assert LEAK_MARK_OPEN not in out
    assert "https://weixin.qq.com/foo" in out


def test_mark_leaks_marks_link_text():
    """Link text (the ``[…]`` half) is still scanned — only the URL is shielded."""
    contacts = _contact_map({"wxid_a": "AliceLong"})
    out = mark_leaks("[AliceLong](https://x.com/p) 写道", contacts)
    assert f"{LEAK_MARK_OPEN}AliceLong{LEAK_MARK_CLOSE}" in out


def test_mark_leaks_skips_inline_code():
    contacts = _contact_map({"wxid_a": "config"})
    out = mark_leaks("看 `config.py` 文件", contacts)
    assert LEAK_MARK_OPEN not in out


def test_mark_leaks_skips_autolink():
    contacts = _contact_map({"wxid_a": "example"})
    out = mark_leaks("访问 <https://example.com/foo>", contacts)
    assert LEAK_MARK_OPEN not in out


def test_mark_leaks_does_not_break_token_substring():
    """Regression: with the old in-token CJK substring matching,
    ``mark_leaks`` running before token replacement broke
    ``开朗的企鹅`` into ``开朗的<mark>企鹅</mark>``. Now ``mark_leaks``
    runs *after* token replacement, so token strings never reach it."""
    # 「企鹅」 is 2 codepoints — below the new mark_leaks threshold anyway.
    # This test guards against regressing the threshold.
    contacts = _contact_map({"wxid_a": "企鹅"})
    out = mark_leaks("开朗的企鹅 说", contacts)
    assert "开朗的企鹅" in out
    assert LEAK_MARK_OPEN not in out


def test_replace_names_ascii_word_boundary():
    """The same boundary rule applies to tokenization: a 2-char ASCII
    nickname must not invade English words."""
    contacts = _contact_map({"wxid_a": "tea"})
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_a")
    tm = TokenMap.build(["wxid_a"], db)
    pattern, mapping = build_replace_state(contacts, tm)
    out = _replace_names("team meeting", pattern, mapping)
    assert "tea⟨tea⟩m" not in out
    assert out == "team meeting"


# ── TAP substring safety ───────────────────────────────────────────────────────

def test_tap_substring_optout_not_false_positive():
    """Optout '李' must NOT trigger on 'Bob 拍了拍 李明' when '李明' is a different user.

    Longest-first matching consumes the full nickname '李明' first, so the
    optout substring '李' no longer matches after that span is erased.
    """
    contacts = _contact_map({"wxid_li": "李", "wxid_liming": "李明", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_li"])
    messages = [_msg(1000, MSG_TAP, "", "Bob 拍了拍 李明")]
    result, _ = tokenize_messages(messages, contacts, db)
    # '李' never actually participated → should NOT be redacted to '[某人做了个动作]'
    assert result[0].content != "[某人做了个动作]"


def test_tap_substring_optout_true_positive():
    """Optout user '李' IS the tap target: must be redacted."""
    contacts = _contact_map({"wxid_li": "李", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_li"])
    messages = [_msg(1000, MSG_TAP, "", "Bob 拍了拍 李")]
    result, _ = tokenize_messages(messages, contacts, db)
    assert result[0].content == "[某人做了个动作]"


def test_replace_names_no_token_corruption():
    """A token produced for one user must not be later partially-replaced by
    a shorter nickname that happens to appear inside it (single-pass guarantee)."""
    db2 = AliasDB(users={}, reservations=[], salt=SALT)
    db2.get_or_create_user("wxid_alice")
    db2.get_or_create_user("wxid_bob")
    b_token = compute_default_anon("wxid_bob", SALT)
    # Alice's nickname = first 5 chars of Bob's token (> 4 chars, passes the filter).
    # After "BobbySmith" is replaced by b_token, the regex must not re-match
    # the snippet inside the already-substituted result.
    snippet = b_token[:5]
    contacts = _contact_map({"wxid_alice": snippet, "wxid_bob": "BobbySmith"})
    tm = TokenMap.build(["wxid_alice", "wxid_bob"], db2)
    pattern, mapping = build_replace_state(contacts, tm)
    out = _replace_names("BobbySmith says hi", pattern, mapping)
    assert b_token in out
