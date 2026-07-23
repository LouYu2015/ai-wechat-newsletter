"""WeChat contact map: wxid → display nickname.

Two sources, merged per wxid:
- 群昵称 from ``ChatroomMembers`` (only for members who set one) — preferred.
- 微信昵称 from ``contact.nick_name`` — fallback (covers ~all group members
  whose profile WeChat has cached locally, including non-friends).

``by_wxid`` returns the preferred name (group display if any, else 微信昵称,
else the wxid itself). ``variants`` returns every distinct known name for a
wxid — used as alternates by privacy.tokenization and roster.build_roster.
"""

from __future__ import annotations

from wechat_daily import chatroom_members, wechat_db


class ContactMap:
    def __init__(
        self,
        wechat_nicks: dict[str, str],
        group_displays: dict[str, str] | None = None,
    ) -> None:
        self._wechat = wechat_nicks
        self._group = group_displays or {}

    @classmethod
    def from_db(cls, members: chatroom_members.ChatroomMembers | None = None) -> "ContactMap":
        conn = wechat_db.get_conn("contact/contact.db")
        cur = conn.cursor()
        cur.execute(
            "SELECT username, nick_name FROM contact "
            "WHERE nick_name IS NOT NULL AND nick_name != ''"
        )
        wechat = {row[0]: row[1] for row in cur.fetchall()}
        if members is None:
            members = chatroom_members.ChatroomMembers.from_db()
        return cls(wechat, dict(members.items()))

    @classmethod
    def from_dict(
        cls,
        wechat_nicks: dict[str, str],
        group_displays: dict[str, str] | None = None,
    ) -> "ContactMap":
        return cls(dict(wechat_nicks), dict(group_displays or {}))

    def by_wxid(self, wxid: str) -> str:
        """Return preferred display name, or wxid if neither source has one."""
        if wxid in self._group:
            return self._group[wxid]
        return self._wechat.get(wxid, wxid)

    def variants(self, wxid: str) -> list[str]:
        """Return all distinct known names for *wxid* (group first, then 微信)."""
        out: list[str] = []
        seen: set[str] = set()
        for src in (self._group.get(wxid), self._wechat.get(wxid)):
            if not src or src == wxid or src in seen:
                continue
            seen.add(src)
            out.append(src)
        return out

    def all_pairs(self) -> list[tuple[str, str]]:
        """Return every ``(name, wxid)`` across both sources, deduped per wxid.

        Used by privacy._nickname_pairs and _tap_has_optout_party to build a
        substitution / scan pattern. Unsorted; callers may sort by name length
        as needed.
        """
        out: list[tuple[str, str]] = []
        for wxid in set(self._group.keys()) | set(self._wechat.keys()):
            for name in self.variants(wxid):
                out.append((name, wxid))
        return out

    def __contains__(self, wxid: str) -> bool:
        return wxid in self._group or wxid in self._wechat
