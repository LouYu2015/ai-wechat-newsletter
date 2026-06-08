"""Constants, paths, and environment loading."""

from pathlib import Path

from dotenv import load_dotenv
import os

# ── Paths ───────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEBUG_DIR = PROJECT_ROOT / "debug"
OUTPUT_DIR = PROJECT_ROOT
ARCHIVE_DIR = PROJECT_ROOT / "archive"

CHATLOG_DIR = Path.home() / "Documents/chatlog"
CHATLOG_MAC_DIR = PROJECT_ROOT / "chatlog-mac"
WECHAT_DATA_DIR = (
    Path.home()
    / "Library/Containers/com.tencent.xinWeChat"
    / "Data/Documents/xwechat_files"
)

# ── Group Chat ──────────────────────────────────────────────────────────────────
GROUP_CHAT_ID = "26389512912@chatroom"
# MD5 of GROUP_CHAT_ID
GROUP_TABLE = "Msg_1f5cd6985e2d31687fc076061b1fa6da"

# ── Models ──────────────────────────────────────────────────────────────────────
# AB test: 报告生成对比 Opus 4.6（主版本，发布 + 喂续写）vs DeepSeek V4 Pro
# （旁路，仅本地 PDF/debug）。链接摘要统一改用 DeepSeek V4 Pro（关 thinking），
# 两版日报共用同一批摘要，把唯一变量收敛到报告生成模型上。
CLAUDE_MODEL = "claude-opus-4-6"            # 主版本报告生成（发布）
DEEPSEEK_REPORT_MODEL = "deepseek-v4-pro"   # 对比版报告生成（旁路，不发布）
LINK_SUMMARY_MODEL = "deepseek-v4-pro"      # 链接摘要（thinking off）
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_SUMMARY_MODEL = "gemini-3.5-flash"        # 旧版：纯 Markdown 日报（--summary gemini）
GEMINI_CAPTION_MODEL = "gemini-3.5-flash"        # 图片 caption（喂 DeepSeek 对比版）

# ── Anthropic API pricing (USD per 1M tokens) ──────────────────────────────────
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# (verified 2026-05). Cache-write 5m = 1.25× base input; cache-read = 0.1× base
# input; we list them out explicitly so call sites don't have to multiply.
MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_write_5m": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write_5m": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_write_5m": 1.25, "cache_read": 0.10},
    # DeepSeek 官方价（https://api-docs.deepseek.com/quick_start/pricing）：
    # 输入 cache-miss $0.435/M、cache-hit $0.0036/M、输出 $0.87/M。DeepSeek 缓存
    # 写入按普通输入计费（无单独 write 价），故 cache_write_5m 取 = input。
    # usage 归一在 cost_tracker.usage_to_dict：miss→input、hit→cache_read。
    "deepseek-v4-pro":   {"input": 0.435, "output": 0.87, "cache_write_5m": 0.435, "cache_read": 0.003625},
    # Gemini 3.5 Flash（https://ai.google.dev/gemini-api/docs/pricing，2026-06）：
    # 输入 $1.50/M、输出 $9.00/M、缓存读 $0.15/M。无 Anthropic 式 cache-write。
    # usage 归一在 cost_tracker.usage_to_dict（prompt/candidates token → in/out）。
    "gemini-3.5-flash":  {"input": 1.50, "output": 9.00, "cache_write_5m": 1.50, "cache_read": 0.15},
}

# ── Alias / Privacy ─────────────────────────────────────────────────────────────
ALIASES_FILE = DATA_DIR / "aliases.json"
ALIASES_CURSOR_FILE = DATA_DIR / "aliases.cursor"
ANON_SALT_FILE = DATA_DIR / "anon_salt.txt"
ALIASES_BACKUP_DIR = DATA_DIR / "aliases.backup"
ALIAS_RESERVATION_DAYS = 30

# ── Publisher ───────────────────────────────────────────────────────────────────
PUBLIC_REPO_URL = "git@github.com:LouYu2015/AI-chatgroup-daily.git"
PUBLIC_REPO_DIR = DATA_DIR / "public_repo"

# ── API Keys ────────────────────────────────────────────────────────────────────

def load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def get_anthropic_key() -> str:
    load_env()
    return os.getenv("ANTHROPIC_API_KEY", "")


def get_gemini_key() -> str:
    load_env()
    return os.getenv("GEMINI_API_KEY", "")


def get_deepseek_key() -> str:
    load_env()
    return os.getenv("DEEPSEEK_API_KEY", "")
