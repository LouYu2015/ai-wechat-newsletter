"""WeChat contact map: wxid → display nickname."""

from __future__ import annotations

from .wechat_db import get_conn


class ContactMap:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    @classmethod
    def from_db(cls) -> "ContactMap":
        conn = get_conn("contact/contact.db")
        cur = conn.cursor()
        cur.execute(
            "SELECT username, nick_name FROM contact "
            "WHERE nick_name IS NOT NULL AND nick_name != ''"
        )
        return cls({row[0]: row[1] for row in cur.fetchall()})

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "ContactMap":
        return cls(data)

    def by_wxid(self, wxid: str) -> str:
        """Return nickname for *wxid*, falling back to the wxid itself."""
        return self._data.get(wxid, wxid)

    def all_nicknames(self) -> list[str]:
        """Return all known nicknames, longest first (for safe substring replacement)."""
        return sorted(self._data.values(), key=len, reverse=True)

    def wxid_for_nickname(self, nickname: str) -> str | None:
        for wxid, name in self._data.items():
            if name == nickname:
                return wxid
        return None

    def __contains__(self, wxid: str) -> bool:
        return wxid in self._data
