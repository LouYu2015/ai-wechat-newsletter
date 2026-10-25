"""Load recent days' raw LLM markdown so today's run can reference / continue them.

The model writes its raw output (with tokens like 「沉稳的大象」 still in
place) to ``debug/extract-{YYYY-MM-DD}.md``. That file is the right input to
re-feed: it uses the same tokens the next run will use, and it contains the
section structure that today's report may want to cross-reference via the
``[[ref:YYYY-MM-DD|章节标题]]`` placeholder.

Two granularities are exposed:

* :func:`load_prior_reports` returns the **full** markdown body for the most
  recent N days (default 3). The body is the primary continuation context.
* :func:`load_prior_report_titles` returns just the ``##`` / ``###`` outline
  for older days (e.g. 4–7 days back). The outline is far cheaper on tokens
  but still lets the model de-dup against and ``[[ref:…]]`` into older
  reports.
"""

from __future__ import annotations

import datetime
import pathlib
import re

from wechat_daily import config

# Match ``## title`` and ``### title`` lines. Anchored to start of line.
_TITLE_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")

# Hide marker (mirrors renderer._HIDE_RE).
_HIDE_RE = re.compile(r"\[章节不公开[：:]\s*([^\]]*)\]")

# Final-form cross-day link the public renderer emits (mirrors renderer._FINAL_REF_RE).
# Kept local so prior_report has no import dep on renderer's regex internals.
_EXPANDED_REF_RE = re.compile(
    r"\[「(?P<title>[^」\n]+?)」\]"
    r"\(\{\{\s*'/daily/(?P<y>\d{4})/(?P<mo>\d{2})/(?P<d>\d{2})/daily/"
    r"(?:#[^']*)?'\s*\|\s*relative_url\s*\}\}\)"
)


def _normalize_to_ref_placeholders(markdown: str) -> str:
    """Rewrite expanded ``[「…」]({{ '…' | relative_url }})`` back to ``[[ref:…|…]]``.

    The extract files we re-feed as ``<previous_reports>`` carry whatever
    shape the LLM emitted. When a previous run bypassed the placeholder and
    wrote the final Liquid link inline, that shape persists across days —
    the next run sees it in the previous report and copies the same
    anti-pattern, keeping the bug alive. Normalizing on the way in shows the
    model a single uniform reference syntax to imitate.
    """

    def sub(m: re.Match[str]) -> str:
        title = m.group("title").strip()
        return f"[[ref:{m.group('y')}-{m.group('mo')}-{m.group('d')}|{title}]]"

    return _EXPANDED_REF_RE.sub(sub, markdown)


def _extract_path(date_str: str, debug_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Canonical extract path for *date_str*.

    New layout is the per-date folder ``debug/{date}/extract.md``; we fall back
    to the legacy flat ``debug/extract-{date}.md`` when the new one is absent,
    so continuity keeps working across the layout switch (yesterday's flat file
    still loads today).
    """
    base = debug_dir or config.DEBUG_DIR
    year, month, day = date_str.split("-")
    new = base / year / month / day / "extract.md"
    if new.exists():
        return new
    legacy = base / f"extract-{date_str}.md"
    return legacy if legacy.exists() else new


def expected_dates(date_str: str, n_days: int) -> list[str]:
    """Return [date-n, …, date-1] in ascending order."""
    base = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    return [(base - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n_days, 0, -1)]


def load_prior_reports(
    date_str: str,
    n_days: int = 3,
    debug_dir: pathlib.Path | None = None,
) -> list[tuple[str, str]]:
    """Return ``[(YYYY-MM-DD, raw_markdown), …]`` for the *n_days* preceding *date_str*.

    Ascending date order. Missing days are silently omitted; check
    ``len(...) < n_days`` if the caller wants to detect gaps.
    """
    out: list[tuple[str, str]] = []
    for d in expected_dates(date_str, n_days):
        p = _extract_path(d, debug_dir)
        if not p.exists():
            continue
        try:
            md = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if md.strip():
            out.append((d, _normalize_to_ref_placeholders(md)))
    return out


def missing_prior_dates(
    date_str: str,
    n_days: int = 3,
    debug_dir: pathlib.Path | None = None,
) -> list[str]:
    """Return the subset of expected_dates(...) that have no usable extract on disk."""
    have = {d for d, _ in load_prior_reports(date_str, n_days, debug_dir)}
    return [d for d in expected_dates(date_str, n_days) if d not in have]


def format_prior_reports_block(reports: list[tuple[str, str]]) -> str:
    """Wrap loaded reports in a single ``<previous_reports>`` XML block.

    Empty list → empty string (caller can concatenate unconditionally).
    """
    if not reports:
        return ""
    parts = ["<previous_reports>"]
    for d, md in reports:
        parts.append(f'<report date="{d}">')
        parts.append(md.rstrip("\n"))
        parts.append("</report>")
    parts.append("</previous_reports>")
    return "\n".join(parts) + "\n"


def extract_titles_outline(markdown: str) -> str:
    """Return the ``##`` / ``###`` outline of *markdown* with hierarchy preserved.

    Each matched header is emitted on its own line as ``## title`` /
    ``### title`` in source order. Other content (intro paragraphs, body text,
    blockquotes, ``---`` / tags footer) is dropped — except ``[章节不公开]``
    markers, which are kept on their own line right under the heading they
    hide. Without them the model can't tell a hidden section from a public
    one and ``[[ref:…]]``s titles the public site doesn't have (2026-10-24 →
    2026-10-20「遥测与 local proxy：继续拆 Claude Code」).

    Returns the empty string if no headers are found.
    """
    lines: list[str] = []
    marked = False  # current heading already carries a marker
    for line in markdown.split("\n"):
        m = _TITLE_RE.match(line)
        if m:
            lines.append(f"{m.group(1)} {m.group(2).strip()}")
            marked = False
            continue
        h = _HIDE_RE.search(line)
        if h and lines and not marked:
            lines.append(h.group(0))
            marked = True
    return "\n".join(lines)


def load_prior_report_titles(
    date_str: str,
    n_days: int,
    debug_dir: pathlib.Path | None = None,
    skip_dates: set[str] | frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """Return ``[(YYYY-MM-DD, outline), …]`` for the *n_days* preceding *date_str*.

    Like :func:`load_prior_reports` but each value is the ``##`` / ``###``
    outline (see :func:`extract_titles_outline`) rather than the full body.

    *skip_dates* — dates already covered by a full-body load; they're skipped
    so the caller can stack a wider title window on top of a narrower body
    window without duplication.

    Ascending date order. Days with no extract on disk, no headers, or in
    *skip_dates* are silently omitted.
    """
    skip = skip_dates or frozenset()
    out: list[tuple[str, str]] = []
    for d in expected_dates(date_str, n_days):
        if d in skip:
            continue
        p = _extract_path(d, debug_dir)
        if not p.exists():
            continue
        try:
            md = p.read_text(encoding="utf-8")
        except OSError:
            continue
        outline = extract_titles_outline(md)
        if outline:
            out.append((d, outline))
    return out


def format_prior_report_titles_block(reports: list[tuple[str, str]]) -> str:
    """Wrap title outlines in a single ``<previous_report_titles>`` XML block.

    Same shape as :func:`format_prior_reports_block` but the inner content is
    the ``##`` / ``###`` outline of each day, not the full body. Empty list →
    empty string.
    """
    if not reports:
        return ""
    parts = ["<previous_report_titles>"]
    for d, outline in reports:
        parts.append(f'<report date="{d}">')
        parts.append(outline.rstrip("\n"))
        parts.append("</report>")
    parts.append("</previous_report_titles>")
    return "\n".join(parts) + "\n"
