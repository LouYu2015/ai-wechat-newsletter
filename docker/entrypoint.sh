#!/usr/bin/env bash
# 容器入口：保证 Claude Code 已安装且为最新版，然后执行 CMD（默认 claude）。
#
# Claude Code 用官方原生安装器装进 /root/.local（由 dev.sh 挂成持久卷），
# 自带后台自更新；这里每次启动再显式 `claude update` 一次，确保「保持最新」。
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

if ! command -v claude >/dev/null 2>&1; then
  echo "▶ 首次启动：安装 Claude Code（原生安装器）…"
  curl -fsSL https://claude.ai/install.sh | bash
fi

# 拉到最新版（无网络 / 已最新时静默跳过，不阻塞进入）
echo "▶ 检查 Claude Code 更新…"
claude update >/dev/null 2>&1 || true
echo "▶ Claude Code 版本：$(claude --version 2>/dev/null || echo '未知')"

exec "$@"
