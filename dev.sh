#!/usr/bin/env bash
# 一键进入开发沙箱：构建/复用镜像 → 只读挂载微信库 + 读写挂载项目 → 进入 Claude Code。
#
#   ./dev.sh            进入 Claude Code（默认）
#   ./dev.sh bash       进入 shell（调试镜像用）
#   ./dev.sh <cmd...>   在容器里跑任意命令
#
# 环境变量：
#   REBUILD=1 ./dev.sh  强制不带缓存重建镜像
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="wechat-overview-dev"

# 宿主机上微信加密数据库目录（36G，只读挂载）
WECHAT_HOST="$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
# 容器内必须落在 $HOME 下：config.py 用 Path.home() 定位（HOME=/root）
WECHAT_GUEST="/root/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"

if [ ! -d "$WECHAT_HOST" ]; then
  echo "✗ 找不到微信数据目录：$WECHAT_HOST" >&2
  echo "  请确认微信已在本机登录并产生过聊天数据。" >&2
  exit 1
fi

# ── 构建镜像（首次较慢，之后走缓存秒级）─────────────────────────────────
BUILD_ARGS=()
[ "${REBUILD:-0}" = "1" ] && BUILD_ARGS+=(--no-cache)
echo "▶ 构建镜像 $IMAGE …"
docker build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -f "$SCRIPT_DIR/docker/Dockerfile" -t "$IMAGE" "$SCRIPT_DIR"

# ── 运行 ────────────────────────────────────────────────────────────────
# 持久卷：
#   wechat-dev-local  → /root/.local  （Claude Code 原生二进制 + 自更新结果）
#   wechat-dev-claude → /root/.claude （登录态 / 配置 / 会话历史）
# 挂载：
#   $SCRIPT_DIR → /workspace          （读写，开发目录）
#   微信库       → ...xwechat_files:ro （只读）
# 默认进入 Claude Code 并开 YOLO（跳过权限确认）——容器已隔离，挂载的微信库只读。
# 传了自定义命令（如 ./dev.sh bash）则原样执行。
# 决定容器内要跑的命令：
#   无参数            → claude（带 YOLO 跳过权限确认）
#   首参以 - 开头     → 视为透传给 claude 的 flag（如 ./dev.sh --continue / --resume）
#   其它（bash/ls…）  → 原样执行
if [ "$#" -eq 0 ]; then
  set -- claude --dangerously-skip-permissions
elif [ "${1#-}" != "$1" ]; then
  set -- claude --dangerously-skip-permissions "$@"
fi

# 注：不向容器透传 SSH 私钥 —— 公开版站点（data/public_repo）的推送在宿主机/外部完成，
# 容器内只做提交与构建测试，避免把密钥带进沙箱。
exec docker run --rm -it \
  --shm-size=1g \
  -e IS_SANDBOX=1 \
  -v "$SCRIPT_DIR":/workspace \
  -v "$WECHAT_HOST":"$WECHAT_GUEST":ro \
  -v wechat-dev-local:/root/.local \
  -v wechat-dev-claude:/root/.claude \
  -w /workspace \
  "$IMAGE" "$@"
