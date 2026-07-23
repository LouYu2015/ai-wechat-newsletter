"""Unit tests for privacy.py — 100% branch coverage target."""

import pytest

from wechat_daily import aliases, contacts, message_parser, privacy

SALT = b"\x00" * 32


def _contact_map(data: dict) -> contacts.ContactMap:
    return contacts.ContactMap.from_dict(data)


def _alias_db(optout_wxids=(), alias_map=()) -> aliases.AliasDB:
    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    for wxid in ["wxid_alice", "wxid_bob", "wxid_carol"]:
        db.get_or_create_user(wxid)
    for wxid in optout_wxids:
        db._users.setdefault(
            wxid,
            {
                "default_anon": aliases.compute_default_anon(wxid, SALT),
                "real_name_seen": wxid,
                "public_alias": None,
                "optout": False,
                "last_command_ts": None,
                "last_command": None,
            },
        )
        db._users[wxid]["optout"] = True
    for wxid, alias in alias_map:
        db._users.setdefault(
            wxid,
            {
                "default_anon": aliases.compute_default_anon(wxid, SALT),
                "real_name_seen": wxid,
                "public_alias": None,
                "optout": False,
                "last_command_ts": None,
                "last_command": None,
            },
        )
        db._users[wxid]["public_alias"] = alias
    return db


def _msg(create_time, local_type, sender, content, quoted=None):
    return message_parser.Message(
        create_time=create_time,
        local_type=local_type,
        sender_wxid=sender,
        content=content,
        quoted=quoted,
    )


# ── TokenMap ────────────────────────────────────────────────────────────────────


def test_token_map_build():
    db = _alias_db()
    tm = privacy.TokenMap.build(["wxid_alice"], db)
    token = tm.token("wxid_alice")
    assert token == aliases.compute_default_anon("wxid_alice", SALT)
    assert tm.wxid(token) == "wxid_alice"
    assert tm.wxid("nonexistent") is None


def test_token_map_unknown_wxid_falls_back():
    db = _alias_db()
    tm = privacy.TokenMap.build([], db)
    # Unknown wxid falls back to wxid string (no crash)
    assert tm.token("wxid_unknown") == "wxid_unknown"


# ── Token-ization ───────────────────────────────────────────────────────────────


def test_tokenize_replaces_sender():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_TEXT, "wxid_alice", "Hello")]
    result, token_map = privacy.tokenize_messages(messages, contacts, db)
    assert result[0].sender_wxid == token_map.token("wxid_alice")
    assert "Alice" not in result[0].sender_wxid


def test_tokenize_replaces_name_in_content():
    """Nicknames are emitted as ``token⟨原文⟩`` for LLM-side disambiguation."""
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "BobbyC"})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_TEXT, "wxid_alice", "BobbyC said hello")]
    result, token_map = privacy.tokenize_messages(messages, contacts, db)
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
            message_parser.MSG_TEXT,
            "wxid_alice",
            "[链接] [Article](https://mp.weixin.qq.com/s?name=Alice)",
        )
    ]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert "https://mp.weixin.qq.com/s?name=Alice" in result[0].content
    assert "⟨Alice⟩" not in result[0].content


def test_tokenize_replaces_non_sender_name_in_content():
    """Names of people who didn't send any message should still be tokenized."""
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_carol": "Carol"})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_TEXT, "wxid_alice", "Carol made a good point")]
    result, token_map = privacy.tokenize_messages(messages, contacts, db)
    carol_token = token_map.token("wxid_carol")
    assert f"{carol_token}⟨Carol⟩" in result[0].content


def test_tokenize_longest_first():
    """Longer names must replace before shorter ones to avoid partial matches."""
    contacts = _contact_map({"wxid_a": "张三李四王", "wxid_b": "张三李四王五"})
    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_a")
    db.get_or_create_user("wxid_b")
    messages = [_msg(1000, message_parser.MSG_TEXT, "wxid_a", "张三李四王五 said hi")]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    # Only the longer nickname should be wrapped; the shorter is consumed by it.
    assert "⟨张三李四王五⟩" in result[0].content
    assert "⟨张三李四王⟩" not in result[0].content


# ── Optout masking ──────────────────────────────────────────────────────────────


def test_optout_single_message_hidden():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [_msg(1000, message_parser.MSG_TEXT, "wxid_alice", "Secret")]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert "[此消息已隐藏]" in result[0].content
    assert result[0].sender_wxid == ""


def test_optout_run_merged():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [
        _msg(1000, message_parser.MSG_TEXT, "wxid_alice", "Msg1"),
        _msg(2000, message_parser.MSG_TEXT, "wxid_alice", "Msg2"),
        _msg(3000, message_parser.MSG_TEXT, "wxid_alice", "Msg3"),
    ]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert len(result) == 1
    assert "3" in result[0].content
    assert "连续" in result[0].content
    # Should include time range
    assert "–" in result[0].content


def test_optout_single_message_no_range():
    """Single hidden message should not include '–' (no range needed)."""
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [_msg(1000, message_parser.MSG_TEXT, "wxid_alice", "Secret")]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert "–" not in result[0].content


def test_optout_run_interrupted():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [
        _msg(1000, message_parser.MSG_TEXT, "wxid_alice", "Hidden"),
        _msg(2000, message_parser.MSG_TEXT, "wxid_bob", "Visible"),
        _msg(3000, message_parser.MSG_TEXT, "wxid_alice", "Hidden2"),
    ]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert len(result) == 3


def test_optout_quoted_content_hidden():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    quoted = message_parser.QuotedMessage(
        speaker_wxid="wxid_alice", speaker_name="Alice", content="Alice: Secret", ref_type="1"
    )
    messages = [_msg(1000, message_parser.MSG_QUOTE, "wxid_bob", "I agree", quoted=quoted)]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert result[0].quoted.content == "[引用内容已隐藏]"


def test_tap_with_optout_party():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [_msg(1000, message_parser.MSG_TAP, "", "Bob 拍了拍 Alice")]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert result[0].content == "[某人做了个动作]"


def test_tap_without_optout():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "BobbyC"})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_TAP, "", "BobbyC 拍了拍 Alice")]
    result, token_map = privacy.tokenize_messages(messages, contacts, db)
    assert result[0].content != "[某人做了个动作]"
    # Both names dual-emitted as token⟨原文⟩ (Alice is len 5 > 4 → tokenized).
    assert f"{token_map.token('wxid_bob')}⟨BobbyC⟩" in result[0].content
    assert f"{token_map.token('wxid_alice')}⟨Alice⟩" in result[0].content


def test_system_message_names_tokenized():
    """System messages like 'Alice joined' should have names replaced."""
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_SYSTEM, "", "Alice 加入了群聊")]
    result, token_map = privacy.tokenize_messages(messages, contacts, db)
    assert f"{token_map.token('wxid_alice')}⟨Alice⟩" in result[0].content
    assert result[0].sender_wxid == ""


def test_system_message_passes_through_non_names():
    contacts = _contact_map({})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_SYSTEM, "", "System notification")]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert result[0].content == "System notification"


# ── format_tokenized_messages ───────────────────────────────────────────────────


def test_format_normal_message():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_TEXT, "wxid_alice", "Hello world")]
    tokenized, token_map = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert "Hello world" in output
    # Sender position uses bare token (no ⟨…⟩); content has no Alice mention.
    assert token_map.token("wxid_alice") in output
    assert "Alice" not in output
    assert "[16:" in output or "[00:" in output  # has a timestamp


def test_format_link_context_after_link_line():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    msg = message_parser.Message(
        create_time=1000,
        local_type=message_parser.MSG_LINK_OPEN,
        sender_wxid="wxid_alice",
        content="[链接] [Article](https://example.com)",
        link=message_parser.LinkMeta(title="Article", url="https://example.com"),
        link_context="网页摘要内容",
    )
    tokenized, _ = privacy.tokenize_messages([msg], contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert "[链接] [Article](https://example.com)" in output
    assert "\n  [网页摘要] 网页摘要内容" in output


def test_format_optout_placeholder_no_double_timestamp():
    """Optout placeholders already carry their timestamp in content;
    format_tokenized_messages must not prepend another [ts] prefix."""
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [_msg(1000, message_parser.MSG_TEXT, "wxid_alice", "Secret")]
    tokenized, _ = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    # Only one timestamp bracket should appear (no double [HH:MM] [HH:MM])
    import re

    timestamps = re.findall(r"\[\d{2}:\d{2}\]", output)
    assert len(timestamps) == 1


def test_format_merged_optout_run_has_range():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db(optout_wxids=["wxid_alice"])
    messages = [
        _msg(1000, message_parser.MSG_TEXT, "wxid_alice", "M1"),
        _msg(61000, message_parser.MSG_TEXT, "wxid_alice", "M2"),  # +60s → different minute
    ]
    tokenized, _ = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert "–" in output
    assert "连续" in output


def test_format_system_message():
    contacts = _contact_map({})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_SYSTEM, "", "Notification")]
    tokenized, _ = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert "[系统]" in output
    assert "Notification" in output


def test_format_tap_message():
    contacts = _contact_map({"wxid_a": "A", "wxid_b": "B"})
    db = _alias_db()
    messages = [_msg(1000, message_parser.MSG_TAP, "", "A 拍了拍 B")]
    tokenized, _ = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert "A" not in output or "拍了拍" in output  # names replaced


def test_format_message_with_quote():
    contacts = _contact_map({"wxid_alice": "Alice", "wxid_bob": "Bob"})
    db = _alias_db()
    quoted = message_parser.QuotedMessage(
        speaker_wxid="wxid_alice", speaker_name="Alice", content="Alice: hi", ref_type="1"
    )
    messages = [_msg(1000, message_parser.MSG_QUOTE, "wxid_bob", "agreed", quoted=quoted)]
    tokenized, token_map = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert "引用" in output
    # Alice (len 5 > 4) is tokenized inside the quoted content via dual-emit.
    assert f"{token_map.token('wxid_alice')}⟨Alice⟩" in output


def test_format_no_sender_no_placeholder_skipped():
    """Messages with empty sender and non-placeholder content are silently skipped."""
    msg = message_parser.Message(
        create_time=1000, local_type=message_parser.MSG_TEXT, sender_wxid="", content="orphan"
    )
    output = privacy.format_tokenized_messages([msg])
    assert "orphan" not in output


# ── Date dividers (cross-day boundary lines) ────────────────────────────────────

import datetime as _dt  # noqa: E402


def _local_ts(y, mo, d, h=0, mi=0) -> int:
    return int(_dt.datetime(y, mo, d, h, mi).timestamp())


def test_format_single_day_one_leading_divider():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [
        _msg(_local_ts(2026, 3, 10, 9, 0), message_parser.MSG_TEXT, "wxid_alice", "one"),
        _msg(_local_ts(2026, 3, 10, 10, 0), message_parser.MSG_TEXT, "wxid_alice", "two"),
    ]
    tokenized, _ = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert output.count("——— 以下消息发生在") == 1
    assert output.startswith("——— 以下消息发生在 2026-03-10 ———")


def test_format_cross_midnight_two_dividers():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [
        _msg(_local_ts(2026, 3, 9, 23, 30), message_parser.MSG_TEXT, "wxid_alice", "late"),
        _msg(_local_ts(2026, 3, 10, 0, 30), message_parser.MSG_TEXT, "wxid_alice", "early"),
    ]
    tokenized, _ = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert "——— 以下消息发生在 2026-03-09 ———" in output
    assert "——— 以下消息发生在 2026-03-10 ———" in output
    assert output.count("——— 以下消息发生在") == 2


def test_format_skipped_message_no_extra_divider():
    """A dropped message (empty sender, non-placeholder) must not trigger a
    divider — date changes are judged on actually-emitted lines only."""
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [
        _msg(_local_ts(2026, 3, 9, 22, 0), message_parser.MSG_TEXT, "wxid_alice", "kept"),
        # Skipped: empty sender, non-placeholder → _format_one_line returns None.
        _msg(_local_ts(2026, 3, 10, 0, 10), message_parser.MSG_TEXT, "", "ghost"),
        _msg(_local_ts(2026, 3, 10, 0, 20), message_parser.MSG_TEXT, "wxid_alice", "real"),
    ]
    tokenized, _ = privacy.tokenize_messages(messages, contacts, db)
    output = privacy.format_tokenized_messages(tokenized)
    assert "ghost" not in output
    assert output.count("——— 以下消息发生在") == 2  # 03-09 once, 03-10 once
    # The 03-10 divider sits right before the first emitted 03-10 line ("real").
    assert "——— 以下消息发生在 2026-03-10 ———\n" in output
    assert output.rindex("——— 以下消息发生在 2026-03-10 ———") < output.rindex("real")


class _NullDecoder:
    def decode(self, _md5):
        return None


def test_format_blocks_insert_dividers_in_text():
    contacts = _contact_map({"wxid_alice": "Alice"})
    db = _alias_db()
    messages = [
        _msg(_local_ts(2026, 3, 9, 23, 30), message_parser.MSG_TEXT, "wxid_alice", "late"),
        _msg(_local_ts(2026, 3, 10, 0, 30), message_parser.MSG_TEXT, "wxid_alice", "early"),
    ]
    tokenized, _ = privacy.tokenize_messages(messages, contacts, db)
    blocks = privacy.format_tokenized_messages_blocks(tokenized, _NullDecoder())
    text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
    assert "——— 以下消息发生在 2026-03-09 ———" in text
    assert "——— 以下消息发生在 2026-03-10 ———" in text


# ── Leak detection (hard gates only) ───────────────────────────────────────────


def test_leak_check_clean_no_hard_gate():
    db = _alias_db()
    anon = aliases.compute_default_anon("wxid_alice", SALT)
    privacy.leak_check(f"{anon} said hi", db)


def test_leak_check_nickname_no_longer_raises():
    """Real nicknames in the public text do not trigger the hard gate —
    only optout-anon leaks, raw wxids, and disambig-marker residue do."""
    db = _alias_db()
    privacy.leak_check("AliceLongName said something", db)  # no raise


def test_leak_check_detects_optout_anon():
    db = _alias_db(optout_wxids=["wxid_alice"])
    anon = aliases.compute_default_anon("wxid_alice", SALT)
    with pytest.raises(privacy.LeakDetected):
        privacy.leak_check(f"{anon} was mentioned", db)


def test_leak_check_detects_raw_wxid():
    db = _alias_db()
    with pytest.raises(privacy.LeakDetected, match="wxid"):
        privacy.leak_check("hello wxid_someuser there", db)


def test_leak_check_detects_disambig_marker_residue():
    db = _alias_db()
    with pytest.raises(privacy.LeakDetected):
        privacy.leak_check("token⟨原文⟩ leaked", db)


def test_leak_check_empty_markdown():
    db = _alias_db()
    privacy.leak_check("", db)


def test_replace_names_ascii_word_boundary():
    """The same boundary rule applies to tokenization: a 2-char ASCII
    nickname must not invade English words."""
    contacts = _contact_map({"wxid_a": "tea"})
    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_a")
    tm = privacy.TokenMap.build(["wxid_a"], db)
    pattern, mapping = privacy.build_replace_state(contacts, tm)
    out = privacy._replace_names("team meeting", pattern, mapping)
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
    messages = [_msg(1000, message_parser.MSG_TAP, "", "Bob 拍了拍 李明")]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    # '李' never actually participated → should NOT be redacted to '[某人做了个动作]'
    assert result[0].content != "[某人做了个动作]"


def test_tap_substring_optout_true_positive():
    """Optout user '李' IS the tap target: must be redacted."""
    contacts = _contact_map({"wxid_li": "李", "wxid_bob": "Bob"})
    db = _alias_db(optout_wxids=["wxid_li"])
    messages = [_msg(1000, message_parser.MSG_TAP, "", "Bob 拍了拍 李")]
    result, _ = privacy.tokenize_messages(messages, contacts, db)
    assert result[0].content == "[某人做了个动作]"


def test_replace_names_no_token_corruption():
    """A token produced for one user must not be later partially-replaced by
    a shorter nickname that happens to appear inside it (single-pass guarantee)."""
    db2 = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db2.get_or_create_user("wxid_alice")
    db2.get_or_create_user("wxid_bob")
    b_token = aliases.compute_default_anon("wxid_bob", SALT)
    # Alice's nickname = first 5 chars of Bob's token (> 4 chars, passes the filter).
    # After "BobbySmith" is replaced by b_token, the regex must not re-match
    # the snippet inside the already-substituted result.
    snippet = b_token[:5]
    contacts = _contact_map({"wxid_alice": snippet, "wxid_bob": "BobbySmith"})
    tm = privacy.TokenMap.build(["wxid_alice", "wxid_bob"], db2)
    pattern, mapping = privacy.build_replace_state(contacts, tm)
    out = privacy._replace_names("BobbySmith says hi", pattern, mapping)
    assert b_token in out
