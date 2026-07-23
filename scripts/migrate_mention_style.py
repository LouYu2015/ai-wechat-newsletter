"""一次性迁移：把已发布公开站日报里的旧昵称标记升级为 @mention pill。

背景（2026-06-01）：
渲染器原来把解析出的昵称包成 `<u>昵称</u>`（在站点上显示为下划线）。新样式改成
Slack 风格的 @ 提及胶囊 `<span class="mention">@昵称</span>`（蓝字蓝框、微圆角、无
背景），并在 Chirpy 主题的 custom.scss 里加了 `.mention` 规则。新生成的日报已经走
新格式，但 data/public_repo/_posts 下已发布的历史文档仍是旧的 `<u>` 标记，本脚本把
它们一次性重写为新格式，使全站观感统一。

安全性：
- 公开站日报里的 `<u>…</u>` 只可能由 renderer 的昵称解析产生（已核对：全部为裸
  `<u>`，无属性、无嵌套，内容不含 `<`），因此 `<u>([^<]*)</u>` → @mention 的替换
  不会误伤任何非昵称内容。
- 幂等：只匹配 `<u>`，重复运行不会二次加 `@` 或重复包裹。

用法:
    python -m scripts.migrate_mention_style          # 重写 + 在 public_repo 内提交
    python -m scripts.migrate_mention_style --dry-run # 只统计，不写文件、不提交
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from wechat_daily.config import PUBLIC_REPO_DIR

# 与 renderer._mention 保持一致：裸 <u>，内容不含 '<'。
_U_RE = re.compile(r"<u>([^<]*)</u>")


def _convert(text: str) -> tuple[str, int]:
    """把 ``<u>名</u>`` 重写为 ``<span class="mention">@名</span>``，返回 (新文本, 次数)。"""
    new, n = _U_RE.subn(r'<span class="mention">@\1</span>', text)
    return new, n


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]

    posts_dir = PUBLIC_REPO_DIR / "_posts"
    if not posts_dir.is_dir():
        print(f"[err] 找不到公开站 _posts 目录：{posts_dir}", file=sys.stderr)
        return 1

    posts = sorted(posts_dir.rglob("*.md"))
    changed: list[Path] = []
    total = 0
    for p in posts:
        text = p.read_text(encoding="utf-8")
        new, n = _convert(text)
        if n == 0:
            continue
        total += n
        changed.append(p)
        if not dry_run:
            p.write_text(new, encoding="utf-8")

    print(f"扫描 {len(posts)} 篇日报 → 改动 {len(changed)} 篇，共替换 {total} 处昵称")
    for p in changed:
        print(f"  - {p.relative_to(PUBLIC_REPO_DIR)}")

    if dry_run:
        print("（dry-run：未写文件、未提交）")
        return 0
    if not changed:
        print("无可改动文件，跳过提交。")
        return 0

    # 提交：连同已经加好 .mention 规则的 custom.scss 一起，否则样式不会生效。
    scss = PUBLIC_REPO_DIR / "_sass" / "addon" / "custom.scss"
    paths = [str(p.relative_to(PUBLIC_REPO_DIR)) for p in changed]
    paths.append(str(scss.relative_to(PUBLIC_REPO_DIR)))
    subprocess.run(["git", "add", *paths], cwd=PUBLIC_REPO_DIR, check=True)

    msg = (
        "历史日报昵称改用 @mention 样式\n\n"
        f"把 {len(changed)} 篇已发布日报里的 <u>昵称</u> 重写为 "
        '<span class="mention">@昵称</span>（Slack 风胶囊），\n'
        "并加入 .mention 主题样式，使全站昵称观感与新版日报统一。"
    )
    subprocess.run(["git", "commit", "-m", msg], cwd=PUBLIC_REPO_DIR, check=True)
    print("已在 public_repo 内提交（未 push）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
