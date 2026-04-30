"""Daily report data model.

Holds the model's raw markdown output plus the date. Section/comment structure
lives entirely inside the markdown — the renderer parses it.
"""

from __future__ import annotations


class DailyReport:
    def __init__(self, date: str, markdown: str) -> None:
        self.date = date
        self.markdown = markdown
