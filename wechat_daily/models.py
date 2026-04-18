"""Shared data models for structured daily reports.

Used by both llm_extractor (producer) and renderer (consumer).
"""

from __future__ import annotations

from typing import Literal


SectionType = Literal["news", "tool", "methodology", "anecdote"]

VALID_SECTION_TYPES = {"news", "tool", "methodology", "anecdote"}


class Comment:
    def __init__(self, token: str, text: str) -> None:
        self.token = token
        self.text = text


class Section:
    def __init__(
        self,
        section_type: str,
        title: str,
        body: str,
        comments: list[Comment],
        tags: list[str],
        public_safe: bool,
        public_safe_reason: str | None,
    ) -> None:
        if section_type not in VALID_SECTION_TYPES:
            raise ValueError(f"无效的 section type: {section_type!r}，必须是 {VALID_SECTION_TYPES}")
        if not isinstance(public_safe, bool):
            raise TypeError(f"public_safe 必须是 bool，got {type(public_safe).__name__}")
        self.type = section_type
        self.title = title
        self.body = body
        self.comments = comments
        self.tags = tags
        self.public_safe = public_safe
        self.public_safe_reason = public_safe_reason

    @classmethod
    def from_dict(cls, d: dict) -> "Section":
        comments = [Comment(c['token'], c['text']) for c in d.get('comments', [])]
        return cls(
            section_type=d['type'],
            title=d['title'],
            body=d['body'],
            comments=comments,
            tags=d.get('tags', []),
            public_safe=bool(d['public_safe']),
            public_safe_reason=d.get('public_safe_reason') or None,
        )


class DailyReport:
    def __init__(self, date: str, intro: str, sections: list[Section]) -> None:
        self.date = date
        self.intro = intro
        self.sections = sections

    @classmethod
    def from_dict(cls, d: dict) -> "DailyReport":
        sections = [Section.from_dict(s) for s in d.get('sections', [])]
        return cls(date=d['date'], intro=d['intro'], sections=sections)
