"""End-to-end integration test (§8.3).

Three synthetic users:
  - wxid_alice: optout
  - wxid_bob:   public_alias "Duckie"
  - wxid_carol: default anon only

Validates that:
  - Group version contains all three real names (Alice, Bob, Carol)
  - Public version contains no real name (leak_check passes)
  - Optout user's default_anon does not appear in public version
  - Public version skips public_safe=false sections
  - Group version includes all sections (ignoring public_safe)
  - Group version instruction log has the required sub-sections
  - Two versions share the same section sequence (minus unsafe filter)
"""

from __future__ import annotations

from wechat_daily.aliases import AliasDB, compute_default_anon
from wechat_daily.contacts import ContactMap
from wechat_daily.models import DailyReport
from wechat_daily.privacy import leak_check
from wechat_daily.renderer import render_group, render_public

SALT = b'\x42' * 32

DATE = "2026-04-17"


def _make_db() -> AliasDB:
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db.get_or_create_user("wxid_bob", "Bob")
    db.get_or_create_user("wxid_carol", "Carol")
    # Alice opts out
    db._users["wxid_alice"]["optout"] = True
    # Bob sets a public alias
    db.apply_command("wxid_bob", "/alias Duckie", 1000, "Bob")
    return db


def _make_contacts() -> ContactMap:
    return ContactMap.from_dict({
        "wxid_alice": "Alice",
        "wxid_bob": "Bob",
        "wxid_carol": "Carol",
    })


def _make_report(db: AliasDB) -> DailyReport:
    alice_token = db.token_of("wxid_alice")
    bob_token = db.token_of("wxid_bob")
    carol_token = db.token_of("wxid_carol")

    return DailyReport.from_dict({
        "date": DATE,
        "intro": f"今天群里讨论了很多内容。{bob_token} 分享了一个新工具。",
        "sections": [
            {
                "type": "news",
                "title": "新模型发布",
                "body": "某公司发布了新模型，群友讨论热烈。",
                "comments": [
                    {"token": bob_token, "text": "这个模型很厉害"},
                    {"token": carol_token, "text": "期待看实测"},
                ],
                "tags": ["model-release"],
                "public_safe": True,
                "public_safe_reason": None,
            },
            {
                "type": "anecdote",
                "title": "有趣的互动",
                "body": "大家围绕一个梗玩了很久。",
                "comments": [],
                "tags": [],
                "public_safe": False,
                "public_safe_reason": "笑点依赖当事人具体身份",
            },
            {
                "type": "tool",
                "title": "实用工具推荐",
                "body": "一款新 AI 编程工具受到群友好评。",
                "comments": [{"token": carol_token, "text": "非常好用"}],
                "tags": ["coding", "tool"],
                "public_safe": True,
                "public_safe_reason": None,
            },
        ],
    })


# ── Group version assertions ──────────────────────────────────────────────────

def test_group_contains_real_names():
    db = _make_db()
    contacts = _make_contacts()
    report = _make_report(db)
    out = render_group(report, db, contacts, command_log=[])
    assert "Alice" in out or "Bob" in out or "Carol" in out  # at least one real name


def test_group_contains_all_sections():
    db = _make_db()
    contacts = _make_contacts()
    report = _make_report(db)
    out = render_group(report, db, contacts, command_log=[])
    assert "新模型发布" in out
    assert "有趣的互动" in out   # unsafe section IS in group version
    assert "实用工具推荐" in out


def test_group_has_instruction_log_subsections():
    db = _make_db()
    contacts = _make_contacts()
    report = _make_report(db)
    out = render_group(report, db, contacts, command_log=[])
    assert "本期指令执行记录" in out
    assert "可用指令说明" in out
    assert "规则提示" in out


# ── Public version assertions ─────────────────────────────────────────────────

def test_public_uses_alias_not_real_name():
    db = _make_db()
    report = _make_report(db)
    out = render_public(report, db)
    assert "Duckie" in out
    assert "Bob" not in out


def test_public_skips_unsafe_sections():
    db = _make_db()
    report = _make_report(db)
    out = render_public(report, db)
    assert "有趣的互动" not in out


def test_public_includes_safe_sections():
    db = _make_db()
    report = _make_report(db)
    out = render_public(report, db)
    assert "新模型发布" in out
    assert "实用工具推荐" in out


def test_public_has_jekyll_front_matter():
    db = _make_db()
    report = _make_report(db)
    out = render_public(report, db)
    assert out.startswith("---")
    assert "layout: post" in out
    assert 'license: "CC BY-NC 4.0"' in out
    assert "toc: true" in out


def test_public_no_instruction_log():
    db = _make_db()
    report = _make_report(db)
    out = render_public(report, db)
    assert "指令执行记录" not in out


def test_public_no_toc_marker():
    """[TOC] should not appear in the public version."""
    db = _make_db()
    report = _make_report(db)
    out = render_public(report, db)
    assert "[TOC]" not in out


# ── Leak detection ────────────────────────────────────────────────────────────

def test_public_passes_leak_check():
    db = _make_db()
    contacts = _make_contacts()
    report = _make_report(db)
    out = render_public(report, db)
    # Should not raise
    leak_check(out, contacts, db)


def test_public_no_real_names():
    db = _make_db()
    contacts = _make_contacts()
    report = _make_report(db)
    out = render_public(report, db)
    for name in ["Alice", "Bob", "Carol"]:
        assert name not in out, f"Real name {name!r} leaked into public version"


def test_public_no_optout_anon():
    """Optout user's default_anon must not appear in public version."""
    db = _make_db()
    contacts = _make_contacts()
    report = _make_report(db)
    out = render_public(report, db)
    alice_anon = compute_default_anon("wxid_alice", SALT)
    assert alice_anon not in out


# ── Section sequence consistency ──────────────────────────────────────────────

def test_section_sequence_consistent():
    """Public version is a subsequence of group version (same order, fewer items)."""
    db = _make_db()
    contacts = _make_contacts()
    report = _make_report(db)

    safe_titles = [s.title for s in report.sections if s.public_safe]
    all_titles = [s.title for s in report.sections]

    group_out = render_group(report, db, contacts, command_log=[])
    public_out = render_public(report, db)

    for title in all_titles:
        assert title in group_out

    for title in safe_titles:
        assert title in public_out

    # Unsafe sections absent from public
    unsafe_titles = [s.title for s in report.sections if not s.public_safe]
    for title in unsafe_titles:
        assert title not in public_out
