"""End-to-end integration test for the markdown-based DailyReport pipeline.

Three synthetic users:
  - wxid_alice: optout (default_anon must not leak; references → 「某群友」)
  - wxid_bob:   public_alias "Duckie"
  - wxid_carol: default anon only

Validates:
  - Group version replaces tokens with real names; preserves all sections
    including those marked `[章节不公开：…]` (rendered with 🔒 + banner).
  - Public version replaces tokens with public aliases / 某群友;
    drops sections marked `[章节不公开：…]` entirely;
    contains Jekyll front matter.
  - Leak check passes on the public version.
"""

from __future__ import annotations

from wechat_daily.aliases import AliasDB, compute_default_anon
from wechat_daily.contacts import ContactMap
from wechat_daily.models import DailyReport
from wechat_daily.privacy import leak_check
from wechat_daily.renderer import render_group, render_public

SALT = b"\x42" * 32
DATE = "2026-04-30"


def _make_db() -> AliasDB:
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db.get_or_create_user("wxid_bob", "Bob")
    db.get_or_create_user("wxid_carol", "Carol")
    db._users["wxid_alice"]["optout"] = True
    db.apply_command("wxid_bob", "/alias Duckie", 1000, "Bob")
    return db


def _make_contacts() -> ContactMap:
    return ContactMap.from_dict({
        "wxid_alice": "Alice",
        "wxid_bob": "Bob",
        "wxid_carol": "Carol",
    })


def _make_report(db: AliasDB) -> DailyReport:
    a = db.token_of("wxid_alice")
    b = db.token_of("wxid_bob")
    c = db.token_of("wxid_carol")

    md = (
        f"今天群里讨论了很多内容。{b} 分享了一个新工具。\n\n"
        f"## 行业新闻\n\n"
        f"### 新模型发布\n"
        f"某公司发布了新模型，群友讨论热烈。\n\n"
        f"> {b}：这个模型很厉害\n\n"
        f"> {c}：期待看实测\n\n"
        f"## 闲聊花絮\n\n"
        f"### 有趣的互动\n"
        f"大家围绕一个梗玩了很久，{a} 也参与了。\n\n"
        f"[章节不公开：笑点依赖当事人具体身份]\n\n"
        f"## 工具\n\n"
        f"### 实用工具推荐\n"
        f"一款新 AI 编程工具受到群友好评。\n\n"
        f"> {c}：非常好用\n\n"
        f"---\n\ntags: model-release, coding, tool\n"
    )
    return DailyReport(date=DATE, markdown=md)


# ── Group version assertions ──────────────────────────────────────────────────


def test_group_contains_real_names():
    db = _make_db()
    out = render_group(_make_report(db), db, _make_contacts(), command_log=[])
    assert "Bob" in out
    assert "Carol" in out


def test_group_contains_all_sections():
    db = _make_db()
    out = render_group(_make_report(db), db, _make_contacts(), command_log=[])
    assert "新模型发布" in out
    assert "有趣的互动" in out  # hidden section still renders in group version
    assert "实用工具推荐" in out


def test_group_marks_hidden_section():
    db = _make_db()
    out = render_group(_make_report(db), db, _make_contacts(), command_log=[])
    assert "🔒 有趣的互动" in out
    assert "公开版隐藏" in out
    assert "笑点依赖当事人具体身份" in out


def test_group_optout_token_replaced_with_real_name_in_internal():
    """In the internal version we still want to know who Alice is."""
    db = _make_db()
    out = render_group(_make_report(db), db, _make_contacts(), command_log=[])
    assert "Alice" in out


def test_group_has_instruction_log_subsections():
    db = _make_db()
    out = render_group(_make_report(db), db, _make_contacts(), command_log=[])
    assert "本期指令执行记录" in out
    assert "可用指令说明" in out
    assert "规则提示" in out


# ── Public version assertions ─────────────────────────────────────────────────


def test_public_uses_alias_not_real_name():
    db = _make_db()
    out = render_public(_make_report(db), db)
    assert "Duckie" in out
    assert "Bob" not in out


def test_public_drops_hidden_section():
    db = _make_db()
    out = render_public(_make_report(db), db)
    assert "有趣的互动" not in out
    assert "笑点依赖当事人具体身份" not in out
    assert "[章节不公开" not in out


def test_public_includes_safe_sections():
    db = _make_db()
    out = render_public(_make_report(db), db)
    assert "新模型发布" in out
    assert "实用工具推荐" in out


def test_public_optout_token_to_某群友():
    db = _make_db()
    out = render_public(_make_report(db), db)
    alice_anon = compute_default_anon("wxid_alice", SALT)
    assert alice_anon not in out
    # Alice was only mentioned in the (now hidden) anecdote, so 某群友 may not
    # appear in the public output. The hard requirement is that her token and
    # real name are both absent.
    assert "Alice" not in out


def test_public_has_jekyll_front_matter():
    db = _make_db()
    out = render_public(_make_report(db), db)
    assert out.startswith("---\n")
    assert "layout: post" in out
    assert 'license: "CC BY-NC 4.0"' in out
    assert "toc: true" in out


def test_public_front_matter_carries_tags():
    db = _make_db()
    out = render_public(_make_report(db), db)
    assert "  - model-release" in out
    assert "  - coding" in out
    assert "  - tool" in out


def test_public_no_instruction_log():
    db = _make_db()
    out = render_public(_make_report(db), db)
    assert "指令执行记录" not in out


# ── Leak detection ────────────────────────────────────────────────────────────


def test_public_passes_leak_check():
    db = _make_db()
    out = render_public(_make_report(db), db)
    leak_check(out, db)


def test_public_no_real_names():
    db = _make_db()
    out = render_public(_make_report(db), db)
    for name in ["Alice", "Bob", "Carol"]:
        assert name not in out, f"Real name {name!r} leaked into public version"


def test_public_no_optout_anon():
    db = _make_db()
    out = render_public(_make_report(db), db)
    alice_anon = compute_default_anon("wxid_alice", SALT)
    assert alice_anon not in out
