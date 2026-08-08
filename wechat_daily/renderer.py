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

import datetime
import functools
import pathlib
import re
import sys
import urllib.parse
from typing import TYPE_CHECKING, Callable

from wechat_daily import config, models

if TYPE_CHECKING:
    from wechat_daily import aliases, contacts


# ── Regexes ─────────────────────────────────────────────────────────────────────

# Match ``[章节不公开：原因]`` with either Chinese or ASCII colon.
# Reason is anything up to the closing ']'; allowed empty.
_HIDE_RE = re.compile(r"\[章节不公开[：:]\s*([^\]]*)\]")

# Match a markdown ATX heading line and capture (level, text).
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

# Match the trailing ``tags: ...`` line (after last meaningful content).
_TAGS_LINE_RE = re.compile(r"^tags\s*:\s*(.*)$", re.IGNORECASE)

# Cross-day section reference placeholder emitted by the LLM:
# ``[[ref:YYYY-MM-DD|章节标题]]`` → expanded by render_public/render_group.
_REF_RE = re.compile(r"\[\[ref:(\d{4})-(\d{2})-(\d{2})\|([^\]\n]+?)\]\]")

# Final-form cross-day link the public renderer emits (and which the LLM
# occasionally writes directly, bypassing the placeholder):
#   [「title」]({{ '/daily/Y/M/D/daily/#slug' | relative_url }})
_FINAL_REF_RE = re.compile(
    r"\[「(?P<title>[^」\n]+?)」\]"
    r"\(\{\{\s*'/daily/(?P<y>\d{4})/(?P<mo>\d{2})/(?P<d>\d{2})/daily/"
    r"(?:#(?P<slug>[^']*))?'\s*\|\s*relative_url\s*\}\}\)"
)

# Inline markdown link ``[text](href)`` — the negative lookbehind skips image
# syntax ``![alt](src)``. Used to catch hallucinated non-URL link targets.
_LINK_RE = re.compile(r"(?<!!)\[(?P<text>[^\]\n]+)\]\((?P<href>[^)\n]+)\)")
# Allowed link-target prefixes: http(s) citations, root/relative paths, in-page
# anchors, mailto, and the ``{{ … | relative_url }}`` Liquid form used for
# cross-day refs. Anything else is a hallucinated caption, not a URL.
_VALID_HREF_RE = re.compile(r"^\s*(?:https?://|/|#|\.{1,2}/|mailto:|\{\{)")

# Characters dropped from a slug: anything that is not a word character (CJK
# included), a hyphen, a space, or a tab — mirroring kramdown-parser-gfm's
# NON_WORD_RE = /[^\p{Word}\- \t]/. CJK is preserved; browsers handle it in
# URL fragments.
_SLUG_DROP_RE = re.compile(r"[^\w\- \t]", re.UNICODE)
_SLUG_SPACE_RE = re.compile(r"[ \t]")

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
        for j, lvl2 in headings[k + 1 :]:
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


# ── URL tracking-param cleaning ─────────────────────────────────────────────────

# Per-host tracking params to strip from link URLs. Keys not listed are kept
# verbatim ("其他参数一律保留，不确定的不删"). For 公众号 links the reading
# params __biz/mid/idx/sn/chksm are absent from every strip set on purpose —
# dropping them makes the link un-openable.
_UTM_PREFIX = "utm_"

_WEIXIN_STRIP = frozenset(
    {"mpshare", "scene", "srcid", "sharer_shareinfo", "sharer_shareinfo_first"}
)
_BILI_STRIP = frozenset(
    {
        "share_source",
        "vd_source",
        "share_medium",
        "share_plat",
        "share_session_id",
        "share_tag",
        "spm_id_from",
    }
)
# 知乎 分享尾巴形如 #showWechatShareTip?utm_source=…&wechatShare=1&s_r=0：utm_*
# 已由通用规则清掉，另补 wechatShare / s_r 这两个明确的微信分享跟踪键。
_ZHIHU_STRIP = frozenset({"wechatShare", "s_r"})

# Match an http(s) URL (bare or inside a markdown link's ``(…)``). Boundaries
# stop at whitespace, markdown/HTML delimiters, and common CJK closing
# punctuation so a trailing 。」）等 in prose isn't swallowed into the URL.
_URL_RE = re.compile(r"https?://[^\s)>\]<」），。、；！？“”]+")
# Inline code span — protected so URLs inside `code` are left untouched.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_OR_CODE_RE = re.compile(rf"(?P<code>{_INLINE_CODE_RE.pattern})|(?P<url>{_URL_RE.pattern})")
# Opening/closing line of a fenced code block (``` or ~~~).
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _host_dropper(netloc: str) -> Callable[[str], bool]:
    """Return a predicate ``key → should_drop`` tuned to the URL's host."""
    host = netloc.lower()

    def drop(key: str) -> bool:
        if key.startswith(_UTM_PREFIX):
            return True
        if host.endswith("weixin.qq.com"):
            return key in _WEIXIN_STRIP
        if host.endswith("bilibili.com") or host.endswith("b23.tv"):
            return key in _BILI_STRIP
        if host.endswith("zhihu.com"):
            return key in _ZHIHU_STRIP
        return False

    return drop


def _strip_keys_from_query(query: str, drop: Callable[[str], bool]) -> str:
    """Drop ``key=value`` pairs whose key matches *drop*, preserving order/format."""
    if not query:
        return query
    kept = [p for p in query.split("&") if not drop(p.split("=", 1)[0])]
    return "&".join(kept)


def _clean_url(url: str) -> str:
    """Strip tracking params from a single URL; keep everything else intact.

    Handles the well-formed ``?a&b#frag`` shape and the malformed 知乎 form
    where the query rides *inside* the fragment
    (``…/p/123#showWechatShareTip?utm_source=…``). A query left empty after
    stripping loses its now-dangling ``?``.
    """
    parts = urllib.parse.urlsplit(url)
    drop = _host_dropper(parts.netloc)

    query = _strip_keys_from_query(parts.query, drop)

    fragment = parts.fragment
    if "?" in fragment:
        fbase, fquery = fragment.split("?", 1)
        fquery = _strip_keys_from_query(fquery, drop)
        fragment = f"{fbase}?{fquery}" if fquery else fbase

    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, fragment))


def _clean_line_urls(line: str) -> str:
    """Clean every URL on *line* while leaving inline-code spans untouched."""

    def sub(m: re.Match[str]) -> str:
        if m.group("code") is not None:
            return m.group(0)
        return _clean_url(m.group("url"))

    return _URL_OR_CODE_RE.sub(sub, line)


def _clean_tracking_params(markdown: str) -> str:
    """Strip URL tracking params from every link in the body.

    Covers markdown-link URLs and bare URLs alike, skipping fenced code blocks
    and inline code spans so example snippets stay verbatim.
    """
    out: list[str] = []
    in_fence = False
    for line in _split_lines(markdown):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        out.append(line if in_fence else _clean_line_urls(line))
    return "\n".join(out)


# ── Cross-day reference expansion ───────────────────────────────────────────────


def _slugify_heading(title: str) -> str:
    """kramdown-parser-gfm-compatible slug for a Chinese/English heading.

    Mirrors ``generate_gfm_header_id`` exactly: lowercase, drop everything that
    is not a word char / hyphen / space / tab, then turn each space or tab into
    a single hyphen. No hyphen collapsing and no leading/trailing strip — a
    heading like ``Claude `-p` 禁令`` keeps the double hyphen (``claude--p-禁令``)
    that the rendered HTML id carries, so htmlproofer's hash check passes.
    """
    s = title.strip().lower()
    s = _SLUG_DROP_RE.sub("", s)
    s = _SLUG_SPACE_RE.sub("-", s)
    return s


def _expand_refs_group(text: str) -> str:
    """Group/PDF version: keep only the section title in 「…」.

    Date is intentionally dropped; the model is instructed to write a natural-
    language date marker in surrounding prose ("昨天", "上周三" 等).
    """
    return _REF_RE.sub(lambda m: f"「{m.group(4).strip()}」", text)


@functools.lru_cache(maxsize=None)
def _post_heading_slugs(post_path: pathlib.Path) -> frozenset[str]:
    """Slug set for every ``##``/``###`` heading in *post_path*.

    The check matters because htmlproofer validates hash fragments against the
    deployed HTML — a slug that doesn't correspond to a real heading 404s the
    anchor even when the page itself loads. Reading is cached so repeated refs
    to the same prior post don't re-parse it.
    """
    try:
        text = post_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    slugs: set[str] = set()
    for line in text.split("\n"):
        h = _heading_at(line)
        if h is None:
            continue
        level, title = h
        if level < 2:
            continue
        slugs.add(_slugify_heading(title))
    return frozenset(slugs)


def _expand_refs_public(
    text: str,
    posts_dir: pathlib.Path | None = None,
) -> str:
    """Public version: expand to ``[「title」]({{ '/daily/Y/M/D/daily/#slug' | relative_url }})``.

    Wrapping the path in Jekyll's ``relative_url`` Liquid filter is what lets
    the link resolve under the site's ``baseurl`` (``/AI-chatgroup-daily``).
    Emitting a bare ``/daily/...`` path here yields a link that 404s on the
    deployed site and trips htmlproofer in CI.

    Degrades to plain ``「title」`` text (no URL) when either:
    - the target ``_posts/YYYY/MM/YYYY-MM-DD-daily.md`` does not exist, or
    - it exists but contains no heading whose slug matches the ref's title.

    The second case catches the common LLM mistake of attributing a section to
    the wrong date — better to drop the URL silently than ship a dangling hash
    that htmlproofer rejects.
    """
    base = posts_dir if posts_dir is not None else (config.PUBLIC_REPO_DIR / "_posts")

    def sub(m: re.Match[str]) -> str:
        y, mo, d, raw_title = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        post = base / y / mo / f"{y}-{mo}-{d}-daily.md"
        if not post.exists():
            return f"「{raw_title}」"
        slug = _slugify_heading(raw_title)
        if slug and slug not in _post_heading_slugs(post):
            print(
                f"[warn] cross-day ref to {y}-{mo}-{d}#{slug} "
                f"({raw_title!r}) not found in target post; dropping URL",
                file=sys.stderr,
            )
            return f"「{raw_title}」"
        anchor = f"#{slug}" if slug else ""
        url = f"/daily/{y}/{mo}/{d}/daily/{anchor}"
        return f"[「{raw_title}」]({{{{ '{url}' | relative_url }}}})"

    return _REF_RE.sub(sub, text)


def _validate_final_refs_public(
    text: str,
    posts_dir: pathlib.Path | None = None,
) -> str:
    """Second pass: re-check every final-form cross-day link against headings.

    `_expand_refs_public` only inspects `[[ref:…]]` placeholders. When the LLM
    skips that syntax and writes the expanded Liquid link inline — copying
    the pattern it sees in `<previous_reports>` — the placeholder-side
    validation never runs and a wrong-date URL ships unchecked (this is how
    the 2026-05-13 → 2026-05-12 "AI 正在吞掉哪些 SaaS" deadlink slipped past
    b622e1487b52). This pass walks every emitted link, validates the slug
    against the target post's headings, and degrades mismatches to plain
    `「title」` with the same warning.
    """
    base = posts_dir if posts_dir is not None else (config.PUBLIC_REPO_DIR / "_posts")

    def sub(m: re.Match[str]) -> str:
        title = m.group("title").strip()
        y, mo, d = m.group("y"), m.group("mo"), m.group("d")
        slug = (m.group("slug") or "").strip()
        post = base / y / mo / f"{y}-{mo}-{d}-daily.md"
        if not post.exists():
            print(
                f"[warn] cross-day link to {y}-{mo}-{d} "
                f"({title!r}) — target post missing; dropping URL",
                file=sys.stderr,
            )
            return f"「{title}」"
        if slug and slug not in _post_heading_slugs(post):
            print(
                f"[warn] cross-day link to {y}-{mo}-{d}#{slug} "
                f"({title!r}) not found in target post; dropping URL",
                file=sys.stderr,
            )
            return f"「{title}」"
        return m.group(0)

    return _FINAL_REF_RE.sub(sub, text)


def _warn_malformed_links(text: str) -> str:
    """Warn about markdown links whose target isn't a real URL/anchor.

    The LLM occasionally emits ``[visible text](描述文字)`` where the
    parenthesised part is a hallucinated caption rather than a URL — e.g. the
    2026-06-10 ``[Microsoft 因数据保留顾虑限制员工使用 Fable](Microsoft restricts
    Fable 截图)`` deadlink that failed htmlproofer's internal-link check. Real
    citations carry an ``http(s)`` scheme and cross-day refs use the
    ``{{ … | relative_url }}`` Liquid form, so any target that doesn't start
    with an allowed prefix is flagged. The text is returned unchanged — fixing
    the link is left to a human / AI review pass, not done automatically.
    """

    for m in _LINK_RE.finditer(text):
        href = m.group("href")
        if _VALID_HREF_RE.match(href):
            continue
        print(
            f"[warn] markdown link target {href!r} ({m.group('text')!r}) is not "
            f"a URL/anchor — likely a hallucinated caption; please fix manually",
            file=sys.stderr,
        )

    return text


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


def _mention(name: str) -> str:
    """Wrap a resolved nickname as a Slack-style ``@`` mention pill.

    Every render target styles the ``mention`` class (PDF CSS in ``pdf.py``,
    preview HTML in ``publisher.preview``, public site in the Chirpy theme's
    ``custom.scss``).
    """
    return f'<span class="mention">@{name}</span>'


def _build_token_replacer(
    alias_db: aliases.AliasDB,
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
        return "\n".join(lines).rstrip() + "\n\n[TOC]\n"

    # Trim trailing blank lines from intro, then place [TOC] separated by blanks.
    intro = lines[:insert_at]
    while intro and not intro[-1].strip():
        intro.pop()
    rest = lines[insert_at:]
    return "\n".join(intro + ["", "[TOC]", ""] + rest)


# ── Group version ────────────────────────────────────────────────────────────────


def render_group(
    report: models.DailyReport,
    alias_db: aliases.AliasDB,
    contact_map: contacts.ContactMap,
    command_log: list[dict] | None = None,
    token_map=None,
) -> str:
    """Render the internal version: real names, 🔒 markers, [TOC], command log."""

    body, tags = _strip_trailing_tags(report.markdown)
    body = _clean_tracking_params(body)
    body = _annotate_hidden_for_group(body)
    body = _expand_refs_group(body)
    body = _insert_toc(body)

    def token_to_real(token: str) -> str:
        wxid = (token_map.wxid(token) if token_map else None) or alias_db.wxid_of_token(token)
        if not wxid:
            return _mention(token)
        real = contact_map.by_wxid(wxid)
        if real == wxid:
            real = alias_db.real_name_seen(wxid) or wxid
        return _mention(real)

    extra = token_map.all_tokens() if token_map else None
    text_resolver = _build_token_replacer(alias_db, token_to_real, extra)
    body = text_resolver(body)

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
    alias_db: aliases.AliasDB,
    contact_map: contacts.ContactMap,
) -> str:
    lines = ["## 本期指令执行记录", "", "### 今日生效指令"]
    if log:
        for entry in log:
            ts_str = datetime.datetime.fromtimestamp(entry["ts"]).strftime("%H:%M")
            wxid = entry["wxid"]
            real_name = contact_map.by_wxid(wxid)
            if real_name == wxid:
                real_name = alias_db.real_name_seen(wxid) or wxid
            ok_mark = "✓" if entry["ok"] else "✗"
            lines.append(f"- {ts_str}  {_mention(real_name)}：{entry['msg']}  {ok_mark}")
    else:
        lines.append("- （今日无指令）")

    lines += [
        "",
        "### 可用指令说明",
        "- `/alias <名字>`：设置在公开版日报中的显示别名。最多 6 个汉字"
        "（12 个英文字符），支持中英文/数字/`_`/`-`/`·`。",
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
    report: models.DailyReport,
    alias_db: aliases.AliasDB,
    token_map=None,
) -> str:
    """Render the public version: anonymized, hidden sections fully removed."""

    body, tags = _strip_trailing_tags(report.markdown)
    body = _clean_tracking_params(body)
    body = _strip_hidden_for_public(body)
    body = _expand_refs_public(body)
    body = _validate_final_refs_public(body)
    _warn_malformed_links(body)

    def token_to_public(token: str) -> str:
        wxid = (token_map.wxid(token) if token_map else None) or alias_db.wxid_of_token(token)
        if not wxid:
            return _mention(token)
        if alias_db.is_optout(wxid):
            return _mention("某群友")
        return _mention(alias_db.public_name_of(wxid))

    extra = token_map.all_tokens() if token_map else None
    text_resolver = _build_token_replacer(alias_db, token_to_public, extra)
    body = text_resolver(body)

    publish_dt = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
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
