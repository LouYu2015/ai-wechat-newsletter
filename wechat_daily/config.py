"""Constants, paths, and environment loading."""

import os
import pathlib

import dotenv

# ── Paths ───────────────────────────────────────────────────────────────────────
PROJECT_ROOT = pathlib.Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEBUG_DIR = PROJECT_ROOT / "debug"
OUTPUT_DIR = PROJECT_ROOT
ARCHIVE_DIR = PROJECT_ROOT / "archive"

def debug_dir_for(date_str: str) -> pathlib.Path:
    """Per-date debug archive folder, nested ``YYYY/MM/DD``.

    ``2026-06-07`` → ``debug/2026/06/07/``. All per-day debug artifacts
    (full LLM input/output, thinking, outline, rendered group markdown,
    preview, leak report) live here, grouped under year/month folders so a busy
    month doesn't flood one directory. The cross-date cost ledger
    (``debug/costs.jsonl``) stays at the top level on purpose.
    """
    year, month, day = date_str.split("-")  # "2026-06-07" → 2026 / 06 / 07
    return DEBUG_DIR / year / month / day


CHATLOG_DIR = pathlib.Path.home() / "Documents/chatlog"
CHATLOG_MAC_DIR = PROJECT_ROOT / "chatlog-mac"
WECHAT_DATA_DIR = (
    pathlib.Path.home()
    / "Library/Containers/com.tencent.xinWeChat"
    / "Data/Documents/xwechat_files"
)

# ── Group Chat ──────────────────────────────────────────────────────────────────
GROUP_CHAT_ID = "26389512912@chatroom"
# MD5 of GROUP_CHAT_ID
GROUP_TABLE = "Msg_1f5cd6985e2d31687fc076061b1fa6da"

# ── Models ──────────────────────────────────────────────────────────────────────
# AB test: 报告生成对比 Opus 4.6（主版本，发布 + 喂续写）vs Fable 5（旁路，仅本地
# PDF/debug）。两版都走 Anthropic 同一条 extract_report 路径、同套提示词、原生喂图，
# 只有报告生成模型不同——把唯一变量真正收敛到模型上。链接摘要走 DeepSeek V4
# （成本约为 Sonnet 的 1/10；deepseek 前缀触发 url_enricher 的 OpenAI 兼容分支，
# thinking 关闭），两版日报共用同一批摘要。
CLAUDE_MODEL = "claude-opus-4-6"            # 主版本报告生成（发布）
COMPARE_REPORT_MODEL = "claude-fable-5"     # 对比版报告生成（旁路，不发布）
LINK_SUMMARY_MODEL = "deepseek-v4-pro"      # 链接摘要（DeepSeek V4，无思考）

# ── Anthropic API pricing (USD per 1M tokens) ──────────────────────────────────
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# (verified 2026-05). Cache-write 5m = 1.25× base input; cache-read = 0.1× base
# input; we list them out explicitly so call sites don't have to multiply.
MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_write_5m": 6.25, "cache_read": 0.50},
    # Fable 5（对比版报告生成）：$10/M 输入、$50/M 输出。cache-write 5m = 1.25×
    # 输入、cache-read = 0.1× 输入。注意 Fable 新分词器同样内容 token 数约 +30%，
    # 实际日报成本会高于这里按 token 数线性外推的直觉值。
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_write_5m": 12.50, "cache_read": 1.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write_5m": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_write_5m": 1.25, "cache_read": 0.10},
    # DeepSeek 官方价（https://api-docs.deepseek.com/quick_start/pricing）：
    # 输入 cache-miss $0.435/M、cache-hit $0.0036/M、输出 $0.87/M。DeepSeek 缓存
    # 写入按普通输入计费（无单独 write 价），故 cache_write_5m 取 = input。
    # usage 归一在 cost_tracker.usage_to_dict：miss→input、hit→cache_read。
    "deepseek-v4-pro":   {"input": 0.435, "output": 0.87, "cache_write_5m": 0.435, "cache_read": 0.003625},
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
    dotenv.load_dotenv(PROJECT_ROOT / ".env")


def get_anthropic_key() -> str:
    load_env()
    return os.getenv("ANTHROPIC_API_KEY", "")


def get_deepseek_key() -> str:
    load_env()
    return os.getenv("DEEPSEEK_API_KEY", "")


def get_glm_key() -> str:
    load_env()
    return os.getenv("GLM_API_KEY", "")
