"""一次性迁移：把所有 default_anon 重新分配为新格式 `{形容词}的{动物}`（无数字后缀）。

背景（2026-04-23）：
原 token 格式 `{形容词}的{动物}NN`（带 2 位数字后缀）虽然唯一性强，但对读者不
友好——日报里满是 "沉稳的大象07"、"豁达的貂79" 这样带数字的名字。同期把
ADJECTIVES/ANIMALS 词表扩到 40×40=1600 后，命名空间足够给现有 500+ 群友与
未来增长留出余量，已不需要数字后缀消歧——冲突时由 AliasDB._allocate_default_anon
确定性顺延即可。本脚本一次性把现有 aliases.json 中所有用户重新分配到新格式。

效果：
- 备份当前 aliases.json 到 data/aliases_backup/pre_token_v2_{timestamp}.json
- 按 wxid 字典序遍历，清空 default_anon，调用 AliasDB._allocate_default_anon 重新分配
- public_alias / optout / real_name_seen / last_command_* 一律保留
- 顶层写入 token_format_version: 2

注意：历史已发布日报里的旧 token 引用从此失效。如需反查旧 token → wxid，请使用
本脚本输出的备份文件。本脚本仅需运行一次。

用法: python -m scripts.migrate_token_format
"""

from __future__ import annotations

import datetime
import json
import pathlib
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from wechat_daily import aliases, config


def migrate() -> None:
    if not config.ALIASES_FILE.exists():
        print(f"[错误] aliases.json 不存在: {config.ALIASES_FILE}")
        sys.exit(1)

    # 1. 备份
    config.ALIASES_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = config.ALIASES_BACKUP_DIR / f"pre_token_v2_{ts}.json"
    shutil.copy2(config.ALIASES_FILE, backup_path)
    print(f"[1/3] 已备份当前 aliases.json → {backup_path}")

    # 2. 加载 + 重新分配
    raw = json.loads(config.ALIASES_FILE.read_text(encoding="utf-8"))
    users: dict[str, dict] = raw.get("users", {})
    reservations = raw.get("alias_reservations", [])
    salt = aliases._load_or_create_salt()

    # 先把所有 default_anon 清空，否则 allocator 会把现存的旧名当作"已占用"
    for u in users.values():
        u["default_anon"] = ""

    db = aliases.AliasDB(users=users, reservations=reservations, salt=salt)

    reassigned: list[tuple[str, str]] = []
    for wxid in sorted(users.keys()):
        new_anon = db._allocate_default_anon(wxid)
        users[wxid]["default_anon"] = new_anon
        reassigned.append((wxid, new_anon))

    # 3. 写回，附带 token_format_version
    out = {
        "version": raw.get("version", 1),
        "token_format_version": 2,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "users": users,
        "alias_reservations": reservations,
    }
    config.ALIASES_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[2/3] 已重新分配 {len(reassigned)} 个用户的 default_anon")

    # 简易 sanity check：所有新名唯一
    anons = [a for _, a in reassigned]
    assert len(set(anons)) == len(anons), "重复 default_anon，allocator 有 bug"
    print("[3/3] 唯一性校验通过")

    # 打印前几行示例
    print("\n示例（前 10 个）:")
    for wxid, anon in reassigned[:10]:
        real = users[wxid].get("real_name_seen") or ""
        alias = users[wxid].get("public_alias") or ""
        extra = f"  /alias={alias}" if alias else ""
        print(f"  {wxid}  →  {anon}    [{real}]{extra}")


if __name__ == "__main__":
    migrate()
