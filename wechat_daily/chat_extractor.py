"""Extract and format chat messages for a given date."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta

from .config import CHATLOG_MAC_DIR, GROUP_TABLE, ARCHIVE_DIR
from .contacts import ContactMap
from .message_parser import MSG_IMAGE, MSG_SYSTEM, MSG_TAP, Message, parse_row
from .wechat_db import get_conn


def _db_rels() -> list[str]:
    return ["message/message_0.db", "message/message_1.db"]


def extract_messages(date_str: str, contact_map: ContactMap | None = None) -> list[Message]:
    """Return raw Message objects for *date_str* (YYYY-MM-DD), window ±1h."""
    if contact_map is None:
        contact_map = ContactMap.from_db()

    date = datetime.strptime(date_str, '%Y-%m-%d')
    start_ts = int((date - timedelta(hours=1)).timestamp())
    end_ts = int((date + timedelta(days=1, hours=1)).timestamp())

    rows: list[tuple] = []
    for rel in _db_rels():
        try:
            conn = get_conn(rel)
            cur = conn.cursor()
            cur.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{GROUP_TABLE}'"
            )
            if not cur.fetchone():
                continue
            cur.execute(
                f"SELECT create_time, local_type, message_content, local_id, server_id "
                f"FROM {GROUP_TABLE} "
                f"WHERE create_time >= ? AND create_time < ? ORDER BY create_time",
                (start_ts, end_ts),
            )
            rows.extend(cur.fetchall())
        except FileNotFoundError:
            continue
        except Exception as e:
            import warnings
            warnings.warn(f"[chat_extractor] 跳过损坏数据库 {rel}: {e}")
            continue

    rows.sort(key=lambda x: x[0])

    messages: list[Message] = []
    image_keys: list[tuple[Message, int, int]] = []
    for create_time, local_type, message_content, local_id, server_id in rows:
        msg = parse_row(create_time, local_type, message_content)
        if msg is None:
            continue
        if msg.local_type == MSG_IMAGE and local_id is not None and server_id is not None:
            image_keys.append((msg, local_id, server_id))
        messages.append(msg)

    if image_keys:
        _fill_image_md5s(image_keys)
    return messages


def _fill_image_md5s(image_keys: list[tuple[Message, int, int]]) -> None:
    """Resolve `image_md5` (= .dat filename) via message_resource.db.

    Mutates each Message in-place. Silently skips on any DB error so a missing
    `message_resource.db` just means no inline images.
    """
    sys.path.insert(0, str(CHATLOG_MAC_DIR))
    try:
        from decode_image import extract_md5_from_packed_info
    except ImportError:
        return

    try:
        conn = get_conn("message/message_resource.db")
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


def format_messages(messages: list[Message], contact_map: ContactMap) -> str:
    """Format a list of Message objects into chat history text (current behaviour)."""
    lines: list[str] = []
    for msg in messages:
        ts = datetime.fromtimestamp(msg.create_time).strftime('%H:%M')

        if msg.local_type == MSG_TAP:
            lines.append(f"[{ts}] {msg.content}")
            continue

        if msg.local_type == MSG_SYSTEM:
            lines.append(f"[{ts}] [系统] {msg.content}")
            continue

        name = contact_map.by_wxid(msg.sender_wxid) if msg.sender_wxid else ''
        if not name:
            continue

        line = f"[{ts}] {name}: {msg.content}"
        if msg.quoted:
            line += f"\n  > 引用 {msg.quoted.content}"
        lines.append(line)

    return '\n'.join(lines)


def extract_chat_from_db(date_str: str) -> str:
    """Top-level convenience: extract + format for *date_str*."""
    contact_map = ContactMap.from_db()
    messages = extract_messages(date_str, contact_map)
    return format_messages(messages, contact_map)


def find_missing_dates(allow_incomplete: bool = False) -> list[str]:
    """Return sorted list of dates (YYYY-MM-DD) that lack an archive PDF."""
    existing: set[str] = set()
    if ARCHIVE_DIR.exists():
        for pdf in ARCHIVE_DIR.rglob("*.pdf"):
            m = re.match(r'^(\d{4}-\d{2}-\d{2})\b', pdf.stem)
            if m:
                existing.add(m.group(1))

    last_ts = 0
    for rel in _db_rels():
        try:
            conn = get_conn(rel)
        except FileNotFoundError:
            continue
        cur = conn.cursor()
        cur.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{GROUP_TABLE}'"
        )
        if not cur.fetchone():
            continue
        cur.execute(f"SELECT MAX(create_time) FROM {GROUP_TABLE}")
        row = cur.fetchone()
        if row and row[0]:
            last_ts = max(last_ts, row[0])

    if not last_ts:
        return []

    last_dt = datetime.fromtimestamp(last_ts)
    last_complete = (last_dt - timedelta(hours=1)).date() - timedelta(days=1)
    if allow_incomplete:
        # buffer zone（午夜前后 1 小时）内不推进到新一天
        last_complete = max(last_complete, (last_dt - timedelta(hours=1)).date())

    if not existing:
        return [last_complete.strftime('%Y-%m-%d')]

    max_archive = datetime.strptime(max(existing), '%Y-%m-%d').date()
    missing: list[str] = []
    current = max_archive + timedelta(days=1)
    while current <= last_complete:
        date_str = current.strftime('%Y-%m-%d')
        if date_str not in existing:
            missing.append(date_str)
        current += timedelta(days=1)
    return missing
