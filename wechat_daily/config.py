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
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_SUMMARY_MODEL = "gemini-3-flash"

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
