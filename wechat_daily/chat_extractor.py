"""Extract and format chat messages for a given date."""

from __future__ import annotations

import datetime
import re
import sys

from wechat_daily import config, contacts, coverage, message_parser, wechat_db


_MESSAGE_SHARD_RE = re.compile(r"message_(\d+)\.db")


def _db_rels() -> list[str]:
    """当前存在的消息分片相对路径，按分片编号排序。

    微信把群消息按时间分片存进多个数据库（message_0.db, message_1.db, ...），
    写满一片后会滚动新建更高编号的分片继续写。这里直接扫描 db_storage/message/
    目录发现有哪些分片，而不是硬编码固定几个——硬编码曾在 2026-08 导致微信滚动出
    message_2.db 后消息「停更」两天都没被发现（脚本一直在读已不再写入的旧分片）。
    找不到 db_storage（测试环境、未解密）时退回原先的两片默认值。
    """
    db_storage = wechat_db._find_db_storage()
    if db_storage is not None:
        shards: list[tuple[int, str]] = []
        for p in (db_storage / "message").glob("message_*.db"):
            m = _MESSAGE_SHARD_RE.fullmatch(p.name)
            if m:
                shards.append((int(m.group(1)), p.name))
        if shards:
            return [f"message/{name}" for _, name in sorted(shards)]
    return ["message/message_0.db", "message/message_1.db"]


def _nth_recent_ts(anchor_ts: int, n: int = config.OVERLAP_MIN_MESSAGES) -> int | None:
    """跨两个消息库，返回 create_time <= *anchor_ts* 的倒数第 *n* 条消息的 create_time。

    不足 n 条时返回最旧的那条；一条都没有返回 None。用来把重叠窗口起点从固定的
    -1h 往前延伸到「至少 n 条消息」，避免安静的夜晚重叠段消息太少接不上跨日话题。
    容错写法与 extract_messages 一致：库缺失/表缺失跳过、损坏库 warn 跳过。
    """
    candidates: list[int] = []
    for rel in _db_rels():
        try:
            conn = wechat_db.get_conn(rel)
            cur = conn.cursor()
            cur.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{config.GROUP_TABLE}'"
            )
            if not cur.fetchone():
                continue
            cur.execute(
                f"SELECT create_time FROM {config.GROUP_TABLE} "
                f"WHERE create_time <= ? ORDER BY create_time DESC LIMIT ?",
                (anchor_ts, n),
            )
            candidates.extend(ct for (ct,) in cur.fetchall())
        except FileNotFoundError:
            continue
        except Exception as e:
            import warnings

            warnings.warn(f"[chat_extractor] 跳过损坏数据库 {rel}: {e}")
            continue
    if not candidates:
        return None
    # 每库各取了自己最新的 n 条，全局第 n 新必在这些候选里；从新到旧取第 n 条
    # （不足 n 条则取最旧的）。
    candidates.sort(reverse=True)
    return candidates[min(n, len(candidates)) - 1]


def extract_messages(
    date_str: str, contact_map: contacts.ContactMap | None = None
) -> list[message_parser.Message]:
    """Return raw Message objects for *date_str* (YYYY-MM-DD).

    每日消息以 ``config.DAY_CUTOFF_HOUR``（21:00）分界而非午夜：日报「D」覆盖
    [D-1 21:00, D 21:00)，21:00 之后的消息算作次日。结尾窗口固定为「D 21:00+1h」；
    起点则是「D-1 21:00−1h」和「重叠段倒数第 20 条」里更早的那个
    （``config.OVERLAP_MIN_MESSAGES``），并钳在前一天的窗口起点（D-2 21:00）不再
    往前。重叠段的锚点优先用前一天日报的覆盖水位线（``coverage.last_covered_ts``）
    ——若昨天按时（21:00）生成，今天就从「21:00 往前数 20 条」开始，把昨天 21:00
    之后从未被报道的尾巴全部纳入今天的输入；缺覆盖记录时锚点退回 D-1 21:00，等价
    于「假设昨天覆盖到了自己的截止点」。重叠因此 ≥ max(1 小时, 20 条)。
    """
    if contact_map is None:
        contact_map = contacts.ContactMap.from_db()

    date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    prev_day = date - datetime.timedelta(days=1)
    cutoff = datetime.timedelta(hours=config.DAY_CUTOFF_HOUR)
    day_start = prev_day + cutoff  # D-1 21:00：今天窗口的起点（不含 buffer）
    day_end = date + cutoff  # D 21:00：今天窗口的终点（不含 buffer）
    default_start = int((day_start - datetime.timedelta(hours=1)).timestamp())

    anchor_ts = coverage.last_covered_ts(prev_day.strftime("%Y-%m-%d"))
    if anchor_ts is None:
        anchor_ts = int(day_start.timestamp())
    candidate = _nth_recent_ts(anchor_ts)
    start_ts = default_start if candidate is None else min(default_start, candidate)
    # 回溯上限：不早于前一天的窗口起点（D-2 21:00）——更早的内容 previous_reports
    # 已覆盖，再往前拉只会白白重复（例如 20 条稀疏到跨进大前天时，钳在这里）。
    start_ts = max(start_ts, int((day_start - datetime.timedelta(days=1)).timestamp()))
    end_ts = int((day_end + datetime.timedelta(hours=1)).timestamp())

    # Each message DB carries its own Name2Id table mapping the integer
    # ``real_sender_id`` column → wxid. This is the authoritative sender source
    # (the embedded ``wxid:\n`` content prefix is missing on the owner's own
    # messages, which is why they used to vanish). Resolve per-db before merging.
    rows: list[tuple] = []
    for rel in _db_rels():
        try:
            conn = wechat_db.get_conn(rel)
            cur = conn.cursor()
            cur.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{config.GROUP_TABLE}'"
            )
            if not cur.fetchone():
                continue
            id2wxid = wechat_db.name2id_map(cur)
            cur.execute(
                f"SELECT create_time, local_type, message_content, local_id, server_id, "
                f"real_sender_id "
                f"FROM {config.GROUP_TABLE} "
                f"WHERE create_time >= ? AND create_time < ? ORDER BY create_time",
                (start_ts, end_ts),
            )
            for ct, lt, mc, lid, sid, rsid in cur.fetchall():
                rows.append((ct, lt, mc, lid, sid, id2wxid.get(rsid, "")))
        except FileNotFoundError:
            continue
        except Exception as e:
            import warnings

            warnings.warn(f"[chat_extractor] 跳过损坏数据库 {rel}: {e}")
            continue

    rows.sort(key=lambda x: x[0])

    messages: list[message_parser.Message] = []
    image_keys: list[tuple[message_parser.Message, int, int]] = []
    for create_time, local_type, message_content, local_id, server_id, sender_wxid in rows:
        msg = message_parser.parse_row(create_time, local_type, message_content, sender_wxid)
        if msg is None:
            continue
        if (
            msg.local_type == message_parser.MSG_IMAGE
            and local_id is not None
            and server_id is not None
        ):
            image_keys.append((msg, local_id, server_id))
        messages.append(msg)

    if image_keys:
        _fill_image_md5s(image_keys)
    return messages


def _fill_image_md5s(image_keys: list[tuple[message_parser.Message, int, int]]) -> None:
    """Resolve `image_md5` (= .dat filename) via message_resource.db.

    Mutates each Message in-place. Silently skips on any DB error so a missing
    `message_resource.db` just means no inline images.
    """
    sys.path.insert(0, str(config.CHATLOG_MAC_DIR))
    try:
        from decode_image import extract_md5_from_packed_info
    except ImportError:
        return

    try:
        conn = wechat_db.get_conn("message/message_resource.db")
    except FileNotFoundError:
        return

    cur = conn.cursor()
    for msg, local_id, server_id in image_keys:
        try:
            row = cur.execute(
                "SELECT packed_info FROM MessageResourceInfo "
                "WHERE message_local_id = ? AND message_svr_id = ?",
                (local_id, server_id),
            ).fetchone()
        except Exception:
            continue
        if row and row[0]:
            md5 = extract_md5_from_packed_info(row[0])
            if md5:
                msg.image_md5 = md5.lower()


def format_messages(
    messages: list[message_parser.Message], contact_map: contacts.ContactMap
) -> str:
    """Format a list of Message objects into chat history text (current behaviour)."""
    lines: list[str] = []
    for msg in messages:
        ts = datetime.datetime.fromtimestamp(msg.create_time).strftime("%H:%M")

        if msg.local_type == message_parser.MSG_TAP:
            lines.append(f"[{ts}] {msg.content}")
            continue

        if msg.local_type == message_parser.MSG_SYSTEM:
            lines.append(f"[{ts}] [系统] {msg.content}")
            continue

        name = contact_map.by_wxid(msg.sender_wxid) if msg.sender_wxid else ""
        if not name:
            continue

        line = f"[{ts}] {name}: {msg.content}"
        if msg.quoted:
            line += f"\n  > 引用 {msg.quoted.content}"
        lines.append(line)

    return "\n".join(lines)


def extract_chat_from_db(date_str: str) -> str:
    """Top-level convenience: extract + format for *date_str*."""
    contact_map = contacts.ContactMap.from_db()
    messages = extract_messages(date_str, contact_map)
    return format_messages(messages, contact_map)


def _cutoff_day(dt: datetime.datetime) -> datetime.date:
    """*dt* 所属的截止日：以 ``config.DAY_CUTOFF_HOUR`` 为界，[D-1 21:00, D 21:00)
    记为 D。用于把一个原始时间戳换算成它落在哪一天的日报窗口里。
    """
    boundary = dt.replace(
        hour=config.DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0
    )
    return dt.date() if dt < boundary else dt.date() + datetime.timedelta(days=1)


def find_missing_dates(allow_incomplete: bool = False) -> list[str]:
    """Return sorted list of dates (YYYY-MM-DD) that lack an archive PDF."""
    existing: set[str] = set()
    if config.ARCHIVE_DIR.exists():
        for pdf in config.ARCHIVE_DIR.rglob("*.pdf"):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\b", pdf.stem)
            if m:
                existing.add(m.group(1))

    last_ts = 0
    for rel in _db_rels():
        try:
            conn = wechat_db.get_conn(rel)
        except FileNotFoundError:
            continue
        cur = conn.cursor()
        cur.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{config.GROUP_TABLE}'"
        )
        if not cur.fetchone():
            continue
        cur.execute(f"SELECT MAX(create_time) FROM {config.GROUP_TABLE}")
        row = cur.fetchone()
        if row and row[0]:
            last_ts = max(last_ts, row[0])

    if not last_ts:
        return []

    last_dt = datetime.datetime.fromtimestamp(last_ts)
    # in_progress：last_dt 减去 1 小时 buffer 之后所属的截止日——即当前正在累积、
    # 尚未确认完整的那一天。last_complete 是它的前一天（buffer 已过，确认完整）。
    in_progress = _cutoff_day(last_dt - datetime.timedelta(hours=1))
    last_complete = in_progress - datetime.timedelta(days=1)
    if allow_incomplete:
        # buffer zone（截止时刻前后 1 小时）内也推进到进行中的这一天
        last_complete = max(last_complete, in_progress)

    if not existing:
        return [last_complete.strftime("%Y-%m-%d")]

    max_archive = datetime.datetime.strptime(max(existing), "%Y-%m-%d").date()
    missing: list[str] = []
    current = max_archive + datetime.timedelta(days=1)
    while current <= last_complete:
        date_str = current.strftime("%Y-%m-%d")
        if date_str not in existing:
            missing.append(date_str)
        current += datetime.timedelta(days=1)
    return missing
