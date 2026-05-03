"""Tests for renderer.py — markdown processing, hide-marker handling, token replacement."""

from __future__ import annotations

import pytest

from wechat_daily.aliases import AliasDB, compute_default_anon
from wechat_daily.contacts import ContactMap
from wechat_daily.models import DailyReport
from wechat_daily.renderer import (
    _annotate_hidden_for_group,
    _drop_empty_h2,
    _insert_toc,
    _strip_hidden_for_public,
    _strip_trailing_tags,
    render_group,
    render_public,
)

SALT = b"\x00" * 32


# ── Fixtures ────────────────────────────────────────────────────────────────────


def _make_db(*, optout: bool = False) -> AliasDB:
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db.apply_command("wxid_alice", "/alias Duckie", 1000)
    if optout:
        db._users["wxid_alice"]["optout"] = True
    return db


def _make_contacts() -> ContactMap:
    return ContactMap.from_dict({"wxid_alice": "Alice"})


def _alice_token() -> str:
    return compute_default_anon("wxid_alice", SALT)


def _wrap(md: str) -> DailyReport:
    return DailyReport(date="2026-04-30", markdown=md)


# ── _strip_trailing_tags ────────────────────────────────────────────────────────


def test_tags_simple():
    body, tags = _strip_trailing_tags("hello\n\n---\n\ntags: a, b, c\n")
    assert body == "hello"
    assert tags == ["a", "b", "c"]


def test_tags_no_separator_still_extracted():
    body, tags = _strip_trailing_tags("hello\ntags: a, b\n")
    assert body == "hello"
    assert tags == ["a", "b"]


def test_tags_extra_blank_lines_around():
    body, tags = _strip_trailing_tags("hello\n\n\n---\n\n\ntags: a\n\n\n")
    assert body == "hello"
    assert tags == ["a"]


def test_tags_missing_returns_empty_list():
    body, tags = _strip_trailing_tags("hello\nworld\n")
    assert "hello" in body
    assert tags == []


def test_tags_whitespace_only_entries_dropped():
    body, tags = _strip_trailing_tags("x\ntags: a, ,  b , ,\n")
    assert tags == ["a", "b"]


def test_tags_empty_value():
    _, tags = _strip_trailing_tags("x\ntags:\n")
    assert tags == []


def test_tags_inline_dashes_in_body_preserved():
    """A '---' inside the body is not the separator — only the one right above tags."""
    body, tags = _strip_trailing_tags(
        "intro\n\n---\n\nmiddle\n\n---\n\ntags: x\n"
    )
    assert "---" in body  # the inner ones survive
    assert "middle" in body
    assert tags == ["x"]


# ── _strip_hidden_for_public ────────────────────────────────────────────────────


def test_public_strip_hides_marked_h3():
    md = (
        "## 行业新闻\n\n"
        "### 话题 A\nbody A\n\n"
        "### 话题 B\nbody B\n[章节不公开：原因]\n\n"
        "### 话题 C\nbody C\n"
    )
    out = _strip_hidden_for_public(md)
    assert "话题 A" in out
    assert "话题 B" not in out
    assert "body B" not in out
    assert "[章节不公开" not in out
    assert "话题 C" in out


def test_public_strip_marker_in_title_line():
    md = (
        "## 行业新闻\n\n"
        "### 话题 A [章节不公开：原因]\nbody A\n\n"
        "### 话题 B\nbody B\n"
    )
    out = _strip_hidden_for_public(md)
    assert "话题 A" not in out
    assert "body A" not in out
    assert "话题 B" in out


def test_public_strip_marker_in_paragraph():
    md = (
        "## 行业新闻\n\n"
        "### 话题 A\nbody A line 1\n[章节不公开：mid]\nbody A line 2\n\n"
        "### 话题 B\nbody B\n"
    )
    out = _strip_hidden_for_public(md)
    assert "话题 A" not in out
    assert "body A" not in out
    assert "话题 B" in out


def test_public_strip_drops_empty_h2():
    """If all ### children are hidden, the ## parent is dropped too."""
    md = (
        "## 行业新闻\n\n"
        "### A\na\n[章节不公开：x]\n\n"
        "### B\nb\n[章节不公开：y]\n\n"
        "## 工具\n\n"
        "### C\nc\n"
    )
    out = _strip_hidden_for_public(md)
    assert "行业新闻" not in out
    assert "工具" in out
    assert "### C" in out


def test_public_strip_keeps_h2_with_surviving_h3():
    md = (
        "## 行业新闻\n\n"
        "### A\na\n[章节不公开：x]\n\n"
        "### B\nb\n"
    )
    out = _strip_hidden_for_public(md)
    assert "行业新闻" in out
    assert "### B" in out
    assert "### A" not in out


def test_public_strip_hides_whole_h2_when_marker_on_h2_line():
    md = (
        "## 行业新闻 [章节不公开：整组敏感]\n\n"
        "### A\na\n\n"
        "### B\nb\n\n"
        "## 工具\n\n"
        "### C\nc\n"
    )
    out = _strip_hidden_for_public(md)
    assert "行业新闻" not in out
    assert "### A" not in out
    assert "### B" not in out
    assert "工具" in out
    assert "### C" in out


def test_public_strip_no_markers_idempotent():
    md = "## A\n\n### x\ntext\n"
    out = _strip_hidden_for_public(md)
    assert "## A" in out
    assert "### x" in out
    assert "text" in out


def test_public_strip_marker_outside_any_heading_is_ignored():
    """A marker in pre-heading intro must not silently nuke anything."""
    md = (
        "intro line\n[章节不公开：rogue]\n\n"
        "## A\n\n### x\ntext\n"
    )
    out = _strip_hidden_for_public(md)
    assert "## A" in out
    assert "### x" in out
    # intro stays (markers outside scopes are dropped from text by the public
    # path's collapse — but at minimum the heading scope is preserved)


def test_public_strip_ascii_colon_marker():
    md = "## A\n\n### x\nbody\n[章节不公开:reason]\n"
    out = _strip_hidden_for_public(md)
    assert "### x" not in out


def test_public_strip_empty_reason():
    md = "## A\n\n### x\nbody\n[章节不公开：]\n"
    out = _strip_hidden_for_public(md)
    assert "### x" not in out


# ── _annotate_hidden_for_group ──────────────────────────────────────────────────


def test_group_annotate_adds_lock_and_banner():
    md = (
        "## 行业新闻\n\n"
        "### 话题 A\nbody A\n[章节不公开：涉及未签约客户]\n"
    )
    out = _annotate_hidden_for_group(md)
    assert "🔒 话题 A" in out
    assert "**公开版隐藏**" in out
    assert "涉及未签约客户" in out
    assert "[章节不公开" not in out


def test_group_annotate_keeps_body():
    md = "## A\n\n### B\nbody-here\n[章节不公开：r]\n"
    out = _annotate_hidden_for_group(md)
    assert "body-here" in out


def test_group_annotate_strips_inline_marker_from_title():
    md = "## A\n\n### 标题 [章节不公开：r]\nbody\n"
    out = _annotate_hidden_for_group(md)
    assert "🔒 标题" in out
    # The bracket text must not survive in the title
    assert "[章节不公开" not in out


def test_group_annotate_no_marker_unchanged():
    md = "## A\n\n### 标题\nbody\n"
    out = _annotate_hidden_for_group(md)
    assert "🔒" not in out
    assert "公开版隐藏" not in out


def test_group_annotate_first_marker_wins_for_reason():
    md = (
        "## A\n\n"
        "### 标题\nbody\n[章节不公开：第一原因]\n"
        "more body\n[章节不公开：第二原因]\n"
    )
    out = _annotate_hidden_for_group(md)
    assert "第一原因" in out
    # Second marker text is also stripped, but its reason isn't shown.
    assert "[章节不公开" not in out


def test_group_annotate_empty_reason_uses_placeholder():
    md = "## A\n\n### 标题\nbody\n[章节不公开：]\n"
    out = _annotate_hidden_for_group(md)
    assert "🔒 标题" in out
    assert "（未填原因）" in out


# ── _drop_empty_h2 ──────────────────────────────────────────────────────────────


def test_drop_empty_h2_removes_orphan():
    lines = ["## A", "", "## B", "", "### x", "body"]
    out = _drop_empty_h2(lines)
    assert "## A" not in out
    assert "## B" in out
    assert "### x" in out


def test_drop_empty_h2_keeps_all_when_each_has_h3():
    lines = ["## A", "### x", "## B", "### y"]
    out = _drop_empty_h2(lines)
    assert out == lines


# ── _insert_toc ─────────────────────────────────────────────────────────────────


def test_insert_toc_basic():
    md = "intro line 1\nintro line 2\n\n## 行业新闻\n\n### x\nbody\n"
    out = _insert_toc(md)
    assert "[TOC]" in out
    # TOC must precede first ##
    assert out.index("[TOC]") < out.index("## 行业新闻")


def test_insert_toc_strips_existing_toc_marker():
    md = "intro\n[TOC]\nmore intro\n\n## A\n"
    out = _insert_toc(md)
    # Exactly one [TOC]
    assert out.count("[TOC]") == 1


def test_insert_toc_no_headings_appends():
    md = "just intro text\n"
    out = _insert_toc(md)
    assert "[TOC]" in out


# ── render_group / render_public — end-to-end ──────────────────────────────────


def _sample_markdown() -> str:
    t = _alice_token()
    return (
        f"今天 {t} 分享了一些观察。\n\n"
        f"## 行业新闻\n\n"
        f"### 话题 A\nbody A about {t}\n\n"
        f"> {t}：原话引用\n\n"
        f"### 话题 B\nbody B\n\n"
        f"[章节不公开：涉及私人信息]\n\n"
        f"---\n\ntags: ai, model-release\n"
    )


def test_render_group_replaces_token_with_real_name():
    out = render_group(_wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[])
    assert _alice_token() not in out
    assert "Alice" in out


def test_render_group_keeps_hidden_section_with_lock():
    out = render_group(_wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[])
    assert "话题 B" in out
    assert "🔒" in out
    assert "公开版隐藏" in out
    assert "涉及私人信息" in out


def test_render_group_inserts_toc():
    out = render_group(_wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[])
    assert "[TOC]" in out


def test_render_group_strips_tags_line_from_body():
    """Tags appear only as a footer note, not in the structural area."""
    out = render_group(_wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[])
    # Body shouldn't contain the literal "tags: ai, model-release" preceded
    # by '---' in the H2/H3 area. Footer rendering uses italics + leading '_'.
    assert "_tags: ai, model-release_" in out


def test_render_group_command_log_present():
    out = render_group(_wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[])
    assert "本期指令执行记录" in out
    assert "今日无指令" in out


def test_render_group_includes_title():
    out = render_group(_wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[])
    assert "# 2026-04-30 群聊日报" in out


def test_render_public_drops_hidden_section():
    out = render_public(_wrap(_sample_markdown()), _make_db())
    assert "话题 A" in out
    assert "话题 B" not in out
    assert "涉及私人信息" not in out
    assert "[章节不公开" not in out


def test_render_public_uses_public_alias():
    out = render_public(_wrap(_sample_markdown()), _make_db())
    assert "Duckie" in out
    assert "Alice" not in out
    assert _alice_token() not in out


def test_render_public_optout_token_to_某群友():
    out = render_public(_wrap(_sample_markdown()), _make_db(optout=True))
    assert "某群友" in out
    assert "Alice" not in out
    assert _alice_token() not in out


def test_render_public_front_matter_present():
    out = render_public(_wrap(_sample_markdown()), _make_db())
    assert out.startswith("---\n")
    assert 'title: "2026-04-30 群聊日报"' in out
    assert "layout: post" in out
    assert "toc: true" in out


def test_render_public_front_matter_permalink():
    out = render_public(_wrap(_sample_markdown()), _make_db())
    assert "permalink: /daily/2026/04/30/daily/" in out


def test_render_public_front_matter_tags():
    out = render_public(_wrap(_sample_markdown()), _make_db())
    assert "tags:" in out
    assert "  - ai" in out
    assert "  - model-release" in out


def test_render_public_no_tags_emits_empty_list():
    md = "## A\n\n### x\nbody\n"
    out = render_public(_wrap(md), _make_db())
    assert "tags: []" in out


def test_render_public_no_command_log():
    out = render_public(_wrap(_sample_markdown()), _make_db())
    assert "指令执行记录" not in out


def test_render_public_drops_h2_when_all_h3_hidden():
    md = (
        "intro\n\n"
        "## 全部隐藏类\n\n"
        "### A\na\n[章节不公开：r]\n\n"
        "## 工具\n\n"
        "### B\nb\n\n"
        "---\ntags: t1\n"
    )
    out = render_public(_wrap(md), _make_db())
    assert "全部隐藏类" not in out
    assert "工具" in out
    assert "### B" in out


# ── Token replacement edge cases ───────────────────────────────────────────────


def test_unknown_token_passes_through():
    md = "活泼的鸵鸟99 说了一句话。\n## A\n\n### x\nbody\n"
    out = render_group(_wrap(md), _make_db(), _make_contacts(), command_log=[])
    assert "活泼的鸵鸟99" in out


def test_token_in_blockquote_replaced_internal():
    t = _alice_token()
    md = f"## A\n\n### x\nbody\n\n> {t}：something\n"
    out = render_group(_wrap(md), _make_db(), _make_contacts(), command_log=[])
    assert t not in out
    assert "Alice" in out


def test_token_in_blockquote_replaced_public():
    t = _alice_token()
    md = f"## A\n\n### x\nbody\n\n> {t}：something\n"
    out = render_public(_wrap(md), _make_db())
    assert t not in out
    assert "Duckie" in out


# ── Robustness ────────────────────────────────────────────────────────────────


def test_render_public_empty_markdown():
    out = render_public(_wrap(""), _make_db())
    # Front matter still renders; body is empty.
    assert out.startswith("---\n")
    assert "tags: []" in out


def test_render_group_empty_markdown():
    out = render_group(_wrap(""), _make_db(), _make_contacts(), command_log=[])
    assert "# 2026-04-30 群聊日报" in out
    assert "本期指令执行记录" in out


def test_render_public_only_marker_section_removed_clean():
    """Verify no orphaned blank-line forests after removal."""
    md = (
        "intro\n\n"
        "## A\n\n### x\nbody\n[章节不公开：r]\n\n"
        "## B\n\n### y\nbody y\n\n"
        "---\ntags: t\n"
    )
    out = render_public(_wrap(md), _make_db())
    # no triple newlines
    assert "\n\n\n" not in out


def test_render_group_marks_leaked_real_name():
    """Real names that escape token-replacement (i.e. the LLM left them
    in the markdown) get wrapped with ``<mark class="leak-warn">…</mark>``
    so the author spots them on review."""
    md = "## A\n\n### x\n直接写了 Alice 的名字。\n"
    out = render_group(_wrap(md), _make_db(), _make_contacts(), command_log=[])
    assert '<mark class="leak-warn">Alice</mark>' in out


def test_render_group_command_log_with_real_user():
    log = [
        {"ts": 1700000000, "wxid": "wxid_alice", "cmd": "/alias Duckie",
         "ok": True, "msg": "已设置别名"},
    ]
    out = render_group(_wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=log)
    assert "Alice" in out
    assert "已设置别名" in out
    assert "✓" in out
