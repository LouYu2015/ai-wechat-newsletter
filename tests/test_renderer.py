"""Tests for renderer.py — markdown processing, hide-marker handling, token replacement."""

from __future__ import annotations

from wechat_daily import aliases, contacts, models, renderer

SALT = b"\x00" * 32


# ── Fixtures ────────────────────────────────────────────────────────────────────


def _make_db(*, optout: bool = False) -> aliases.AliasDB:
    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db.apply_command("wxid_alice", "/alias Duckie", 1000)
    if optout:
        db._users["wxid_alice"]["optout"] = True
    return db


def _make_contacts() -> contacts.ContactMap:
    return contacts.ContactMap.from_dict({"wxid_alice": "Alice"})


def _alice_token() -> str:
    return aliases.compute_default_anon("wxid_alice", SALT)


def _wrap(md: str) -> models.DailyReport:
    return models.DailyReport(date="2026-04-30", markdown=md)


# ── _strip_trailing_tags ────────────────────────────────────────────────────────


def test_tags_simple():
    body, tags = renderer._strip_trailing_tags("hello\n\n---\n\ntags: a, b, c\n")
    assert body == "hello"
    assert tags == ["a", "b", "c"]


def test_tags_no_separator_still_extracted():
    body, tags = renderer._strip_trailing_tags("hello\ntags: a, b\n")
    assert body == "hello"
    assert tags == ["a", "b"]


def test_tags_extra_blank_lines_around():
    body, tags = renderer._strip_trailing_tags("hello\n\n\n---\n\n\ntags: a\n\n\n")
    assert body == "hello"
    assert tags == ["a"]


def test_tags_missing_returns_empty_list():
    body, tags = renderer._strip_trailing_tags("hello\nworld\n")
    assert "hello" in body
    assert tags == []


def test_tags_whitespace_only_entries_dropped():
    body, tags = renderer._strip_trailing_tags("x\ntags: a, ,  b , ,\n")
    assert tags == ["a", "b"]


def test_tags_empty_value():
    _, tags = renderer._strip_trailing_tags("x\ntags:\n")
    assert tags == []


def test_tags_normalize_dots_and_dedupe():
    """Jekyll slugifies ``.`` to ``-``; merge variants so URLs don't collide."""
    _, tags = renderer._strip_trailing_tags("x\ntags: GPT-5.5, gpt-5-5, gpt 5.5, foo_bar\n")
    assert tags == ["gpt-5-5", "foo-bar"]


def test_tags_inline_dashes_in_body_preserved():
    """A '---' inside the body is not the separator — only the one right above tags."""
    body, tags = renderer._strip_trailing_tags("intro\n\n---\n\nmiddle\n\n---\n\ntags: x\n")
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
    out = renderer._strip_hidden_for_public(md)
    assert "话题 A" in out
    assert "话题 B" not in out
    assert "body B" not in out
    assert "[章节不公开" not in out
    assert "话题 C" in out


def test_public_strip_marker_in_title_line():
    md = "## 行业新闻\n\n### 话题 A [章节不公开：原因]\nbody A\n\n### 话题 B\nbody B\n"
    out = renderer._strip_hidden_for_public(md)
    assert "话题 A" not in out
    assert "body A" not in out
    assert "话题 B" in out


def test_public_strip_marker_in_paragraph():
    md = (
        "## 行业新闻\n\n"
        "### 话题 A\nbody A line 1\n[章节不公开：mid]\nbody A line 2\n\n"
        "### 话题 B\nbody B\n"
    )
    out = renderer._strip_hidden_for_public(md)
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
    out = renderer._strip_hidden_for_public(md)
    assert "行业新闻" not in out
    assert "工具" in out
    assert "### C" in out


def test_public_strip_keeps_h2_with_surviving_h3():
    md = "## 行业新闻\n\n### A\na\n[章节不公开：x]\n\n### B\nb\n"
    out = renderer._strip_hidden_for_public(md)
    assert "行业新闻" in out
    assert "### B" in out
    assert "### A" not in out


def test_public_strip_hides_whole_h2_when_marker_on_h2_line():
    md = "## 行业新闻 [章节不公开：整组敏感]\n\n### A\na\n\n### B\nb\n\n## 工具\n\n### C\nc\n"
    out = renderer._strip_hidden_for_public(md)
    assert "行业新闻" not in out
    assert "### A" not in out
    assert "### B" not in out
    assert "工具" in out
    assert "### C" in out


def test_public_strip_no_markers_idempotent():
    md = "## A\n\n### x\ntext\n"
    out = renderer._strip_hidden_for_public(md)
    assert "## A" in out
    assert "### x" in out
    assert "text" in out


def test_public_strip_marker_outside_any_heading_is_ignored():
    """A marker in pre-heading intro must not silently nuke anything."""
    md = "intro line\n[章节不公开：rogue]\n\n## A\n\n### x\ntext\n"
    out = renderer._strip_hidden_for_public(md)
    assert "## A" in out
    assert "### x" in out
    # intro stays (markers outside scopes are dropped from text by the public
    # path's collapse — but at minimum the heading scope is preserved)


def test_public_strip_ascii_colon_marker():
    md = "## A\n\n### x\nbody\n[章节不公开:reason]\n"
    out = renderer._strip_hidden_for_public(md)
    assert "### x" not in out


def test_public_strip_empty_reason():
    md = "## A\n\n### x\nbody\n[章节不公开：]\n"
    out = renderer._strip_hidden_for_public(md)
    assert "### x" not in out


# ── _annotate_hidden_for_group ──────────────────────────────────────────────────


def test_group_annotate_adds_lock_and_banner():
    md = "## 行业新闻\n\n### 话题 A\nbody A\n[章节不公开：涉及未签约客户]\n"
    out = renderer._annotate_hidden_for_group(md)
    assert "🔒 话题 A" in out
    assert "**公开版隐藏**" in out
    assert "涉及未签约客户" in out
    assert "[章节不公开" not in out


def test_group_annotate_keeps_body():
    md = "## A\n\n### B\nbody-here\n[章节不公开：r]\n"
    out = renderer._annotate_hidden_for_group(md)
    assert "body-here" in out


def test_group_annotate_strips_inline_marker_from_title():
    md = "## A\n\n### 标题 [章节不公开：r]\nbody\n"
    out = renderer._annotate_hidden_for_group(md)
    assert "🔒 标题" in out
    # The bracket text must not survive in the title
    assert "[章节不公开" not in out


def test_group_annotate_no_marker_unchanged():
    md = "## A\n\n### 标题\nbody\n"
    out = renderer._annotate_hidden_for_group(md)
    assert "🔒" not in out
    assert "公开版隐藏" not in out


def test_group_annotate_first_marker_wins_for_reason():
    md = "## A\n\n### 标题\nbody\n[章节不公开：第一原因]\nmore body\n[章节不公开：第二原因]\n"
    out = renderer._annotate_hidden_for_group(md)
    assert "第一原因" in out
    # Second marker text is also stripped, but its reason isn't shown.
    assert "[章节不公开" not in out


def test_group_annotate_empty_reason_uses_placeholder():
    md = "## A\n\n### 标题\nbody\n[章节不公开：]\n"
    out = renderer._annotate_hidden_for_group(md)
    assert "🔒 标题" in out
    assert "（未填原因）" in out


# ── _drop_empty_h2 ──────────────────────────────────────────────────────────────


def test_drop_empty_h2_removes_orphan():
    lines = ["## A", "", "## B", "", "### x", "body"]
    out = renderer._drop_empty_h2(lines)
    assert "## A" not in out
    assert "## B" in out
    assert "### x" in out


def test_drop_empty_h2_keeps_all_when_each_has_h3():
    lines = ["## A", "### x", "## B", "### y"]
    out = renderer._drop_empty_h2(lines)
    assert out == lines


# ── _insert_toc ─────────────────────────────────────────────────────────────────


def test_insert_toc_basic():
    md = "intro line 1\nintro line 2\n\n## 行业新闻\n\n### x\nbody\n"
    out = renderer._insert_toc(md)
    assert "[TOC]" in out
    # TOC must precede first ##
    assert out.index("[TOC]") < out.index("## 行业新闻")


def test_insert_toc_strips_existing_toc_marker():
    md = "intro\n[TOC]\nmore intro\n\n## A\n"
    out = renderer._insert_toc(md)
    # Exactly one [TOC]
    assert out.count("[TOC]") == 1


def test_insert_toc_no_headings_appends():
    md = "just intro text\n"
    out = renderer._insert_toc(md)
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
    out = renderer.render_group(
        _wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[]
    )
    assert _alice_token() not in out
    assert "Alice" in out


def test_render_group_keeps_hidden_section_with_lock():
    out = renderer.render_group(
        _wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[]
    )
    assert "话题 B" in out
    assert "🔒" in out
    assert "公开版隐藏" in out
    assert "涉及私人信息" in out


def test_render_group_inserts_toc():
    out = renderer.render_group(
        _wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[]
    )
    assert "[TOC]" in out


def test_render_group_strips_tags_line_from_body():
    """Tags appear only as a footer note, not in the structural area."""
    out = renderer.render_group(
        _wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[]
    )
    # Body shouldn't contain the literal "tags: ai, model-release" preceded
    # by '---' in the H2/H3 area. Footer rendering uses italics + leading '_'.
    assert "_tags: ai, model-release_" in out


def test_render_group_command_log_present():
    out = renderer.render_group(
        _wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[]
    )
    assert "本期指令执行记录" in out
    assert "今日无指令" in out


def test_render_group_includes_title():
    out = renderer.render_group(
        _wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=[]
    )
    assert "# 2026-04-30 群聊日报" in out


def test_render_public_drops_hidden_section():
    out = renderer.render_public(_wrap(_sample_markdown()), _make_db())
    assert "话题 A" in out
    assert "话题 B" not in out
    assert "涉及私人信息" not in out
    assert "[章节不公开" not in out


def test_render_public_uses_public_alias():
    out = renderer.render_public(_wrap(_sample_markdown()), _make_db())
    assert "Duckie" in out
    assert "Alice" not in out
    assert _alice_token() not in out


def test_render_public_optout_token_to_某群友():
    out = renderer.render_public(_wrap(_sample_markdown()), _make_db(optout=True))
    assert "某群友" in out
    assert "Alice" not in out
    assert _alice_token() not in out


def test_render_public_front_matter_present():
    out = renderer.render_public(_wrap(_sample_markdown()), _make_db())
    assert out.startswith("---\n")
    assert 'title: "2026-04-30 群聊日报"' in out
    assert "layout: post" in out
    assert "toc: true" in out


def test_render_public_front_matter_permalink():
    out = renderer.render_public(_wrap(_sample_markdown()), _make_db())
    assert "permalink: /daily/2026/04/30/daily/" in out


def test_render_public_front_matter_tags():
    out = renderer.render_public(_wrap(_sample_markdown()), _make_db())
    assert "tags:" in out
    assert "  - ai" in out
    assert "  - model-release" in out


def test_render_public_no_tags_emits_empty_list():
    md = "## A\n\n### x\nbody\n"
    out = renderer.render_public(_wrap(md), _make_db())
    assert "tags: []" in out


def test_render_public_no_command_log():
    out = renderer.render_public(_wrap(_sample_markdown()), _make_db())
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
    out = renderer.render_public(_wrap(md), _make_db())
    assert "全部隐藏类" not in out
    assert "工具" in out
    assert "### B" in out


# ── Token replacement edge cases ───────────────────────────────────────────────


def test_unknown_token_passes_through():
    md = "活泼的鸵鸟99 说了一句话。\n## A\n\n### x\nbody\n"
    out = renderer.render_group(_wrap(md), _make_db(), _make_contacts(), command_log=[])
    assert "活泼的鸵鸟99" in out


def test_token_in_blockquote_replaced_internal():
    t = _alice_token()
    md = f"## A\n\n### x\nbody\n\n> {t}：something\n"
    out = renderer.render_group(_wrap(md), _make_db(), _make_contacts(), command_log=[])
    assert t not in out
    assert "Alice" in out


def test_token_in_blockquote_replaced_public():
    t = _alice_token()
    md = f"## A\n\n### x\nbody\n\n> {t}：something\n"
    out = renderer.render_public(_wrap(md), _make_db())
    assert t not in out
    assert "Duckie" in out


# ── Robustness ────────────────────────────────────────────────────────────────


def test_render_public_empty_markdown():
    out = renderer.render_public(_wrap(""), _make_db())
    # Front matter still renders; body is empty.
    assert out.startswith("---\n")
    assert "tags: []" in out


def test_render_group_empty_markdown():
    out = renderer.render_group(_wrap(""), _make_db(), _make_contacts(), command_log=[])
    assert "# 2026-04-30 群聊日报" in out
    assert "本期指令执行记录" in out


def test_render_public_only_marker_section_removed_clean():
    """Verify no orphaned blank-line forests after removal."""
    md = "intro\n\n## A\n\n### x\nbody\n[章节不公开：r]\n\n## B\n\n### y\nbody y\n\n---\ntags: t\n"
    out = renderer.render_public(_wrap(md), _make_db())
    # no triple newlines
    assert "\n\n\n" not in out


def test_render_group_command_log_with_real_user():
    log = [
        {
            "ts": 1700000000,
            "wxid": "wxid_alice",
            "cmd": "/alias Duckie",
            "ok": True,
            "msg": "已设置别名",
        },
    ]
    out = renderer.render_group(
        _wrap(_sample_markdown()), _make_db(), _make_contacts(), command_log=log
    )
    assert "Alice" in out
    assert "已设置别名" in out
    assert "✓" in out


# ── Cross-day reference placeholders ───────────────────────────────────────────


def test_slugify_basic_ascii():
    assert renderer._slugify_heading("Hello World") == "hello-world"


def test_slugify_strips_punctuation():
    assert renderer._slugify_heading("Claude Opus 4.7 发布!") == "claude-opus-47-发布"


def test_slugify_preserves_repeated_hyphens():
    # kramdown-parser-gfm turns each space into its own hyphen and never
    # collapses runs of hyphens, so we must not collapse either.
    assert renderer._slugify_heading("  multi   space  ") == "multi---space"


def test_slugify_inline_code_keeps_double_hyphen():
    # An inline-code segment like `-p` surrounded by spaces yields a literal
    # double hyphen in the rendered HTML id; the anchor must match it exactly.
    assert (
        renderer._slugify_heading("Claude `-p` 禁令与远程控制困局")
        == "claude--p-禁令与远程控制困局"
    )


def test_slugify_strips_chinese_punctuation():
    assert renderer._slugify_heading("话题：副标题") == "话题副标题"


def test_expand_refs_group_keeps_only_title():
    """Group/PDF version drops the date — model writes natural-language date in prose."""
    text = "昨天 [[ref:2026-05-09|Claude Opus 4.7 发布]] 已经写过要点。"
    assert renderer._expand_refs_group(text) == "昨天 「Claude Opus 4.7 发布」 已经写过要点。"


def test_expand_refs_group_handles_multiple():
    text = "[[ref:2026-05-08|话题 A]] 与 [[ref:2026-05-09|话题 B]]"
    assert renderer._expand_refs_group(text) == "「话题 A」 与 「话题 B」"


def test_expand_refs_group_no_match_unchanged():
    text = "no refs here"
    assert renderer._expand_refs_group(text) == text


def test_expand_refs_public_with_existing_post(tmp_path):
    """When the target post exists and contains the heading, emit a link."""
    posts = tmp_path / "_posts" / "2026" / "05"
    posts.mkdir(parents=True)
    (posts / "2026-05-09-daily.md").write_text(
        "## 行业新闻\n\n### Claude Opus 4.7 发布\nbody\n", encoding="utf-8"
    )

    text = "上一期写过 [[ref:2026-05-09|Claude Opus 4.7 发布]] 那一节。"
    out = renderer._expand_refs_public(text, posts_dir=tmp_path / "_posts")
    assert "[「Claude Opus 4.7 发布」]" in out
    # URL must be wrapped in Jekyll's relative_url filter so it picks up the
    # site baseurl (/AI-chatgroup-daily); bare /daily/... 404s on deployment.
    assert "({{ '/daily/2026/05/09/daily/" in out
    assert "| relative_url }})" in out
    assert "claude-opus-47-发布" in out


def test_expand_refs_public_missing_post_degrades_to_plain_text(tmp_path):
    """When the target post hasn't been published, drop the URL — no broken links."""
    text = "之前 [[ref:2026-05-09|某话题]] 提过"
    out = renderer._expand_refs_public(text, posts_dir=tmp_path / "_posts")
    assert out == "之前 「某话题」 提过"


def test_expand_refs_public_heading_not_in_post_degrades(tmp_path, capsys):
    """When the post exists but lacks the heading, drop URL and warn.

    Catches the LLM mistake of attributing a section to the wrong date — the
    file exists so the file-level check passes, but the hash would 404.
    """
    posts = tmp_path / "_posts" / "2026" / "05"
    posts.mkdir(parents=True)
    (posts / "2026-05-10-daily.md").write_text(
        "## 行业新闻\n\n### 别的话题\nbody\n", encoding="utf-8"
    )

    text = "前天 [[ref:2026-05-10|Multi-agent 通过 handoff file 通信]] 提到过"
    out = renderer._expand_refs_public(text, posts_dir=tmp_path / "_posts")
    assert "「Multi-agent 通过 handoff file 通信」" in out
    assert "/daily/2026/05/10/" not in out  # no URL emitted
    captured = capsys.readouterr()
    assert "not found in target post" in captured.err


def test_expand_refs_public_handles_multiple_mixed_existence(tmp_path):
    posts = tmp_path / "_posts" / "2026" / "05"
    posts.mkdir(parents=True)
    (posts / "2026-05-09-daily.md").write_text("### 新话题\nbody\n", encoding="utf-8")
    # 2026-05-08 absent

    text = "[[ref:2026-05-08|旧话题]] 与 [[ref:2026-05-09|新话题]]"
    out = renderer._expand_refs_public(text, posts_dir=tmp_path / "_posts")
    assert "「旧话题」" in out
    assert "/daily/2026/05/08/" not in out  # missing → plain, no URL at all
    assert "[「新话题」]" in out
    assert "({{ '/daily/2026/05/09/daily/" in out


def test_expand_refs_public_no_match_unchanged(tmp_path):
    text = "plain text"
    assert renderer._expand_refs_public(text, posts_dir=tmp_path) == text


def test_validate_final_refs_public_keeps_valid_link(tmp_path):
    """Final-form link with a real heading slug passes through unchanged."""
    posts = tmp_path / "_posts" / "2026" / "05"
    posts.mkdir(parents=True)
    (posts / "2026-05-11-daily.md").write_text(
        "## 行业新闻\n\n### AI 正在吞掉哪些 SaaS\nbody\n", encoding="utf-8"
    )

    text = (
        "前天 [「AI 正在吞掉哪些 SaaS」]"
        "({{ '/daily/2026/05/11/daily/#ai-正在吞掉哪些-saas' | relative_url }}) 说了"
    )
    out = renderer._validate_final_refs_public(text, posts_dir=tmp_path / "_posts")
    assert out == text


def test_validate_final_refs_public_wrong_date_degrades(tmp_path, capsys):
    """LLM bypassed the placeholder and wrote the final link with the wrong date.

    This is the exact 2026-05-13 bug: target post exists but doesn't contain
    the heading slug. b622e1487b52 only validates `[[ref:…]]` placeholders,
    so this final-form path needs its own check.
    """
    posts = tmp_path / "_posts" / "2026" / "05"
    posts.mkdir(parents=True)
    # 05-12 exists but never had the SaaS heading (it's in 05-11, not here).
    (posts / "2026-05-12-daily.md").write_text(
        "## 方法论\n\n### 别的话题\nbody\n", encoding="utf-8"
    )

    text = (
        "前天 [「AI 正在吞掉哪些 SaaS」]"
        "({{ '/daily/2026/05/12/daily/#ai-正在吞掉哪些-saas' | relative_url }}) 说了"
    )
    out = renderer._validate_final_refs_public(text, posts_dir=tmp_path / "_posts")
    assert out == "前天 「AI 正在吞掉哪些 SaaS」 说了"
    captured = capsys.readouterr()
    assert "not found in target post" in captured.err
    assert "ai-正在吞掉哪些-saas" in captured.err


def test_validate_final_refs_public_missing_post_degrades(tmp_path, capsys):
    """Target post file doesn't exist at all — drop URL, warn."""
    text = "上周 [「某话题」]({{ '/daily/2026/05/01/daily/#某话题' | relative_url }}) 提到过"
    out = renderer._validate_final_refs_public(text, posts_dir=tmp_path / "_posts")
    assert out == "上周 「某话题」 提到过"
    captured = capsys.readouterr()
    assert "target post missing" in captured.err


def test_validate_final_refs_public_no_match_unchanged(tmp_path):
    """Text without final-form links passes through untouched."""
    text = "plain text with no daily links"
    assert renderer._validate_final_refs_public(text, posts_dir=tmp_path / "_posts") == text


def test_render_public_catches_inline_final_form_wrong_date(monkeypatch, tmp_path):
    """End-to-end: render_public must drop the URL when the LLM writes the
    final-form link directly (no `[[ref:…]]`) with a heading that doesn't
    live at the named date — even though `_expand_refs_public` never sees it.
    """
    posts = tmp_path / "_posts" / "2026" / "05"
    posts.mkdir(parents=True)
    # 05-12 exists, but the SaaS section actually lives in 05-11 (not created here).
    (posts / "2026-05-12-daily.md").write_text(
        "## 方法论\n\n### 别的话题\nbody\n", encoding="utf-8"
    )

    monkeypatch.setattr("wechat_daily.config.PUBLIC_REPO_DIR", tmp_path)

    md = (
        "intro 前天 [「AI 正在吞掉哪些 SaaS」]"
        "({{ '/daily/2026/05/12/daily/#ai-正在吞掉哪些-saas' | relative_url }}) 说\n\n"
        "## A\n\n### x\nbody\n"
    )
    out = renderer.render_public(_wrap(md), _make_db())
    assert "「AI 正在吞掉哪些 SaaS」" in out
    assert "/daily/2026/05/12/" not in out


def test_render_group_expands_ref_placeholder():
    md = "intro 昨天 [[ref:2026-05-09|某话题]] 已经写过\n\n## A\n\n### x\nbody\n"
    out = renderer.render_group(_wrap(md), _make_db(), _make_contacts(), command_log=[])
    assert "「某话题」" in out
    assert "[[ref:" not in out


def test_render_public_expands_ref_placeholder_degraded(monkeypatch, tmp_path):
    """In tests there's no published post; expansion should degrade to plain text."""
    monkeypatch.setattr("wechat_daily.config.PUBLIC_REPO_DIR", tmp_path)

    md = "intro 昨天 [[ref:2026-05-09|某话题]] 已经写过\n\n## A\n\n### x\nbody\n"
    out = renderer.render_public(_wrap(md), _make_db())
    assert "「某话题」" in out
    assert "[[ref:" not in out
    # No URL emitted because target post doesn't exist in tmp_path
    assert "/daily/2026/05/09/" not in out


def test_render_public_expands_ref_to_link_when_post_exists(monkeypatch, tmp_path):
    posts = tmp_path / "_posts" / "2026" / "05"
    posts.mkdir(parents=True)
    (posts / "2026-05-09-daily.md").write_text("### 某话题\nbody\n", encoding="utf-8")

    monkeypatch.setattr("wechat_daily.config.PUBLIC_REPO_DIR", tmp_path)

    md = "intro 昨天 [[ref:2026-05-09|某话题]] 已经写过\n\n## A\n\n### x\nbody\n"
    out = renderer.render_public(_wrap(md), _make_db())
    assert "[「某话题」]({{ '/daily/2026/05/09/daily/#某话题' | relative_url }})" in out


# ── URL tracking-param cleaning ────────────────────────────────────────────────


def test_clean_url_weixin_strips_share_tail_keeps_reading_params():
    """公众号 link: drop mpshare/scene/srcid/sharer_shareinfo*, keep __biz/mid/idx/sn/chksm."""
    url = (
        "https://mp.weixin.qq.com/s?__biz=MzIyMzA5NjEyMA==&mid=2647684233&idx=1"
        "&sn=391c84ef158f723c83108c29d8e2d666"
        "&chksm=f1ad35033b666b99941064ecaea284a87daf08c949c9f1e81de8fe49531abae8"
        "&mpshare=1&scene=1&srcid=0713RGLlyOjOmY2kzYhgqbAa"
        "&sharer_shareinfo=73acbae4a8efb9178cc64ea88d52a5df"
        "&sharer_shareinfo_first=73acbae4a8efb9178cc64ea88d52a5df#rd"
    )
    out = renderer._clean_url(url)
    assert out == (
        "https://mp.weixin.qq.com/s?__biz=MzIyMzA5NjEyMA==&mid=2647684233&idx=1"
        "&sn=391c84ef158f723c83108c29d8e2d666"
        "&chksm=f1ad35033b666b99941064ecaea284a87daf08c949c9f1e81de8fe49531abae8#rd"
    )
    for gone in ("mpshare", "scene=", "srcid", "sharer_shareinfo"):
        assert gone not in out
    for kept in ("__biz", "mid=", "idx=", "sn=", "chksm="):
        assert kept in out


def test_clean_url_weixin_keeps_unknown_param():
    """Params not on the strip list survive (e.g. xtrack); scene still goes."""
    url = "https://mp.weixin.qq.com/s?__biz=A&sn=B&scene=0&xtrack=1#rd"
    out = renderer._clean_url(url)
    assert out == "https://mp.weixin.qq.com/s?__biz=A&sn=B&xtrack=1#rd"


def test_clean_url_generic_utm_stripped_on_any_host():
    url = "https://example.com/a?x=1&utm_source=news&utm_medium=email&y=2"
    assert renderer._clean_url(url) == "https://example.com/a?x=1&y=2"


def test_clean_url_zhihu_malformed_fragment_query():
    """知乎 share tail: query rides inside the fragment after '#'."""
    url = (
        "https://zhuanlan.zhihu.com/p/2059827535890878759#showWechatShareTip"
        "?utm_source=wechat_session&utm_medium=social&wechatShare=1&s_r=0"
    )
    out = renderer._clean_url(url)
    assert out == "https://zhuanlan.zhihu.com/p/2059827535890878759#showWechatShareTip"


def test_clean_url_bilibili_strips_share_params():
    url = (
        "https://www.bilibili.com/video/BV1UoNn6pExS/"
        "?share_source=copy_web&vd_source=5844e923d38f06c074049013c344b1a7"
    )
    assert renderer._clean_url(url) == "https://www.bilibili.com/video/BV1UoNn6pExS/"


def test_clean_url_empty_query_drops_dangling_question_mark():
    url = "https://www.bilibili.com/video/BV1x/?share_source=copy_web"
    out = renderer._clean_url(url)
    assert "?" not in out
    assert out == "https://www.bilibili.com/video/BV1x/"


def test_clean_url_no_tracking_unchanged():
    url = "https://mp.weixin.qq.com/s/2GhWYcLU1U4QrNHB1j2VSw"
    assert renderer._clean_url(url) == url


def test_clean_tracking_params_in_markdown_link():
    md = (
        "分享[《标题》](https://mp.weixin.qq.com/s?__biz=A&sn=B&mpshare=1&scene=1"
        "&srcid=0713x&sharer_shareinfo=abc#rd)，配评。"
    )
    out = renderer._clean_tracking_params(md)
    assert "[《标题》](https://mp.weixin.qq.com/s?__biz=A&sn=B#rd)" in out
    assert "mpshare" not in out
    assert "sharer_shareinfo" not in out


def test_clean_tracking_params_skips_fenced_code():
    md = "```\ncurl 'https://mp.weixin.qq.com/s?__biz=A&mpshare=1&scene=1'\n```\n"
    out = renderer._clean_tracking_params(md)
    assert "mpshare=1&scene=1" in out  # untouched inside the fence


def test_clean_tracking_params_skips_inline_code():
    md = "看 `https://x.com/a?utm_source=z` 这个例子"
    out = renderer._clean_tracking_params(md)
    assert "utm_source=z" in out  # inline code preserved verbatim


def test_render_group_cleans_tracking_params():
    url = "https://mp.weixin.qq.com/s?__biz=A&sn=B&mpshare=1&scene=1&sharer_shareinfo=abc#rd"
    md = f"## A\n\n### x\n分享[《标题》]({url})。\n"
    out = renderer.render_group(_wrap(md), _make_db(), _make_contacts(), command_log=[])
    assert "mpshare" not in out
    assert "sharer_shareinfo" not in out
    assert "__biz=A&sn=B#rd" in out


def test_render_public_cleans_tracking_params():
    url = (
        "https://zhuanlan.zhihu.com/p/123#showWechatShareTip"
        "?utm_source=wechat_session&utm_medium=social"
    )
    md = f"## A\n\n### x\n知乎长文[《标题》]({url})。\n"
    out = renderer.render_public(_wrap(md), _make_db())
    assert "utm_source" not in out
    assert "[《标题》](https://zhuanlan.zhihu.com/p/123#showWechatShareTip)" in out
