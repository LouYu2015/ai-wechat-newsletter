"""Unit tests for privacy.py — 100% branch coverage target."""

import pytest
from unittest.mock import MagicMock

from wechat_daily.message_parser import Message, MSG_TEXT, MSG_TAP, MSG_SYSTEM, MSG_QUOTE, QuotedMessage
from wechat_daily.privacy import (
    tokenize_messages, format_tokenized_messages,
    leak_check, LeakDetected, TokenMap, _replace_names,
    ClaudeLeakConfirmer, build_replace_state,
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


# ── Fake confirmer for testing ──────────────────────────────────────────────────

class _FakeConfirmer:
    """Test double for ClaudeLeakConfirmer."""

    def __init__(self, verdict: bool = True, raises: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._verdict = verdict
        self._raises = raises

    def confirm_is_person(self, nickname: str, context: str) -> bool:
        self.calls.append((nickname, context))
        if self._raises:
            raise RuntimeError("simulated API failure")
        return self._verdict


# ── Leak detection ─────────────────────────────────────────────────────────────

def test_leak_check_clean():
    contacts = _contact_map({"wxid_a": "Alice"})
    db = _alias_db()
    anon = compute_default_anon("wxid_a", SALT)
    confirmer = _FakeConfirmer(verdict=False)
    leak_check(f"{anon} said hi", contacts, db, confirmer)
    assert confirmer.calls == []  # anon is not a real nickname, no call needed


def test_leak_check_non_person_not_blocked():
    """LLM says 'non-person' → no raise even though nickname appears."""
    contacts = _contact_map({"wxid_a": "Whisper"})
    db = _alias_db()
    confirmer = _FakeConfirmer(verdict=False)  # non-person
    leak_check("比较 Whisper 模型和 Qwen ASR 的效果", contacts, db, confirmer)
    assert len(confirmer.calls) == 1
    assert "Whisper" in confirmer.calls[0][1]  # context contains the word


def test_leak_check_person_confirmed_raises():
    """LLM says 'person' → raise LeakDetected."""
    contacts = _contact_map({"wxid_a": "Whisper"})
    db = _alias_db()
    confirmer = _FakeConfirmer(verdict=True)  # person
    with pytest.raises(LeakDetected, match="Whisper"):
        leak_check("Whisper 说得对", contacts, db, confirmer)


def test_leak_check_detects_real_name():
    contacts = _contact_map({"wxid_a": "AliceLongName"})
    db = _alias_db()
    confirmer = _FakeConfirmer(verdict=True)
    with pytest.raises(LeakDetected, match="AliceLongName"):
        leak_check("AliceLongName said something", contacts, db, confirmer)


def test_leak_check_detects_optout_anon():
    """Optout anon is a hard gate — confirmer must NOT be called."""
    contacts = _contact_map({"wxid_a": "Alice"})
    db = _alias_db(optout_wxids=["wxid_a"])
    anon = compute_default_anon("wxid_a", SALT)
    confirmer = _FakeConfirmer(verdict=False)
    with pytest.raises(LeakDetected):
        leak_check(f"{anon} was mentioned", contacts, db, confirmer)
    assert confirmer.calls == []  # hard gate, no LLM call


def test_leak_check_detects_raw_wxid():
    """Raw wxid is a hard gate — confirmer must NOT be called."""
    contacts = _contact_map({})
    db = _alias_db()
    confirmer = _FakeConfirmer(verdict=False)
    with pytest.raises(LeakDetected, match="wxid"):
        leak_check("hello wxid_someuser there", contacts, db, confirmer)
    assert confirmer.calls == []


def test_leak_check_empty_markdown():
    contacts = _contact_map({"wxid_a": "Alice"})
    db = _alias_db()
    confirmer = _FakeConfirmer(verdict=False)
    leak_check("", contacts, db, confirmer)  # should not raise


def test_leak_check_confirmer_exception_is_leak():
    """Confirmer exception → fail-closed, raise LeakDetected."""
    contacts = _contact_map({"wxid_a": "LongNickname"})
    db = _alias_db()
    confirmer = _FakeConfirmer(raises=True)
    with pytest.raises(LeakDetected):
        leak_check("LongNickname appeared here", contacts, db, confirmer)


def test_leak_check_multiple_occurrences_each_confirmed():
    """Same nickname appearing N times → confirmer called N times."""
    contacts = _contact_map({"wxid_a": "LongNickname"})
    db = _alias_db()
    confirmer = _FakeConfirmer(verdict=False)  # all non-person
    leak_check("LongNickname and LongNickname and LongNickname", contacts, db, confirmer)
    assert len(confirmer.calls) == 3


def test_leak_check_any_person_occurrence_blocks():
    """If any one occurrence is judged 'person', raise even if others are non-person."""
    contacts = _contact_map({"wxid_a": "LongNickname"})
    db = _alias_db()
    call_count = [0]

    def _verdict(nickname, context):
        call_count[0] += 1
        return call_count[0] == 2  # second call → person

    class _DynConfirmer:
        def confirm_is_person(self, nickname, context):
            return _verdict(nickname, context)

    with pytest.raises(LeakDetected):
        leak_check(
            "LongNickname tool, LongNickname said, LongNickname model",
            contacts, db, _DynConfirmer(),
        )
    assert call_count[0] == 2  # stops at first person hit


def test_leak_check_context_contains_nickname_and_surroundings():
    """Context passed to confirmer must contain the nickname and surrounding text."""
    contacts = _contact_map({"wxid_a": "LongNickname"})
    db = _alias_db()
    captured = []

    class _CapConfirmer:
        def confirm_is_person(self, nickname, context):
            captured.append(context)
            return False

    leak_check("prefix text LongNickname suffix text", contacts, db, _CapConfirmer())
    assert len(captured) == 1
    assert "LongNickname" in captured[0]
    assert "prefix" in captured[0]
    assert "suffix" in captured[0]


def test_leak_check_short_nickname_skipped():
    """Nicknames ≤ 4 codepoints are never checked (consistent with tokenization)."""
    contacts = _contact_map({"wxid_a": "Bob"})  # len=3 ≤ 4
    db = _alias_db()
    confirmer = _FakeConfirmer(verdict=True)
    leak_check("Bob said something about AI", contacts, db, confirmer)
    assert confirmer.calls == []  # short nickname skipped


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
