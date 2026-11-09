#!/usr/bin/env bash
# 激活项目 venv 后运行日报生成器，参数原样透传给 main.py：
#   ./run.sh                 默认（批量 + 推送）
#   ./run.sh --no-batch      流式生成
#   ./run.sh --no-push -y …  任意组合
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
  echo "✗ 找不到 $SCRIPT_DIR/.venv，请先按 README 创建虚拟环境：" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$SCRIPT_DIR/.venv/bin/activate"
cd "$SCRIPT_DIR"
exec python main.py "$@"
