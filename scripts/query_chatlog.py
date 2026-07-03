#!/usr/bin/env python3
"""查询群聊记录的 helper script。

支持：
  1. 选择时间范围（可选，--since / --until）
  2. 关键词过滤（可选，--keyword），命中可附带前后上下文（--context N）
  3. 消息数量上限（--limit，默认 20，取最新 N 条）
  4. 解码图片并存到临时目录（--decode-images），文本中嵌入图片路径

输出纯文本聊天记录到 stdout；运行信息（命中数、图片目录等）打印到 stderr。

发送者与正文里 @提及的他人都会按项目的匿名机制（tokenize_messages）替换成
匿名别名（有公开别名优先用别名）；optout 用户的消息按日报逻辑隐藏/合并占位。

用法示例：
    python scripts/query_chatlog.py --since 2026-06-01 --until 2026-06-05
    python scripts/query_chatlog.py --keyword 显卡 --context 2 --limit 50
    python scripts/query_chatlog.py --since "2026-06-04 18:00" --decode-images
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from wechat_daily.aliases import AliasDB
from wechat_daily.chat_extractor import _fill_image_md5s
from wechat_daily.config import GROUP_TABLE
from wechat_daily.contacts import ContactMap
from wechat_daily.message_parser import (
    MSG_IMAGE,
    MSG_SYSTEM,
    MSG_TAP,
    Message,
    parse_row,
)
from wechat_daily.privacy import tokenize_messages
from wechat_daily.wechat_db import get_conn, name2id_map

DB_RELS = ["message/message_0.db", "message/message_1.db"]


# ── Time parsing ──────────────────────────────────────────────────────────────


def parse_when(s: str, *, is_end: bool) -> int:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM[:SS]' into a Unix timestamp.

    Date-only `--until` is treated as inclusive: it advances to the next
    midnight so the whole day is covered.
    """
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            pass
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"无法解析时间：{s!r}（请用 YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'）")
    if is_end:
        dt += timedelta(days=1)
    return int(dt.timestamp())


# ── DB query ──────────────────────────────────────────────────────────────────


def fetch_messages(
    start_ts: int | None,
    end_ts: int | None,
    sql_limit: int | None,
) -> list[Message]:
    """Fetch + parse messages from GROUP_TABLE across both message DBs.

    When *sql_limit* is given, each DB returns its newest rows (DESC LIMIT);
    the merged result is re-sorted ascending. With a keyword the caller passes
    sql_limit=None so every in-range message is available for matching.
    """
    raw_rows: list[tuple] = []
    for rel in DB_RELS:
        try:
            conn = get_conn(rel)
        except FileNotFoundError:
            continue
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (GROUP_TABLE,),
        )
        if not cur.fetchone():
            continue

        clauses: list[str] = []
        params: list[int] = []
        if start_ts is not None:
            clauses.append("create_time >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("create_time < ?")
            params.append(end_ts)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if sql_limit else "ASC"
        sql = (
            f"SELECT create_time, local_type, message_content, local_id, server_id, "
            f"real_sender_id "
            f"FROM {GROUP_TABLE}{where} ORDER BY create_time {order}"
        )
        if sql_limit:
            sql += " LIMIT ?"
            # pad: parse_row drops some rows (empty/unknown types); fetch a few
            # extra so the final slice can still reach the requested count.
            params.append(sql_limit + 20)
        try:
            # Each DB carries its own Name2Id table mapping the integer
            # real_sender_id → wxid (authoritative sender, see chat_extractor).
            # Resolve per-db before merging.
            id2wxid = name2id_map(cur)
            cur.execute(sql, params)
            for ct, lt, mc, lid, sid, rsid in cur.fetchall():
                raw_rows.append((ct, lt, mc, lid, sid, id2wxid.get(rsid, "")))
        except Exception as e:
            import warnings

            warnings.warn(f"[query_chatlog] 跳过损坏数据库 {rel}: {e}")
            continue

    messages: list[Message] = []
    image_keys: list[tuple[Message, int, int]] = []
    for create_time, local_type, content, local_id, server_id, sender_wxid in raw_rows:
        msg = parse_row(create_time, local_type, content, sender_wxid)
        if msg is None:
            continue
        if msg.local_type == MSG_IMAGE and local_id is not None and server_id is not None:
            image_keys.append((msg, local_id, server_id))
        messages.append(msg)

    if image_keys:
        _fill_image_md5s(image_keys)

    messages.sort(key=lambda m: m.create_time)
    return messages


# ── Keyword filtering ───────────────────────────────────────────────────────────


def _matches(msg: Message, kw_lower: str) -> bool:
    if msg.content and kw_lower in msg.content.lower():
        return True
    if msg.quoted and msg.quoted.content and kw_lower in msg.quoted.content.lower():
        return True
    return False


def select_messages(
    messages: list[Message],
    keyword: str | None,
    context: int,
    limit: int | None,
) -> list[Message]:
    """Apply keyword + context + limit. Returns ascending-by-time subset.

    Keyword matching runs on the *original* (pre-anonymization) text so a name
    search still works. With a keyword, `limit` caps the number of matched
    messages (newest N); each match is then expanded by ±context neighbours.
    Without a keyword, `limit` simply keeps the newest N overall.
    """
    if keyword:
        kw_lower = keyword.lower()
        match_idx = [i for i, m in enumerate(messages) if _matches(m, kw_lower)]
        if limit:
            match_idx = match_idx[-limit:]
        keep: set[int] = set()
        for i in match_idx:
            lo = max(0, i - context)
            hi = min(len(messages), i + context + 1)
            keep.update(range(lo, hi))
        return [messages[i] for i in sorted(keep)]

    if limit:
        return messages[-limit:]
    return messages


# ── Formatting ────────────────────────────────────────────────────────────────


def _is_hidden_placeholder(msg: Message) -> bool:
    return (
        not msg.sender_wxid
        and bool(msg.content)
        and msg.content.startswith("[")
        and "已隐藏]" in msg.content
    )


def build_formatter(token_map, alias_db, decoder):
    """Return a `format(msg) -> str | None` closure.

    Maps tokens (default_anon) → public display name and strips the
    `token⟨原文⟩` disambiguation markers that `tokenize_messages` leaves for
    LLM consumption (irrelevant for human-readable output).
    """
    token_to_public: dict[str, str] = {}
    for tok in token_map.all_tokens():
        wxid = token_map.wxid(tok)
        token_to_public[tok] = alias_db.public_name_of(wxid) if wxid else tok

    toks = sorted(token_to_public, key=len, reverse=True)
    mention_re = (
        re.compile("(" + "|".join(re.escape(t) for t in toks) + r")⟨([^⟩]*)⟩") if toks else None
    )

    def demark(text: str) -> str:
        """Resolve ``token⟨原文⟩`` markers to ``公开别名⟨原文⟩``.

        Unlike the daily pipeline (where an LLM picks one side and leak_check
        strips the ⟨…⟩), this human-facing tool keeps BOTH: the public alias
        for genuine person references, plus the original substring so that
        false-positive matches — a contact whose 2-char nickname happens to be
        a common word fragment like 「都搞」—— stay readable instead of being
        silently garbled into someone's alias.
        """
        if not text:
            return text
        if mention_re:
            text = mention_re.sub(lambda m: f"{token_to_public[m.group(1)]}⟨{m.group(2)}⟩", text)
        return text

    def fmt(msg: Message) -> str | None:
        ts = datetime.fromtimestamp(msg.create_time).strftime("%Y-%m-%d %H:%M")

        if msg.local_type == MSG_TAP:
            return f"[{ts}] {demark(msg.content)}"
        if msg.local_type == MSG_SYSTEM:
            return f"[{ts}] [系统] {demark(msg.content)}"
        if _is_hidden_placeholder(msg):
            return msg.content  # already carries its own [HH:MM] prefix
        if not msg.sender_wxid:
            return None

        display = token_to_public.get(msg.sender_wxid, msg.sender_wxid)

        if msg.local_type == MSG_IMAGE:
            if decoder is not None and msg.image_md5:
                try:
                    jpeg = decoder.decode(msg.image_md5)
                except Exception as e:  # one bad image shouldn't kill the query
                    jpeg = None
                    print(
                        f"[query_chatlog] 图片解码出错 md5={msg.image_md5}: {e}",
                        file=sys.stderr,
                    )
                content = (
                    f"[图片: {jpeg}]"
                    if jpeg is not None
                    else f"[图片: 解码失败 md5={msg.image_md5}]"
                )
            else:
                content = demark(msg.content)
        else:
            content = demark(msg.content)

        line = f"[{ts}] {display}: {content}"
        if msg.quoted and msg.quoted.content:
            line += f"\n  > 引用 {demark(msg.quoted.content)}"
        return line

    return fmt


# ── Main ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="查询群聊记录（匿名化纯文本输出，可选解码图片）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--since",
        metavar="TIME",
        help="起始时间（含），YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'；不填则不限",
    )
    parser.add_argument(
        "--until",
        metavar="TIME",
        help="结束时间，YYYY-MM-DD（含当天）或 'YYYY-MM-DD HH:MM'；不填则不限",
    )
    parser.add_argument(
        "--keyword",
        metavar="KW",
        help="关键词，对消息正文/引用做大小写不敏感子串匹配",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=0,
        metavar="N",
        help="关键词命中时附带前后各 N 条消息（默认 0）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="消息数量上限，取最新 N 条（默认 20；0 表示不限）。有关键词时上限作用于命中消息数",
    )
    parser.add_argument(
        "--decode-images",
        action="store_true",
        help="解码图片到临时目录，并在文本中嵌入图片路径",
    )
    parser.add_argument(
        "--image-dir",
        metavar="DIR",
        help="图片输出目录（默认自动创建临时目录）；隐含 --decode-images",
    )
    args = parser.parse_args()

    start_ts = parse_when(args.since, is_end=False) if args.since else None
    end_ts = parse_when(args.until, is_end=True) if args.until else None
    if start_ts is not None and end_ts is not None and start_ts >= end_ts:
        raise SystemExit("--since 必须早于 --until")

    limit = args.limit if args.limit and args.limit > 0 else None
    context = max(0, args.context)

    # 1. 查询 + 解析（无关键词时直接用 SQL LIMIT 取最新 N 条）
    messages = fetch_messages(start_ts, end_ts, sql_limit=None if args.keyword else limit)

    # 2. 关键词 / 上下文 / 数量过滤
    selected = select_messages(messages, args.keyword, context, limit)
    if not selected:
        print("（没有匹配的消息）", file=sys.stderr)
        return

    # 3. 匿名化（发送者 + 正文提及；隐藏 optout 用户）
    contact_map = ContactMap.from_db()
    alias_db = AliasDB.load()
    tok_messages, token_map = tokenize_messages(selected, contact_map, alias_db)

    # 4. 可选：图片解码器
    decoder = None
    image_dir: Path | None = None
    if args.decode_images or args.image_dir:
        from wechat_daily.image_decoder import ImageDecoder

        image_dir = (
            Path(args.image_dir).expanduser()
            if args.image_dir
            else Path(tempfile.mkdtemp(prefix="chatlog_imgs_"))
        )
        decoder = ImageDecoder(image_dir)

    # 5. 格式化输出
    fmt = build_formatter(token_map, alias_db, decoder)
    lines = [line for msg in tok_messages if (line := fmt(msg)) is not None]
    print("\n".join(lines))

    # 运行信息 → stderr
    rng = []
    if start_ts is not None:
        rng.append(datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M"))
    rng.append("…")
    if end_ts is not None:
        rng.append(datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M"))
    print(
        f"\n[query_chatlog] 输出 {len(lines)} 行 / 选中 {len(selected)} 条消息"
        + (f"；关键词={args.keyword!r}（前后各 {context} 条）" if args.keyword else "")
        + f"；时间范围 {' '.join(rng)}",
        file=sys.stderr,
    )
    if image_dir is not None:
        print(f"[query_chatlog] 图片目录：{image_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
