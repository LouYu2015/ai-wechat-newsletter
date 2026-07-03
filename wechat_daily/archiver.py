"""7-day rolling archive: move old PDFs into archive/YYYY/MM/ subdirectories."""

from __future__ import annotations

import datetime
import pathlib
import re

from wechat_daily import config


def get_pdf_path(date_str: str) -> pathlib.Path:
    """Return a unique PDF path inside archive/, never overwriting an existing file."""
    config.ARCHIVE_DIR.mkdir(exist_ok=True)
    stem = f"{date_str} 群聊日报"
    path = config.ARCHIVE_DIR / f"{stem}.pdf"
    counter = 2
    while path.exists():
        path = config.ARCHIVE_DIR / f"{stem} ({counter}).pdf"
        counter += 1
    return path


def archive_old_files() -> int:
    """Move PDFs older than 7 days from archive/ into archive/YYYY/MM/ subdirs.

    Returns the number of files moved.
    """
    if not config.ARCHIVE_DIR.exists():
        return 0

    cutoff = (datetime.datetime.now() - datetime.timedelta(days=7)).date()
    moved = 0

    for pdf in sorted(config.ARCHIVE_DIR.glob("*.pdf")):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\b", pdf.stem)
        if not m:
            continue
        file_date = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").date()
        if file_date >= cutoff:
            continue

        dest_dir = config.ARCHIVE_DIR / file_date.strftime("%Y") / file_date.strftime("%m")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / pdf.name
        if dest.exists():
            counter = 2
            while dest.exists():
                dest = dest_dir / f"{pdf.stem} ({counter}){pdf.suffix}"
                counter += 1
        pdf.rename(dest)
        moved += 1

    return moved
