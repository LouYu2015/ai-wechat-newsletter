"""Encrypted SQLite connection management (read-only, never writes to disk)."""

import json
import pathlib
import re
import sqlite3
import subprocess
from typing import Any

from wechat_daily import config

_db_conns: dict[str, Any] = {}


def _find_db_storage() -> pathlib.Path | None:
    if not config.WECHAT_DATA_DIR.exists():
        return None
    result = subprocess.run(
        [
            "find",
            str(config.WECHAT_DATA_DIR),
            "-name",
            "db_storage",
            "-type",
            "d",
            "-maxdepth",
            "5",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    first = result.stdout.strip().split("\n")[0]
    return pathlib.Path(first) if first else None


_MESSAGE_SHARD_RE = re.compile(r"message_(\d+)\.db")


def message_db_rels() -> list[str]:
    """当前存在的消息分片相对路径，按分片编号排序。

    微信把群消息按时间分片存进多个数据库（message_0.db, message_1.db, ...），
    写满一片后会滚动新建更高编号的分片继续写。这里直接扫描 db_storage/message/
    目录发现有哪些分片，而不是硬编码固定几个——硬编码曾在 2026-08 导致微信滚动出
    message_2.db 后消息「停更」两天都没被发现（脚本一直在读已不再写入的旧分片）。
    找不到 db_storage（测试环境、未解密）时退回原先的两片默认值。
    """
    db_storage = _find_db_storage()
    if db_storage is not None:
        shards: list[tuple[int, str]] = []
        for p in (db_storage / "message").glob("message_*.db"):
            m = _MESSAGE_SHARD_RE.fullmatch(p.name)
            if m:
                shards.append((int(m.group(1)), p.name))
        if shards:
            return [f"message/{name}" for _, name in sorted(shards)]
    return ["message/message_0.db", "message/message_1.db"]


def get_conn(rel_path: str, cipher_key: str | None = None) -> Any:
    """Return a (cached) DB connection for *rel_path*.

    If *cipher_key* is explicitly provided (including empty string sentinel
    ``""``) the connection is opened with plain sqlite3 — used in tests.
    Pass ``cipher_key=None`` (default) to auto-detect from keys.json.
    """
    cache_key = f"{rel_path}|{cipher_key}"
    if cache_key in _db_conns:
        return _db_conns[cache_key]

    # Test/plain mode: caller supplies cipher_key="" to force plain sqlite3
    if cipher_key == "":
        conn = sqlite3.connect(rel_path)
        _db_conns[cache_key] = conn
        return conn

    # Try encrypted source via sqlcipher3
    keys_file = config.CHATLOG_MAC_DIR / "keys.json"
    if keys_file.exists():
        try:
            import sqlcipher3

            keys = json.loads(keys_file.read_text())
            if rel_path in keys:
                db_storage = _find_db_storage()
                if db_storage:
                    src = db_storage / rel_path
                    if src.exists():
                        enc_key = keys[rel_path]["enc_key"]
                        conn = sqlcipher3.connect(f"file:{src}?immutable=1", uri=True)
                        conn.execute(f"PRAGMA key = \"x'{enc_key}'\"")
                        conn.execute("PRAGMA cipher_page_size = 4096")
                        conn.execute("PRAGMA cipher_compatibility = 4")
                        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                        _db_conns[cache_key] = conn
                        return conn
        except Exception:
            pass

    # Fallback: pre-decrypted file
    dec = config.CHATLOG_DIR / rel_path
    if dec.exists():
        conn = sqlite3.connect(str(dec))
        _db_conns[cache_key] = conn
        return conn

    raise FileNotFoundError(
        f"找不到数据库 {rel_path}。\n"
        "请确保 chatlog-mac/keys.json 存在，或先运行 decrypt_wechat.sh 解密。"
    )


def name2id_map(cur) -> dict[int, str]:
    """Return ``{real_sender_id: wxid}`` from a message db's ``Name2Id`` table.

    Each message db carries its own ``Name2Id`` table mapping the integer
    ``real_sender_id`` column (a rowid) → wxid. This is the authoritative sender
    source: the embedded ``wxid:\\n`` content prefix is absent on the owner's own
    messages. Returns an empty map when the table is missing (e.g. synthetic
    test DBs); callers should treat an unresolved sender as empty.
    """
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Name2Id'")
        if not cur.fetchone():
            return {}
        return {rowid: un for rowid, un in cur.execute("SELECT rowid, user_name FROM Name2Id")}
    except Exception:
        return {}


def clear_cache() -> None:
    """Close and evict all cached connections (useful in tests)."""
    for conn in _db_conns.values():
        try:
            conn.close()
        except Exception:
            pass
    _db_conns.clear()
