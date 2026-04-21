"""Render DailyReport → Markdown (group version and public version)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from .models import DailyReport, Section, Comment  # re-exported for convenience

if TYPE_CHECKING:
    from .aliases import AliasDB
    from .contacts import ContactMap


_TYPE_LABELS: dict[str, str] = {
    "news": "行业新闻",
    "tool": "工具",
    "methodology": "方法论",
    "anecdote": "闲聊花絮",
}


def _build_token_replacer(
    alias_db: "AliasDB",
    resolve_fn: Callable[[str], str],
    extra_tokens: list[str] | None = None,
) -> Callable[[str], str]:
    """Return a closure that replaces every known token in a string with resolve_fn(token).

    *extra_tokens* supplements the alias_db's registered users — pass
    token_map.all_tokens() from the extraction step so that users who were
    tokenized on-the-fly (not yet in alias_db._users) are also resolved.
    Tokens are sorted longest-first to prevent substring collisions.
    """
    token_set: set[str] = set(alias_db.all_default_anons())
    if extra_tokens:
        token_set.update(extra_tokens)
    tokens = sorted(token_set, key=len, reverse=True)
    if not tokens:
        return lambda s: s
    pattern = re.compile('|'.join(re.escape(t) for t in tokens))
    mapping = {t: resolve_fn(t) for t in tokens}
    return lambda text: pattern.sub(lambda m: mapping[m.group(0)], text)


# ── Section rendering ────────────────────────────────────────────────────────────

def _render_section(section: Section, token_resolver, text_resolver) -> str:
    title = text_resolver(section.title)
    lines = [f"### {title}", ""]
    lines.append(text_resolver(section.body))
    if section.comments:
        lines.append("")
        comment_blocks = []
        for comment in section.comments:
            name = token_resolver(comment.token)
            text = text_resolver(comment.text)
            comment_blocks.append(f"> {name}：{text}")
        # Blank line between each blockquote so they render as separate <blockquote> elements.
        lines.append('\n\n'.join(comment_blocks))
    return '\n'.join(lines)


def _render_sections(
    sections: list[Section],
    token_resolver,
    text_resolver,
    filter_unsafe: bool,
) -> str:
    # Group sections by type, preserving first-occurrence order.
    groups: dict[str, list[Section]] = {}
    for s in sections:
        if filter_unsafe and not s.public_safe:
            continue
        groups.setdefault(s.type, []).append(s)

    parts: list[str] = []
    for type_, group in groups.items():
        label = _TYPE_LABELS.get(type_, type_)
        group_parts = [f"## {label}"]
        for s in group:
            group_parts.append(_render_section(s, token_resolver, text_resolver))
        parts.append('\n\n'.join(group_parts))
    return '\n\n'.join(parts)


# ── Group version ────────────────────────────────────────────────────────────────

def render_group(
    report: DailyReport,
    alias_db: "AliasDB",
    contact_map: "ContactMap",
    command_log: list[dict] | None = None,
    token_map=None,
) -> str:
    """Render the group (private) version with real names and instruction log.

    *token_map* (a TokenMap from tokenize_messages) ensures every token used
    during extraction is resolvable, including on-the-fly tokens for users not
    yet registered in alias_db.
    """

    def token_to_real(token: str) -> str:
        # Prefer token_map's direct lookup; fall back to alias_db scan.
        wxid = (token_map.wxid(token) if token_map else None) \
               or alias_db.wxid_of_token(token)
        if not wxid:
            return token
        real = contact_map.by_wxid(wxid)
        if real != wxid:
            return real
        return alias_db.real_name_seen(wxid) or wxid

    extra = token_map.all_tokens() if token_map else None
    text_resolver = _build_token_replacer(alias_db, token_to_real, extra)

    parts = [
        f"# {report.date} 群聊日报",
        "",
        text_resolver(report.intro),
        "",
        "[TOC]",
        "",
        _render_sections(report.sections, token_to_real, text_resolver, filter_unsafe=False),
        "",
        _render_command_log(command_log or [], alias_db, contact_map),
        "",
        "---",
        "",
        "公开版日报网站：<https://louyu2015.github.io/AI-chatgroup-daily/>",
    ]
    return '\n'.join(parts)


def _render_command_log(
    log: list[dict],
    alias_db: "AliasDB",
    contact_map: "ContactMap",
) -> str:
    lines = ["## 本期指令执行记录", ""]
    lines.append("### 今日生效指令")
    if log:
        for entry in log:
            ts_str = datetime.fromtimestamp(entry['ts']).strftime('%H:%M')
            wxid = entry['wxid']
            real_name = contact_map.by_wxid(wxid)
            if real_name == wxid:
                real_name = alias_db.real_name_seen(wxid) or wxid
            ok_mark = "✓" if entry['ok'] else "✗"
            lines.append(f"- {ts_str}  {real_name}：{entry['msg']}  {ok_mark}")
    else:
        lines.append("- （今日无指令）")

    lines += [
        "",
        "### 可用指令说明",
        "- `/alias <名字>`：设置在公开版日报中的显示别名。长度 1–16 字符，支持中英文/数字/`_`/`-`/`·`。",
        "- `/alias`：清空别名，恢复默认匿名名。旧名释放后 30 天内其他人不可占用。",
        "- `/optout`：不参与公开版。后续发言将从公开版完全移除，其他群友对你的引用也会被遮蔽。",
        "- `/optin`：重新参与公开版（此前已发布日报不会自动补回）。",
        "",
        "### 规则提示",
        "- 所有指令需**单独成行**发送；行尾多余内容会被忽略。",
        "- 指令不会实时回复，在下一份日报中统一生效并公布执行结果。",
        "- 若设置的别名与他人冲突，**先到先得**；被拒绝的指令会显示在本章节。",
    ]
    return '\n'.join(lines)


# ── Public version ───────────────────────────────────────────────────────────────

def render_public(
    report: DailyReport,
    alias_db: "AliasDB",
    token_map=None,
) -> str:
    """Render the public (anonymized) version with Jekyll front matter."""

    def token_to_public(token: str) -> str:
        wxid = (token_map.wxid(token) if token_map else None) \
               or alias_db.wxid_of_token(token)
        if not wxid:
            return token
        if alias_db.is_optout(wxid):
            return "某群友"
        return alias_db.public_name_of(wxid)

    extra = token_map.all_tokens() if token_map else None
    text_resolver = _build_token_replacer(alias_db, token_to_public, extra)

    # Gather tags from safe sections only
    all_tags: list[str] = []
    for s in report.sections:
        if s.public_safe:
            all_tags.extend(s.tags)
    all_tags = list(dict.fromkeys(all_tags))  # deduplicate, preserve order

    front_matter_lines = [
        "---",
        f'title: "{report.date} 群聊日报"',
        f"date: {report.date} 12:00:00 +0800",
        "categories:",
        "  - Daily",
    ]
    if all_tags:
        front_matter_lines.append("tags:")
        front_matter_lines.extend(f"  - {t}" for t in all_tags)
    else:
        front_matter_lines.append("tags: []")
    front_matter_lines += [
        "layout: post",
        "toc: true",
        'license: "CC BY-NC 4.0"',
        "---",
    ]
    front_matter = '\n'.join(front_matter_lines)

    intro = text_resolver(report.intro.replace('[TOC]', '').strip())
    body = _render_sections(report.sections, token_to_public, text_resolver, filter_unsafe=True)

    return '\n'.join([front_matter, "", intro, "", body])
