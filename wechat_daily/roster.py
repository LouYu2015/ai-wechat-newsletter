"""Build the 群友花名册 (roster): a token → known-real-name-variants table
that we hand to Claude alongside each day's tokenized chat.

The roster lets the LLM resolve informal references (谐音、外号、缩写) that
slip through ``privacy._replace_names`` because they aren't registered
nicknames. Excludes opted-out users — their real names should never reach
the model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wechat_daily.aliases import AliasDB
    from wechat_daily.contacts import ContactMap
    from wechat_daily.privacy import TokenMap


def build_roster(
    token_map: "TokenMap",
    contact_map: "ContactMap",
    alias_db: "AliasDB",
) -> list[tuple[str, list[str]]]:
    """Return ``[(token, [variant, ...]), ...]`` sorted by token.

    For each wxid known to ``token_map``, collect its WeChat nickname and last
    seen group display name. Drops opt-out users entirely. Drops entries with
    no usable variants.
    """
    entries: list[tuple[str, list[str]]] = []
    for token in token_map.all_tokens():
        wxid = token_map.wxid(token)
        if not wxid or alias_db.is_optout(wxid):
            continue

        variants = list(contact_map.variants(wxid))
        # Splice in alias_db.real_name_seen as a tertiary source if it's a
        # value the contact map didn't already cover (rare — kept so manually
        # logged display names don't silently disappear).
        seen_extra = alias_db.real_name_seen(wxid)
        if seen_extra and seen_extra not in variants:
            variants.append(seen_extra)

        if not variants:
            continue
        entries.append((token, variants))

    entries.sort(key=lambda e: e[0])
    return entries


def format_roster(entries: list[tuple[str, list[str]]]) -> str:
    """Render the roster as a Markdown block to prepend to the LLM input."""
    if not entries:
        return ""
    lines = [
        "## 群友花名册（用于解析消息中的代称、谐音、缩写）",
        "",
        "下表列出每个 token 对应的真实昵称与已知群昵称变体。聊天记录中可能",
        "出现未列入花名册的代称（外号、谐音、缩写），请基于上下文与本表推断",
        "对应 token。**输出摘要时只能使用 token，绝不可保留任何真实昵称或代称。**",
        "",
    ]
    for token, variants in entries:
        lines.append(f"- {token}：{'、'.join(variants)}")
    return "\n".join(lines)
