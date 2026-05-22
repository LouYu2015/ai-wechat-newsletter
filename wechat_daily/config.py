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
CLAUDE_MODEL = "claude-opus-4-6"
LINK_SUMMARY_MODEL = "claude-sonnet-4-6"
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_SUMMARY_MODEL = "gemini-3-flash"

# ── Anthropic API pricing (USD per 1M tokens) ──────────────────────────────────
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# (verified 2026-05). Cache-write 5m = 1.25× base input; cache-read = 0.1× base
# input; we list them out explicitly so call sites don't have to multiply.
MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_write_5m": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write_5m": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_write_5m": 1.25, "cache_read": 0.10},
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
