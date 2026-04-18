"""Snapshot tests for renderer.py."""

import pytest
from wechat_daily.aliases import AliasDB, compute_default_anon
from wechat_daily.contacts import ContactMap
from wechat_daily.renderer import DailyReport, Section, Comment, render_group, render_public

SALT = b'\x00' * 32


def _make_report() -> DailyReport:
    return DailyReport.from_dict({
        "date": "2026-04-17",
        "intro": "今天群里讨论了很多内容。",
        "sections": [
            {
                "type": "news",
                "title": "新模型发布",
                "body": "某公司发布了新模型。",
                "comments": [
                    {"token": compute_default_anon("wxid_alice", SALT), "text": "很厉害"},
                ],
                "tags": ["model-release"],
                "public_safe": True,
                "public_safe_reason": None,
            },
            {
                "type": "anecdote",
                "title": "有趣的互动",
                "body": "大家玩了个梗。",
                "comments": [],
                "tags": [],
                "public_safe": False,
                "public_safe_reason": "笑点依赖当事人身份",
            },
        ],
    })


def _make_db() -> AliasDB:
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db.apply_command("wxid_alice", "/alias Duckie", 1000)
    return db


def _make_contacts() -> ContactMap:
    return ContactMap.from_dict({"wxid_alice": "Alice"})


# ── render_group ────────────────────────────────────────────────────────────────

def test_render_group_contains_real_name():
    report = _make_report()
    db = _make_db()
    contacts = _make_contacts()
    out = render_group(report, db, contacts, command_log=[])
    assert "Alice" in out


def test_render_group_contains_all_sections():
    report = _make_report()
    db = _make_db()
    contacts = _make_contacts()
    out = render_group(report, db, contacts, command_log=[])
    assert "新模型发布" in out
    assert "有趣的互动" in out  # unsafe section still in group version


def test_render_group_contains_command_log():
    report = _make_report()
    db = _make_db()
    contacts = _make_contacts()
    log = [{'ts': 1000, 'wxid': 'wxid_alice', 'cmd': '/alias Duckie', 'ok': True, 'msg': '已设置别名'}]
    out = render_group(report, db, contacts, command_log=log)
    assert "指令执行记录" in out
    assert "可用指令说明" in out
    assert "规则提示" in out


def test_render_group_no_commands_shows_placeholder():
    report = _make_report()
    db = _make_db()
    contacts = _make_contacts()
    out = render_group(report, db, contacts, command_log=[])
    assert "今日无指令" in out


# ── render_public ───────────────────────────────────────────────────────────────

def test_render_public_uses_alias():
    report = _make_report()
    db = _make_db()
    out = render_public(report, db)
    assert "Duckie" in out
    assert "Alice" not in out


def test_render_public_skips_unsafe_sections():
    report = _make_report()
    db = _make_db()
    out = render_public(report, db)
    assert "有趣的互动" not in out


def test_render_public_includes_safe_sections():
    report = _make_report()
    db = _make_db()
    out = render_public(report, db)
    assert "新模型发布" in out


def test_render_public_has_jekyll_front_matter():
    report = _make_report()
    db = _make_db()
    out = render_public(report, db)
    assert out.startswith("---")
    assert "title:" in out
    assert "layout: post" in out
    assert "license:" in out


def test_render_public_no_command_log():
    report = _make_report()
    db = _make_db()
    out = render_public(report, db)
    assert "指令执行记录" not in out


# ── Front-matter edge cases ─────────────────────────────────────────────────────

def _report_with_intro(intro: str, tags=None) -> DailyReport:
    return DailyReport.from_dict({
        "date": "2026-04-17",
        "intro": intro,
        "sections": [{
            "type": "news",
            "title": "T",
            "body": "B",
            "comments": [],
            "tags": tags or [],
            "public_safe": True,
            "public_safe_reason": None,
        }],
    })


def _extract_front_matter(md: str) -> str:
    """Return the raw YAML text between the leading '---' fences."""
    assert md.startswith("---\n"), f"front matter must start with '---', got {md[:20]!r}"
    body = md[4:]
    end = body.find("\n---")
    assert end != -1, "closing '---' not found — intro may have broken front matter"
    return body[:end]


def _fm_lines(md: str) -> list[str]:
    return _extract_front_matter(md).splitlines()


def test_front_matter_basic_keys_present():
    report = _report_with_intro("Normal intro text.")
    db = _make_db()
    lines = _fm_lines(render_public(report, db))
    assert "layout: post" in lines
    assert "toc: true" in lines
    assert 'license: "CC BY-NC 4.0"' in lines


def test_front_matter_has_tags():
    report = _report_with_intro("Hi", tags=["model-release", "agent"])
    db = _make_db()
    fm = _extract_front_matter(render_public(report, db))
    assert "tags:" in fm
    assert "- model-release" in fm
    assert "- agent" in fm


def test_intro_with_dashes_does_not_break_front_matter():
    """An intro containing '---' lines must not extend/close front matter.

    render_public emits a blank line between the closing fence and intro,
    so the scanner's first '\\n---' hit is the intentional close.
    """
    report = _report_with_intro("Before\n---\nAfter")
    db = _make_db()
    out = render_public(report, db)
    # Front matter still extractable (closing fence found before the intro's ---)
    fm = _extract_front_matter(out)
    assert "layout: post" in fm
    # Both surrounding lines appear in body
    assert "Before" in out
    assert "After" in out


def test_intro_with_quotes_preserved():
    report = _report_with_intro('群友说 "这很棒"')
    db = _make_db()
    out = render_public(report, db)
    _extract_front_matter(out)  # still parseable
    assert '"这很棒"' in out


def test_intro_with_colon_preserved():
    report = _report_with_intro("关键点: 新模型发布")
    db = _make_db()
    out = render_public(report, db)
    _extract_front_matter(out)
    assert "关键点: 新模型发布" in out


def test_toc_marker_stripped_from_intro():
    report = _report_with_intro("Intro line.\n[TOC]\nMore intro.")
    db = _make_db()
    out = render_public(report, db)
    assert "[TOC]" not in out
    assert "Intro line." in out
    assert "More intro." in out


def test_empty_tags_renders_empty_list_literal():
    report = _report_with_intro("Hi", tags=[])
    db = _make_db()
    fm = _extract_front_matter(render_public(report, db))
    # When no tags, renderer emits the literal 'tags: []' form
    assert "tags: []" in fm


def test_front_matter_date_format():
    report = _report_with_intro("Hi")
    db = _make_db()
    fm = _extract_front_matter(render_public(report, db))
    assert "date: 2026-04-17 12:00:00 +0800" in fm


# ── Heading structure (H2 type group + H3 section title) ───────────────────────

def test_render_group_uses_h2_for_type_and_h3_for_title():
    report = _make_report()
    db = _make_db()
    contacts = _make_contacts()
    out = render_group(report, db, contacts, command_log=[])
    assert "## 行业新闻" in out
    assert "### 新模型发布" in out


def test_render_group_anecdote_type_label():
    report = _make_report()
    db = _make_db()
    contacts = _make_contacts()
    out = render_group(report, db, contacts, command_log=[])
    assert "## 闲聊花絮" in out
    assert "### 有趣的互动" in out


def test_render_public_uses_h2_for_type_and_h3_for_title():
    report = _make_report()
    db = _make_db()
    out = render_public(report, db)
    assert "## 行业新闻" in out
    assert "### 新模型发布" in out
    # unsafe anecdote section is filtered — its type header should not appear
    assert "## 闲聊花絮" not in out


# ── Token replacement in free-text fields ──────────────────────────────────────

def _make_report_with_tokens() -> DailyReport:
    """Report whose intro and body contain raw token strings."""
    alice_token = compute_default_anon("wxid_alice", SALT)
    return DailyReport.from_dict({
        "date": "2026-04-17",
        "intro": f"今天 {alice_token} 分享了很多内容。",
        "sections": [{
            "type": "news",
            "title": "新模型发布",
            "body": f"{alice_token} 认为这个模型很不错。",
            "comments": [
                {"token": alice_token, "text": f"{alice_token} 补充道：很厉害"},
            ],
            "tags": [],
            "public_safe": True,
            "public_safe_reason": None,
        }],
    })


def test_render_group_replaces_token_in_intro():
    report = _make_report_with_tokens()
    db = _make_db()
    contacts = _make_contacts()
    alice_token = compute_default_anon("wxid_alice", SALT)
    out = render_group(report, db, contacts, command_log=[])
    assert alice_token not in out
    assert "Alice" in out


def test_render_group_replaces_token_in_body():
    report = _make_report_with_tokens()
    db = _make_db()
    contacts = _make_contacts()
    alice_token = compute_default_anon("wxid_alice", SALT)
    out = render_group(report, db, contacts, command_log=[])
    assert alice_token not in out


def test_render_group_unknown_token_preserved():
    """A token not in alias_db must pass through unchanged."""
    unknown_token = "活泼的鸵鸟99"
    report = DailyReport.from_dict({
        "date": "2026-04-17",
        "intro": f"{unknown_token} 说了一句话。",
        "sections": [],
    })
    db = _make_db()
    contacts = _make_contacts()
    out = render_group(report, db, contacts, command_log=[])
    assert unknown_token in out


def test_render_public_replaces_token_with_public_alias():
    report = _make_report_with_tokens()
    db = _make_db()  # alice has public_alias = "Duckie"
    alice_token = compute_default_anon("wxid_alice", SALT)
    out = render_public(report, db)
    assert alice_token not in out
    assert "Duckie" in out
    assert "Alice" not in out


def test_render_public_masks_optout_token_in_body():
    """Optout user's token in body text is replaced with '某群友'."""
    alice_token = compute_default_anon("wxid_alice", SALT)
    report = DailyReport.from_dict({
        "date": "2026-04-17",
        "intro": f"{alice_token} 提出了一个观点。",
        "sections": [],
    })
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db._users["wxid_alice"]["optout"] = True
    out = render_public(report, db)
    assert alice_token not in out
    assert "某群友" in out
    assert "Alice" not in out
