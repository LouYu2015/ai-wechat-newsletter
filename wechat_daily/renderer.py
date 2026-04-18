"""Render DailyReport → Markdown (group version and public version)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from .models import DailyReport, Section, Comment  # re-exported for convenience

if TYPE_CHECKING:
    from .aliases import AliasDB
    from .contacts import ContactMap


# ── Section rendering ────────────────────────────────────────────────────────────

def _render_section(section: Section, token_resolver) -> str:
    lines = [f"## {section.title}", ""]
    # Body may contain newline-separated points; emit as-is
    lines.append(section.body)
    if section.comments:
        lines.append("")
        for comment in section.comments:
            name = token_resolver(comment.token)
            lines.append(f"> {name}：{comment.text}")
    return '\n'.join(lines)


def _render_sections(
    sections: list[Section],
    token_resolver,
    filter_unsafe: bool,
) -> str:
    parts = [
        _render_section(s, token_resolver)
        for s in sections
        if not (filter_unsafe and not s.public_safe)
    ]
    return '\n\n'.join(parts)


# ── Group version ────────────────────────────────────────────────────────────────

def render_group(
    report: DailyReport,
    alias_db: "AliasDB",
    contact_map: "ContactMap",
    command_log: list[dict] | None = None,
) -> str:
    """Render the group (private) version with real names and instruction log."""

    def token_to_real(token: str) -> str:
        wxid = alias_db.wxid_of_token(token)
        if not wxid:
            return token
        real = contact_map.by_wxid(wxid)
        if real != wxid:
            return real
        # Fallback to last-seen name stored in aliases (covers contacts not in contact.db)
        return alias_db.real_name_seen(wxid) or wxid

    parts = [
        f"# {report.date} 群聊日报",
        "",
        report.intro,
        "",
        "[TOC]",
        "",
        _render_sections(report.sections, token_to_real, filter_unsafe=False),
        "",
        _render_command_log(command_log or [], alias_db, contact_map),
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
) -> str:
    """Render the public (anonymized) version with Jekyll front matter."""

    def token_to_public(token: str) -> str:
        wxid = alias_db.wxid_of_token(token)
        if wxid:
            return alias_db.public_name_of(wxid)
        return token

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

    # Strip any stray [TOC] Claude may have included in intro
    intro = report.intro.replace('[TOC]', '').strip()

    body = _render_sections(report.sections, token_to_public, filter_unsafe=True)

    return '\n'.join([front_matter, "", intro, "", body])
