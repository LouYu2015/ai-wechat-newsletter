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

# ── Cross-day overlap ───────────────────────────────────────────────────────────
# 每日消息截止时间：日报「D」覆盖 [D-1 21:00, D 21:00) 而非整个自然日，让日报能在
# 21:00 之后按时生成，不必等到过午夜。改这个值会同时移动 chat_extractor 的窗口
# 边界和 find_missing_dates 的「完整/进行中」判定。
DAY_CUTOFF_HOUR = 21

# 跨日重叠窗口的消息数下限：安静的夜晚 ±1h 里可能只有两三条消息，跨天话题接不上，
# 故起点在固定的 -1h 之外再往前延伸，保证重叠段 ≥ 这么多条消息（见 chat_extractor
# 的窗口起点算法）。
OVERLAP_MIN_MESSAGES = 20

# ── Group Chat ──────────────────────────────────────────────────────────────────
GROUP_CHAT_ID = "26389512912@chatroom"
# MD5 of GROUP_CHAT_ID
GROUP_TABLE = "Msg_1f5cd6985e2d31687fc076061b1fa6da"

# ── Models ──────────────────────────────────────────────────────────────────────
# AB test: 报告生成对比 Opus 4.6（主版本，发布 + 喂续写）vs Opus 5（旁路，仅本地
# PDF/debug）。两版都走 Anthropic 同一条 extract_report 路径、同套提示词、原生喂图，
# 只有报告生成模型不同——把唯一变量真正收敛到模型上。链接摘要走 DeepSeek
# V4.1 Flash（deepseek 前缀触发 url_enricher 的 OpenAI 兼容分支，thinking 开启），
# 两版日报共用同一批摘要。2026-09 盲评实测 Flash 在「保留日报有用信息」上优于
# V4 Pro，且快 4–5 倍、便宜约 6 倍。
CLAUDE_MODEL = "claude-opus-4-6"  # 主版本报告生成（发布）
COMPARE_REPORT_MODEL = "claude-opus-5"  # 对比版报告生成（旁路，不发布）
LINK_SUMMARY_MODEL = "deepseek-flash"  # 链接摘要（DeepSeek-V4.1-Flash，开思考）

# ── Anthropic API pricing (USD per 1M tokens) ──────────────────────────────────
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# (verified 2026-05). Cache-write 5m = 1.25× base input; cache-read = 0.1× base
# input; we list them out explicitly so call sites don't have to multiply.
MODEL_PRICES: dict[str, dict[str, float]] = {
    # Opus 5（对比版报告生成）：$5/M 输入、$25/M 输出。cache-write 5m = 1.25×
    # 输入、cache-read = 0.1× 输入。
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_write_5m": 6.25, "cache_read": 0.50},
    # Opus 4.6（主版本报告生成）：$5/M 输入、$25/M 输出。cache-write 5m = 1.25×
    # 输入、cache-read = 0.1× 输入。
    "claude-opus-4-6": {
        "input": 5.00,
        "output": 25.00,
        "cache_write_5m": 6.25,
        "cache_read": 0.50,
    },
    # Fable 5：$10/M 输入、$50/M 输出。cache-write 5m = 1.25×输入、cache-read =
    # 0.1×输入。注意 Fable 新分词器同样内容 token 数约 +30%，实际日报成本会高于
    # 这里按 token 数线性外推的直觉值。
    "claude-fable-5": {
        "input": 10.00,
        "output": 50.00,
        "cache_write_5m": 12.50,
        "cache_read": 1.00,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_write_5m": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_write_5m": 1.25, "cache_read": 0.10},
    # DeepSeek 官方价（https://api-docs.deepseek.com/quick_start/pricing，2026-09
    # 核对）：分峰时 / 错峰两档，错峰为峰时一半；峰时为 UTC 周一至周五 01:00–04:00
    # 与 06:00–10:00。日报常在太平洋时间深夜跑、正好跨两档，这里保守按峰时价估。
    # DeepSeek 缓存写入按普通输入计费（无单独 write 价），故 cache_write_5m 取 = input。
    # usage 归一在 cost_tracker.usage_to_dict：miss→input、hit→cache_read。
    "deepseek-flash": {
        "input": 0.30,
        "output": 1.20,
        "cache_write_5m": 0.30,
        "cache_read": 0.006,
    },
    "deepseek-v4-pro": {
        "input": 1.32,
        "output": 3.96,
        "cache_write_5m": 1.32,
        "cache_read": 0.044,
    },
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

# ── Public-repo images ──────────────────────────────────────────────────────────
# Every byte published here lands in the GitHub Pages repo's git history and
# cannot be removed later, so these are hard caps rather than targets. Today
# `assets/img` holds 20 hand-added images averaging 500KB — 10MB of a 13MB
# repo — which is exactly the shape this is meant to avoid repeating daily.
#
# Long edge: Chirpy's content column is ~890px (1250px max-width × 9/12 minus
# padding); WeChat's HD decode tops out at 1284px anyway, so 1280 means "never
# upscale, only shrink" and still gives the lightbox something to zoom into.
# Byte cap: WebP q80 measured 60–69KB on real referenced screenshots, so 80KB
# per image and a 200KB daily budget bound the worst case at ~73MB/year, versus
# ~25MB/year at the observed rate of ~1 published image per day.
PUBLIC_IMG_LONG_EDGE = 1280
PUBLIC_IMG_MAX_BYTES = 80_000
PUBLIC_IMG_DAY_BUDGET = 200_000
# Quality before resolution: a text screenshot's legibility rides on pixels,
# and downscaling is not even monotonically cheaper (1284→1100 at q85 measured
# *larger*, resampling noise defeating the encoder).
PUBLIC_IMG_LADDER = ((1280, 80), (1280, 70), (1100, 80), (1100, 70), (900, 70), (900, 60))
# Post-relative URL prefix. Chirpy rewrites a site-absolute path to include
# `baseurl`, so posts carry `/assets/img/daily/...` verbatim. Kept as a constant
# so moving images to a separate assets repo later is a one-line change that
# needs no rewrite of already-published posts.
PUBLIC_IMG_URL_PREFIX = "/assets/img/daily"
PUBLIC_IMG_SUBDIR = "assets/img/daily"

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
