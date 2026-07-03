"""Tests for prior_report.py — disk loader + XML block formatter."""

from __future__ import annotations

from wechat_daily.prior_report import (
    _normalize_to_ref_placeholders,
    expected_dates,
    extract_titles_outline,
    format_prior_report_titles_block,
    format_prior_reports_block,
    load_prior_report_titles,
    load_prior_reports,
    missing_prior_dates,
)

# ── expected_dates ─────────────────────────────────────────────────────────────


def test_expected_dates_three_days():
    assert expected_dates("2026-05-10", 3) == [
        "2026-05-07", "2026-05-08", "2026-05-09",
    ]


def test_expected_dates_one_day():
    assert expected_dates("2026-05-10", 1) == ["2026-05-09"]


def test_expected_dates_zero_returns_empty():
    assert expected_dates("2026-05-10", 0) == []


def test_expected_dates_crosses_month_boundary():
    assert expected_dates("2026-05-01", 3) == [
        "2026-04-28", "2026-04-29", "2026-04-30",
    ]


# ── load_prior_reports ─────────────────────────────────────────────────────────


def _write_extract(debug_dir, date_str: str, content: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"extract-{date_str}.md").write_text(content, encoding="utf-8")


def test_load_returns_existing_in_ascending_order(tmp_path):
    debug = tmp_path / "debug"
    _write_extract(debug, "2026-05-08", "day 8")
    _write_extract(debug, "2026-05-09", "day 9")

    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == [("2026-05-08", "day 8"), ("2026-05-09", "day 9")]


def test_load_skips_missing(tmp_path):
    debug = tmp_path / "debug"
    _write_extract(debug, "2026-05-09", "only yesterday")

    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == [("2026-05-09", "only yesterday")]


def test_load_returns_empty_when_no_dir(tmp_path):
    debug = tmp_path / "no-such-dir"
    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == []


def test_load_skips_empty_files(tmp_path):
    debug = tmp_path / "debug"
    _write_extract(debug, "2026-05-08", "")  # empty
    _write_extract(debug, "2026-05-09", "   \n  ")  # whitespace only

    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == []


def test_load_rewrites_expanded_refs_back_to_placeholders(tmp_path):
    """Final-form ``[「title」]({{ '…' | relative_url }})`` from a previous
    LLM run gets re-folded into ``[[ref:…]]`` before being fed back as
    ``<previous_reports>``. Otherwise the model copies the expanded shape
    and bypasses today's placeholder validation.
    """
    debug = tmp_path / "debug"
    _write_extract(
        debug,
        "2026-05-09",
        "intro [「话题 A」]({{ '/daily/2026/05/07/daily/#话题-a' | relative_url }}) 提过\n",
    )

    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == [("2026-05-09", "intro [[ref:2026-05-07|话题 A]] 提过\n")]


# ── missing_prior_dates ────────────────────────────────────────────────────────


def test_missing_dates_all_present(tmp_path):
    debug = tmp_path / "debug"
    for d in ["2026-05-07", "2026-05-08", "2026-05-09"]:
        _write_extract(debug, d, "x")
    assert missing_prior_dates("2026-05-10", 3, debug) == []


def test_missing_dates_partial(tmp_path):
    debug = tmp_path / "debug"
    _write_extract(debug, "2026-05-09", "x")
    assert missing_prior_dates("2026-05-10", 3, debug) == [
        "2026-05-07", "2026-05-08",
    ]


def test_missing_dates_all_absent(tmp_path):
    assert missing_prior_dates("2026-05-10", 3, tmp_path / "nope") == [
        "2026-05-07", "2026-05-08", "2026-05-09",
    ]


# ── format_prior_reports_block ─────────────────────────────────────────────────


def test_format_empty_returns_empty_string():
    assert format_prior_reports_block([]) == ""


def test_format_wraps_each_report():
    block = format_prior_reports_block([
        ("2026-05-08", "## A\nbody A\n"),
        ("2026-05-09", "## B\nbody B"),
    ])
    assert block.startswith("<previous_reports>\n")
    assert block.rstrip("\n").endswith("</previous_reports>")
    assert '<report date="2026-05-08">' in block
    assert '<report date="2026-05-09">' in block
    assert "body A" in block
    assert "body B" in block
    # Both <report> tags closed
    assert block.count("</report>") == 2


def test_format_strips_trailing_newlines_within_reports():
    """Report bodies shouldn't have unbounded trailing whitespace bleed into XML."""
    block = format_prior_reports_block([("2026-05-09", "x\n\n\n")])
    # No "x\n\n\n</report>" — trailing \n stripped before close tag.
    assert "x\n</report>" in block


# ── extract_titles_outline ─────────────────────────────────────────────────────


def test_extract_titles_preserves_h2_h3_hierarchy_in_order():
    md = (
        "intro paragraph\n\n"
        "## 行业新闻\n"
        "blurb\n\n"
        "### Claude 4.7 发布\n"
        "body body body\n"
        "> 沉稳的大象：略\n\n"
        "### Anthropic 调价\n"
        "more body\n\n"
        "## 方法论\n"
        "### 用 sub-agent 做并行搜索的小技巧\n"
        "body\n\n"
        "---\n"
        "tags: model-release, agent\n"
    )
    assert extract_titles_outline(md) == (
        "## 行业新闻\n"
        "### Claude 4.7 发布\n"
        "### Anthropic 调价\n"
        "## 方法论\n"
        "### 用 sub-agent 做并行搜索的小技巧"
    )


def test_extract_titles_includes_hidden_section_titles():
    """`[章节不公开]` markers shouldn't filter out the surrounding ### title."""
    md = (
        "## 行业新闻\n"
        "### 某客户案例\n"
        "body\n\n"
        "[章节不公开：涉及保密客户的敏感信息]\n\n"
        "### Claude 4.7 发布\n"
        "body\n"
    )
    assert extract_titles_outline(md) == (
        "## 行业新闻\n"
        "### 某客户案例\n"
        "### Claude 4.7 发布"
    )


def test_extract_titles_ignores_h1_and_h4():
    md = (
        "# top\n"
        "## keep me\n"
        "#### too deep\n"
        "### keep me too\n"
    )
    assert extract_titles_outline(md) == "## keep me\n### keep me too"


def test_extract_titles_ignores_hash_inside_blockquote_body():
    """Headers must be at start of line, not inside a `> ` blockquote."""
    md = (
        "## real\n"
        "> ## not a header (inside blockquote)\n"
        "### also real\n"
    )
    assert extract_titles_outline(md) == "## real\n### also real"


def test_extract_titles_empty_when_no_headers():
    assert extract_titles_outline("just a body paragraph, no headers\n") == ""
    assert extract_titles_outline("") == ""


# ── load_prior_report_titles ───────────────────────────────────────────────────


def _write_with_titles(debug_dir, date_str: str, h3_titles: list[str]) -> None:
    body = "## 行业新闻\n" + "\n".join(f"### {t}\nbody\n" for t in h3_titles)
    _write_extract(debug_dir, date_str, body)


def test_load_titles_returns_outline_per_day(tmp_path):
    debug = tmp_path / "debug"
    _write_with_titles(debug, "2026-05-04", ["A1", "A2"])
    _write_with_titles(debug, "2026-05-05", ["B1"])

    out = load_prior_report_titles("2026-05-10", n_days=7, debug_dir=debug)
    assert out == [
        ("2026-05-04", "## 行业新闻\n### A1\n### A2"),
        ("2026-05-05", "## 行业新闻\n### B1"),
    ]


def test_load_titles_skips_dates_already_covered(tmp_path):
    debug = tmp_path / "debug"
    for d in ["2026-05-04", "2026-05-08", "2026-05-09"]:
        _write_with_titles(debug, d, ["x"])

    # Pretend full-body load already grabbed the last 3 days.
    out = load_prior_report_titles(
        "2026-05-10", n_days=7, debug_dir=debug,
        skip_dates={"2026-05-07", "2026-05-08", "2026-05-09"},
    )
    assert out == [("2026-05-04", "## 行业新闻\n### x")]


def test_load_titles_skips_missing_and_empty(tmp_path):
    debug = tmp_path / "debug"
    _write_with_titles(debug, "2026-05-04", ["only one"])
    _write_extract(debug, "2026-05-05", "")  # empty file
    _write_extract(debug, "2026-05-06", "no headers at all\n")  # no ##/### lines

    out = load_prior_report_titles("2026-05-10", n_days=7, debug_dir=debug)
    assert out == [("2026-05-04", "## 行业新闻\n### only one")]


def test_load_titles_returns_empty_when_no_dir(tmp_path):
    out = load_prior_report_titles(
        "2026-05-10", n_days=7, debug_dir=tmp_path / "nope",
    )
    assert out == []


# ── format_prior_report_titles_block ───────────────────────────────────────────


def test_format_titles_empty_returns_empty_string():
    assert format_prior_report_titles_block([]) == ""


def test_format_titles_wraps_each_day():
    block = format_prior_report_titles_block([
        ("2026-05-04", "## 行业新闻\n### A"),
        ("2026-05-05", "## 方法论\n### B"),
    ])
    assert block.startswith("<previous_report_titles>\n")
    assert block.rstrip("\n").endswith("</previous_report_titles>")
    assert '<report date="2026-05-04">' in block
    assert '<report date="2026-05-05">' in block
    assert "### A" in block
    assert "### B" in block
    assert block.count("</report>") == 2


# ── _normalize_to_ref_placeholders ─────────────────────────────────────────────


def test_normalize_rewrites_expanded_link_with_hash():
    src = (
        "前天 [「AI 正在吞掉哪些 SaaS」]"
        "({{ '/daily/2026/05/11/daily/#ai-正在吞掉哪些-saas' | relative_url }}) 说"
    )
    assert _normalize_to_ref_placeholders(src) == (
        "前天 [[ref:2026-05-11|AI 正在吞掉哪些 SaaS]] 说"
    )


def test_normalize_rewrites_expanded_link_without_hash():
    """Page-level link (no `#slug`) — still collapse to placeholder so the
    LLM sees uniform syntax."""
    src = "去年 [「某话题」]({{ '/daily/2026/01/01/daily/' | relative_url }}) 提过"
    assert _normalize_to_ref_placeholders(src) == (
        "去年 [[ref:2026-01-01|某话题]] 提过"
    )


def test_normalize_leaves_existing_placeholders_alone():
    """Already-placeholder text shouldn't be double-rewritten or otherwise touched."""
    src = "intro [[ref:2026-05-09|话题 A]] body"
    assert _normalize_to_ref_placeholders(src) == src


def test_normalize_handles_multiple_links_in_one_text():
    src = (
        "[「A」]({{ '/daily/2026/05/07/daily/#a' | relative_url }}) 与 "
        "[「B」]({{ '/daily/2026/05/08/daily/#b' | relative_url }})"
    )
    assert _normalize_to_ref_placeholders(src) == (
        "[[ref:2026-05-07|A]] 与 [[ref:2026-05-08|B]]"
    )


def test_normalize_noop_on_plain_text():
    assert _normalize_to_ref_placeholders("just prose, no refs.") == (
        "just prose, no refs."
    )
