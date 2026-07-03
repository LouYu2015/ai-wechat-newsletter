"""Rebuild aliases.json from scratch by replaying all historical commands.

Usage: python -m scripts.rebuild_aliases
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from wechat_daily.aliases import AliasDB
from wechat_daily.config import ALIASES_CURSOR_FILE, ALIASES_FILE
from wechat_daily.contacts import ContactMap


def rebuild() -> None:
    print("正在从零重建 aliases.json...")

    # Load existing aliases to preserve cached default_anon values
    existing: dict[str, dict] = {}
    if ALIASES_FILE.exists():
        import json
        try:
            data = json.loads(ALIASES_FILE.read_text(encoding='utf-8'))
            existing = data.get('users', {})
        except Exception:
            print("[警告] 现有 aliases.json 解析失败，将从头建立")

    try:
        contact_map = ContactMap.from_db()
    except Exception as e:
        print(f"[错误] 无法读取联系人数据库: {e}")
        sys.exit(1)

    # Reset cursor to scan all history
    if ALIASES_CURSOR_FILE.exists():
        ALIASES_CURSOR_FILE.write_text("0")

    # Load DB and preserve existing default_anon values
    db = AliasDB.load()
    for wxid, user in existing.items():
        if wxid not in db._users:
            db._users[wxid] = user
        else:
            # Preserve cached default_anon
            if 'default_anon' in user:
                db._users[wxid]['default_anon'] = user['default_anon']

    log = db.scan_commands(contact_map)
    db.save()

    print(f"完成。扫描到 {len(log)} 条指令，已保存到 aliases.json")
    for entry in log:
        from datetime import datetime
        ts_str = datetime.fromtimestamp(entry['ts']).strftime('%Y-%m-%d %H:%M')
        mark = "✓" if entry['ok'] else "✗"
        print(f"  {ts_str}  {entry['wxid']}  {entry['cmd']}  → {entry['msg']} {mark}")


if __name__ == "__main__":
    rebuild()
