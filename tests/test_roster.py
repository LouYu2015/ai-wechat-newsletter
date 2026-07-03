"""Unit tests for roster.py."""

from __future__ import annotations

from wechat_daily import aliases, contacts, privacy, roster

SALT = b'\x00' * 32


def _db() -> aliases.AliasDB:
    return aliases.AliasDB(users={}, reservations=[], salt=SALT)


def _make_token_map(db: aliases.AliasDB, wxids: list[str]) -> privacy.TokenMap:
    for w in wxids:
        db.get_or_create_user(w)
    return privacy.TokenMap.build(wxids, db)


def test_build_roster_basic():
    db = _db()
    db.get_or_create_user("wxid_a", real_name="鸭哥")
    db.get_or_create_user("wxid_b", real_name="李四")
    # 群昵称（preferred）+ 微信昵称（fallback）混合
    cmap = contacts.ContactMap.from_dict(
        {"wxid_a": "Alice", "wxid_b": "李四"},      # 微信昵称
        {"wxid_a": "鸭哥"},                         # 群昵称（仅 a 设了）
    )
    tm = privacy.TokenMap.build(["wxid_a", "wxid_b"], db)

    entries = roster.build_roster(tm, cmap, db)
    rdict = dict(entries)
    a_token = db.token_of("wxid_a")
    b_token = db.token_of("wxid_b")

    # a 的群昵称在前、微信昵称在后
    assert rdict[a_token] == ["鸭哥", "Alice"]
    # b 只有微信昵称且 real_name_seen 与之相同 → 去重为一条
    assert rdict[b_token] == ["李四"]


def test_build_roster_two_codepoint_group_display_kept():
    """A 2-character group display name (like 「鸭哥」) must show up — the
    old ≤ 4 filter is gone."""
    db = _db()
    db.get_or_create_user("wxid_a")
    cmap = contacts.ContactMap.from_dict({}, {"wxid_a": "鸭哥"})
    tm = privacy.TokenMap.build(["wxid_a"], db)

    entries = roster.build_roster(tm, cmap, db)
    rdict = dict(entries)
    assert rdict[db.token_of("wxid_a")] == ["鸭哥"]


def test_build_roster_excludes_optout():
    db = _db()
    db.get_or_create_user("wxid_a", real_name="Alice")
    db.apply_command("wxid_a", "/optout", 1000)
    cmap = contacts.ContactMap.from_dict({"wxid_a": "Alice"})
    tm = privacy.TokenMap.build(["wxid_a"], db)

    entries = roster.build_roster(tm, cmap, db)
    assert entries == []


def test_build_roster_drops_users_without_variants():
    """A wxid with no contact entry and no real_name_seen produces no entries row."""
    db = _db()
    # No real_name passed; real_name_seen falls back to wxid → filtered out
    db.get_or_create_user("wxid_ghost")
    cmap = contacts.ContactMap.from_dict({}, {})
    tm = privacy.TokenMap.build(["wxid_ghost"], db)

    entries = roster.build_roster(tm, cmap, db)
    assert entries == []


def test_build_roster_sorted_by_token():
    db = _db()
    for w in ["wxid_a", "wxid_b", "wxid_c"]:
        db.get_or_create_user(w, real_name=f"name_{w}")
    cmap = contacts.ContactMap.from_dict({}, {})
    tm = privacy.TokenMap.build(["wxid_c", "wxid_a", "wxid_b"], db)

    entries = roster.build_roster(tm, cmap, db)
    tokens = [t for t, _ in entries]
    assert tokens == sorted(tokens)


def test_format_roster_empty():
    assert roster.format_roster([]) == ""


def test_format_roster_renders():
    entries = [("沉稳的狐狸", ["Alice", "鸭哥"]), ("聪明的老虎", ["李四"])]
    out = roster.format_roster(entries)
    assert "沉稳的狐狸：Alice、鸭哥" in out
    assert "聪明的老虎：李四" in out
    assert out.startswith("## 群友花名册")
