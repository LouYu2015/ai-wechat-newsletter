"""Load recent days' raw LLM markdown so today's run can reference / continue them.

The model writes its raw output (with tokens like 「沉稳的大象」 still in
place) to ``debug/extract-{YYYY-MM-DD}.md``. That file is the right input to
re-feed: it uses the same tokens the next run will use, and it contains the
section structure that today's report may want to cross-reference via the
``[[ref:YYYY-MM-DD|章节标题]]`` placeholder.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .config import DEBUG_DIR


def _extract_path(date_str: str, debug_dir: Path | None = None) -> Path:
    return (debug_dir or DEBUG_DIR) / f"extract-{date_str}.md"


def expected_dates(date_str: str, n_days: int) -> list[str]:
    """Return [date-n, …, date-1] in ascending order."""
    base = datetime.strptime(date_str, "%Y-%m-%d").date()
    return [
        (base - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(n_days, 0, -1)
    ]


def load_prior_reports(
    date_str: str,
    n_days: int = 3,
    debug_dir: Path | None = None,
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
            out.append((d, md))
    return out


def missing_prior_dates(
    date_str: str,
    n_days: int = 3,
    debug_dir: Path | None = None,
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
