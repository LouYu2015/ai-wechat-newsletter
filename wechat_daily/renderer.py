"""Render DailyReport(markdown) → group / public Markdown.

Pipeline (shared, then per-version):

1. Strip the trailing ``tags: …`` footer (with its preceding ``---`` separator).
2. Parse heading structure (``##``/``###``) and find every line containing the
   ``[章节不公开：原因]`` hide marker.
3. **Public version**: drop each affected heading's full scope (heading line
   through next same-or-higher-level heading); afterwards, drop any ``##``
   left with no ``###`` child. Wrap in Jekyll front matter.
4. **Group version**: keep all sections, but for each affected heading prepend
   ``🔒`` to the title and insert a ``> ⚠️ **公开版隐藏** · 原因：…`` banner
   right below it. Strip the marker text itself. Insert ``[TOC]`` after intro.
5. Token replacement runs on the final text (group → real names; public →
   public alias / 某群友). Token regex is built from alias_db + token_map so
   on-the-fly tokens are also resolved.

The hide-marker regex is intentionally strict (matches the format the prompt
asks for). Variants — typos, missing colon, etc. — fall through and are
caught by manual review of the group version before publishing.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from .models import DailyReport
from .privacy import mark_leaks

if TYPE_CHECKING:
    from .aliases import AliasDB
    from .contacts import ContactMap


# ── Regexes ─────────────────────────────────────────────────────────────────────

# Match ``[章节不公开：原因]`` with either Chinese or ASCII colon.
# Reason is anything up to the closing ']'; allowed empty.
_HIDE_RE = re.compile(r"\[章节不公开[：:]\s*([^\]]*)\]")

# Match a markdown ATX heading line and capture (level, text).
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

# Match the trailing ``tags: ...`` line (after last meaningful content).
_TAGS_LINE_RE = re.compile(r"^tags\s*:\s*(.*)$", re.IGNORECASE)

_TAG_INVALID_RE = re.compile(r"[^a-z0-9-]+")
_TAG_DASHES_RE = re.compile(r"-{2,}")


def _normalize_tag(raw: str) -> str:
    """Normalize a tag to ``[a-z0-9-]+`` so Jekyll slug collisions disappear.

    LLM occasionally emits ``gpt-5.5`` and ``gpt-5-5`` for the same concept;
    Jekyll slugifies ``.`` to ``-``, so both collide on the same tag URL.
    """
    s = _TAG_INVALID_RE.sub("-", raw.strip().lower())
    s = _TAG_DASHES_RE.sub("-", s).strip("-")
    return s


# ── Common helpers ──────────────────────────────────────────────────────────────

def _split_lines(text: str) -> list[str]:
    """Split preserving no trailing empty line; safe for re-join with '\\n'."""
    return text.split("\n")


def _heading_at(line: str) -> tuple[int, str] | None:
    m = _HEADING_RE.match(line)
    if not m:
        return None
    return len(m.group(1)), m.group(2)


def _strip_trailing_tags(markdown: str) -> tuple[str, list[str]]:
    """Pop the trailing ``tags: …`` line (and its preceding ``---``).

    Returns ``(body_without_tags, tags_list)``. If no tags line is present,
    returns the original markdown and an empty list. Comparison is case-
    insensitive on the ``tags`` key. Tags are split by comma and trimmed.
    """
    lines = _split_lines(markdown.rstrip("\n"))
    # Drop trailing blank lines
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return markdown, []

    m = _TAGS_LINE_RE.match(lines[-1].strip())
    if not m:
        return markdown, []

    raw = m.group(1)
    tags = [_normalize_tag(t) for t in raw.split(",") if t.strip()]
    tags = list(dict.fromkeys(t for t in tags if t))
    lines.pop()

    # Drop blank lines between '---' separator and the tags line
    while lines and not lines[-1].strip():
        lines.pop()
    # Drop the optional '---' separator
    if lines and lines[-1].strip() == "---":
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines), tags


def _section_ranges(lines: list[str]) -> list[tuple[int, int, int]]:
    """For each heading line, return (start_index, end_index_exclusive, level).

    A heading's range extends from its line through the line before the next
    heading whose level <= its own (or end of document).
    """
    headings: list[tuple[int, int]] = []  # (line_index, level)
    for i, line in enumerate(lines):
        h = _heading_at(line)
        if h is not None:
            headings.append((i, h[0]))

    out: list[tuple[int, int, int]] = []
    for k, (start, level) in enumerate(headings):
        end = len(lines)
        for j, lvl2 in headings[k + 1:]:
            if lvl2 <= level:
                end = j
                break
        out.append((start, end, level))
    return out


def _enclosing_heading_idx(
    line_idx: int,
    ranges: list[tuple[int, int, int]],
) -> int | None:
    """Return the index into *ranges* of the deepest heading whose scope contains *line_idx*.

    Deepest = highest level number = innermost. If multiple headings contain
    the line, we want the most-nested one (typically ``###`` over its ``##``).
    """
    best: int | None = None
    best_level = -1
    for i, (start, end, level) in enumerate(ranges):
        if start <= line_idx < end and level > best_level:
            best = i
            best_level = level
    return best


def _find_hidden_heading_indices(
    lines: list[str],
    ranges: list[tuple[int, int, int]],
) -> dict[int, str]:
    """Return {ranges_index: reason} for each heading whose scope contains a marker.

    If multiple markers appear in one section, the first wins. If a marker
    appears outside any heading (before the first heading), it's ignored.
    """
    out: dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _HIDE_RE.search(line)
        if not m:
            continue
        idx = _enclosing_heading_idx(i, ranges)
        if idx is None:
            continue
        if idx not in out:  # first marker wins
            out[idx] = m.group(1).strip()
    return out


# ── Public-version stripping ────────────────────────────────────────────────────

def _strip_hidden_for_public(markdown: str) -> str:
    """Drop entire heading scopes containing a hide marker; clean empty ##.

    Two passes:
      1. Compute every line index belonging to a hidden heading's scope,
         delete them.
      2. After deletion, walk the remaining ``##`` headings; if a ``##`` block
         (until next ``##``) contains no ``###`` heading, drop the whole block.
    """
    lines = _split_lines(markdown)
    ranges = _section_ranges(lines)
    hidden = _find_hidden_heading_indices(lines, ranges)

    if hidden:
        drop: set[int] = set()
        for idx in hidden:
            start, end, _ = ranges[idx]
            for k in range(start, end):
                drop.add(k)
        lines = [ln for k, ln in enumerate(lines) if k not in drop]

    # Second pass: drop empty ## blocks
    lines = _drop_empty_h2(lines)

    # Collapse ≥3 blank lines to 1, since deletions can leave ugly gaps.
    return _collapse_blanks("\n".join(lines))


def _drop_empty_h2(lines: list[str]) -> list[str]:
    """Remove any ``## …`` heading whose scope (until next ``##``) has no ``###``."""
    keep = [True] * len(lines)
    n = len(lines)

    h2_starts: list[int] = []
    for i, line in enumerate(lines):
        h = _heading_at(line)
        if h is not None and h[0] == 2:
            h2_starts.append(i)

    for k, start in enumerate(h2_starts):
        end = h2_starts[k + 1] if k + 1 < len(h2_starts) else n
        has_h3 = False
        for j in range(start + 1, end):
            h = _heading_at(lines[j])
            if h is not None and h[0] >= 3:
                has_h3 = True
                break
        if not has_h3:
            for j in range(start, end):
                keep[j] = False

    return [ln for k, ln in enumerate(lines) if keep[k]]


def _collapse_blanks(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n") + "\n"


# ── Group-version annotation ────────────────────────────────────────────────────

def _annotate_hidden_for_group(markdown: str) -> str:
    """Mark hidden sections with 🔒 + banner; strip marker text from anywhere."""
    lines = _split_lines(markdown)
    ranges = _section_ranges(lines)
    hidden = _find_hidden_heading_indices(lines, ranges)

    # Build a map: heading_line_idx → reason
    heading_to_reason: dict[int, str] = {}
    for ridx, reason in hidden.items():
        heading_line, _, _ = ranges[ridx]
        heading_to_reason[heading_line] = reason

    out: list[str] = []
    for i, line in enumerate(lines):
        if i in heading_to_reason:
            h = _heading_at(line)
            if h is not None:  # always true here, but be defensive
                level, title = h
                # Strip any inline marker from title itself, just in case
                title = _HIDE_RE.sub("", title).strip()
                out.append(f"{'#' * level} 🔒 {title}")
                reason = heading_to_reason[i].strip() or "（未填原因）"
                out.append("")
                out.append(f"> ⚠️ **公开版隐藏** · 原因：{reason}")
                continue
        # Strip standalone marker lines anywhere; if a marker appears mid-line,
        # also strip just the marker substring.
        cleaned = _HIDE_RE.sub("", line)
        # If the line was *only* the marker (now empty/whitespace) drop it.
        if not cleaned.strip() and _HIDE_RE.search(line):
            continue
        out.append(cleaned)

    return _collapse_blanks("\n".join(out))


# ── Token replacement ───────────────────────────────────────────────────────────

def _build_token_replacer(
    alias_db: "AliasDB",
    resolve_fn: Callable[[str], str],
    extra_tokens: list[str] | None = None,
) -> Callable[[str], str]:
    """Build a closure that replaces every known token in text via ``resolve_fn``.

    *extra_tokens* supplements alias_db's registered users (lazy-allocated
    tokens from the current run). Tokens are sorted longest-first so substring
    overlaps don't corrupt replacements.
    """
    token_set: set[str] = set(alias_db.all_default_anons())
    if extra_tokens:
        token_set.update(extra_tokens)
    tokens = sorted(token_set, key=len, reverse=True)
    if not tokens:
        return lambda s: s
    pattern = re.compile("|".join(re.escape(t) for t in tokens))
    mapping = {t: resolve_fn(t) for t in tokens}
    return lambda text: pattern.sub(lambda m: mapping[m.group(0)], text)


# ── TOC insertion (group version only) ──────────────────────────────────────────

def _insert_toc(markdown: str) -> str:
    """Insert ``[TOC]`` between intro and the first heading.

    Intro = lines from the top until (but not including) the first ATX
    heading. If there is no heading, ``[TOC]`` is appended at the end.
    """
    lines = _split_lines(markdown)
    # First strip any model-emitted [TOC] line.
    lines = [ln for ln in lines if ln.strip() != "[TOC]"]

    insert_at: int | None = None
    for i, line in enumerate(lines):
        if _heading_at(line) is not None:
            insert_at = i
            break

    if insert_at is None:
        return ("\n".join(lines).rstrip() + "\n\n[TOC]\n")

    # Trim trailing blank lines from intro, then place [TOC] separated by blanks.
    intro = lines[:insert_at]
    while intro and not intro[-1].strip():
        intro.pop()
    rest = lines[insert_at:]
    return "\n".join(intro + ["", "[TOC]", ""] + rest)


# ── Group version ────────────────────────────────────────────────────────────────

def render_group(
    report: DailyReport,
    alias_db: "AliasDB",
    contact_map: "ContactMap",
    command_log: list[dict] | None = None,
    token_map=None,
) -> str:
    """Render the internal version: real names, 🔒 markers, [TOC], command log."""

    body, tags = _strip_trailing_tags(report.markdown)
    body = _annotate_hidden_for_group(body)
    body = _insert_toc(body)

    def token_to_real(token: str) -> str:
        wxid = (token_map.wxid(token) if token_map else None) \
               or alias_db.wxid_of_token(token)
        if not wxid:
            return f"<u>{token}</u>"
        real = contact_map.by_wxid(wxid)
        if real == wxid:
            real = alias_db.real_name_seen(wxid) or wxid
        return f"<u>{real}</u>"

    extra = token_map.all_tokens() if token_map else None
    text_resolver = _build_token_replacer(alias_db, token_to_real, extra)
    body = text_resolver(body)
    # mark_leaks runs after token replacement so CJK substrings (e.g. 「企鹅」
    # inside the token 「开朗的企鹅」) don't get a <mark> inserted that breaks
    # the subsequent token regex match. Token-resolved <u>…</u> regions are
    # skipped inside mark_leaks.
    body = mark_leaks(body, contact_map)

    parts = [
        f"# {report.date} 群聊日报",
        "",
        body.rstrip(),
        "",
        _render_command_log(command_log or [], alias_db, contact_map),
    ]

    if tags:
        parts += ["", "---", "", f"_tags: {', '.join(tags)}_"]

    parts += [
        "",
        "---",
        "",
        "公开版日报网站：<https://louyu2015.github.io/AI-chatgroup-daily/>",
    ]
    return "\n".join(parts)


def _render_command_log(
    log: list[dict],
    alias_db: "AliasDB",
    contact_map: "ContactMap",
) -> str:
    lines = ["## 本期指令执行记录", "", "### 今日生效指令"]
    if log:
        for entry in log:
            ts_str = datetime.fromtimestamp(entry['ts']).strftime('%H:%M')
            wxid = entry['wxid']
            real_name = contact_map.by_wxid(wxid)
            if real_name == wxid:
                real_name = alias_db.real_name_seen(wxid) or wxid
            ok_mark = "✓" if entry['ok'] else "✗"
            lines.append(f"- {ts_str}  <u>{real_name}</u>：{entry['msg']}  {ok_mark}")
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
    return "\n".join(lines)


# ── Public version ───────────────────────────────────────────────────────────────

def render_public(
    report: DailyReport,
    alias_db: "AliasDB",
    token_map=None,
) -> str:
    """Render the public version: anonymized, hidden sections fully removed."""

    body, tags = _strip_trailing_tags(report.markdown)
    body = _strip_hidden_for_public(body)

    def token_to_public(token: str) -> str:
        wxid = (token_map.wxid(token) if token_map else None) \
               or alias_db.wxid_of_token(token)
        if not wxid:
            return f"<u>{token}</u>"
        if alias_db.is_optout(wxid):
            return "<u>某群友</u>"
        return f"<u>{alias_db.public_name_of(wxid)}</u>"

    extra = token_map.all_tokens() if token_map else None
    text_resolver = _build_token_replacer(alias_db, token_to_public, extra)
    body = text_resolver(body)

    publish_dt = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    year, month, day = report.date.split("-")
    permalink = f"/daily/{year}/{month}/{day}/daily/"

    front_matter_lines = [
        "---",
        f'title: "{report.date} 群聊日报"',
        f"date: {publish_dt}",
        f"permalink: {permalink}",
        "categories:",
        "  - Daily",
    ]
    if tags:
        front_matter_lines.append("tags:")
        front_matter_lines.extend(f"  - {t}" for t in tags)
    else:
        front_matter_lines.append("tags: []")
    front_matter_lines += [
        "layout: post",
        "toc: true",
        'license: "CC BY-NC 4.0"',
        "---",
    ]

    return "\n".join(front_matter_lines) + "\n\n" + body.rstrip() + "\n"
