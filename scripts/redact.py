"""Retroactive redaction tool: re-render public posts for a wxid after setting optout.

Usage:
    python -m scripts.redact --wxid <wxid> --from 2026-04-01 --to 2026-04-14 [-y]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from wechat_daily.aliases import AliasDB
from wechat_daily.config import DEBUG_DIR
from wechat_daily.publisher import ensure_repo, commit, push_pending
from wechat_daily.renderer import DailyReport, render_public
from wechat_daily.privacy import leak_check
from wechat_daily.contacts import ContactMap


def redact(wxid: str, from_date: str, to_date: str, push: bool) -> None:
    db = AliasDB.load()
    if wxid not in db._users:
        print(f"[错误] 未知 wxid: {wxid}")
        sys.exit(1)

    # Mark optout
    db._users[wxid]['optout'] = True
    db.save()

    try:
        contact_map = ContactMap.from_db()
    except Exception as e:
        contact_map = None
        print(f"[警告] 无法读取联系人: {e}，泄漏检测将跳过")

    start = datetime.strptime(from_date, '%Y-%m-%d').date()
    end = datetime.strptime(to_date, '%Y-%m-%d').date()

    repo = ensure_repo()
    redacted = []

    current = start
    while current <= end:
        date_str = current.strftime('%Y-%m-%d')
        current += timedelta(days=1)

        cache_file = DEBUG_DIR / f"extract-{date_str}.json"
        if not cache_file.exists():
            print(f"  {date_str}: 无缓存 JSON，跳过")
            continue

        try:
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            report = DailyReport.from_dict(data)
        except Exception as e:
            print(f"  {date_str}: 缓存解析失败 ({e})，跳过")
            continue

        public_md = render_public(report, db)

        if contact_map:
            try:
                leak_check(public_md, contact_map, db)
            except Exception as e:
                print(f"  {date_str}: 泄漏检测失败 ({e})，跳过")
                continue

        # Write to repo
        year, month, _ = date_str.split('-')
        post_dir = repo / '_posts' / year / month
        post_dir.mkdir(parents=True, exist_ok=True)
        post_path = post_dir / f"{date_str}-daily.md"
        post_path.write_text(public_md, encoding='utf-8')

        rel = f"_posts/{year}/{month}/{date_str}-daily.md"
        import subprocess
        subprocess.run(['git', 'add', rel], cwd=repo, check=True)
        subprocess.run(
            ['git', 'commit', '-m', f"Redact user on request: {date_str}"],
            cwd=repo, check=True,
        )
        redacted.append(date_str)
        print(f"  {date_str}: 已撤回并 commit")

    if not redacted:
        print("没有需要撤回的日期。")
        return

    print(f"\n共撤回 {len(redacted)} 天：{', '.join(redacted)}")
    if push:
        if push_pending():
            print("已推送到 GitHub。")
        else:
            print("推送失败或无需推送。")
    else:
        print("带 -y 参数重新运行可推送到 GitHub。")


def main():
    parser = argparse.ArgumentParser(description="事后撤回工具")
    parser.add_argument("--wxid", required=True, help="需要撤回的微信 ID")
    parser.add_argument("--from", dest="from_date", required=True, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="截止日期 YYYY-MM-DD")
    parser.add_argument("-y", action="store_true", help="撤回后推送到 GitHub")
    args = parser.parse_args()
    redact(args.wxid, args.from_date, args.to_date, args.y)


if __name__ == "__main__":
    main()
